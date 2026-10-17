"""章-节-子节层级结构：给语义层提供「合乎逻辑的上下文」与「位置感」。

为什么需要它（语义层要实现的核心）：

    生成合乎逻辑安排的文本，并且 **上承章节结构、下接术语解释**。

    * 上下文：逐段解释必须知道本段在一段论证里的位置，否则（实测）17% 的段是硬切分切断的
      半截话，孤立解释只能写套话；
    * 窗口大小：层级本身就是自适应的窗口 —— 深层子节天然是个小窗口，比"整节/整章"更贴合；
    * 上承：章内层级树可以直接作为 L2（章节层）的输入（自下而上），也让 L3 知道自己挂在哪；
    * 下接：子节边界是术语解释范围的自然单位（scope=section）。

现实约束（实测这本《精神现象学》）:

    txt 源没有标题标签（content_html 只有 <br>），层级只能从文本行模式推断；
    epub 源则在入库时就抽好了层级（chapters.heading_json）—— **优先用那份**，
    推断只是兜底。来源优先级：

        ① heading_json（epub 源，入库时抽的 h1-h6）
        ② 文本行模式推断（txt 源，下面的 DEFAULT_RULES）
        ③ 模型扫一章（都没有时，尚未实现）

    所以规则表按书可配，但只在第②档起作用。
"""

import json
import re
import sys as _sys
from pathlib import Path as _Path

# 允许 `uv run python backend/agent/tools/context.py` 直接跑文件尾的自检（与 reader/import_epub.py 同约定）
_BACKEND_DIR = str(_Path(__file__).resolve().parents[2])
if _BACKEND_DIR not in _sys.path:
    _sys.path.insert(0, _BACKEND_DIR)

from agent.tools.book import book_db, logical_paragraphs, normalize_text

# ── 标题规则表 ─────────────────────────────────────────
# 规则**按顺序**匹配：命中的规则下标就是层级深度（0 最浅）。顺序即层级，不要随意重排。
# 每项：(名字, 正则)
DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    ("章", r"^第[一二三四五六七八九十百零\d]+章"),
    ("节", r"^第[一二三四五六七八九十百零\d]+节"),
    ("汉数顿号", r"^[\[【(（]?\s*[一二三四五六七八九十]+\s*[、．.．]"),
    ("罗马点", r"^[\[【(（]?\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅰⅱⅲⅳ]+[\s、．.]"),
    ("阿数点", r"^[\[【(（]?\s*\d+\s*[、．.]"),
    ("括数字", r"^[\[【]?\s*[（(]\s*\d+\s*[）)]"),
    ("括字母", r"^[\[【]?\s*[（(]\s*[a-zA-Zａ-ｚＡ-Ｚ]\s*[）)]"),
)

MAX_HEADING_CHARS = 40          # 超过这个长度不可能是标题（是正文）
MIN_CONTEXT_PARAGRAPHS = 3      # 窗口至少装几段；不够就往上走一层
MAX_CONTEXT_CHARS = 6000        # 窗口上限；超过就退回「本段 ± 局部窗口」
LOCAL_WINDOW = 2                # 退回时的局部窗口半径（前后各几段）


