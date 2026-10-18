"""Shared output and review rules for the reading layers."""

import re
from pathlib import Path

from agent.runtime.config import ROOT_DIR
from agent.tools.book import fingerprint, get_paragraph

REVIEW_FIELDS = ("approved", "issues", "alternatives")
# 各层结果正文必须出现的 Markdown 章节标题（validate_result 逐项校验）。
SECTIONS = {
    "L1": ["核心问题", "思想地图", "论证主线", "章节关系"],
    "L2": ["章级总纲", "论证区块", "关键转折", "承上启下"],
}
# 交卷结果的 JSON Schema：正文 Markdown + 可核验的原文位置与逐字引文。
RESULT_PARAMS = {
    "content": {"type": "string"},
    "sources": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "chapter_idx": {"type": "integer"},
                "paragraph_index": {"type": "integer"},
                "quote": {"type": "string"},
            },
            "required": ["chapter_idx", "paragraph_index", "quote"],
            "additionalProperties": False,
        },
    },
}


def validate_review(args: dict) -> dict:
    """校验独立复核结果：必须明确 approved，拒绝时须列出实质问题。

    issues / alternatives 各自限 12 条、单条 1500 字，避免把复核意见写成又一篇长文。
    """
    if set(args) != set(REVIEW_FIELDS):
        raise ValueError("复核结果必须包含 approved、issues、alternatives")
    if not isinstance(args["approved"], bool):
        raise ValueError("approved 必须为布尔值")
    for key in ("issues", "alternatives"):
        if not isinstance(args[key], list) or any(
            not isinstance(item, str) for item in args[key]
        ):
            raise ValueError(f"{key} 必须是字符串数组")
        if len(args[key]) > 12 or any(len(item) > 1500 for item in args[key]):
            raise ValueError(f"{key} 内容过长")
    if not args["approved"] and not args["issues"]:
        raise ValueError("拒绝通过时必须指出实质问题")
    return args


def review_parameters() -> dict:
    """复核交卷工具的入参形状。"""
    return {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
    }


def spec_text(spec_type: str) -> tuple[str, str]:
    """返回所选书目规范正文及其内容指纹。

    指纹用于缓存失效：规范文件改动后 version 变化，旧结果不再被复用。
    只有 hegel_helin 需要「可选书目知识」整段；其余层截掉该段，避免把别的书的
    译名卡塞进提示词。
    """
    content = (ROOT_DIR / "docs" / "reading_semantics.md").read_text(encoding="utf-8")
    version = fingerprint(content)
    if spec_type == "hegel_helin":
        return content, version
    return content.split("## 可选书目知识：", 1)[0], version


def validate_result(book_id, value, ledger, sections=()):
    """校验 L1/L2 结果：正文非空、结构标题齐全、引文双重复核。

    引文要过两道关：先必须出现在本任务已读账本（ledger）里，避免模型凭记忆编造；
    再回到当前原文逐字比对，位置或文本已变化就拒绝。通过后把段落 fingerprint
    写回 sources，供渲染/存储侧判定引用是否过期。
    """
    if set(value) != {"content", "sources"} or not isinstance(value["content"], str) or not value["content"].strip():
        raise ValueError("提交 content（Markdown）和 sources（原文位置及逐字引文），正文不能为空")
    for section in sections:
        if not re.search(r"^#{1,6}[^\n]*" + re.escape(section), value["content"], re.M):
            raise ValueError("Markdown 缺少结构标题：" + section)
    if not isinstance(value["sources"], list) or not value["sources"]:
        raise ValueError("至少提供一处可核验的原文证据")
    for item in value["sources"]:
        chapter_idx, paragraph_index, quote = item.get("chapter_idx"), item.get("paragraph_index"), item.get("quote")
        if type(chapter_idx) is not int or type(paragraph_index) is not int or not isinstance(quote, str) or not quote:
            raise ValueError("sources 需要 chapter_idx、paragraph_index 和非空 quote")
        if not any(quote in source for source in ledger.get((chapter_idx, paragraph_index), [])):
            raise ValueError("引文不在该位置的已读原文中")
        paragraph = get_paragraph(book_id, chapter_idx, paragraph_index)
        if quote not in paragraph["text"]:
            raise ValueError("原文已变化或引文位置不正确")
        item["fingerprint"] = paragraph["fingerprint"]
    return value


def markdown(result):
    """把结构化结果渲染成读者可见 Markdown：正文 + 可跳转的「原文依据」列表。

    链接写成 #source=章序:段序（从 0 开始），前端据此跳转并高亮原文。
    """
    lines = [result["content"]]
    if result.get("sources"):
        lines.append("\n\n### 原文依据\n")
        for ref in result["sources"]:
            chapter_idx, paragraph_index = ref["chapter_idx"], ref["paragraph_index"]
            lines.append(f"- [第 {chapter_idx + 1} 章 · 第 {paragraph_index + 1} 段](#source={chapter_idx}:{paragraph_index})：{ref['quote']}")
    return "\n".join(lines)


# 收尾规则单独成段，位置在领域提示词之前。
RULES = """## 收尾规则（按此执行，不要自己发明流程）
1. 每一轮只调用一个工具，不要在一轮里同时调多个。
2. 先读后写。读原文用 read_book_paragraph，检索用 search_book_text，读已有笔记用 read_existing_note。
3. 引文必须先用 verify_source_quote 核验过一次才写进结果。核验不通过就改用自己的话复述，
   不要反复重试同一句引文。
4. 同一工具 + 同一组参数不要重复调用第二次；已经有结论就往前走。
5. 整个任务最多 {tool_budget} 次工具调用，超了直接失败。用掉一半还没开始动笔，就直接动笔：
   宁可写得保守一些，也不要因为“材料读得不够”而交不出稿。
6. 只要你能写出一份符合规范的结果，立刻调用 {submit} 交卷 —— 交卷即任务结束，
   不要再补做一次核验或补充阅读。
7. {submit} 返回错误说明时，含义是“改一处再交一次”：按错误信息修改后重新提交，
   不要重新从头探索。"""


def build_instructions(spec, *, tool_budget: int) -> str:
    """将统一收尾规则与领域提示词组合起来。

    收尾规则在前、领域提示词在后：前者约束流程（预算、交卷时机），
    后者才描述「这一层要做什么」，避免各层各自发明收尾流程。
    """
    return (
        RULES.format(submit=spec.submit.name, tool_budget=tool_budget)
        + "\n\n"
        + spec.prompt
    )
