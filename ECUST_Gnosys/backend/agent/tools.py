"""书内阅读工具，供段落 Agent 按需调用。"""

import json
import re

from agent.book import (
    book_db,
    get_paragraph,
    logical_paragraphs,
    normalize_text,
    text_hash,
)
from agent.engine import ToolSpec


def _tool(
    name: str, description: str, properties: dict, required: list[str], handler
) -> ToolSpec:
    return ToolSpec(name, description, properties, handler, required)


def _chapter_layer_row(db, book_int_id: int, chapter_idx: int, book_id: str):
    """取某逻辑章的 chapter_layer。

    注意：chapter_layer 是按 chapters.spine_order 存的，而逻辑章索引是阅读器切章的结果，
    两者可能错位 —— 所以用该章的 start_page 反查 spine_order（沿用原 read_notes 的做法）。
    """
    try:
        from reader.book_service import load_book_cached, _get_toc_pages, _build_chapters

        book = load_book_cached(book_id)
        pages = _get_toc_pages(book_id, book)
        chapters = _build_chapters(pages)
        start_page = chapters[chapter_idx].get("start_page")
        spine = (
            pages[start_page].get("spine_order")
            if start_page is not None and start_page < len(pages)
            else None
        )
        if spine is None:
            return None
        return db.execute(
            "SELECT cl.content, cl.model_version, cl.updated_at FROM chapter_layer cl "
            "JOIN chapters ch ON ch.id=cl.chapter_id WHERE ch.book_id=? AND ch.spine_order=?",
            (book_int_id, spine),
        ).fetchone()
    except (IndexError, TypeError, KeyError):
        return None


def _sub_terms(term: str) -> list[str]:
    """复合术语的子词候选（仅用于精确检索落空时的兑底）。

    「自在自为」→「自在」「自为」，因为书里带常把它们拆开写。
    命中结果会标 `expanded: true`，免得模型把子词命中当成整词逐字出现。
    """
    parts = [p for p in re.split(r"[、，,和与及／/]", term) if len(p) >= 2]
    if len(parts) > 1:
        return parts
    if len(term) >= 4 and len(term) % 2 == 0:
        half = len(term) // 2
        return [term[:half], term[half:]]
    return []


# 省略号：引文里用省略号截断是常态，不该因此判核验失败（不然模型会反复重试同一句）
_ELLIPSIS_RE = re.compile(r"…+|\.{3,}|⋯+|\*{2,}")


