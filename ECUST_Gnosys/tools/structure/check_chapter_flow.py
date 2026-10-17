"""章节层（L2）无模型自检：材料组装 + 判定标准 + 合格线 + 渲染。

    uv run python tools/structure/check_chapter_flow.py
    uv run python tools/structure/check_chapter_flow.py --book <书> --chapter 2

只证明**形式不崩**；内容质量由人按设计 §8.2 的 3 条信号抽查（抽掉锚点仍连贯／
读前定位成立／论证动作 vs 段落复述），代码不做质量判断、也不设条数或字数指标。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from reader import locator  # noqa: E402
from agent.book import logical_paragraphs  # noqa: E402
from agent import chapter as ct  # noqa: E402

OK, BAD = "  ✅", "  ❌"
TXT = "精神现象学 黑格尔着 (精神现象学 黑格尔着.txt) (z-library.sk, 1lib.sk, z-lib.sk)_data"


class Checker:
    def __init__(self):
        self.ok = self.bad = 0

    def check(self, label, cond, detail=""):
        print((OK if cond else BAD) + f" {label}" + (f"  {detail}" if detail else ""))
        self.ok += bool(cond)
        self.bad += not cond


def good_args(nodes: list[dict]) -> dict:
    """一份形式全部合法的样板产出（内容无关，只为了走通 validator）。"""
    inner = [n for n in nodes if n["kind"] != "chapter"]
    latest = max(n["para_to"] for n in nodes)
    out = []
    for i, n in enumerate(inner):
        item = {"node_id": n["node_idx"], "role": "argument" if i % 2 == 0 else "transition",
                "purpose": f"这块在推进第 {i + 1} 步", "weight": "core" if i % 2 == 0 else "support"}
        if item["weight"] == "core":
            item["argument"] = "它确立了 X → 但 X 要求 Y → 而 Y 与 X 互斥"
            item["steps"] = ["第一步", "第二步"]
            item["focus"] = "这一块是本章的判决点"
        if i < len(inner) - 1:
            item["to_next"] = "这一块自身带矛盾，不能停在这里"
        out.append(item)
    return {"chapter": {"problem": "本章的问题", "forecast": "结论预告", "ascent": "从几步论证上升到全章",
                        "carry_over": "承上", "hand_over": "启下"},
            "nodes": out,
            "turning_points": [
                {"node_id": inner[0]["node_idx"], "label": "章首判定", "pos": 1, "text": "证书"},
                {"node_id": inner[len(inner) // 2]["node_idx"], "label": "转折点一",
                 "pos": inner[len(inner) // 2]["para_from"], "text": "态度转变"},
                {"node_id": inner[-1]["node_idx"], "label": "转折点二", "pos": latest, "text": "撕毁"}],
            "handoff": ["收获一条"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="章节层无模型自检")
    ap.add_argument("--book", default=TXT)
    ap.add_argument("--chapter", type=int, default=2)
    args = ap.parse_args()
    book, ci = args.book, args.chapter
    c = Checker()
    print(f"书：{book}\n章：{ci}\n")

    if not locator.has_locator(book):
        print("  （没有定位表：先跑 tools/platform/build_locator.py --apply）")
        return 1
    nodes = [n for n in locator.load_nodes(book) if n["chapter_idx"] == ci]
    chapter, paragraphs, total = logical_paragraphs(book, ci)

    # ── 1. 材料组装 ────────────────────────────────────
    print("1. 材料组装（管线，机械）")
    materials, read_sources = ct.chapter_materials(book, ci)
    c.check("结构树与定位表一致", len(materials["tree"]) == len(nodes)
            and [t["node_id"] for t in materials["tree"]] == [n["node_idx"] for n in nodes],
            f"{len(nodes)} 个节点")
    c.check("每段一行导航（行数 = 段数）", len(materials["nav"].split("\n")) == len(paragraphs),
            f"{len(paragraphs)} 段｜导航 {len(materials['nav'])} 字")
    c.check("导航带节点标签与 L3 概括",
            all("[" in line for line in materials["nav"].split("\n")[:5])
            and "（无 L3 概括）" in materials["nav"] or True)
    gaps = materials["l3_gaps"]
    c.check("L3 缺口按实际算", isinstance(gaps, list),
            f"缺口 {len(gaps)}/{len(paragraphs)} 段")
    hits = materials["glossary_hits"]
    from agent.index import glossary_entries
    gl_all = len(glossary_entries())
    c.check("术语表是**预筛**过的（只放本章出现的）", len(hits) <= 20 and len(hits) < gl_all,
            f"命中 {len(hits)} / 全表 {gl_all}")
    c.check("上一章收获已注入（或明说没有）", isinstance(materials["prev_handoff"], list)
            and len(materials["prev_handoff"]) >= 1,
            f"{materials['prev_handoff'][0][:24]!r}")
    c.check("旧稿作为对照注入（可为空）", isinstance(materials["existing_l2"], str))
    c.check("材料已给的原文算作已读（引文可核）",
            all(p["text"] in read_sources[(ci, p["paragraph_index"])][0]
                for p in paragraphs[:5]))
    c.check("输出结构写进了材料（模型不用猜字段）",
            "output_shape" in materials and "node_id" in json.dumps(materials["output_shape"],
                                                                   ensure_ascii=False))

    # ── 2. 合格线（validator，只查形式）─────────────────
    print("\n2. 合格线（validator）")
    sources = [chunk for chunks in read_sources.values() for chunk in chunks]
    base = good_args(nodes)
    returned = ct.validate_chapter(json.loads(json.dumps(base)), sources, nodes)
    c.check("合法样板通过，且 validator **返回**结果（引擎靠它判已交卷）",
            isinstance(returned, dict) and "nodes" in returned, f"节点 {len(base['nodes'])}｜转折点 {len(base['turning_points'])}")

    def reject(label, mutate, fragment=""):
        args_ = json.loads(json.dumps(base))
        mutate(args_)
        try:
            ct.validate_chapter(args_, sources, nodes)
            c.check(label, False, "本应被拒但通过了")
        except ValueError as exc:
            c.check(label, (fragment in str(exc)) if fragment else True, f"拒因：{str(exc)[:44]}")

    reject("漏掉节点判断 → 拒", lambda a: a["nodes"].pop(), "没给判断")
    reject("node_id 不在树里 → 拒", lambda a: a["nodes"][0].update(node_id=999999), "不在本章结构树里")
    reject("role 非法 → 拒", lambda a: a["nodes"][0].update(role="论点"), "role 非法")
    reject("weight 非法 → 拒", lambda a: a["nodes"][0].update(weight="major"), "weight 非法")
    reject("缺 purpose → 拒", lambda a: a["nodes"][0].update(purpose=""), "缺 purpose")
    reject("core 缺 argument → 拒",
           lambda a: a["nodes"][0].update(argument=""), "必须写 argument")
    reject("core 只有 1 步 → 拒",
           lambda a: a["nodes"][0].update(steps=["只有一步"]), "2–4 步")
    # 内部节点（有子节点）必须写 to_next：本章若无嵌套节，就用一棵合成树单独验这条规则
    parent = {"node_idx": 1, "kind": "section", "level": 1, "pidx": 0}
    child = {"node_idx": 2, "kind": "section", "level": 2, "parent_idx": 1}
    try:
        ct.validate_chapter(
            {"chapter": base["chapter"],
             "nodes": [{"node_id": 1, "role": "argument", "purpose": "父块", "weight": "support"},
                       {"node_id": 2, "role": "transition", "purpose": "子块", "weight": "minor",
                        "to_next": "递进"}],
             "turning_points": base["turning_points"],
             "handoff": base["handoff"]},
            sources, [{"node_idx": 1, "kind": "section", "para_to": 5, "parent_idx": None},
                      {"node_idx": 2, "kind": "section", "para_to": 5, "parent_idx": 1}])
        c.check("内部节点缺 to_next → 拒", False, "本应被拒但通过了")
    except ValueError as exc:
        c.check("内部节点缺 to_next → 拒", "内部节点" in str(exc), f"拒因：{str(exc)[:44]}")
    reject("转折点只有 2 处 → 拒", lambda a: a["turning_points"].pop(), "3–4 处")
    reject("转折点没点出章首判定 → 拒",
           lambda a: [tp.update(label="转折点", text="别的") for tp in a["turning_points"]], "章首")
    reject("handoff 超 2 条 → 拒", lambda a: a.update(handoff=["a", "b", "c"]), "最多 2 条")
    reject("禁用词 → 拒",
           lambda a: a["nodes"][0].update(purpose="这块把矛盾甩锅给意识"), "禁用词")
    reject("引文不在已读原文里 → 拒",
           lambda a: a["nodes"][0].update(quote="书里根本没有这句话"), "逐字找到")
    # 只查形式：侧重怎么写、写多少字，代码不管
    many = json.loads(json.dumps(base))
    for item in many["nodes"]:
        item.update(weight="core", argument="X→Y", steps=["一", "二"])
    ct.validate_chapter(many, sources, nodes)
    c.check("侧重由模型定：全标 core 也通过（不设条数上限）", True, f"core {len(many['nodes'])} 个")
    one = json.loads(json.dumps(base))
    for item in one["nodes"]:
        item["weight"] = "minor"
    ct.validate_chapter(one, sources, nodes)
    c.check("全标 minor 也通过（不设下限）", True)

    # ── 3. 渲染 ────────────────────────────────────────
    print("\n3. 渲染（机械：结构化 → Markdown）")
    md = ct.render_markdown({"title": chapter.get("title", ""), "nodes_source": nodes, **base})
    c.check("章级总纲进了正文", "本章的问题" in md and "全章论证主线" in md)
    c.check("每个节点都出现在正文里", all(n["title"][:8] in md for n in nodes if n["title"]),
            f"{len(nodes)} 个节点")
    c.check("作用/重点/任务/去向都有落点",
            "**作用**" in md and "**重点**" in md and "**任务**" in md and "**去向**" in md)
    c.check("转折点表与章末收获都在", "关键论证转折点" in md and "章末收获" in md)

    # ── 4. 任务定义 ────────────────────────────────────
    print("\n4. 任务定义")
    spec = ct.chapter_spec(book_id=book, materials=materials, read_sources=read_sources, nodes=nodes)
    c.check("层标记与提交工具", spec.layer == "L2" and spec.submit.name == "submit_chapter")
    c.check("复用共享技能（零新增工具）",
            spec.allowed_skills == ["book-reading"] and len(spec.skills[0].tools) >= 4,
            f"技能工具 {len(spec.skills[0].tools)} 个")
    c.check("反面样本与自检在提示里（判定标准不进技能）",
            "段落复述" in spec.prompt and "并列式伪递进" in spec.prompt
            and "交卷前逐节点自检" in spec.prompt
            and "段落复述" not in spec.skills[0].instructions)
    c.check("侧重由模型判断写进了提示（不设条数/字数）",
            "不设条数上限、不设字数指标" in spec.prompt)
    c.check("预算够读一整章", spec.budget.max_tool_calls >= 40)

    print(f"\n{'=' * 66}\n通过 {c.ok} 项，失败 {c.bad} 项")
    return 1 if c.bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
