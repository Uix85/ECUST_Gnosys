"""L4 概念层的技能与工具。

设计依据：docs/概念层_设计_v1.md §10（主线：为每个术语维护意义地图，划词只在图上取值）、
§12（工具清单）。

两组工具，对应主线的两个阶段：

    ┌ 建图组（对某个词跑一次，可缓存、可离线批量）
    │   find_term_occurrences     分布 + 抽样（判是否分簇、定范围）
    │   find_definition_passages  作者解释该词的段落（窗口扫描 + 转折加权）
    │   read_paragraphs_at        批量读多处完整段落（代回测试需要 A/B 两处上下文）
    │   read_concept_entry        读已有地图（避免重跑、保持义项编号稳定）
    └ 定位组（每次划词，必须快）
        locate_term_context       本句 + 邻段 + 该段的 L3 三段解释

工具实现与 book-reading 共用（同一批 handler 工厂），不复制代码。
定位组刻意只给 1 个工具：地图、L3 内容、译名卡全部由调用方注入 materials ——
「快」是结构保证，不是靠调预算。定位 agent 若发现证据不足，用
submit_concept_reading(needs_mapping=true) 上报，由系统触发后台建图，**不许硬猜**。
"""

import json
from pathlib import Path

from agent.book import (book_db, fingerprint, get_paragraph, logical_paragraphs,
                        normalize_text, split_sentences, text_hash)
from agent.engine import SkillSpec
from agent.tools import _tool, book_reading_tools

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# 作者解释某概念时的锚点词（粗筛用；按书可配，见 docs/reading_semantics.md 的两段式结构）
DEFINITION_ANCHORS = ("意味着", "指的是", "称为", "叫做", "这就是说", "所谓",
                      "意思是", "定义为", "就其本身而言", "在这个意义上")
# 作者换用法时常用的转折语 —— 义项分界与重新定义的高发位置
PIVOT_WORDS = ("然而", "但是", "其实", "更确切地说", "毋宁说", "严格说来", "相反地")
MAX_PARAGRAPHS_AT = 4          # 一次批量读几段（防止一口气拉太多进上下文）


