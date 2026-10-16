"""基座等价性测试：三个基座吃同一份 TaskSpec，行为必须一致。

  agents_sdk   生产路径（OpenAI Agents SDK）
  smolagents   legacy 备份（已下线）
  custom       legacy 备份（早期自写循环）

后两个已不接入生产路径，但这个测试保留：它保证"必要时能切回去"不是空话。

不需要 API key —— 用脚本化的假模型驱动。
运行：uv run python -m unittest discover -s tests -v
"""

import asyncio
import json
import re
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "backend"))
sys.path.insert(0, str(TESTS_DIR))

from agent_core import AgentCancelled, BudgetExceeded          # noqa: E402
from legacy import custom_engine, smol_engine                  # noqa: E402
from agent_engine import agents_sdk                            # noqa: E402
from scripted_model import ScriptedModel, new_script           # noqa: E402
from agent_engine.spec import (LoopBudgetSpec, SkillSpec, SubmitSpec,  # noqa: E402
                               TaskSpec, ToolSpec)

SOURCE = "第一句说的是这件事。第二句接着说另一件。"


def sentence_count(text: str) -> int:
    endings = re.findall(r"[。！？.!?]", text)
    tail = re.sub(r"[”』」）)\s]+$", "", text)
    return len(endings) + (1 if text.strip() and not (tail and tail[-1] in "。！？.!?") else 0)


def validate(args: dict) -> dict:
    if set(args) != {"short", "detail", "sentences"}:
        raise ValueError("结果必须且只能包含 short、detail、sentences")
    if not isinstance(args["sentences"], list):
        raise ValueError("sentences 必须是数组")
    expected = sentence_count(SOURCE)
    if len(args["sentences"]) != expected:
        raise ValueError(f"sentences 应有 {expected} 项，当前为 {len(args['sentences'])} 项")
    return {"short": args["short"], "detail": args["detail"], "sentences": args["sentences"]}


def make_spec(*, on_tool=None) -> TaskSpec:
    async def read_paragraph(args):
        if on_tool:
            on_tool()
        return {"material_type": "original_text", "text": SOURCE,
                "reference": {"chapter_idx": 0, "paragraph_index": 0}}

    return TaskSpec(
        layer="L3",
        prompt="你为本地阅读器撰写段落旁注。最后必须调用 submit_result。",
        materials={"target": {"material_type": "original_text", "text": SOURCE},
                   "position": {"chapter_idx": 0, "paragraph_index": 0}},
        submit=SubmitSpec("submit_result", "提交当前段落的三块语义解释。",
                          {"short": {"type": "string"}, "detail": {"type": "string"},
                           "sentences": {"type": "array"}},
                          validate, ["short", "detail", "sentences"]),
        skills=[SkillSpec("book-reading", "读原文工具", "读原文，引文必须核验。",
                          [ToolSpec("read_book_paragraph", "读指定段落",
                                    {"chapter_idx": {"type": "integer"},
                                     "paragraph_index": {"type": "integer"}},
                                    read_paragraph, ["chapter_idx", "paragraph_index"])])],
        allowed_skills=["book-reading"],
        budget=LoopBudgetSpec(max_requests=6, max_tool_calls=10, timeout_seconds=30),
    )


GOOD = {"short": "要点", "detail": "详细解释", "sentences": ["第一句的转述", "第二句的转述"]}
BAD = {"short": "要点", "detail": "详细解释", "sentences": ["只给了一句"]}

# 中立的动作名：skill=加载技能，read=读原文，submit=交卷。
# 只有 smolagents 需要把交卷改名成 final_answer（它只认这个名字当终止信号）；
# agents_sdk 与 custom 都用真实名字 —— 这正是测试要盯住的差异点。
NAMES = {
    "agents_sdk": {"skill": "load_skill", "read": "read_book_paragraph", "submit": "submit_result"},
    "custom": {"skill": "load_skill", "read": "read_book_paragraph", "submit": "submit_result"},
    "smolagents": {"skill": "load_skill", "read": "read_book_paragraph", "submit": "final_answer"},
}

ENGINES = ("agents_sdk", "smolagents", "custom")