def heading_level(text: str, rules: tuple = DEFAULT_RULES) -> int | None:
    """判断一行是不是标题，返回层级（规则下标）；不是标题返回 None。

    两个护栏：标题必须短，且不以句末标点收尾 —— 否则「1．这句话…。」这类正文会被误判。
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_HEADING_CHARS:
        return None
    if t[-1] in "。！？!?；;":
        return None
    # 方括号批注剥壳后按规则判（如 [2．最高本质…] → 阿数点）。
    # 鱼尾括号【…】是版式标记（丛书页、版权页），**不当标题** —— 实测《精神现象学》序言里
    # 的【丛书名】被当标题后一口吞掉 122 段（占全章 46%）。
    if t.startswith("【") and t.endswith("】"):
        return None
    body = t
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1].strip()
    for idx, (_name, pattern) in enumerate(rules):
        if re.match(pattern, body):
            return idx
    # 方括号批注但不匹配任何模式：按「节」处理（译者加的批注一律是结构标记）
    if t != body and body:
        return 2
    return None


def _spine_order(book_id: str, chapter_idx: int) -> int | None:
    """逻辑章索引 → chapters.spine_order。

    阅读器切章（按标题锚点/文件边界）与入库切章（按 spine 文件）可能不一致，
    所以不能直接用 chapter_idx 去查 chapters 表。
    """
    try:
        from reader.book_service import load_book_cached, _get_toc_pages, _build_chapters
        book = load_book_cached(book_id)
        pages = _get_toc_pages(book_id, book)
        chapters = _build_chapters(pages)
        start = chapters[chapter_idx].get("start_page")
        if start is None or start >= len(pages):
            return None
        return pages[start].get("spine_order")
    except (IndexError, KeyError, TypeError):
        return None


def headings_from_field(book_id: str, chapter_idx: int, paragraphs: list) -> list | None:
    """① 用入库时抽好的章节层级（epub 源）。

    heading_json 只有 [{level, text}]，没有段落下标，所以按**标题文本**回找段落：
    epub 源里 h1-h6 本身就是段落之一，能被逐字对上。对不上的标题跳过（不编位置）。
    """
    spine = _spine_order(book_id, chapter_idx)
    if spine is None:
        return None
    row = book_db(book_id).execute(
        "SELECT heading_json FROM chapters WHERE spine_order=?", (spine,)).fetchone()
    if not row or not (row["heading_json"] or "").strip():
        return None
    try:
        entries = json.loads(row["heading_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list) or not entries:
        return None
    by_text: dict[str, list[int]] = {}
    for para in paragraphs:
        key = normalize_text(para["text"])
        if key:
            by_text.setdefault(key, []).append(para["paragraph_index"])
    out = []
    last_pi = -1
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or "").strip()
        level = entry.get("level")
        if not text or not isinstance(level, int):
            continue
        pi = next((p for p in by_text.get(normalize_text(text), []) if p > last_pi), None)
        if pi is None:
            continue
        last_pi = pi
        out.append({"level": max(0, level - 1), "title": text, "paragraph_index": pi})
    return out or None


def _node_ranges_from_store(book_id: str, chapter_idx: int) -> list[dict] | None:
    """定位表里的节点区间。入库时判定一次（章就是一级标题），这里只读。"""
    from reader import locator
    if not locator.has_locator(book_id):
        return None
    nodes = [n for n in locator.load_nodes(book_id) if n["chapter_idx"] == chapter_idx]
    if not nodes:
        return None
    return [{"level": n["level"], "title": n["title"], "head_para": n["head_para"],
             "para_from": n["para_from"], "para_to": n["para_to"],
             "synthetic": bool(n["synthetic"])} for n in nodes]


def _node_ranges_from_headings(headings: list[dict], n_paragraphs: int) -> list[dict]:
    """没建定位时的回退：按标题行现算区间（与旧实现同规则）。"""
    out = []
    for i, head in enumerate(headings):
        end = n_paragraphs
        for later in headings[i + 1:]:
            if later["level"] <= head["level"]:
                end = later["paragraph_index"]
                break
        out.append({"level": head["level"], "title": head["title"],
                    "head_para": head["paragraph_index"],
                    "para_from": head["paragraph_index"],
                    "para_to": max(head["paragraph_index"], end - 1)})
    return out


def structure(book_id: str, chapter_idx: int, rules: tuple = DEFAULT_RULES) -> dict:
    """切出章内的层级结构。

    标题树优先从**定位表**读（入库时判定一次，见 backend/reader/locator.py）；
    没建定位的书回退到现场判定（h 标签 → epub 的 heading_json → txt 的文本行模式）。

    返回：
        headings   [{level, title, paragraph_index}]（合成章节点不带 paragraph_index）
        para_meta  {paragraph_index: {level, path[标题], parent(标题),
                                     pos(在所属节里第几段), size(该节非标题段数),
                                     context_range(上下文段落区间)}}
    """
    _chapter, paragraphs, _total = logical_paragraphs(book_id, chapter_idx)
    n_paragraphs = len(paragraphs)

    node_ranges = _node_ranges_from_store(book_id, chapter_idx)
    if node_ranges:
        headings = [{"level": n["level"], "title": n["title"],
                     "paragraph_index": n["head_para"]}
                    for n in node_ranges if n["head_para"] is not None]
    else:
        # ① h 标签（渲染后段落带 tag）→ ② 入库层级（epub 源）→ ③ 文本行模式推断（txt 源）
        headings = [{"level": int(p["tag"][1]) - 1, "title": p["text"].strip(),
                     "paragraph_index": p["paragraph_index"]}
                    for p in paragraphs if re.fullmatch(r"h[1-6]", p.get("tag", ""))]
        headings = headings or headings_from_field(book_id, chapter_idx, paragraphs) or []
        if not headings:
            for para in paragraphs:
                level = heading_level(para["text"], rules)
                if level is None:
                    continue
                headings.append({"level": level, "title": para["text"].strip(),
                                 "paragraph_index": para["paragraph_index"]})
        node_ranges = _node_ranges_from_headings(headings, n_paragraphs)

    heading_index = {h["paragraph_index"] for h in headings}
    stack: list[tuple[int, str]] = []            # (level, title)
    meta: dict[int, dict] = {}

    for para in paragraphs:
        pi = para["paragraph_index"]
        if pi in heading_index:
            level = next(h["level"] for h in headings if h["paragraph_index"] == pi)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, para["text"].strip()))
            continue
        meta[pi] = {
            "level": stack[-1][0] if stack else None,
            "path": [t for _l, t in stack],
            "parent": stack[-1][1] if stack else None,
        }

    # 每个节点覆盖的非标题段落（含子树；更深的节点稍后覆盖上一层）
    for node in node_ranges:
        start = (node["head_para"] + 1) if node["head_para"] is not None else 0
        members = [p for p in range(start, node["para_to"] + 1) if p in meta]
        node["_members"] = members
        for k, pi in enumerate(members, 1):
            meta[pi].update({"section": node["title"], "section_level": node["level"],
                             "pos": k, "size": len(members)})

    # 自适应上下文窗口：取能装下 ≥3 段的最深一层；装不下就往上走；都太大则本段±局部窗口
    # 候选按「区间包含本段」挑，不按标题查表 —— 同名标题（如多个「第一圈」）查表会串层。
    plain = list(meta.keys())
    for ordinal, pi in enumerate(plain, 1):
        info = meta[pi]
        info.setdefault("pos", ordinal)
        info.setdefault("size", len(plain))
        cands = [n for n in node_ranges
                 if ((n["head_para"] + 1) if n["head_para"] is not None else 0) <= pi <= n["para_to"]]
        cands.sort(key=lambda n: -n["level"])
        chosen = next((n for n in cands if len(n.get("_members", [])) >= MIN_CONTEXT_PARAGRAPHS), None)
        if chosen is None:
            chosen = cands[-1] if cands else None
        if chosen is not None:
            start = (chosen["head_para"] + 1) if chosen["head_para"] is not None else 0
            chars = sum(len(paragraphs[p]["text"])
                        for p in range(start, min(chosen["para_to"] + 1, n_paragraphs)))
            if chars <= MAX_CONTEXT_CHARS:
                info.update({"context_range": (start, chosen["para_to"] + 1),
                             "context_title": chosen["title"],
                             "context_level": chosen["level"], "context_kind": "section"})
                continue
        info.update({"context_range": (max(0, pi - LOCAL_WINDOW),
                                        min(n_paragraphs, pi + LOCAL_WINDOW + 1)),
                     "context_title": info.get("parent"),
                     "context_level": info.get("level"), "context_kind": "local"})
    return {"headings": headings, "para_meta": meta, "n_paragraphs": n_paragraphs,
            "nodes": node_ranges}


def chapter_outline(book_id: str, chapter_idx: int, rules: tuple = DEFAULT_RULES) -> str:
    """章内层级树的紧凑文本（注入 prompt 用；通常几百字）。"""
    data = structure(book_id, chapter_idx, rules)
    lines = []
    for head in data["headings"]:
        node = next((n for n in data["headings"] if n is head), head)
        indent = "  " * max(0, head["level"] - 1)
        lines.append(f"{indent}- {head['title']}")
    return "\n".join(lines) if lines else "（本章没有可识别的节标题）"


def section_context(book_id: str, chapter_idx: int, paragraph_index: int,
                    rules: tuple = DEFAULT_RULES) -> dict:
    """给某一层生成用的上下文：本段位置感 + 所属窗口的全文。

    窗口大小由层级自适应：默认取能装下 ≥3 段的最深一层；装不下就往上走。
    """
    _chapter, paragraphs, _total = logical_paragraphs(book_id, chapter_idx)
    data = structure(book_id, chapter_idx, rules)
    info = data["para_meta"].get(paragraph_index)
    if info is None:
        return {"is_heading": True, "outline": chapter_outline(book_id, chapter_idx, rules)}
    start, end = info["context_range"]
    body = [{"paragraph_index": p, "text": paragraphs[p]["text"]}
            for p in range(start, end) if p < len(paragraphs)]
    return {
        "is_heading": False,
        "path": info["path"],
        "context_title": info.get("context_title"),
        "pos_in_section": info["pos"],
        "section_size": info["size"],
        "section_text": "\n".join(p["text"] for p in body),
        "section_chars": sum(len(p["text"]) for p in body),
        "outline": chapter_outline(book_id, chapter_idx, rules),
        "same_context_paragraphs": [p["paragraph_index"] for p in body],
    }


if __name__ == "__main__":                       # ponytail: 一条可运行的自检
    assert heading_level("[一、力与力的交互作用]") == 2
    assert heading_level("[Ⅰ．超感官世界]") == 3
    assert heading_level("[1．内在核心，外表现象]") == 4
    assert heading_level("[（1）概念与规律经验]") == 5
    assert heading_level("一、观察的理性") == 2
    assert heading_level("【丛书名】") is None                 # 鱼尾括号是版式标记，不是标题
    assert heading_level("[2．最高本质的概念]") == 4           # 方括号批注仍是标题
    assert heading_level("第三章　力和知性；现象和超感官世界") == 0
    assert heading_level("那超感官界因此乃是作为现象的现象。") is None      # 正文，不是标题
    assert heading_level("所以万有引力的规律或纯粹概念与特殊的规律相对立。") is None
    assert heading_level("1．这是一句以编号开头但其实是正文的完整句子。") is None
    print("agent.tools.context 自检通过")
