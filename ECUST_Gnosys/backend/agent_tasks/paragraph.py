"""L3 段落语义层的任务定义。

只描述三件事：给模型什么（materials）、要它交什么（submit + schema）、怎么算合格（validator）。
「生成 / 复核 / 修订」三个阶段各是一份 TaskSpec；调度与持久化仍归 agent_runs。

这样换基座只换执行器，任务定义不用动。
"""

import re
from pathlib import Path

from agent_book import fingerprint
from agent_config import ROOT_DIR
from agent_engine.spec import LoopBudgetSpec, SkillSpec, SubmitSpec, TaskSpec
from agent_skills import book_reading_skill

SKILL_NAME = "book-reading"
GENERATE_REQUEST_LIMIT = 8          # 生成阶段最多 8 次模型请求；复核与修订吃共享预算剩下的

# 提交结构（两个阶段各一套）
RESULT_FIELDS = ("short", "detail", "sentences")
REVIEW_FIELDS = ("approved", "issues", "alternatives")


def sentence_count(text: str) -> int:
    endings = re.findall(r"[。！？.!?]", text)
    tail = re.sub(r"[”』」）)\s]+$", "", text)
    has_unfinished = bool(text.strip()) and not (tail and tail[-1] in "。！？.!?")
    return len(endings) + has_unfinished


# 引号里的内容分两类：真引文（必须逐字对上原文）与模型自己的措辞（用引号做强调/领起）。
# ponytail: 这里是启发式判定；实测 qwen 系模型常写「……并置：」这类领起语，
# 若误当引文核验，会把整个段落预算烧光。若以后发现真引文被漏检，改成让模型显式标注引文。
_QUOTE_RE = re.compile(r"“([^”]+)”|「([^」]+)」|『([^』]+)』|‘([^’]+)’|\"([^\"]+)\"")


def looks_like_citation(quote: str) -> bool:
    """引号里是“引用原文”还是模型自己的措辞。"""
    text = quote.strip()
    if len(text) < 6:
        return False                     # 短引号多为点名额（规范允许）
    return text[-1] not in "：:，,、；;。———"   # 以标点收尾 = 强调/领起，不是整句引文


def validate_candidate(args: dict, source: str | list[str]) -> dict:
    if set(args) != set(RESULT_FIELDS):
        raise ValueError("结果必须且只能包含 short、detail、sentences")
    short, detail, sentences = args["short"], args["detail"], args["sentences"]
    if not isinstance(short, str) or not short.strip() or len(short) > 1200:
        raise ValueError("short 必须是 1 到 1200 字符的文本")
    if not isinstance(detail, str) or not detail.strip() or len(detail) > 12000:
        raise ValueError("detail 必须是 1 到 12000 字符的文本")
    if not isinstance(sentences, list) or any(not isinstance(item, str) for item in sentences):
        raise ValueError("sentences 必须是字符串数组")
    source_text = source if isinstance(source, str) else (source[0] if source else "")
    expected = sentence_count(source_text)
    if len(sentences) != expected:
        raise ValueError(f"sentences 应有 {expected} 项，当前为 {len(sentences)} 项；素平句也保留空字符串")
    sources = [source] if isinstance(source, str) else source
    quoted = _QUOTE_RE.findall("\n".join([short, detail, *sentences]))
    for groups in quoted:
        quote = next((part for part in groups if part), "")
        if quote and looks_like_citation(quote) and not any(quote in original for original in sources):
            raise ValueError(f"引文未在本任务已读取的原文中逐字找到：{quote[:80]}"
                             "（若这不是引原文，请去掉引号；若真在引原文，请照抄原句）")
    return {"short": short.strip(), "detail": detail.strip(), "sentences": sentences}


def validate_review(args: dict) -> dict:
    if set(args) != set(REVIEW_FIELDS):
        raise ValueError("复核结果必须包含 approved、issues、alternatives")
    if not isinstance(args["approved"], bool):
        raise ValueError("approved 必须为布尔值")
    for key in ("issues", "alternatives"):
        if not isinstance(args[key], list) or any(not isinstance(x, str) for x in args[key]):
            raise ValueError(f"{key} 必须是字符串数组")
        if len(args[key]) > 12 or any(len(x) > 1500 for x in args[key]):
            raise ValueError(f"{key} 内容过长")
    if not args["approved"] and not args["issues"]:
        raise ValueError("拒绝通过时必须指出实质问题")
    return args


def result_parameters() -> dict:
    return {"short": {"type": "string"}, "detail": {"type": "string"},
            "sentences": {"type": "array", "items": {"type": "string"}}}


