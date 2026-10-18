"""概念层无模型端到端验证：拦截 → 材料组装 → 工具 → 校验 → 任务定义。

不调用任何模型，所以随时可跑、零成本：
    uv run python tools/concept/check_concept_flow.py
    uv run python tools/concept/check_concept_flow.py --term 对象 --chapter 2

验证的是「接线是否正确、不变量是否成立」，不是「模型答得好不好」：
  1. gate 把脏划词收敛成规范的 (term, 位置)，且校正以原文为准
  2. locate_materials 把本段/邻段写进材料账本（引文核验只认账本，这个不变量必须成立）
  3. 定位组与建图组的工具在真书上能跑通，且返回值带位置
  4. 两套 validator 对**该拒**的结果确实拒（反偷懒、维度/义项、侧重门槛）
  5. TaskSpec 两套能被构造出来（技能包 → ToolSpec 接线正确）
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

PASS, FAIL = "  ✅", "  ❌"


def default_book() -> str:
    for path in sorted((ROOT / "database" / "books").iterdir()):
        if path.is_dir() and (path / "book.db").is_file() and "精神" in path.name:
            return path.name
    raise SystemExit("database/books/ 下没找到《精神现象学》书库")


class Checker:
    def __init__(self):
        self.ok = 0
        self.bad = 0

    def check(self, label: str, cond: bool, detail: str = ""):
        print((PASS if cond else FAIL) + f" {label}" + (f"  {detail}" if detail else ""))
        self.ok += bool(cond)
        self.bad += not cond


def find_position_with(book_id: str, term: str) -> tuple[int, int]:
    from agent.tools import book as agent_book
    for ci in range(20):
        try:
            _, paragraphs, _ = agent_book.logical_paragraphs(book_id, ci)
        except Exception:
            break
        for para in paragraphs:
            if term in para["text"]:
                return ci, para["paragraph_index"]
    raise SystemExit(f"书里找不到「{term}」")


def main() -> int:
    parser = argparse.ArgumentParser(description="概念层无模型端到端验证")
    parser.add_argument("--book", default=default_book())
    parser.add_argument("--term", default="对象")
    parser.add_argument("--chapter", type=int, default=None)
    args = parser.parse_args()

    from agent.tools import book as agent_book
    from agent.tools.concept import concept_skills
    from agent.layers import l4_concept as task
    from agent.layers.l4_selection import gate

    book = args.book
    term = args.term
    c = Checker()

    print(f"书：{book}\n词：{term}\n")

    # ── 1. 拦截 ────────────────────────────────────────
    print("1. 输入拦截")
    if args.chapter is None:
        ci, pi = find_position_with(book, term)
    else:
        ci, pi = args.chapter, 0
    para = agent_book.get_paragraph(book, ci, pi)
    print(f"   位置：第 {ci} 章第 {pi} 段｜{para['chapter_title'][:24]}")
    d_pass = gate(term, para["text"])
    c.check("干净划词 → pass 且 term 为原文片段",
            d_pass.action in ("pass", "corrected") and d_pass.term in para["text"],
            f"action={d_pass.action} term={d_pass.term!r}")
    d_dirty = gate(term + "，", para["text"])
    c.check("多划一个标点 → 静默校正回原文片段", d_dirty.term == term,
            f"term={d_dirty.term!r}")
    d_fake = gate("这段原文里没有的词组", para["text"])
    c.check("原文里没有 → 拒绝并给统一用语",
            d_fake.action == "reject" and d_fake.reason == "no_source_evidence",
            f"reason={d_fake.reason}")

    # ── 2. 材料组装与账本不变量 ────────────────────────
    print("\n2. 定位材料与材料账本")
    ledger: dict = {}
    dist = None
    materials = task.locate_materials(book, term, ci, pi, ledger, window=1)
    c.check("materials 带上了这一句", bool(materials["sentence"]) or term not in para["text"],
            f"sentence_index={materials['sentence_index']}")
    c.check("本段原文进了账本", para["text"] in ledger[(ci, pi)][0][:len(para["text"])])
    c.check("邻段也进了账本", len(ledger) >= 2, f"账本位置数={len(ledger)}")
    c.check("语义层被标注为「参考，不是原文依据」",
            materials["semantic_layer"] is None
            or materials["semantic_layer"]["quality"] == "参考，不是原文依据")

    # ── 3. 工具在真书上跑通 ────────────────────────────
    print("\n3. 工具（真书）")
    locating = concept_skills(book, ledger, "locating")[0]
    mapping = concept_skills(book, ledger, "mapping")[0]
    c.check("定位组只给 1 个工具",
            [t.name for t in locating.tools] == ["locate_term_context"],
            str([t.name for t in locating.tools]))
    c.check("建图组含 4 个新增工具",
            {"find_term_occurrences", "find_definition_candidates",
             "read_paragraphs_at", "read_concept_entry"} <=
            {t.name for t in mapping.tools})

    locate_handlers = {t.name: t.handler for t in locating.tools}
    mapping_handlers = {t.name: t.handler for t in mapping.tools}

    async def run_tools():
        out = {}
        out["locate"] = await locate_handlers["locate_term_context"](
            {"term": term, "chapter_idx": ci, "paragraph_index": pi, "window": 1})
        out["occur"] = await mapping_handlers["find_term_occurrences"]({"term": term, "limit": 6})
        out["defs"] = await mapping_handlers["find_definition_candidates"]({"term": term, "limit": 4})
        out["entry"] = await mapping_handlers["read_concept_entry"]({"term": term})
        return out

    tools_out = asyncio.run(run_tools())
    loc = tools_out["locate"]
    c.check("locate_term_context 返回原文与位置",
            loc["paragraph_text"] == para["text"] and loc["reference"]["paragraph_index"] == pi)
    c.check("分布缓存生效（term_scan 已由 L0 写好）",
            tools_out["occur"]["cached"] or tools_out["occur"]["total"] > 0,
            f"total={tools_out['occur']['total']} cached={tools_out['occur']['cached']}")
    c.check("定义段落召回有候选（且明示只是召回）",
            bool(tools_out["defs"]["candidates"]) and "由你判断" in tools_out["defs"]["note"],
            f"候选数={len(tools_out['defs']['candidates'])}")
    c.check("已有地图读取为空（概念层尚未生成）", tools_out["entry"]["existing_senses"] == [])
    dist = tools_out["occur"]["distribution"]

    # ── 4. validator 该拒的确实拒 ──────────────────────
    print("\n4. 终止标准（validator）")
    sources = [chunk for chunks in ledger.values() for chunk in chunks]
    quote_a = para["text"][:24]
    # 找另一个含该词的段落作为「别处证据」
    ci2, pi2 = None, None
    for cxi, _ in [(x["chapter_idx"], 0) for x in dist]:
        if cxi == ci:
            continue
        _, paragraphs, _ = agent_book.logical_paragraphs(book, cxi)
        hits = [p for p in paragraphs if term in p["text"]]
        if hits:
            ci2, pi2 = cxi, hits[0]["paragraph_index"]
            break
    other_sources = list(sources)
    if ci2 is not None:
        other = agent_book.get_paragraph(book, ci2, pi2)
        other_sources.append(other["text"])
        quote_b = other["text"][:24]
    else:
        quote_b = quote_a

    def sense(**over):
        base = {"reading": "此处指意识所面对的被规定者", "common_reading": "日常指客观事物或目标",
                "scope": "book", "range_note": "", "evolves_to_next": False,
                "distinction_note": "", "dimensions": [], "emphasis_here": {},
                "evidence": [{"chapter_idx": ci, "paragraph_index": pi, "quote": quote_a}]}
        base.update(over)
        return base

    def map_args(**over):
        base = {"has_definition": "inferred", "definition_note": "",
                "confusion_types": ["idiom_shift"], "senses": [sense()], "quotes": [quote_a]}
        base.update(over)
        return base

    def expect_reject(label, fn, fragment=""):
        try:
            fn()
            c.check(label, False, "本应被拒但通过了")
        except ValueError as exc:
            ok = fragment in str(exc) if fragment else True
            c.check(label, ok, f"拒因：{str(exc)[:44]}")

    task.validate_map(map_args(senses=[sense(scope="chapter", range_note="第2章")]),
                      sources, book, term_chapter_count=len(dist))
    c.check("合法单义结果通过", True)
    expect_reject("book 却只有一处证据 → 拒（反偷懒）",
                  lambda: task.validate_map(map_args(), sources, book, term_chapter_count=len(dist)),
                  "跨两章")
    if ci2 is not None:
        two = [{"chapter_idx": ci, "paragraph_index": pi, "quote": quote_a},
               {"chapter_idx": ci2, "paragraph_index": pi2, "quote": quote_b}]
        task.validate_map(map_args(senses=[sense(evidence=two)]),
                          sources + [quote_b], book, term_chapter_count=len(dist))
        c.check("跨两章证据 → 通过", True)
    expect_reject("两个义项却没写 distinction_note",
                  lambda: task.validate_map(map_args(senses=[
                      sense(scope="chapter", range_note="1-2章"),
                      sense(scope="chapter", range_note="3章起", evolves_to_next=False,
                            evidence=[{"chapter_idx": ci, "paragraph_index": pi, "quote": quote_a}])]),
                      sources, book, term_chapter_count=len(dist)),
                  "distinction_note")
    expect_reject("演化链有两个终点",
                  lambda: task.validate_map(map_args(senses=[
                      sense(scope="chapter", range_note="1-2章", distinction_note="读法A"),
                      sense(scope="chapter", range_note="3章起", distinction_note="读法B")]),
                      sources, book, term_chapter_count=len(dist)),
                  "终点")
    expect_reject("写了侧重却没有 reason_kind",
                  lambda: task.validate_map(map_args(senses=[sense(emphasis_here={
                      "dimension": "审美", "why": "会被读成空间距离", "evidence": []})]),
                      sources, book, term_chapter_count=len(dist)),
                  "reason_kind")
    expect_reject("维度名超过 10 字",
                  lambda: task.validate_map(map_args(senses=[sense(dimensions=[
                      {"name": "这是一个非常冗长的维度名称超过限制"}])]),
                      sources, book, term_chapter_count=len(dist)),
                  "维度名")
    expect_reject("引文不在账本里",
                  lambda: task.validate_map(map_args(
                      senses=[sense(scope="chapter", range_note="第 2 章")],
                      quotes=["这句话书里根本没有"]),
                      sources, book, term_chapter_count=len(dist)),
                  "逐字找到")

    task.validate_reading({"reading": "", "common_reading": "", "emphasis_here": {},
                           "stance": "normal", "quotes": [], "needs_mapping": True}, sources)
    c.check("上报证据不足（needs_mapping）→ 通过", True)
    expect_reject("既不给读法也不上报",
                  lambda: task.validate_reading({"reading": "", "common_reading": "x",
                                                 "emphasis_here": {}, "stance": "normal",
                                                 "quotes": [], "needs_mapping": False}, sources),
                  "needs_mapping")
    expect_reject("stance 非法",
                  lambda: task.validate_reading({"reading": "读法", "common_reading": "日常义",
                                                 "emphasis_here": {}, "stance": "weird",
                                                 "quotes": [], "needs_mapping": False}, sources),
                  "stance")
    task.validate_reading({"reading": "读法", "common_reading": "日常义", "emphasis_here": {},
                           "stance": "boundary", "quotes": [], "needs_mapping": False}, sources)
    c.check("boundary（义项过渡处）被接受", True)

    # ── 5. 任务定义接线 ────────────────────────────────
    print("\n5. TaskSpec 接线")
    m_spec = task.map_spec(book_id=book, materials=task.map_materials(
        term, ci, pi, tools_out["occur"]["total"], dist), ledger=ledger,
        term_chapter_count=len(dist))
    l_spec = task.locate_spec(book_id=book, materials=materials, ledger=ledger, has_map=False)
    c.check("建图 TaskSpec：终止工具与技能名正确",
            m_spec.submit.name == "submit_concept_map" and m_spec.allowed_skills == ["concept-mapping"],
            f"budget={m_spec.budget.max_requests}请求/{m_spec.budget.max_tool_calls}工具")
    c.check("定位 TaskSpec：终止工具与技能名正确",
            l_spec.submit.name == "submit_concept_reading" and l_spec.allowed_skills == ["concept-reading"],
            f"budget={l_spec.budget.max_requests}请求/{l_spec.budget.max_tool_calls}工具")
    c.check("技能挂上的工具带 handler", len(m_spec.skills[0].tools) >= 4
            and all(t.handler for t in l_spec.skills[0].tools))
    # 判定标准必须在 prompt 里（曾经错放在 SKILL.md，load_skill 不调就看不到）
    c.check("判定标准在 prompt（不在按需加载的技能里）",
            "代回测试" in m_spec.prompt and "默认沉默" in l_spec.prompt
            and "代回测试" not in l_spec.skills[0].instructions)
    c.check("技能规程只讲工具用法", "工具" in l_spec.skills[0].instructions
            and "三条铁律" not in l_spec.skills[0].instructions)
    # 别名与书目知识
    c.check("建图组能查书目知识（read_glossary）",
            "read_glossary" in {t.name for t in m_spec.skills[0].tools})

    print(f"\n{'=' * 60}\n通过 {c.ok} 项，失败 {c.bad} 项")
    return 1 if c.bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
