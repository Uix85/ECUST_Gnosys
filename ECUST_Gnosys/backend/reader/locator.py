"""定位层：入库时一次性把「章 → 标题区 → 段」的地址做出来，之后所有人只读它。

为什么要在最前面做（见 docs/定位系统_设计_v1.md）：
    四层（全书/章节/语义/概念）的所有产出都存在坐标上——L2 的区块锚点、L3 的段落笔记、
    L4 的义项范围、前端高亮。定位若在运行时现算，它就寄生在渲染管线上（旧路径：
    logical_paragraphs → _get_chapter_slice → 统一 HTML），渲染一变、上层数据全错位。
    所以：**定位是入库产物，最早做、只做一次，下游一律只读**。

坐标（双坐标：位置 + 指纹）：
    Locator = (chapter_idx, para_idx, [sent_idx, [char_from, char_to]])
    位置负责取数据，指纹负责判断"这份数据还对不对"。指纹不是主键，是校验位。

两张表：
    book_nodes       标题树：章与节都是它（章就是一级标题），一个节点一行
    book_paragraphs  唯一段落真相：正文只存这一份，别处只存地址
"""

import json
import sys as _sys
from datetime import datetime
from pathlib import Path as _Path

# 允许 `uv run python backend/reader/locator.py` 直接跑文件尾的自检（与其他模块同约定）
_BACKEND_DIR = str(_Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in _sys.path:
    _sys.path.insert(0, _BACKEND_DIR)

from agent.book import book_db, fingerprint, normalize_text, split_paragraphs


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _int_id(db) -> int | None:
    row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    return row["id"] if row else None


_HAS_CACHE: dict[str, bool] = {}


def clear_cache(book_id: str | None = None) -> None:
    """定位表变动后清缓存（构建工具写完要调用）。"""
    if book_id is None:
        _HAS_CACHE.clear()
    else:
        _HAS_CACHE.pop(book_id, None)


def has_locator(book_id: str) -> bool:
    """这本书是否已经建好定位（表存在且有数据）。进程内缓存，构建后清。"""
    if book_id in _HAS_CACHE:
        return _HAS_CACHE[book_id]
    got = _has_locator_uncached(book_id)
    _HAS_CACHE[book_id] = got
    return got


def _has_locator_uncached(book_id: str) -> bool:
    try:
        db = book_db(book_id)
        pk = _int_id(db)
        if pk is None:
            return False
        return bool(db.execute("SELECT 1 FROM book_nodes WHERE book_id=? LIMIT 1", (pk,)).fetchone())
    except Exception:
        return False


# ── 读取 ────────────────────────────────────────────────

def load_nodes(book_id: str) -> list[dict]:
    """全书标题树节点（文档序）。空列表 = 没建过定位。"""
    db = book_db(book_id)
    pk = _int_id(db)
    if pk is None:
        return []
    return [dict(r) for r in db.execute(
        "SELECT * FROM book_nodes WHERE book_id=? ORDER BY node_idx", (pk,)).fetchall()]


def load_paragraphs(book_id: str, chapter_idx: int) -> list[dict] | None:
    """某章的段落（含 node_idx / seg_in_node）。None = 没建过定位，调用方回退现解。"""
    db = book_db(book_id)
    pk = _int_id(db)
    if pk is None:
        return None
    rows = [dict(r) for r in db.execute(
        "SELECT chapter_idx, para_idx, node_idx, seg_in_node, text, fingerprint, char_count "
        "FROM book_paragraphs WHERE book_id=? AND chapter_idx=? ORDER BY para_idx",
        (pk, chapter_idx)).fetchall()]
    return rows or None


def chapter_count(book_id: str) -> int:
    db = book_db(book_id)
    pk = _int_id(db)
    if pk is None:
        return 0
    return db.execute("SELECT COUNT(*) c FROM book_nodes WHERE book_id=? AND kind='chapter'",
                      (pk,)).fetchone()["c"]


def node_path(nodes: list[dict], node_idx: int) -> list[str]:
    """节点的祖先标题路径（含自身标题）。"""
    by_idx = {n["node_idx"]: n for n in nodes}
    out = []
    cur = by_idx.get(node_idx)
    while cur:
        out.append(cur["title"])
        cur = by_idx.get(cur["parent_idx"]) if cur["parent_idx"] is not None else None
    return list(reversed(out))


def locate(book_id: str, chapter_idx: int, para_idx: int) -> dict:
    """段地址 → 完整位置（含所属节点与祖先路径）。"""
    paragraphs = load_paragraphs(book_id, chapter_idx)
    if not paragraphs or para_idx < 0 or para_idx >= len(paragraphs):
        raise KeyError(f"位置不存在：chapter_idx={chapter_idx} para_idx={para_idx}")
    node = None
    nodes = load_nodes(book_id)
    para = paragraphs[para_idx]
    for n in nodes:
        if n["node_idx"] == para["node_idx"]:
            node = n
            break
    return {"chapter_idx": chapter_idx, "para_idx": para_idx, "text": para["text"],
            "fingerprint": para["fingerprint"], "char_count": para["char_count"],
            "node": node, "node_path": node_path(nodes, para["node_idx"]) if node else [],
            "seg_in_node": para["seg_in_node"]}


def find_by_fingerprint(book_id: str, fp: str) -> list[dict]:
    """指纹 → 位置列表（同一段文字可能在一本书里出现多次，所以是列表）。"""
    db = book_db(book_id)
    pk = _int_id(db)
    if pk is None:
        return []
    return [{"chapter_idx": r["chapter_idx"], "para_idx": r["para_idx"]}
            for r in db.execute("SELECT chapter_idx, para_idx FROM book_paragraphs "
                                "WHERE book_id=? AND fingerprint=? ORDER BY chapter_idx, para_idx",
                                (pk, fp)).fetchall()]


# ── 树构建（纯函数，可单测）──────────────────────────────

def build_tree(chapter_title: str, headings: list[dict], n_paragraphs: int,
               chapter_idx: int, node_start_idx: int) -> list[dict]:
    """由「章标题 + 章内标题行」长出节点树。

    headings：`[{level, title, paragraph_index, synthetic?}]`，level 0 = 章标题。
    返回节点列表（文档序），字段与 book_nodes 对应。
    """
    top = [h for h in headings if h.get("level") == 0]
    inner = [h for h in headings if h.get("level") != 0]
    if top:
        head = top[0]
        chapter_node = {"kind": "chapter", "level": 0, "title": head["title"],
                        "head_para": head.get("paragraph_index"),
                        "synthetic": 1 if head.get("synthetic") else 0, "parent_idx": None}
        # 章内的其它 level-0 标题（误切/嵌套章）降一级，仍留在本章里
        inner = [dict(h, level=max(1, h["level"])) for h in top[1:]] + inner
    else:
        chapter_node = {"kind": "chapter", "level": 0, "title": chapter_title,
                        "head_para": None, "synthetic": 1, "parent_idx": None}
    nodes = [dict(chapter_node, node_idx=node_start_idx, chapter_idx=chapter_idx,
                  para_from=0, para_to=max(0, n_paragraphs - 1))]
    stack = [0]                                   # 栈里存 nodes 的下标
    for h in sorted(inner, key=lambda x: (x.get("paragraph_index") or 0)):
        while len(stack) > 1 and nodes[stack[-1]]["level"] >= h["level"]:
            stack.pop()
        parent = stack[-1]
        nodes.append({"node_idx": node_start_idx + len(nodes), "chapter_idx": chapter_idx,
                      "kind": "section", "level": h["level"], "parent_idx": nodes[parent]["node_idx"],
                      "title": h["title"], "head_para": h.get("paragraph_index"),
                      "synthetic": 0, "para_from": 0, "para_to": 0})
        stack.append(len(nodes) - 1)
    # 每个节点的覆盖区间：到下一个"同级或更浅"的标题为止
    for i, node in enumerate(nodes):
        if i == 0:
            continue
        end = n_paragraphs
        for later in nodes[i + 1:]:
            if later["level"] <= node["level"]:
                end = later["head_para"] if later["head_para"] is not None else n_paragraphs
                break
        node["para_from"] = node["head_para"] if node["head_para"] is not None else 0
        node["para_to"] = max(node["para_from"], end - 1)
    for node in nodes:
        node["n_paragraphs"] = node["para_to"] - node["para_from"] + 1
        node["title_fp"] = fingerprint(node["title"]) if node["title"] else None
    return nodes


def assign_paragraphs(nodes: list[dict], n_paragraphs: int) -> list[dict]:
    """把每段挂到"就近的标题祖先"：返回 [{para_idx, node_idx, seg_in_node}]。

    标题行归它自己的节点；其余段落归"文档序上最后一个已出现的标题节点"
    （与 agent.outline 的 path 栈同语义：出现更浅的标题时自动回到那一层）。
    """
    head_at: dict[int, int] = {}                   # 标题段落 → nodes 下标
    for i, node in enumerate(nodes):
        if node.get("head_para") is not None:
            prev = head_at.get(node["head_para"])
            if prev is None or node["level"] >= nodes[prev]["level"]:
                head_at[node["head_para"]] = i     # 同一段被多个节点认领时取最深的
    out = []
    cur = 0
    counter: dict[int, int] = {}
    for pi in range(n_paragraphs):
        if pi in head_at:
            cur = head_at[pi]
        nid = nodes[cur]["node_idx"]
        counter[nid] = counter.get(nid, 0) + 1
        out.append({"para_idx": pi, "node_idx": nid, "seg_in_node": counter[nid] - 1})
    return out


# ── 构建（入库阶段调用一次）──────────────────────────────

def chapter_spine_rows(book_id: str, book, pages: list, chapters: list) -> list[list[int]]:
    """每章涉及哪些 spine 行（章 = 由若干 spine 块拼成的统一 HTML 切片）。"""
    out = []
    for i in range(len(chapters)):
        start = chapters[i]["start_page"]
        end = chapters[i + 1]["start_page"] if i + 1 < len(chapters) else len(pages)
        rows, seen = [], set()
        for p in pages[start:end]:
            si = p.get("spine_order")
            if si is not None and si not in seen:
                seen.add(si)
                rows.append(si)
        out.append(rows)
    return out


def headings_from_spine(book_id: str, spine_rows: list[int], paragraphs: list[dict]) -> tuple[list, list]:
    """epub 源：把本章覆盖的**全部** spine 块的 heading_json 合起来，按标题文本对回段落。

    旧实现只读章首那一个 spine 块 —— 那正是"标题导不进来"的根因。
    返回 (headings, skipped)：skipped 是文本对不上段落的标题（不编位置，但要报告）。
    """
    db = book_db(book_id)
    entries = []
    for si in spine_rows:
        row = db.execute("SELECT heading_json FROM chapters WHERE spine_order=?", (si,)).fetchone()
        if not row or not (row["heading_json"] or "").strip():
            continue
        try:
            items = json.loads(row["heading_json"])
        except (TypeError, ValueError):
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("text") and isinstance(it.get("level"), int):
                entries.append({"text": (it["text"] or "").strip(), "level": max(0, it["level"] - 1)})
    by_text = {}
    for para in paragraphs:
        key = normalize_text(para["text"])
        if key and key not in by_text:
            by_text[key] = para["para_index"] if "para_index" in para else para["paragraph_index"]
    out, skipped = [], []
    for e in entries:
        pi = by_text.get(normalize_text(e["text"]))
        if pi is None:
            skipped.append(e["text"])
            continue
        out.append({"level": e["level"], "title": e["text"], "paragraph_index": pi})
    return out, skipped


def headings_from_text(paragraphs: list[dict], rules=None) -> list[dict]:
    """txt 源（无 h 标签）：按文本行模式推断层级。"""
    from agent import outline as book_outline
    out = []
    for para in paragraphs:
        pi = para.get("para_index", para.get("paragraph_index"))
        active_rules = rules or book_outline.DEFAULT_RULES
        inline = book_outline.split_inline_heading(para["text"], active_rules)
        if inline:
            title, _body, level = inline
        else:
            title, level = para["text"].strip(), book_outline.heading_level(para["text"], active_rules)
        if level is not None:
            out.append({"level": level, "title": title, "paragraph_index": pi})
    return out


def build(book_id: str, apply: bool = False) -> dict:
    """构建（或只报告）。不重跑正文解析，只读库里已有的 chapters.content_html。"""
    from reader.book_service import (load_book_cached, _get_toc_pages, _build_chapters,
                                     _slice_content, _chapter_slice_anchors, _get_unified_html_cached)
    from agent.book import split_paragraphs

    book = load_book_cached(book_id)
    if book is None:
        raise ValueError(f"书不存在：{book_id}")
    pages = _get_toc_pages(book_id, book)
    chapters = _build_chapters(pages)
    unified = _get_unified_html_cached(book_id, book)
    spine_of_chapter = chapter_spine_rows(book_id, book, pages, chapters)
    db = book_db(book_id)
    pk = _int_id(db)

    all_nodes, all_paras, report = [], [], []
    for ci in range(len(chapters)):
        start_a, end_a = _chapter_slice_anchors(chapters, pages, book, book_id, ci)
        html = _slice_content(unified, start_a, end_a)
        texts = split_paragraphs(html)
        paras = [{"para_index": i, "text": t} for i, t in enumerate(texts)]
        headings, skipped = headings_from_spine(book_id, spine_of_chapter[ci], paras)
        source = "epub"
        if not headings:
            headings = headings_from_text(paras)
            source = "text" if headings else "none"
        nodes = build_tree(chapters[ci].get("title", ""), headings, len(paras),
                           ci, len(all_nodes))
        assign = assign_paragraphs(nodes, len(paras))
        now = _now()
        for n in nodes:
            all_nodes.append({**n, "built_at": now})
        for p, a in zip(paras, assign):
            all_paras.append({"chapter_idx": ci, "para_idx": p["para_index"],
                              "node_idx": a["node_idx"], "seg_in_node": a["seg_in_node"],
                              "text": p["text"], "fingerprint": fingerprint(p["text"]),
                              "char_count": len(p["text"]), "built_at": now})
        report.append({"chapter_idx": ci, "title": chapters[ci].get("title", "")[:40],
                       "n_paragraphs": len(paras), "n_nodes": len(nodes),
                       "heading_source": source, "skipped_headings": skipped[:5],
                       "n_skipped": len(skipped)})

    result = {"book_id": book_id, "n_chapters": len(chapters), "n_nodes": len(all_nodes),
              "n_paragraphs": len(all_paras), "report": report, "applied": False}
    if apply:
        if pk is None:
            raise ValueError("书库缺少 books 行")
        db.execute("DELETE FROM book_nodes WHERE book_id=?", (pk,))
        db.execute("DELETE FROM book_paragraphs WHERE book_id=?", (pk,))
        db.executemany(
            "INSERT INTO book_nodes (book_id,node_idx,kind,level,parent_idx,chapter_idx,title,"
            "title_fp,head_para,para_from,para_to,n_paragraphs,synthetic,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(pk, n["node_idx"], n["kind"], n["level"], n["parent_idx"], n["chapter_idx"],
              n["title"], n["title_fp"], n["head_para"], n["para_from"], n["para_to"],
              n["n_paragraphs"], n["synthetic"], n["built_at"]) for n in all_nodes])
        db.executemany(
            "INSERT INTO book_paragraphs (book_id,chapter_idx,para_idx,node_idx,seg_in_node,"
            "text,fingerprint,char_count,built_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(pk, p["chapter_idx"], p["para_idx"], p["node_idx"], p["seg_in_node"],
              p["text"], p["fingerprint"], p["char_count"], p["built_at"]) for p in all_paras])
        db.commit()
        result["applied"] = True
        clear_cache(book_id)
    return result