def _skill_file(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _chapters(book_id: str) -> list:
    n = 0
    while True:
        try:
            logical_paragraphs(book_id, n)
            n += 1
        except Exception:
            return list(range(max(n, 1)))


def _distribution(book_id: str, term: str) -> dict:
    """该词的全书分布。优先用 L0 落库的 term_scan 缓存（划词时秒级）。"""
    db = book_db(book_id)
    book_row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    norm = normalize_text(term)
    row = (db.execute("SELECT total, distribution_json FROM term_scan WHERE book_id=? AND term_norm=?",
                      (book_row["id"], norm)).fetchone() if book_row else None)
    if row:
        return {"total": row["total"], "chapters": json.loads(row["distribution_json"]), "cached": True}
    # 缓存未命中：现场扫一遍（1.1s 级）并写回，下次即命中
    per_chapter = []
    total = 0
    for ci in _chapters(book_id):
        _, paragraphs, _ = logical_paragraphs(book_id, ci)
        n = sum(1 for p in paragraphs if norm and norm in normalize_text(p["text"]))
        if n:
            per_chapter.append([ci, n])
            total += n
    if book_row and total:
        from datetime import datetime
        db.execute("INSERT OR REPLACE INTO term_scan(book_id,term_norm,total,distribution_json,built_at)"
                   " VALUES(?,?,?,?,?)",
                   (book_row["id"], norm, total, json.dumps(per_chapter, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds")))
        db.commit()
    return {"total": total, "chapters": per_chapter, "cached": False}


def concept_skills(book_id: str, ledger: dict, mode: str) -> list[SkillSpec]:
    """构造 L4 技能包。mode: "mapping"（建图，后台）| "locating"（定位，前台）。"""
    if mode not in ("mapping", "locating"):
        raise ValueError("mode 只能是 mapping 或 locating")
    base = book_reading_tools(book_id, ledger)            # 复用同一批 handler
    db = book_db(book_id)

    # ── 定位组 ────────────────────────────────────────────
    async def locate_term_context(args):
        """本句 + 整段原文 + 邻近段落 + 该段的 L3 三段解释。"""
        term = (args.get("term") or "").strip()
        ci, pi = args.get("chapter_idx"), args.get("paragraph_index")
        window = args.get("window", 1)
        if not term or not isinstance(ci, int) or not isinstance(pi, int):
            raise ValueError("需要 term、chapter_idx、paragraph_index")
        if not isinstance(window, int) or not 0 <= window <= 2:
            raise ValueError("window 必须在 0 到 2 之间")
        item = get_paragraph(book_id, ci, pi)
        text = item["text"]
        sentences = split_sentences(text)
        idx = next((i for i, s in enumerate(sentences) if term in s), None)
        row = db.execute("SELECT content, detail, sentences FROM semantic_layer "
                         "WHERE text_hash=? AND kind='para'", (text_hash(text),)).fetchone()
        semantic = None
        if row:
            sents = json.loads(row["sentences"] or "[]")
            semantic = {"short": row["content"], "detail": row["detail"], "sentences": sents,
                        "sentence_explanation": (sents[idx] if idx is not None and idx < len(sents) else "")}
        neighbors = []
        for off in range(1, window + 1):
            for sign in (-1, 1):
                j = pi + sign * off
                try:
                    nb = get_paragraph(book_id, ci, j)
                except Exception:
                    continue
                neighbors.append({"paragraph_index": j, "text": nb["text"]})
                ledger.setdefault((ci, j), []).append(nb["text"])
        ledger.setdefault((ci, pi), []).append(text)       # 记账：引文核验只认账本
        return {"material_type": "original_text",
                "reference": {"chapter_idx": ci, "paragraph_index": pi, "fingerprint": item["fingerprint"]},
                "chapter_title": item["chapter_title"], "sentence_index": idx,
                "sentence": sentences[idx] if idx is not None else "",
                "paragraph_text": text, "neighbors": neighbors,
                "semantic": semantic,                      # None = 该段没有 L3 笔记（降级，不是错误）
                "note_state": "published" if row else None}

    # ── 建图组 ────────────────────────────────────────────
    async def find_term_occurrences(args):
        """全书分布 + 均匀抽样。370 条不可能全给，按章序截断又是有偏样本。"""
        term = (args.get("term") or "").strip()
        if not term:
            raise ValueError("term 不能为空")
        limit = args.get("limit", 12)
        dist = _distribution(book_id, term)
        norm = normalize_text(term)
        samples, seen_chapters = [], set()
        for ci, _count in dist["chapters"]:
            if len(samples) >= limit:
                break
            _, paragraphs, _ = logical_paragraphs(book_id, ci)
            hits = [p for p in paragraphs if norm in normalize_text(p["text"])]
            step = max(1, len(hits) // 3)                  # 每章最多抽 3 段，均匀取样
            for para in hits[::step][:3]:
                if len(samples) >= limit:
                    break
                ledger.setdefault((ci, para["paragraph_index"]), []).append(para["text"])
                samples.append({"material_type": "original_text",
                                "reference": {"chapter_idx": ci, "paragraph_index": para["paragraph_index"],
                                              "fingerprint": para["fingerprint"]},
                                "snippet": para["text"][:400]})
            seen_chapters.add(ci)
        return {"total": dist["total"], "cached": dist["cached"],
                "distribution": [{"chapter_idx": ci, "count": n} for ci, n in dist["chapters"]],
                "samples": samples}

    async def find_definition_candidates(args):
        """**召回**作者解释该词的段落候选（不是“判定哪些段是定义”——那个判断归模型）。

        扫 [出现段落 ± window]：作者解释某概念时不一定再写那个词，邻近段落是主要来源。
        打分只是召回手段（锚点词 + 转折语 + 章首），返回里带 note 明示这一点。
        chapter_range 由调用方按「该词是否全书一致」的结论给定 —— 检索范围由一致性结论决定。
        """
        term = (args.get("term") or "").strip()
        if not term:
            raise ValueError("term 不能为空")
        window = args.get("window", 1)
        span = args.get("chapter_range") or [0, len(_chapters(book_id)) - 1]
        if not (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) for x in span)):
            raise ValueError("chapter_range 必须是 [起, 止] 两个整数")
        scored = []
        for ci in range(max(0, span[0]), span[1] + 1):
            _, paragraphs, _ = logical_paragraphs(book_id, ci)
            seeds = [p["paragraph_index"] for p in paragraphs if term in p["text"]]
            window_idx = {i for s in seeds for i in range(s - window, s + window + 1)
                          if 0 <= i < len(paragraphs)}
            for i in sorted(window_idx):
                text = paragraphs[i]["text"]
                score = sum(1 for a in DEFINITION_ANCHORS if a in text)
                score += 2 * sum(1 for w in PIVOT_WORDS if w in text)    # 转折处更容易重新定义
                score += 1 if i <= 2 else 0                              # 章首
                score += 1 if term in text else 0                        # 含该词本身
                if score:
                    scored.append((score, ci, i, text))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, ci, i, text in scored[:args.get("limit", 6)]:
            ledger.setdefault((ci, i), []).append(text)
            out.append({"score": score, "chapter_idx": ci,
                        "reference": {"chapter_idx": ci, "paragraph_index": i,
                                      "fingerprint": fingerprint(text)},
                        "text": text[:600]})
        return {"candidates": out, "scanned_chapters": [span[0], span[1]],
                "note": "这些是召回候选（按锚点词机械打分），是不是定义段落由你判断；"
                        "判断前请用 read_paragraphs_at 读完整段落"}

    async def read_paragraphs_at(args):
        """一次读多处完整段落（代回测试要比较 A/B 两处的读法）。"""
        positions = args.get("positions") or []
        if not isinstance(positions, list) or not positions:
            raise ValueError("positions 不能为空")
        if len(positions) > MAX_PARAGRAPHS_AT:
            raise ValueError(f"一次最多读 {MAX_PARAGRAPHS_AT} 段（分开几次读）")
        out = []
        for pos in positions:
            ci, pi = pos.get("chapter_idx"), pos.get("paragraph_index")
            if not isinstance(ci, int) or not isinstance(pi, int):
                raise ValueError("positions 每项都要有 chapter_idx 与 paragraph_index")
            item = get_paragraph(book_id, ci, pi)
            ledger.setdefault((ci, pi), []).append(item["text"])
            out.append({"material_type": "original_text",
                        "reference": {"chapter_idx": ci, "paragraph_index": pi,
                                      "fingerprint": item["fingerprint"]},
                        "chapter_title": item["chapter_title"],
                        "text": item["text"][:args.get("chars", 3000)]})
        return out

    async def read_concept_entry(args):
        """读该词已有的义项（避免自相矛盾、保持义项编号稳定）。"""
        term = (args.get("term") or "").strip()
        rows = db.execute(
            "SELECT sense_idx, has_definition, scope, range_note, reading, common_reading,"
            "confusion_types, dimensions_json, aliases_json, stance, revision FROM concept_sense "
            "WHERE term_norm=? ORDER BY sense_idx", (normalize_text(term),)).fetchall()
        return {"term": term, "existing_senses": [
            {**dict(r), "dimensions": json.loads(r["dimensions_json"] or "[]"),
             "aliases": json.loads(r["aliases_json"] or "[]")} for r in rows]}

    # ── 组装 ──────────────────────────────────────────────
    if mode == "locating":
        tools = [
            _tool("locate_term_context", "读取该词所在句、整段原文、邻近段落，以及该段的语义层解释（简短/具体/分句）。",
                  {"term": {"type": "string"}, "chapter_idx": {"type": "integer"},
                   "paragraph_index": {"type": "integer"}, "window": {"type": "integer"}},
                  ["term", "chapter_idx", "paragraph_index"], locate_term_context),
        ]
        description = "在当前书范围内定位一个词的位置与语境。"
    else:
        tools = list(base["tools"]) + [
            _tool("find_term_occurrences", "检索该词在全书出现的位置分布与抽样上下文。",
                  {"term": {"type": "string"}, "limit": {"type": "integer"}}, ["term"],
                  find_term_occurrences),
            _tool("find_definition_candidates", "召回作者解释该词的段落候选，可按章范围限制。返回的是候选，不是判定。",
                  {"term": {"type": "string"}, "chapter_range": {"type": "array"},
                   "window": {"type": "integer"}, "limit": {"type": "integer"}}, ["term"],
                  find_definition_candidates),
            _tool("read_paragraphs_at", "批量读取多处完整段落（用于比较不同位置的读法）。",
                  {"positions": {"type": "array"}, "chars": {"type": "integer"}}, ["positions"],
                  read_paragraphs_at),
            _tool("read_concept_entry", "读取该词已有的义项记录。",
                  {"term": {"type": "string"}}, ["term"], read_concept_entry),
        ]
        description = "为书中术语建立意义地图：分布、定义段落、义项与维度。"

    name = "concept-mapping" if mode == "mapping" else "concept-reading"
    return [SkillSpec(name, description, _skill_file(name), tools)]
