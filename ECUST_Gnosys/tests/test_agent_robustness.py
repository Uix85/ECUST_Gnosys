"""基座的健壮性测试：重试退避 / 用量统计 / 工具期间的时限检查。

不需要 API key。运行：uv run python -m unittest discover -s tests -v
"""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_config import AgentConfig                                   # noqa: E402
from agent_core import (AgentCancelled, AgentError, BudgetExceeded,    # noqa: E402
                       LoopBudget)
from legacy.custom_loop import run_tool_loop                           # noqa: E402
from legacy.provider import ChatCompletionsClient                     # noqa: E402


def make_config(**overrides) -> AgentConfig:
    base = dict(base_url="https://example.test/v1", model="test-model", api_key="k",
                request_timeout=5, max_requests=6, max_tool_calls=10,
                run_timeout=30, max_parallel=1, max_retries=2, retry_base_delay=0)
    base.update(overrides)
    return AgentConfig(**base)


def ok_response(prompt_tokens=10, completion_tokens=5):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    })


class ProviderRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_then_succeeds(self):
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(429, json={"error": "rate limited"}) if len(seen) < 3 else ok_response()

        client = ChatCompletionsClient(make_config(), transport=httpx.MockTransport(handler))
        data = await client.complete([{"role": "user", "content": "hi"}], [], asyncio.Event())
        self.assertEqual(len(seen), 3, "429 应被重试")
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(client.usage["requests"], 1, "只有成功的请求计入用量")
        self.assertEqual(client.usage["total_tokens"], 15)

    async def test_all_attempts_fail(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={"error": "upstream"})

        client = ChatCompletionsClient(make_config(max_retries=1), transport=httpx.MockTransport(handler))
        with self.assertRaises(AgentError) as ctx:
            await client.complete([{"role": "user", "content": "hi"}], [], asyncio.Event())
        self.assertEqual(len(calls), 2, "max_retries=1 表示共请求 2 次")
        self.assertIn("503", str(ctx.exception))

    async def test_client_error_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, json={"error": "bad key"})

        client = ChatCompletionsClient(make_config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(AgentError) as ctx:
            await client.complete([{"role": "user", "content": "hi"}], [], asyncio.Event())
        self.assertEqual(len(calls), 1, "401 是请求本身的问题，不该重试")
        self.assertIn("bad key", str(ctx.exception))

    async def test_cancel_during_backoff(self):
        def handler(request):
            return httpx.Response(429, json={"error": "rate limited"})

        client = ChatCompletionsClient(
            make_config(max_retries=5, retry_base_delay=5.0),
            transport=httpx.MockTransport(handler))
        cancelled = asyncio.Event()
        task = asyncio.create_task(client.complete([{"role": "user", "content": "hi"}], [], cancelled))
        await asyncio.sleep(0.05)
        cancelled.set()
        with self.assertRaises(AgentCancelled):
            await asyncio.wait_for(task, timeout=3)


class LoopUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_accumulated_and_reported(self):
        events = []

        async def event(stage, message, data=None):
            events.append((stage, data))

        def request_model(messages, tools, cancelled):
            async def call():
                return {"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "submit_result",
                                  "arguments": json.dumps({"short": "s", "detail": "d"})}}]}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
            return call()

        async def handler(args):
            return {"ok": True}

        budget = LoopBudget(max_requests=3, max_tool_calls=5, deadline=time.monotonic() + 30)
        result = await run_tool_loop(
            prompt="p", materials={"a": 1}, validator=lambda value: value,
            allowed_skills=[], skill_registry={}, budget=budget, cancelled=asyncio.Event(),
            request_model=request_model, event=event,
            task_tools=[{"type": "function", "function": {
                "name": "submit_result", "description": "submit", "parameters": {}}}],
            task_handlers={},
        )
        self.assertEqual(result, {"short": "s", "detail": "d"})
        self.assertEqual(budget.input_tokens, 100)
        self.assertEqual(budget.output_tokens, 20)
        self.assertEqual(budget.total_tokens, 120)
        usage_events = [d for s, d in events if s == "model_response"]
        self.assertEqual(usage_events[0]["total_tokens"], 120, "用量要进事件，前端/日志才看得到")

    async def test_deadline_checked_during_tool_execution(self):
        """工具自己跑很久（如全书检索）时，不能被悄悄超时。"""
        budget = LoopBudget(max_requests=3, max_tool_calls=5, deadline=time.monotonic() + 30)
        calls = []

        async def burn_deadline(args):
            calls.append(args["n"])
            budget.deadline = time.monotonic() - 1     # 模拟工具耗掉了整个时限
            return {"ok": True}

        async def unused(args):
            calls.append("unused")
            return {"ok": True}

        def request_model(messages, tools, cancelled):
            async def call():
                return {"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "slow_tool", "arguments": json.dumps({"n": 1})}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "submit_result", "arguments": "{}"}}]}}]}
            return call()

        async def event(stage, message, data=None):
            return None

        with self.assertRaises(BudgetExceeded):
            await run_tool_loop(
                prompt="p", materials={}, validator=lambda value: value,
                allowed_skills=[], skill_registry={}, budget=budget, cancelled=asyncio.Event(),
                request_model=request_model, event=event,
                task_tools=[{"type": "function", "function": {
                    "name": "submit_result", "description": "s", "parameters": {}}}],
                task_handlers={"slow_tool": burn_deadline, "submit_result": unused},
            )
        self.assertEqual(calls, [1], "第一个工具跑完后应立刻发现超时，不再执行第二个")


if __name__ == "__main__":
    unittest.main()
