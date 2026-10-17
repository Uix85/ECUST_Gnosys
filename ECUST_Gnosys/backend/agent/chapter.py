"""章节层（L2·论证脚手架）：材料组装 + 判定标准 + 合格线 + 渲染。

职责划分（见 docs/章节层_设计_v1.md）：
    机械（管线/代码）  组装材料、把结构树的锚点从定位表接上、渲染 Markdown
    判断（模型，提示里） 每个节点是什么作用、哪些是重点、写多详、递进关系、转折点
    形式（validator）   节点全覆盖、字段有无、枚举成员、字数上限、引文可核

骨架来自定位表：章就是一级标题，节/子节都是 book_nodes 里的节点（见 backend/reader/locator.py）。
模型**不重报锚点**——区间是定位表的机械事实，模型只交判断；渲染与存储时按 node_id 接上。
"""

import json
import re

from agent.book import (book_db, fingerprint, get_paragraph, logical_paragraphs,
                        normalize_text, split_sentences, text_hash)
from agent.engine import LoopBudgetSpec, SubmitSpec, TaskSpec
from agent.tasks import skills as reading_skills

ROLES = ("frontmatter", "setting", "definition", "argument", "transition", "recap", "aside")
WEIGHTS = ("core", "support", "minor")
MAX_NAV_CHARS = 80                  # 每段一行导航：概括截断长度（在标点处断）
BANNED = re.compile(r"甩锅|爆雷|账本|钉死|我们读者|你会发现")


# ── 材料组装（管线，机械）────────────────────────────────