def book_reading_tools(book_id: str, ledger: dict[tuple[int, int], list[str]]) -> dict:
    async def outline(args):
        from reader.book_service import (
            load_book_cached,
            _get_toc_pages,
            _build_chapters,
        )

        book = load_book_cached(book_id)
        if not book:
            raise ValueError("当前书籍不可用")
        chapters = _build_chapters(_get_toc_pages(book_id, book))
        return [
            {"chapter_idx": i, "title": c.get("title", "")}
            for i, c in enumerate(chapters)
        ]

    async def search(args):
        query = (args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or limit < 1 or limit > 20:
            raise ValueError("limit 必须为 1 到 20")
        from reader.book_service import (
            load_book_cached,
            _get_toc_pages,
            _build_chapters,
        )

        book = load_book_cached(book_id)
        if not book:
            raise ValueError("当前书籍不可用")
        chapters = _build_chapters(_get_toc_pages(book_id, book))

        def collect(needle: str, expanded: bool, out: list) -> bool:
            """扫全书找 needle；装满 limit 返回 True。命中的节选记入材料账本。"""
            for chapter_idx in range(len(chapters)):
                chapter, paragraphs, _ = logical_paragraphs(book_id, chapter_idx)
                for para in paragraphs:
                    if needle not in normalize_text(para["text"]):
                        continue
                    raw_pos = para["text"].find(query)  # 摘录取原词位置；子词命中时为 -1
                    start = max(0, raw_pos - 400) if raw_pos >= 0 else 0
                    end = min(len(para["text"]), start + 1200)
                    excerpt = para["text"][start:end]
                    ledger.setdefault((chapter_idx, para["paragraph_index"]), []).append(
                        excerpt
                    )
                    out.append(
                        {
                            "material_type": "original_text",
                            "reference": {
                                "chapter_idx": chapter_idx,
                                "paragraph_index": para["paragraph_index"],
                                "fingerprint": para["fingerprint"],
                            },
                            "chapter_title": chapter.get("title", ""),
                            "excerpt": excerpt,
                            "truncated": start > 0 or end < len(para["text"]),
                            "matched": needle,
                            "expanded": expanded,
                        }
                    )
                    if len(out) >= limit:
                        return True
            return False

        hits: list[dict] = []
        if not collect(normalize_text(query), False, hits):
            # 精确搜不到时，试子词（复合术语常被拆开写）
            for sub in _sub_terms(query):
                if collect(normalize_text(sub), True, hits):
                    break
        return hits

    async def read_paragraph_tool(args):
        ci, pi = args.get("chapter_idx"), args.get("paragraph_index")
        if not isinstance(ci, int) or not isinstance(pi, int):
            raise ValueError("chapter_idx 和 paragraph_index 必须为整数")
        offset = args.get("offset", 0)
        chars = args.get("chars", 5000)
        if (
            not isinstance(offset, int)
            or offset < 0
            or not isinstance(chars, int)
            or chars < 100
            or chars > 8000
        ):
            raise ValueError("offset 或 chars 超出范围")
        item = get_paragraph(book_id, ci, pi)
        text = item["text"]
        end = min(len(text), offset + chars)
        ledger.setdefault((ci, pi), []).append(text[offset:end])
        if item["previous"]:
            ledger.setdefault((ci, pi - 1), []).append(item["previous"][:500])
        if item["next"]:
            ledger.setdefault((ci, pi + 1), []).append(item["next"][:500])
        return {
            "material_type": "original_text",
            "reference": {
                "chapter_idx": ci,
                "paragraph_index": pi,
                "fingerprint": item["fingerprint"],
            },
            "chapter_title": item["chapter_title"],
            "previous_excerpt": item["previous"][:500],
            "text": text[offset:end],
            "offset": offset,
            "truncated": end < len(text),
            "next_offset": end if end < len(text) else None,
            "next_excerpt": item["next"][:500],
        }

    async def read_notes(args):
        ci, pi = args.get("chapter_idx"), args.get("paragraph_index")
        if not isinstance(ci, int) or not isinstance(pi, int):
            raise ValueError("chapter_idx 和 paragraph_index 必须为整数")
        item = get_paragraph(book_id, ci, pi)
        db = book_db(book_id)
        book_row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
        book_layer = db.execute(
            "SELECT content FROM book_layer WHERE book_id=?", (book_row["id"],)
        ).fetchone()
        chapter_layer = _chapter_layer_row(db, book_row["id"], ci, book_id)
        row = db.execute(
            "SELECT short, detail, sentences, state FROM agent_notes "
            "WHERE chapter_idx=? AND paragraph_index=? AND fingerprint=? AND state IN ('published','accepted') "
            "ORDER BY updated_at DESC LIMIT 1",
            (ci, pi, item["fingerprint"]),
        ).fetchone()
        legacy = db.execute(
            "SELECT content, detail, sentences, kind FROM semantic_layer WHERE text_hash=? LIMIT 1",
            (text_hash(item["text"]),),
        ).fetchone()
        legacy_sentences = []
        if legacy:
            try:
                legacy_sentences = json.loads(legacy["sentences"] or "[]")
            except (TypeError, ValueError):
                pass
        paragraph_norm = normalize_text(item["text"])
        concepts = [
            {"term": r["term"], "content": r["content"] or ""}
            for r in db.execute(
                "SELECT term,content FROM concept_layer ORDER BY term"
            ).fetchall()
            if r["term"] and normalize_text(r["term"]) in paragraph_norm
        ][:20]
        layers = {
            "book": (book_layer["content"] or "")[:8000] if book_layer else "",
            "chapter": (chapter_layer["content"] or "")[:8000] if chapter_layer else "",
            "semantic": (
                {
                    "content": legacy["content"] or "",
                    "detail": legacy["detail"] or "",
                    "sentences": legacy_sentences,
                    "kind": legacy["kind"],
                }
                if legacy
                else None
            ),
            "concepts": concepts,
        }
        reference = {
            "chapter_idx": ci,
            "paragraph_index": pi,
            "fingerprint": item["fingerprint"],
        }
        if row:
            return {
                "material_type": "existing_note",
                "origin": "agent",
                "state": row["state"],
                "short": row["short"],
                "detail": row["detail"],
                "sentences": json.loads(row["sentences"]),
                "layers": layers,
                "reference": reference,
            }
        if not legacy:
            return {
                "material_type": "existing_note",
                "matched": bool(any(layers.values())),
                "layers": layers,
                "reference": reference,
            }
        try:
            sentences = json.loads(legacy["sentences"] or "[]")
        except (ValueError, TypeError):
            sentences = []
        return {
            "material_type": "existing_note",
            "origin": "legacy",
            "matched": True,
            "short": legacy["content"] or "",
            "detail": legacy["detail"] or "",
            "sentences": sentences,
            "layers": layers,
            "reference": reference,
        }

    async def read_layer_note(args):
        """单独读全书层 / 章节层（不截断）。

        原 read_existing_note 把两层内容截到 8000 字混在一起返回，判断“这个术语全书什么位置”
        时需要完整文本（概念层设计 §12.1）。返回内容标记为层解释，**不是原文证据**。
        """
        layer = (args.get("layer") or "").strip()
        db = book_db(book_id)
        book_row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
        if layer == "book":
            ci = None
            row = db.execute(
                "SELECT content, model_version FROM book_layer WHERE book_id=?",
                (book_row["id"],),
            ).fetchone()
        elif layer == "chapter":
            ci = args.get("chapter_idx")
            if not isinstance(ci, int):
                raise ValueError("读章节层需要 chapter_idx")
            row = _chapter_layer_row(db, book_row["id"], ci, book_id)
        else:
            raise ValueError("layer 只能是 book 或 chapter")
        if not row:
            return {
                "material_type": "layer_note",
                "layer": layer,
                "chapter_idx": ci,
                "available": False,
                "content": "",
                "note": "该层还没有内容（这不是错误，降级处理即可）",
            }
        return {
            "material_type": "layer_note",
            "layer": layer,
            "chapter_idx": ci,
            "available": True,
            "content": row["content"] or "",
            "model_version": row["model_version"] or "",
            "note": "这是上一层的解释内容，不是原文证据，不得当作引文依据",
        }

    async def read_glossary(args):
        """查本书术语规范里的书目知识（译名卡 / 前提追溯库）。

        用途：判断“这个词是本书特别交代的术语，还是普通词”；卡内术语的解释必须与卡一致。
        返回的是**书目知识，不是原文证据**（与 read_existing_note 同类，不引作引文）。
        """
        keyword = (args.get("keyword") or "").strip()
        if not keyword:
            raise ValueError("keyword 不能为空")
        from agent.index import glossary_entries

        key = normalize_text(keyword)
        hits = []
        for name, entry in glossary_entries().items():
            n = normalize_text(name)
            if n == key or (key and (key in n or n in key)):
                hits.append({"term": name, "entry": entry})
                if len(hits) >= 3:
                    break
        if not hits:
            return {
                "matched": False,
                "keyword": keyword,
                "note": "本书规范里没有这个词条 —— 它不是本书特别交代的术语，按普通词处理",
            }
        return {
            "matched": True,
            "entries": hits,
            "note": "书目知识，不是原文证据；卡内术语的解释必须与卡一致",
        }

    async def verify_quote(args):
        quote = args.get("quote")
        if not isinstance(quote, str) or not quote:
            raise ValueError("quote 不能为空")
        ci, pi = args.get("chapter_idx"), args.get("paragraph_index")
        if isinstance(ci, int) and isinstance(pi, int) and (ci, pi) in ledger:
            candidates = ledger[(ci, pi)]
        else:
            candidates = [source for sources in ledger.values() for source in sources]
        if any(quote in source for source in candidates):
            return {
                "exact_match": True,
                "mode": "exact",
                "checked_sources": len(candidates),
                "quote": quote,
                "note": "与本任务已读取原文逐字一致",
            }
        # 省略号截断是写引文的常态（“A……B”），分段核验而不是直接判失败 ——
        # 否则模型会反复重试同一句引文，把预算烧光（L3 已实测过这个症状）。
        segments = [s for s in _ELLIPSIS_RE.split(quote) if s.strip()]
        if len(segments) > 1:
            missing = [s for s in segments if not any(s in source for source in candidates)]
            return {
                "exact_match": not missing,
                "mode": "ellipsis_split",
                "segments_checked": len(segments),
                "missing": missing[:3],
                "checked_sources": len(candidates),
                "quote": quote,
                "note": (
                    "省略号分段均逐字命中"
                    if not missing
                    else "省略号分段有未命中项：请照抄原文，或删掉未命中的片段"
                ),
            }
        return {
            "exact_match": False,
            "mode": "none",
            "checked_sources": len(candidates),
            "quote": quote,
            "note": "引文不在本任务已读取原文中",
        }

    tools = [
        _tool("get_book_outline", "查看当前书的逻辑章节目录。", {}, [], outline),
        _tool(
            "search_book_text",
            "在当前书中搜索原文，返回带位置的节选。",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            ["query"],
            search,
        ),
        _tool(
            "read_book_paragraph",
            "读取当前书指定逻辑章节和段落，可按字符偏移续读。",
            {
                "chapter_idx": {"type": "integer"},
                "paragraph_index": {"type": "integer"},
                "offset": {"type": "integer"},
                "chars": {"type": "integer"},
            },
            ["chapter_idx", "paragraph_index"],
            read_paragraph_tool,
        ),
        _tool(
            "read_existing_note",
            "读取当前书指定段落已有的四层笔记，返回内容标记为 existing_note。",
            {
                "chapter_idx": {"type": "integer"},
                "paragraph_index": {"type": "integer"},
            },
            ["chapter_idx", "paragraph_index"],
            read_notes,
        ),
        _tool(
            "read_layer_note",
            "单独读取全书层或章节层的已有内容（上层解释，不是原文证据）。",
            {
                "layer": {"type": "string"},
                "chapter_idx": {"type": "integer"},
            },
            ["layer"],
            read_layer_note,
        ),
        _tool(
            "read_glossary",
            "查本书术语规范里的词条（译名卡/前提追溯库），判断是否本书术语。",
            {
                "keyword": {"type": "string"},
            },
            ["keyword"],
            read_glossary,
        ),
        _tool(
            "verify_source_quote",
            "核验引文是否逐字出现于本任务已读取的原文。",
            {
                "quote": {"type": "string"},
                "chapter_idx": {"type": "integer"},
                "paragraph_index": {"type": "integer"},
            },
            ["quote"],
            verify_quote,
        ),
    ]
    return {
        "description": "在当前书范围内查目录、检索和补读原文，并核验引文。",
        "tools": tools,
    }
