"""Shared output and review rules for the reading layers."""

import re
from pathlib import Path

from agent.runtime.config import ROOT_DIR
from agent.tools.book import fingerprint, get_paragraph

REVIEW_FIELDS = ("approved", "issues", "alternatives")
SECTIONS = {
    "L1": ["核心问题", "思想地图", "论证主线", "章节关系"],
    "L2": ["章级总纲", "论证区块", "关键转折", "承上启下"],
}
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
    return {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
    }


def spec_text(spec_type: str) -> tuple[str, str]:
    """Return the selected reading rules and their content fingerprint."""
    content = (ROOT_DIR / "docs" / "reading_semantics.md").read_text(encoding="utf-8")
    version = fingerprint(content)
    if spec_type == "hegel_helin":
        return content, version
    return content.split("## 可选书目知识：", 1)[0], version


def validate_result(book_id, value, ledger, sections=()):
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
    lines = [result["content"]]
    if result.get("sources"):
        lines.append("\n\n### 原文依据\n")
        for ref in result["sources"]:
            chapter_idx, paragraph_index = ref["chapter_idx"], ref["paragraph_index"]
            lines.append(f"- [第 {chapter_idx + 1} 章 · 第 {paragraph_index + 1} 段](#source={chapter_idx}:{paragraph_index})：{ref['quote']}")
    return "\n".join(lines)