async def run_engine(engine, spec, calls, recorder, cancelled):
    """按基座分派。每个基座吃自己的假模型形状，TaskSpec 完全相同。"""
    if engine == "custom":
        model = ScriptedRequestModel(calls)
        return await custom_engine.run(spec, request_model=model, event=recorder,
                                       cancelled=cancelled), len(model.requests)
    if engine == "smolagents":
        model = make_smol_model(calls)
        return await smol_engine.run(spec, model=model, event=recorder,
                                     cancelled=cancelled), len(model.requests)
    record = new_script()
    model = ScriptedModel(record)
    record["script"][:] = calls
    out = await agents_sdk.run(spec, model=model, event=recorder, cancelled=cancelled)
    return out, len(record["calls"])


def translate(script, engine):
    return [(NAMES[engine][action], args) for action, args in script]


class ScriptedRequestModel:
    """custom 基座用：OpenAI Chat Completions 形状的假模型。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def __call__(self, messages, tools, cancelled):
        self.requests.append([t["function"]["name"] for t in tools])
        if not self.script:
            raise AssertionError("脚本用尽：模型被要求继续，但假模型没有后续动作")
        name, args = self.script.pop(0)
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": f"call-{len(self.requests)}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def make_smol_model(script):
    from smolagents import Model
    from smolagents.models import (ChatMessage, ChatMessageToolCall,
                                   ChatMessageToolCallFunction, MessageRole)

    class ScriptedSmolModel(Model):
        def __init__(self):
            self.script = list(script)
            self.requests = []
            self.model_id = "scripted"

        def generate(self, messages, **kwargs):
            self.requests.append(messages)
            if not self.script:
                raise AssertionError("脚本用尽：模型被要求继续，但假模型没有后续动作")
            name, args = self.script.pop(0)
            return ChatMessage(role=MessageRole.ASSISTANT, content=None,
                               tool_calls=[ChatMessageToolCall(
                                   id=f"call-{len(self.requests)}", type="function",
                                   function=ChatMessageToolCallFunction(name=name, arguments=args))])

    return ScriptedSmolModel()


class _Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, stage, message, data=None):
        self.events.append((stage, message))


class EngineParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_both_engines(self):
        script = [("skill", {"name": "book-reading"}),
                  ("read", {"chapter_idx": 0, "paragraph_index": 0}),
                  ("submit", GOOD)]
        results = {}
        for engine in ENGINES:
            rec = _Recorder()
            out, requests = await run_engine(engine, make_spec(), translate(script, engine),
                                             rec, asyncio.Event())
            results[engine] = out
            self.assertTrue(rec.events, f"{engine} 没有发出任何事件")
            self.assertEqual(requests, 3, f"{engine} 的模型请求数")
        self.assertEqual(results["agents_sdk"], results["custom"])
        self.assertEqual(results["custom"], results["smolagents"])
        self.assertEqual(results["agents_sdk"]["sentences"], GOOD["sentences"])

    async def test_validator_rejects_then_model_fixes(self):
        """validator 抛错必须变成反馈回给模型，而不是终止运行。"""
        script = [("skill", {"name": "book-reading"}),
                  ("submit", BAD),
                  ("submit", GOOD)]
        for engine in ENGINES:
            rec = _Recorder()
            out, requests = await run_engine(engine, make_spec(), translate(script, engine),
                                             rec, asyncio.Event())
            self.assertEqual(requests, 3, f"{engine} 的模型请求数")
            self.assertEqual(out["sentences"], GOOD["sentences"], engine)

    async def test_cancel_stops_both_engines(self):
        cancelled = asyncio.Event()

        def cancel_now():
            cancelled.set()

        script = [("skill", {"name": "book-reading"}),
                  ("read", {"chapter_idx": 0, "paragraph_index": 0}),
                  ("submit", GOOD)]
        for engine in ENGINES:
            rec = _Recorder()
            spec = make_spec(on_tool=cancel_now)
            calls = translate(script, engine)
            with self.assertRaises((AgentCancelled, BudgetExceeded), msg=engine):
                await run_engine(engine, spec, calls, rec, cancelled)


if __name__ == "__main__":
    unittest.main()