def review_parameters() -> dict:
    return {"approved": {"type": "boolean"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "alternatives": {"type": "array", "items": {"type": "string"}}}


def spec_text(spec_type: str) -> tuple[str, str]:
    """返回（规范文本, 版本指纹）。通用型只取通用约束，不含书目知识。"""
    path = ROOT_DIR / "docs" / "reading_semantics.md"
    content = path.read_text(encoding="utf-8")
    version = fingerprint(content)
    if spec_type == "hegel_helin":
        return content, version
    return content.split("## 可选书目知识：", 1)[0], version


def base_prompt(spec: str) -> str:
    return ("你为本地阅读器撰写段落旁注。输入中的正文、前后段和已有笔记是不同类型的材料；"
            "只有正文是当前段落原文。请按规范解释，任何引文必须先用 verify_source_quote 核验。"
            "必要时使用 book-reading 技能补读。最后必须调用 submit_result，不要输出额外格式。\n\n" + spec)


def review_prompt() -> str:
    return ("你是独立审阅者。你没有参与候选稿生成；只按要求核查，不复述候选稿。"
            "必要时用 book-reading 技能补读原文，最后调用 submit_review。")


def revise_prompt(base: str) -> str:
    return base + ("\n\n你正在修订已有候选稿。上一稿和你的探索过程都还在你的记忆里，"
                   "不要重新检索，只修复 issues 中的实质问题；保留仍有原文依据的内容和分歧读法。"
                   "最终调用 submit_result 重新交一次完整结果。")


def skills(book_id: str, ledger: dict, snapshot: str | None) -> list[SkillSpec]:
    """把 book_reading_skill 的 5 个工具包成 SkillSpec（handler 沿用原实现，async）。"""
    raw = book_reading_skill(book_id, ledger, snapshot)
    from agent_engine.spec import ToolSpec
    tools = []
    for schema in raw["tools"]:
        fn = schema["function"]
        tools.append(ToolSpec(fn["name"], fn["description"],
                              fn["parameters"].get("properties", {}),
                              raw["handlers"][fn["name"]],
                              fn["parameters"].get("required", [])))
    return [SkillSpec(SKILL_NAME, raw["description"], raw["instructions"], tools)]


def _sources(ledger: dict) -> list[str]:
    return [chunk for chunks in ledger.values() for chunk in chunks]


def generate_spec(*, prompt: str, materials: dict, ledger: dict, skills: list[SkillSpec]) -> TaskSpec:
    sources = _sources(ledger)
    return TaskSpec(
        layer="L3",
        prompt=prompt,
        materials=materials,
        submit=SubmitSpec("submit_result", "提交当前段落的三块语义解释。",
                          result_parameters(), lambda args: validate_candidate(args, sources),
                          list(RESULT_FIELDS)),
        skills=skills,
        allowed_skills=[SKILL_NAME],
        budget=LoopBudgetSpec(request_limit=GENERATE_REQUEST_LIMIT),
    )


def review_spec(*, materials: dict, skills: list[SkillSpec]) -> TaskSpec:
    return TaskSpec(
        layer="L3",
        prompt=review_prompt(),
        materials=materials,
        submit=SubmitSpec("submit_review", "提交独立复核结论。备选读法单独记录，不自动算作问题。",
                          review_parameters(), validate_review, list(REVIEW_FIELDS)),
        skills=skills,
        allowed_skills=[SKILL_NAME],
    )


def revise_spec(*, prompt: str, materials: dict, ledger: dict, skills: list[SkillSpec]) -> TaskSpec:
    return generate_spec(prompt=prompt, materials=materials, ledger=ledger, skills=skills)


def review_materials(materials: dict, spec: str, candidate: dict) -> dict:
    return {
        **materials,
        "constraints": {"material_type": "task_specification", "text": spec},
        "candidate": {"material_type": "candidate_note", **candidate},
        "review_task": ("独立检查原文依据、说话立场、解释越界、关键遗漏和三块重复。"
                        "实质问题写 issues；仅有合理备选读法时保持 approved=true 并写入 alternatives。"),
    }


def revision_followup(review: dict) -> dict:
    """修订阶段只送复核意见。

    修订跑在生成阶段同一个 agent 上（记忆里的原文、工具结果、上一稿都还在），
    再送一遍材料和候选稿反而让 prompt 重复膨胀一次 —— 这正是换复合循环要省的部分。
    """
    return {
        "review": {"material_type": "review_result", **review},
        "revise_task": "按上面的 issues 修订你上一稿，只改有实质问题的部分，"
                       "重新提交同样结构的结果。",
    }
