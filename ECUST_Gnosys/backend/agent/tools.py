"""书内阅读工具，供段落 Agent 按需调用。"""

import json

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
        needle = normalize_text(query)
        hits = []
        for chapter_idx in range(len(chapters)):
            chapter, paragraphs, _ = logical_paragraphs(book_id, chapter_idx)
            for para in paragraphs:
                pos = normalize_text(para["text"]).find(needle)
                if pos < 0:
                    continue
                raw_pos = para["text"].find(query)
                start = max(0, raw_pos - 400) if raw_pos >= 0 else 0
                end = min(len(para["text"]), start + 1200)
                excerpt = para["text"][start:end]
                ledger.setdefault((chapter_idx, para["paragraph_index"]), []).append(
                    excerpt
                )
                hits.append(
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
                    }
                )
                if len(hits) >= limit:
                    return hits
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
        chapter_layer = None
        try:
            from reader.book_service import (
                load_book_cached,
                _get_toc_pages,
                _build_chapters,
            )

            book = load_book_cached(book_id)
            pages = _get_toc_pages(book_id, book)
            chapters = _build_chapters(pages)
            start_page = chapters[ci].get("start_page")
            spine_order = (
                pages[start_page].get("spine_order")
                if start_page is not None and start_page < len(pages)
                else None
            )
            if spine_order is not None:
                chapter_layer = db.execute(
                    "SELECT cl.content FROM chapter_layer cl JOIN chapters ch ON ch.id=cl.chapter_id "
                    "WHERE ch.book_id=? AND ch.spine_order=?",
                    (book_row["id"], spine_order),
                ).fetchone()
        except (IndexError, TypeError):
            chapter_layer = None
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

    async def verify_quote(args):
        quote = args.get("quote")
        if not isinstance(quote, str) or not quote:
            raise ValueError("quote 不能为空")
        ci, pi = args.get("chapter_idx"), args.get("paragraph_index")
        candidates = []
        if isinstance(ci, int) and isinstance(pi, int) and (ci, pi) in ledger:
            candidates = ledger[(ci, pi)]
        else:
            candidates = [source for sources in ledger.values() for source in sources]
        matched = any(quote in source for source in candidates)
        return {
            "exact_match": matched,
            "checked_sources": len(candidates),
            "quote": quote,
            "note": (
                "只能确认与本任务已读取原文逐字一致"
                if matched
                else "引文不在本任务已读取原文中"
            ),
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