def diff_report(book_id: str) -> dict:
    """与库中已有定位对比：位置变了 / 新增 / 消失（只报告，不自动搬任何东西）。"""
    db = book_db(book_id)
    pk = _int_id(db)
    if pk is None:
        return {"error": "no books row"}
    old = {(r["chapter_idx"], r["para_idx"]): r["fingerprint"] for r in db.execute(
        "SELECT chapter_idx, para_idx, fingerprint FROM book_paragraphs WHERE book_id=?", (pk,))}
    new = build(book_id, apply=False)
    return {"old_count": len(old), "new_count": new["n_paragraphs"],
            "note": "位置级差异需逐章比对（见 tools/platform/build_locator.py --diff）"}


if __name__ == "__main__":                       # ponytail: 一条可运行的自检
    nodes = build_tree("第二章　知觉；事物和幻觉", [
        {"level": 0, "title": "第二章　知觉；事物和幻觉", "paragraph_index": 0},
        {"level": 1, "title": "[一、事物的简单概念]", "paragraph_index": 4},
        {"level": 1, "title": "[二、事物的矛盾概念]", "paragraph_index": 9},
        {"level": 2, "title": "第一圈", "paragraph_index": 11},
        {"level": 1, "title": "[三、朝向无条件的普遍性…]", "paragraph_index": 20},
    ], 38, 2, 0)
    assert [n["kind"] for n in nodes] == ["chapter"] + ["section"] * 4, nodes
    # 区间含子树：父节点到下一个"同级或更浅"标题才结束
    assert (nodes[0]["para_from"], nodes[0]["para_to"]) == (0, 37)
    assert (nodes[1]["para_from"], nodes[1]["para_to"]) == (4, 8)
    assert (nodes[2]["para_from"], nodes[2]["para_to"]) == (9, 19), nodes[2]   # 被同级的"三"截断
    assert (nodes[3]["para_from"], nodes[3]["para_to"]) == (11, 19)            # 子树不截断父节点
    assert nodes[3]["parent_idx"] == nodes[2]["node_idx"]
    a = assign_paragraphs(nodes, 38)
    assert a[0]["node_idx"] == 0 and a[3]["node_idx"] == 0        # 章首导语挂在章节点
    assert a[4]["node_idx"] == nodes[1]["node_idx"]
    assert a[9]["seg_in_node"] == 0 and a[10]["seg_in_node"] == 1
    assert a[11]["node_idx"] == nodes[3]["node_idx"]
    assert a[19]["node_idx"] == nodes[3]["node_idx"]             # 子节点未结束时一直属于它
    assert a[20]["node_idx"] == nodes[4]["node_idx"]             # 出现更浅的标题 → 回到那一层
    assert a[30]["node_idx"] == nodes[4]["node_idx"]
    # 没有原文标题时：合成章节点，也不炸
    n2 = build_tree("序言：论科学认识", [], 267, 0, 0)
    assert n2[0]["synthetic"] == 1 and n2[0]["para_to"] == 266
    # 章内再出现一个 level-0 标题（误切/嵌套章）→ 降为一级留在本章
    n3 = build_tree("第一章　感性确定性", [
        {"level": 0, "title": "第一章　感性确定性", "paragraph_index": 0},
        {"level": 0, "title": "第二章　知觉", "paragraph_index": 36}], 40, 1, 0)
    assert n3[1]["level"] == 1 and n3[1]["parent_idx"] == 0, n3[1]
    print("reader.locator 自检通过")