def _cut(text: str, limit: int) -> str:
    """在标点处截断，避免把 ①② 的后半句切一半。"""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = max(head.rfind(c) for c in "。；，、,;.")
    return (head[:cut + 1] if cut >= limit // 2 else head) + "…"


def nav_lines(book_id: str, chapter_idx: int, nodes: list[dict], paragraphs: list[dict]) -> str:
    """每段一行导航：`P12 [二、事物的矛盾概念] 概括`（概括取 L3 已有的段解释）。"""
    db = book_db(book_id)
    by_idx = {n["node_idx"]: n for n in nodes}
    out = []
    for para in paragraphs:
        node = by_idx.get(para.get("node_idx"))
        label = _cut(node["title"], 16) if node else ""
        row = db.execute("SELECT content FROM semantic_layer WHERE text_hash=? AND kind='para'",
                         (text_hash(para["text"]),)).fetchone()
        summary = _cut(row["content"], MAX_NAV_CHARS) if row and row["content"] else "（无 L3 概括）"
        out.append(f"P{para['paragraph_index']}\t[{label}]\t{summary}")
    return "\n".join(out)


def prev_handoff(book_id: str, chapter_idx: int) -> list[str]:
    """上一章的「章末收获」（blocks_json.handoff）。没有就返回空，材料里要明说。"""
    if chapter_idx <= 0:
        return []
    db = book_db(book_id)
    pk = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    row = db.execute("SELECT blocks_json FROM chapter_layer WHERE book_id=? AND chapter_idx=?",
                     (pk["id"], chapter_idx - 1)).fetchone()
    if not row or not row["blocks_json"]:
        return []
    try:
        return list(json.loads(row["blocks_json"]).get("handoff") or [])
    except (TypeError, ValueError):
        return []


def existing_l2(book_id: str, chapter_idx: int) -> str:
    db = book_db(book_id)
    row = db.execute("SELECT content FROM chapter_layer WHERE book_id=? AND chapter_idx=?",
                     (db.execute("SELECT id FROM books LIMIT 1").fetchone()["id"], chapter_idx)
                     ).fetchone()
    return (row["content"] or "") if row else ""


def glossary_hits(book_id: str, paragraphs: list[dict]) -> list[dict]:
    """预筛（管线）：只把**在本章出现过**的术语表条目放进材料，不塞全表。"""
    from agent.index import glossary_entries
    blob = normalize_text("".join(p["text"] for p in paragraphs))
    hits = []
    for name, entry in glossary_entries().items():
        key = normalize_text(name)
        if key and key in blob:
            hits.append({"term": name, "entry": _cut(entry, 200)})
    return hits[:20]


def l3_gaps(paragraphs: list[dict], nodes: list[dict], book_id: str) -> list[int]:
    """哪些段没有 L3 概括（这些段必须读原文）。"""
    db = book_db(book_id)
    have = {r["text_hash"] for r in db.execute(
        "SELECT text_hash FROM semantic_layer WHERE kind='para'").fetchall()}
    return [p["paragraph_index"] for p in paragraphs
            if text_hash(p["text"]) not in have]


def chapter_materials(book_id: str, chapter_idx: int) -> tuple[dict, dict]:
    """返回 (materials, read_sources)。read_sources 里预置"材料已给过的原文"。"""
    from reader import locator
    chapter, paragraphs = None, []
    paragraphs = logical_paragraphs(book_id, chapter_idx)[1]
    chapter = logical_paragraphs(book_id, chapter_idx)[0]
    nodes = [n for n in locator.load_nodes(book_id) if n["chapter_idx"] == chapter_idx]
    read_sources = {(chapter_idx, p["paragraph_index"]): [p["text"]] for p in paragraphs}
    db = book_db(book_id)
    row = db.execute("SELECT content FROM book_layer WHERE book_id=?",
                     (db.execute("SELECT id FROM books LIMIT 1").fetchone()["id"],)).fetchone()
    tree = []
    for n in nodes:
        tree.append({"node_id": n["node_idx"], "level": n["level"],
                     "parent": n["parent_idx"], "title": n["title"],
                     "para_from": n["para_from"], "para_to": n["para_to"],
                     "n_paragraphs": n["n_paragraphs"],
                     "head_para": n["head_para"], "unmarked": bool(n["synthetic"])})
    materials = {
        "chapter": {"chapter_idx": chapter_idx, "title": chapter.get("title", ""),
                    "n_paragraphs": len(paragraphs),
                    "prev_title": (logical_paragraphs(book_id, chapter_idx - 1)[0].get("title", "")
                                   if chapter_idx > 0 else ""),
                    "next_title": _next_title(book_id, chapter_idx, chapter)},
        "tree": tree,
        "nav": nav_lines(book_id, chapter_idx, nodes, paragraphs),
        "prev_handoff": prev_handoff(book_id, chapter_idx) or ["（上一章还没有章末收获：按本书已有的全书层与本章结构判断）"],
        "book_layer": _cut(row["content"], 1200) if row and row["content"] else "（没有全书层内容）",
        "glossary_hits": glossary_hits(book_id, paragraphs),
        "l3_gaps": l3_gaps(paragraphs, nodes, book_id)[:80],
        "existing_l2": _cut(existing_l2(book_id, chapter_idx), 2000),
        "output_shape": {
            "chapter": {"problem": "本章的问题（1 句）", "forecast": "结论预告（1 句）",
                        "ascent": "从最深一层几步论证→区块→区块间递进→全章的上升叙述",
                        "carry_over": "承上一句", "hand_over": "启下一句"},
            "nodes": [{"node_id": "树里的 node_id", "role": "/".join(ROLES),
                       "purpose": "这部分干什么（≤60 字）", "weight": "/".join(WEIGHTS),
                       "focus": "core 才写：本块的重点（≤40 字）",
                       "argument": "core 才写：它确立了 X→但 X 要求 Y→而 Y 与 X 互斥",
                       "to_next": "为什么不能停在这里（≤80 字）",
                       "steps": ["core 才写：2–4 步，一步一句"], "quote": "可选，原文整句引用"}],
            "turning_points": [{"node_id": "（树里的 node_id）", "label": "章首判定/转折点一…",
                                "pos": "（段落下标，整数）", "text": "…"}],
            "handoff": ["章末收获，≤2 条"]},
    }
    return materials, read_sources


def _next_title(book_id: str, chapter_idx: int, chapter: dict) -> str:
    try:
        return logical_paragraphs(book_id, chapter_idx + 1)[0].get("title", "")
    except Exception:
        return ""


# ── 判定标准（进提示，不进技能）───────────────────────────

PROMPT = """你是「章节层（L2·论证脚手架）」撰写者。上游受全书层约束，下游给语义层提供区块锚点与分工边界。

## 你要做的事
读者读正文**之前**要先知道各部分在干什么——像向别人陈述代码用途：有的是 import 式无逻辑内容
（版式、丛书页、全局设定），有的是有明确意图的"函数定义"（概念界定），有的是有具体结构的论证区。

骨架已经给你了（材料里的 tree + nav）：**章就是一级标题，节/子节是它下面的节点**，每个节点有
node_id、标题、覆盖的段落区间。你要做的是**沿这棵树逐节点判断并解释**，不要自己另划区块，
也不要重复报区间（区间是程序的机械事实，你只交判断）。

## 判据
1. 每个节点先答 `purpose`（这部分干什么），再谈论证。版式行（丛书页、版权页、目录）判 `frontmatter`
   一句话带过。
2. `role` 七选一：frontmatter 版式／setting 全局设定（给全章提供零件的章程）／definition 有意图的
   概念界定／argument 论证区／transition 过渡／recap 收束复盘／aside 附注旁支。
3. 概括的对象是**论证动作**，不是段落内容：只答"确立了什么／自身带什么矛盾／为什么不能停"。
4. `argument` 必须是递进，不许并列：写"它确立了 X → 但 X 要求 Y → 而 Y 与 X 互斥 → 意识被迫承认 Z"。
   禁止"接着…又…"式罗列。
5. **侧重由你判断**：哪些节点是 `core`（要展开 argument + steps 2–4 步）、哪些 support/minor
   （一句话），不设条数上限、不设字数指标。长章重点多就多写，短章重点少就少写。
6. 每个内部节点都要写 `to_next`：为什么不能停在这里、下一步是被什么逼出来的（≤80 字）。
7. `ascent`（章级）：从最深一层几步论证 → 区块 → 区块间递进 → 全章的上升叙述。
   **章节点（level 0）不要复述全章**：它只给一句 `purpose`（全章在做什么）＋ `role=setting`，
   不要再在它的 `argument`/`steps` 里把全章推演讲一遍（那是 ascent 的活，重复很显眼）。
   `carry_over`/`hand_over` 直接写内容，不要写"上一章："这类前缀。
8. `turning_points`：3–4 处，标出位置（段落下标）与内容；必须含"章首判定"及其"撕毁处"的呼应。
9. `handoff`：章末收获 ≤2 条，只做交接，不提前讲下一章。

## 反面样本（写之前先自比一次）
- ✗ 段落复述：「本段说明知觉的对象是事物，事物有许多特质。」
  ✓ 论证动作：「把对象立成"具有许多特质的共同的东西"，是为了让它能被规定；而规定要求排斥——
    排斥装不进"又"，于是必须另立"单一"这个环节。」
  怎么判：凡句子能原封不动写成该段 L3 概括，就是段落复述。
- ✗ 并列式伪递进：「接着意识又试了一种办法…然后又改认…」
  怎么判：把任何一步单独抽走，若后文依然成立，那就是并列，不是递进。
- ✗ 正确的废话：「本章讨论意识与对象的关系，论证逐步深入，最终得出重要结论。」
  怎么判：把它换到同一本书的另一章，如果照样成立，就是废话。
- ✗ 平均用力：过渡段和判决节点写一样长。
  怎么判：如果全章只能留三个节点，你会留哪三个？用这个反推 core，而不是均匀分配。

## 字数（显示预算，超了会被打回）
- `purpose` ≤60 字｜`focus` ≤40 字｜`argument` ≤400 字｜`to_next` ≤80 字｜`steps` 每步 ≤120 字
- `chapter.ascent` ≤600 字｜`handoff` ≤2 条｜`turning_points` 3–4 处
- 这些是**显示预算**（侧栏放不下），不是对内容重要性的限制：重点块该展开就展开，只是每段别超长。

## 交卷前逐节点自检
- `purpose`：只看这一句，读者知不知道这块在干什么？
- `argument`：有没有说清"它确立的规定自身带什么缺陷"？
- `to_next`：为什么不能停在这里？
- 全篇：把标题与区间都去掉、只按顺序读 purpose/argument/to_next，会不会出现指代不清或逻辑跳跃？
  会就重写。

## 语言与禁令
- 术语严格沿用贺麟译名，首现括注一个白话等价物；解释用平实现代白话，一步一个箭头。
- 每节点至多 1 个比喻（优先用原文自带意象）、至多 1 句原文整句引用（引文必须逐字取自本次已读原文）。
- 客观陈述语气。禁令：不复述单段内容（语义层的活）；不解释单个术语的定义（概念层的活，改为挂接）；
  不引入译本之外的二手解读；禁用口语化私人比喻（甩锅、爆雷、账本、钉死）与自造新词。
- 最后调用 submit_chapter 交卷。"""


# ── 合格线（validator，只查形式）──────────────────────────

def _obj(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象（现在是 {type(value).__name__}）")
    return value


def _arr(value, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组（现在是 {type(value).__name__}）")
    return value


def _iter_nodes(items):
    for item in items:
        yield item
        yield from _iter_nodes(item.get("children") or [])


def validate_chapter(args: dict, sources: list[str], nodes: list[dict]) -> dict:
    """只查可数/可枚举/可逐字比对的事实；判断（侧重、详略、论证好坏）不在这里管。

    **必须返回结果**：引擎把它当作"已交卷"的凭据（`state["submitted"] = validator(args)`），
    只抛异常不返回 = 引擎认为没交卷（这个坑踩过一次，见 check_chapter_flow 的回归断言）。
    """
    args = _obj(args, "提交内容")
    known = {n["node_idx"]: n for n in nodes}
    ch = _obj(args.get("chapter") or {}, "chapter")
    for key, label in (("problem", "本章的问题"), ("forecast", "结论预告"),
                       ("ascent", "全章论证主线"), ("carry_over", "承上"), ("hand_over", "启下")):
        if not (ch.get(key) or "").strip():
            raise ValueError(f"chapter.{key} 不能为空（{label}）")
    if len(ch["ascent"]) > 600:
        raise ValueError("chapter.ascent 超过 600 字")

    out_nodes = _arr(args.get("nodes") or [], "nodes")
    if not out_nodes:
        raise ValueError("nodes 不能为空：结构树里的每个节点都要给判断")
    seen = {}
    for item in out_nodes:
        item = _obj(item, "nodes 的每一项")
        nid = item.get("node_id")
        if nid not in known:
            raise ValueError(f"node_id={nid} 不在本章结构树里")
        if nid in seen:
            raise ValueError(f"node_id={nid} 重复")
        seen[nid] = item
        role = item.get("role")
        weight = item.get("weight")
        if role not in ROLES:
            raise ValueError(f"node {nid} 的 role 非法：{role}（只能是 {'/'.join(ROLES)}）")
        if weight not in WEIGHTS:
            raise ValueError(f"node {nid} 的 weight 非法：{weight}（只能是 {'/'.join(WEIGHTS)}）")
        purpose = (item.get("purpose") or "").strip()
        if not purpose:
            raise ValueError(f"node {nid} 缺 purpose：每个节点都要说明这部分干什么")
        if len(purpose) > 60:
            raise ValueError(f"node {nid} 的 purpose 超过 60 字（显示预算）")
        if weight == "core":
            if not (item.get("argument") or "").strip():
                raise ValueError(f"node {nid} 标为 core 就必须写 argument")
            steps = item.get("steps") or []
            if not 2 <= len(steps) <= 4:
                raise ValueError(f"node {nid} 标为 core 就要给 2–4 步 steps，现在是 {len(steps)}")
            for s in steps:
                if len(s) > 120:
                    raise ValueError(f"node {nid} 的 steps 单步超过 120 字")
        if item.get("argument") and len(item["argument"]) > 400:
            raise ValueError(f"node {nid} 的 argument 超过 400 字")
        if (item.get("to_next") or "") and len(item["to_next"]) > 80:
            raise ValueError(f"node {nid} 的 to_next 超过 80 字")
        if item.get("focus") and len(item["focus"]) > 40:
            raise ValueError(f"node {nid} 的 focus 超过 40 字")
        quote = (item.get("quote") or "").strip()
        if quote and not any(quote in src for src in sources):
            raise ValueError(f"node {nid} 的引文未在本次已读原文中逐字找到：{quote[:30]}")
        blob = " ".join(str(v) for v in item.values())
        if BANNED.search(blob):
            raise ValueError(f"node {nid} 用了禁用词：{BANNED.search(blob).group()}")
    # 结构树里的每个节点都要有判断（含 frontmatter 一句话带过）
    missing = [nid for nid in known if nid not in seen and known[nid]["kind"] != "chapter"]
    if missing:
        raise ValueError(f"这些节点没给判断：{missing[:6]}（共 {len(missing)} 个）")
    # 内部节点（有子节点）必须写 to_next
    for nid, node in known.items():
        if node["kind"] == "chapter":
            continue
        if any(n["parent_idx"] == nid for n in nodes) and not (seen[nid].get("to_next") or "").strip():
            raise ValueError(f"node {nid} 是内部节点，必须写 to_next（区块间递进）")

    tps = _arr(args.get("turning_points") or [], "turning_points")
    if not 3 <= len(tps) <= 4:
        raise ValueError(f"turning_points 要 3–4 处，现在是 {len(tps)}")
    for tp in tps:
        tp = _obj(tp, "turning_points 的每一项")
        if tp.get("node_id") not in known:
            raise ValueError(f"转折点指向了不存在的 node_id={tp.get('node_id')}")
        if not (tp.get("label") or "").strip() or not (tp.get("text") or "").strip():
            raise ValueError("转折点必须写 label 与 text")
        if not isinstance(tp.get("pos"), int) or not 0 <= tp["pos"] < max(
                n["para_to"] for n in nodes) + 1:
            raise ValueError(f"转折点的 pos 不是本章的有效段落下标：{tp.get('pos')}")
    labels = " ".join(tp.get("label", "") for tp in tps)
    if "首" not in labels and not any("章首" in (tp.get("text") or "") for tp in tps):
        raise ValueError("转折点必须含'章首判定'及其'撕毁处'的呼应（label 或 text 里点出）")
    if BANNED.search(json.dumps(args, ensure_ascii=False)):
        raise ValueError("结果里出现禁用词")
    handoff = _arr(args.get("handoff") or [], "handoff")
    if len(handoff) > 2:
        raise ValueError(f"handoff 最多 2 条，现在是 {len(handoff)}")
    return args


# ── 渲染（机械：结构化 → 读者看的 Markdown）──────────────

ROLE_LABEL = {"frontmatter": "版式", "setting": "全局设定", "definition": "概念界定",
              "argument": "论证", "transition": "过渡", "recap": "收束", "aside": "附注"}
WEIGHT_MARK = {"core": "重点", "support": "", "minor": ""}


def render_markdown(blocks: dict) -> str:
    nodes = {n["node_idx"]: n for n in (blocks.get("nodes_source") or [])}
    ch = blocks.get("chapter") or {}
    out = [f"# {blocks.get('title', '')}", ""]
    if ch.get("problem"):
        out += [f"**本章的问题**：{ch['problem']}", f"**结论预告**：{ch.get('forecast', '')}", ""]
    if ch.get("ascent"):
        out += ["**全章论证主线**", ch["ascent"], ""]
    if ch.get("carry_over") or ch.get("hand_over"):
        out += [f"**承上**：{ch.get('carry_over', '')}　**启下**：{ch.get('hand_over', '')}", ""]
    for item in blocks.get("nodes") or []:
        src = nodes.get(item["node_id"]) or {}
        level = min(3, max(1, src.get("level", 1) + 1))
        title = src.get("title") or f"节点{item['node_id']}"
        mark = WEIGHT_MARK.get(item.get("weight", ""), "")
        role = ROLE_LABEL.get(item.get("role", ""), "")
        head = f"{'#' * level} {title}"
        if role or mark:
            head += f"（{'+'.join(x for x in (mark, role) if x)}）"
        out += [head, f"**作用**：{item.get('purpose', '')}"]
        if item.get("focus"):
            out += [f"**重点**：{item['focus']}"]
        if item.get("argument"):
            out += [f"**任务**：{item['argument']}"]
        for i, s in enumerate(item.get("steps") or [], 1):
            out += [f"{i}. {s}"]
        if item.get("to_next"):
            out += [f"**去向**：{item['to_next']}"]
        if item.get("quote"):
            out += [f"> {item['quote']}"]
        out += [""]
    tps = blocks.get("turning_points") or []
    if tps:
        out += ["## 关键论证转折点", "", "| # | 位置 | 内容 |", "|---|---|---|"]
        for tp in tps:
            out.append(f"| {tp.get('label', '')} | P{tp.get('pos', '')} | {tp.get('text', '')} |")
        out.append("")
    if blocks.get("handoff"):
        out += ["## 章末收获（移交下一章）", ""]
        out += [f"- {h}" for h in blocks["handoff"]]
    return "\n".join(out).strip() + "\n"


def save_chapter_layer(book_id: str, chapter_idx: int, args: dict, nodes: list[dict],
                       model_version: str = "") -> dict:
    """落库：把模型的判断与定位表的锚点接起来，写 content + blocks_json + chapter_idx。"""
    from datetime import datetime
    db = book_db(book_id)
    pk = db.execute("SELECT id FROM books LIMIT 1").fetchone()["id"]
    blocks = {"chapter_idx": chapter_idx, "title": logical_paragraphs(book_id, chapter_idx)[0].get("title", ""),
              "chapter": args.get("chapter"), "nodes": args.get("nodes"),
              "turning_points": args.get("turning_points"), "handoff": args.get("handoff") or [],
              "nodes_source": [{"node_idx": n["node_idx"], "level": n["level"], "title": n["title"],
                                "para_from": n["para_from"], "para_to": n["para_to"],
                                "parent_idx": n["parent_idx"], "kind": n["kind"]} for n in nodes]}
    content = render_markdown(blocks)
    now = datetime.now().isoformat(timespec="seconds")
    blob = json.dumps({"chapter": blocks["chapter"], "nodes": blocks["nodes"],
                       "turning_points": blocks["turning_points"], "handoff": blocks["handoff"],
                       "nodes_source": blocks["nodes_source"], "title": blocks["title"]},
                      ensure_ascii=False)
    row = db.execute("SELECT id FROM chapter_layer WHERE book_id=? AND chapter_idx=?",
                     (pk, chapter_idx)).fetchone()
    if row:
        db.execute("UPDATE chapter_layer SET content=?, blocks_json=?, model_version=?, updated_at=? "
                   "WHERE id=?", (content, blob, model_version, now, row["id"]))
    else:
        db.execute("INSERT INTO chapter_layer (chapter_id, book_id, chapter_idx, content, blocks_json,"
                   " model_version, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (None, pk, chapter_idx, content, blob, model_version, now))
    db.commit()
    return {"content": content, "blocks": blocks}


# ── 任务定义 ────────────────────────────────────────────

def submit_parameters() -> dict:
    """交卷工具的入参形状。声明清楚，模型不用猜信封长什么样。"""
    node = {"type": "object", "properties": {
        "node_id": {"type": "integer", "description": "结构树里的 node_id"},
        "role": {"type": "string", "enum": list(ROLES)},
        "purpose": {"type": "string", "description": "这部分干什么（≤60 字）"},
        "weight": {"type": "string", "enum": list(WEIGHTS)},
        "focus": {"type": "string", "description": "core 才写：本块重点（≤40 字）"},
        "argument": {"type": "string", "description": "core 才写：论证动作（≤400 字）"},
        "steps": {"type": "array", "items": {"type": "string"}, "description": "core 才写：2–4 步，每步 ≤120 字"},
        "to_next": {"type": "string", "description": "内部节点必写：为什么不能停（≤80 字）"},
        "quote": {"type": "string", "description": "可选：原文整句引用（必须逐字取自已读原文）"},
    }, "required": ["node_id", "role", "purpose", "weight"]}
    return {
        "chapter": {"type": "object", "description": "章级总纲", "properties": {
            "problem": {"type": "string", "description": "本章的问题（1 句）"},
            "forecast": {"type": "string", "description": "结论预告（1 句）"},
            "ascent": {"type": "string", "description": "从几步论证→区块→区块间递进→全章的上升（≤600 字）"},
            "carry_over": {"type": "string"}, "hand_over": {"type": "string"}},
            "required": ["problem", "forecast", "ascent", "carry_over", "hand_over"]},
        "nodes": {"type": "array", "items": node, "description": "结构树里每个节点都要有一条"},
        "turning_points": {"type": "array", "items": {"type": "object", "properties": {
            "node_id": {"type": "integer"}, "label": {"type": "string"},
            "pos": {"type": "integer", "description": "段落下标"},
            "text": {"type": "string"}}, "required": ["node_id", "label", "pos", "text"]},
            "description": "3–4 处，必须含章首判定与它的撕毁处呼应"},
        "handoff": {"type": "array", "items": {"type": "string"}, "description": "章末收获 ≤2 条"},
    }


def chapter_spec(*, book_id: str, materials: dict, read_sources: dict, nodes: list[dict]) -> TaskSpec:
    sources = [chunk for chunks in read_sources.values() for chunk in chunks]
    return TaskSpec(
        layer="L2",
        prompt=PROMPT,
        materials=materials,
        submit=SubmitSpec("submit_chapter", "提交本章的论证脚手架（章级总纲 + 逐节点判断 + 转折点 + 章末收获）。",
                          submit_parameters(), lambda args: validate_chapter(args, sources, nodes),
                          ["chapter", "nodes", "turning_points", "handoff"]),
        skills=reading_skills(book_id, read_sources, None),
        allowed_skills=["book-reading"],
        budget=LoopBudgetSpec(max_requests=60, max_tool_calls=40,
                              timeout_seconds=1800, request_limit=60),
    )
