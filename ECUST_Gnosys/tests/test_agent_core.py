import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_core import AgentCancelled, BudgetExceeded, LoopBudget
from legacy.custom_loop import run_tool_loop


def tool(name):
    return {"type": "function", "function": {"name": name, "description": name,
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}


def call(call_id, name, arguments="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class AgentCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_load_tool_round_trip_and_result_validation(self):
        requests = []
        events = []
        loaded = False

        async def read_tool(args):
            return {"text": "book source"}

        skill = {"description": "book skill", "title": "book-reading", "instructions": "Read source carefully.",
                 "tools": [tool("read_book")], "handlers": {"read_book": read_tool}}

        async def model(messages, tools, canceled):
            nonlocal loaded
            requests.append((json.loads(json.dumps(messages)), [x["function"]["name"] for x in tools]))
            if len(requests) == 1:
                return {"choices": [{"message": {"content": None, "tool_calls": [call("c1", "load_skill", '{"name":"book-reading"}')]}}]}
            if not loaded:
                loaded = True
                self.assertIn("read_book", requests[-1][1])
                self.assertTrue(any(m.get("role") == "system" and m.get("content") == "Read source carefully." for m in messages))
                return {"choices": [{"message": {"content": None, "tool_calls": [call("c2", "read_book")]}}]}
            if len(requests) == 3:
                return {"choices": [{"message": {"content": None, "tool_calls": [call("c3", "submit_other", '{"value":"first"}')]}}]}
            return {"choices": [{"message": {"content": None, "tool_calls": [call("c4", "submit_other", '{"value":"valid"}')]}}]}

        async def event(stage, message, data=None):
            events.append(stage)

        def validate(args):
            if set(args) != {"value"} or args["value"] != "valid":
                raise ValueError("invalid result")
            return args

        result = await run_tool_loop(
            prompt="generic reusable task", materials={"input": "x"}, validator=validate,
            allowed_skills=["book-reading"], skill_registry={"book-reading": skill},
            budget=LoopBudget(8, 10, 10**12), cancelled=asyncio.Event(), request_model=model,
            event=event, task_tools=[tool("submit_other")], task_handlers={},
        )
        self.assertEqual(result, {"value": "valid"})
        self.assertEqual(len(requests), 4)
        final_messages = requests[-1][0]
        assistant_calls = [m for m in final_messages if m.get("role") == "assistant" and m.get("tool_calls")]
        tool_replies = [m for m in final_messages if m.get("role") == "tool"]
        self.assertEqual([c["id"] for m in assistant_calls for c in m["tool_calls"]],
                         [m["tool_call_id"] for m in tool_replies])
        self.assertIn("tool_error", events)

    async def test_budget_exhaustion_and_cancellation(self):
        async def event(*args):
            return None

        async def repeated_tool(messages, tools, canceled):
            return {"choices": [{"message": {"content": None, "tool_calls": [call("loop", "read")]}}]}

        with self.assertRaises(BudgetExceeded):
            await run_tool_loop(
                prompt="p", materials={}, validator=lambda x: x, allowed_skills=[], skill_registry={},
                budget=LoopBudget(1, 5, 10**12), cancelled=asyncio.Event(), request_model=repeated_tool,
                event=event, task_tools=[tool("submit_result")],
                task_handlers={"read": lambda args: asyncio.sleep(0, result={"ok": True})},
            )

        canceled = asyncio.Event()

        async def cancel_during_request(messages, tools, signal):
            signal.set()
            return {"choices": [{"message": {"content": None, "tool_calls": [call("c", "submit_result")]}}]}

        with self.assertRaises(AgentCancelled):
            await run_tool_loop(
                prompt="p", materials={}, validator=lambda x: x, allowed_skills=[], skill_registry={},
                budget=LoopBudget(2, 5, 10**12), cancelled=canceled, request_model=cancel_during_request,
                event=event, task_tools=[tool("submit_result")], task_handlers={},
            )

        async def request_failure(messages, tools, signal):
            raise RuntimeError("provider failure")

        with self.assertRaisesRegex(RuntimeError, "provider failure"):
            await run_tool_loop(
                prompt="p", materials={}, validator=lambda x: x, allowed_skills=[], skill_registry={},
                budget=LoopBudget(2, 5, 10**12), cancelled=asyncio.Event(), request_model=request_failure,
                event=event, task_tools=[tool("submit_result")], task_handlers={},
            )


if __name__ == "__main__":
    unittest.main()
