"""Offline contract tests for the shared core, using the real Agents SDK loop."""

import asyncio
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agents import SQLiteSession
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)

from agent.runtime.loop import run_task
from agent.runtime.task import (
    AgentCancelled,
    AgentError,
    BudgetExceeded,
    ExecutionContext,
    LoopBudget,
    LoopBudgetSpec,
    SkillSpec,
    SubmitSpec,
    TaskSpec,
    ToolSpec,
)


def call(name, args):
    return ResponseFunctionToolCall(
        id="fc_" + uuid.uuid4().hex,
        call_id=uuid.uuid4().hex,
        name=name,
        arguments=args if isinstance(args, str) else json.dumps(args),
        type="function_call",
        status="completed",
    )


def message(text):
    return ResponseOutputMessage(
        id="msg_" + uuid.uuid4().hex,
        role="assistant",
        status="completed",
        type="message",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[], logprobs=[])],
    )


class ScriptedModel(Model):
    """Return a prescribed sequence while retaining actual SDK tool/session behavior."""

    def __init__(self, *turns, block=False):
        self.turns = list(turns)
        self.inputs = []
        self.tool_names = []
        self.block = block
        self.entered = asyncio.Event()
        self.stopped = asyncio.Event()

    async def _output(self, input, tools):
        self.inputs.append(input)
        self.tool_names.append({tool.name for tool in tools})
        self.entered.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()
        if not self.turns:
            raise AssertionError("Unexpected extra model request")
        return self.turns.pop(0)

    async def get_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
        output = await self._output(input, tools)
        return ModelResponse(output, Usage(requests=1, input_tokens=7, output_tokens=3, total_tokens=10), None)

    async def stream_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
        output = await self._output(input, tools)
        for item in output:
            if isinstance(item, ResponseOutputMessage):
                yield ResponseTextDeltaEvent(
                    type="response.output_text.delta", delta=item.content[0].text,
                    content_index=0, item_id=item.id, output_index=0, logprobs=[], sequence_number=0,
                )
        usage = ResponseUsage(
            input_tokens=7, output_tokens=3, total_tokens=10,
            input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens_details={"reasoning_tokens": 0},
        )
        response = Response.model_construct(id="resp_" + uuid.uuid4().hex, output=output, usage=usage)
        yield ResponseCompletedEvent(type="response.completed", response=response, sequence_number=1)


class TestAgentRuntime(unittest.IsolatedAsyncioTestCase):
    def context(self, model, *, tools=20, timeout=5, session=None, mounted_tools=None):
        self.events = []

        async def emit(kind, text, data=None):
            self.events.append((kind, text, data or {}))

        return ExecutionContext(
            model=model,
            config=SimpleNamespace(disable_thinking=False),
            cancelled=asyncio.Event(),
            emit=emit,
            budget=LoopBudget(12, tools, time.monotonic() + timeout),
            session=session,
            mounted_tools=mounted_tools,
        )

    def spec(self, **kwargs):
        return TaskSpec(name="offline", prompt="Follow the task.", materials={"question": "hello"}, **kwargs)

    async def test_free_text_and_model_usage(self):
        model = ScriptedModel([message("hello")])
        context = self.context(model)
        self.assertEqual(await run_task(self.spec(), context), "hello")
        self.assertEqual((context.budget.requests, context.budget.input_tokens, context.budget.output_tokens), (1, 7, 3))
        self.assertTrue({"model_start", "model_end"}.issubset({kind for kind, _, _ in self.events}))

    async def test_validated_submit_returns_correction_to_model(self):
        def validate(args):
            if args.get("answer") != "correct":
                raise ValueError("answer must be correct")
            return {"answer": args["answer"]}

        submit = SubmitSpec("submit_answer", "Submit answer", {"answer": {"type": "string"}}, validate, ["answer"])
        model = ScriptedModel([call("submit_answer", {"answer": "wrong"})], [call("submit_answer", {"answer": "correct"})])
        context = self.context(model)
        self.assertEqual(await run_task(self.spec(submit=submit), context), {"answer": "correct"})
        self.assertIn("answer must be correct", str(model.inputs[1]))
        self.assertEqual(context.budget.requests, 2)
        self.assertIn("tool_error", {kind for kind, _, _ in self.events})
        self.assertIn("tool_ok", {kind for kind, _, _ in self.events})

    async def test_structured_task_rejects_final_prose_without_submission(self):
        submit = SubmitSpec("submit_answer", "Submit answer", {}, lambda args: args)
        for stream in (False, True):
            with self.subTest(stream=stream):
                model = ScriptedModel([message("unvalidated prose")])
                with self.assertRaisesRegex(AgentError, "提交"):
                    await run_task(self.spec(submit=submit, stream=stream), self.context(model))

    async def test_task_budget_defaults_and_external_shared_budget_precedence(self):
        model = ScriptedModel([message("task budget")], [message("shared budget")])
        context = self.context(model)
        context.budget = None
        context.config = SimpleNamespace(max_requests=8, max_tool_calls=9, run_timeout=60)
        start = time.monotonic()
        spec = self.spec(budget=LoopBudgetSpec(max_requests=2, max_tool_calls=3, timeout_seconds=5))
        self.assertEqual(await run_task(spec, context), "task budget")
        self.assertEqual((context.budget.max_requests, context.budget.max_tool_calls), (2, 3))
        self.assertGreaterEqual(context.budget.deadline, start + 5)
        self.assertLess(context.budget.deadline, time.monotonic() + 5)

        shared = LoopBudget(4, 6, time.monotonic() + 10, requests=1)
        context.budget = shared
        expired_task_budget = self.spec(budget=LoopBudgetSpec(max_requests=0, max_tool_calls=0, timeout_seconds=0))
        self.assertEqual(await run_task(expired_task_budget, context), "shared budget")
        self.assertIs(context.budget, shared)
        self.assertEqual((shared.max_requests, shared.max_tool_calls, shared.requests), (4, 6, 2))

    async def test_malformed_arguments_recover_without_calling_handler(self):
        handled = []

        async def echo(args):
            handled.append(args)
            return {"value": args["value"]}

        tool = ToolSpec("echo", "Read value", {"value": {"type": "string"}}, echo, ["value"])
        model = ScriptedModel([call("echo", '{"value":')], [call("echo", {"value": "ok"})], [message("done")])
        context = self.context(model)
        self.assertEqual(await run_task(self.spec(tools=[tool]), context), "done")
        self.assertEqual(handled, [{"value": "ok"}])
        self.assertIn("tool_error", {kind for kind, _, _ in self.events})
        successful = [data for kind, _, data in self.events if kind == "tool_end" and data.get("tool") == "echo"]
        self.assertTrue(successful)
        self.assertIn("result", successful[-1])

    async def test_finish_policy_blocks_excess_handler_and_allows_answer(self):
        handled = []

        async def read(args):
            handled.append(args)
            return "read result"

        tool = ToolSpec("read", "Read material", {}, read)
        model = ScriptedModel([call("read", {}), call("read", {})], [message("from available material")])
        context = self.context(model, tools=1)
        result = await run_task(self.spec(tools=[tool], tool_limit_action="finish"), context)
        self.assertEqual(result, "from available material")
        self.assertEqual(handled, [{}])
        self.assertEqual(context.budget.tool_calls, 1)

    async def test_fail_policy_blocks_excess_handler(self):
        handled = []

        async def read(args):
            handled.append(args)
            return "read result"

        tool = ToolSpec("read", "Read material", {}, read)
        model = ScriptedModel([call("read", {})], [call("read", {})])
        context = self.context(model, tools=1)
        with self.assertRaises(BudgetExceeded):
            await run_task(self.spec(tools=[tool]), context)
        self.assertEqual(handled, [{}])

    async def test_streaming_uses_same_tools_and_usage_accounting(self):
        async def read(args):
            return {"text": "material"}

        tool = ToolSpec("read", "Read material", {}, read)
        model = ScriptedModel([call("read", {})], [message("streamed answer")])
        context = self.context(model)
        self.assertEqual(await run_task(self.spec(tools=[tool], stream=True), context), "streamed answer")
        self.assertEqual((context.budget.requests, context.budget.tool_calls, context.budget.total_tokens), (2, 1, 20))
        self.assertEqual(sum(kind == "model_end" for kind, _, _ in self.events), 2)
        self.assertIn("streamed answer", str([event for event in self.events if event[0] == "text_delta"]))

    async def test_pre_cancel_does_not_start_model(self):
        model = ScriptedModel([message("should not run")])
        context = self.context(model)
        context.cancelled.set()
        with self.assertRaises(AgentCancelled):
            await run_task(self.spec(), context)
        self.assertEqual(model.inputs, [])

    async def test_phase_request_limit_stops_shared_budget_run(self):
        async def read(args):
            return "material"

        model = ScriptedModel([call("read", {})], [message("beyond phase limit")])
        context = self.context(model)
        spec = self.spec(tools=[ToolSpec("read", "Read", {}, read)], request_limit=1)
        with self.assertRaises(BudgetExceeded):
            await run_task(spec, context)
        self.assertEqual(len(model.inputs), 1)
        self.assertEqual(context.budget.requests, 1)

    async def test_timeout_and_stream_cancel_clean_up_model_jobs(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                model = ScriptedModel(block=True)
                context = self.context(model, timeout=0.05 if not stream else 5)
                before = asyncio.all_tasks()
                job = asyncio.create_task(run_task(self.spec(stream=stream), context))
                await asyncio.wait_for(model.entered.wait(), 1)
                if stream:
                    context.cancelled.set()
                with self.assertRaises(AgentCancelled if stream else BudgetExceeded):
                    await job
                await asyncio.wait_for(model.stopped.wait(), 1)
                await asyncio.sleep(0)
                leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
                self.assertEqual(leaked, [])

    async def test_skill_tools_mount_and_session_history_continue(self):
        async def read(args):
            return "skill material"

        tool = ToolSpec("read_skill", "Read material", {}, read)
        skill = SkillSpec("reading", "Read books", "Read before answering.", [tool])
        model = ScriptedModel([call("load_skill", {"name": "reading"})], [call("read_skill", {})], [message("first")], [message("second")])
        session = SQLiteSession("runtime-contract")
        mounted = []
        try:
            context = self.context(model, session=session, mounted_tools=mounted)
            spec = self.spec(skills=[skill], allowed_skills=["reading"])
            self.assertEqual(await run_task(spec, context), "first")
            self.assertNotIn("read_skill", model.tool_names[0])
            self.assertIn("read_skill", model.tool_names[1])
            self.assertEqual([tool.name for tool in mounted], ["read_skill"])
            self.assertEqual(await run_task(self.spec(), context), "second")
            self.assertIn("first", str(model.inputs[-1]))
            self.assertIn("read_skill", model.tool_names[-1])
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
