"""Offline HTTP contract tests for the standalone Agent service."""

import asyncio
import os
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]

import httpx
from httpx import ASGITransport, AsyncClient
from agent import service
from agent.protocol import ExecutionContract
from agent.runtime.task import LoopBudget, TaskSpec, ToolSpec
from test_agent_runtime import ScriptedModel, call, message


class AgentServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gnosys-agent-test-")
        service.STATE_DB = Path(self.temp.name) / "agent.db"
        service.TOKEN = "offline-service-token"
        self.client = AsyncClient(transport=ASGITransport(app=service.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    def contract(self):
        request = ExecutionContract(
            execution_id=uuid.uuid4().hex,
            task_name="offline-contract",
            prompt="Return a short response.",
            materials={"fixture": "private-material-marker"},
            max_requests=2,
            max_tool_calls=1,
            timeout_seconds=5,
            callback_url="http://test/internal/v1/agent-executions/offline",
            config={
                "base_url": "https://example.invalid/v1",
                "model": "offline-model",
                "api_key": "offline-model-key",
            },
        )
        payload = request.model_dump(mode="json")
        payload["config"]["api_key"] = "offline-model-key"
        return request, payload

    async def test_requires_service_token(self):
        _, payload = self.contract()
        response = await self.client.post("/agent/v1/executions", json=payload)
        self.assertEqual(response.status_code, 401, response.text)

    async def test_execution_http_sse_and_minimal_persistence(self):
        request, payload = self.contract()

        def build_model(config):
            self.assertEqual(config.api_key, "offline-model-key")
            return ScriptedModel([message("Agent service reached")])

        auth = {"Authorization": "Bearer offline-service-token"}
        with patch.object(service, "build_model", side_effect=build_model):
            created = await self.client.post("/agent/v1/executions", json=payload, headers=auth)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["execution_id"], request.execution_id)

            snapshot = None
            for _ in range(200):
                response = await self.client.get(
                    f"/agent/v1/executions/{request.execution_id}", headers=auth
                )
                self.assertEqual(response.status_code, 200, response.text)
                snapshot = response.json()
                if snapshot["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(snapshot["status"], "succeeded", snapshot)
        self.assertEqual(snapshot["result"], "Agent service reached")
        events = await self.client.get(
            f"/agent/v1/executions/{request.execution_id}/events", headers=auth
        )
        self.assertEqual(events.status_code, 200, events.text)
        self.assertIn("execution_finished", events.text)

        with sqlite3.connect(service.STATE_DB) as db:
            row = db.execute("SELECT request_hash FROM executions WHERE id=?",
                             (request.execution_id,)).fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertNotIn("private-material-marker", json.dumps(row))

    async def test_server_adapter_runs_tool_over_http_callback(self):
        from server import app as server_app
        from agent.remote import execute_agent

        class AppRouter(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                target = service.app if request.url.host == "agent.test" else server_app
                return await ASGITransport(app=target).handle_async_request(request)

        class InProcessHTTPClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = AppRouter()
                super().__init__(*args, **kwargs)

        calls = []
        tool = ToolSpec("lookup", "Read a small test value", {"key": {"type": "string"}},
                        lambda args: calls.append(args) or {"value": "from-server"}, ["key"])
        spec = TaskSpec(name="callback-test", prompt="Call lookup, then answer.",
                        materials={"question": "test"}, tools=[tool], stream=True)
        config = type("Config", (), {
            "base_url": "https://example.invalid/v1", "model": "offline-model",
            "api_key": "offline-model-key", "request_timeout": 5, "max_requests": 4,
            "max_tool_calls": 2, "run_timeout": 5, "max_retries": 0,
            "session_id": "", "disable_thinking": False,
        })()
        emitted = []

        async def emit(stage, text, data=None):
            emitted.append((stage, data or {}))

        def build_model(config):
            self.assertEqual(config.api_key, "offline-model-key")
            return ScriptedModel(
                [call("lookup", {"key": "fixture"})],
                [message("Answer from remote Agent")],
            )

        with patch.dict(os.environ, {
            "GNOSYS_AGENT_MODE": "http",
            "GNOSYS_AGENT_URL": "http://agent.test",
            "GNOSYS_SERVER_URL": "http://server.test",
            "GNOSYS_AGENT_TOKEN": "offline-service-token",
        }), patch.object(service, "build_model", side_effect=build_model), patch("httpx.AsyncClient", InProcessHTTPClient):
            result = await execute_agent(
                spec, config, cancelled=asyncio.Event(), emit=emit,
                budget=LoopBudget(4, 2, time.monotonic() + 5),
            )

        self.assertEqual(result, "Answer from remote Agent")
        self.assertEqual(calls, [{"key": "fixture"}])
        tool_event = next(data for stage, data in emitted if stage == "tool_end")
        self.assertEqual(tool_event["result"], {"value": "from-server"})
        with sqlite3.connect(service.STATE_DB) as db:
            stored = db.execute("SELECT data_json FROM execution_events WHERE stage='tool_start'").fetchone()[0]
        self.assertNotIn('"args"', stored)
        self.assertNotIn("fixture", stored)


if __name__ == "__main__":
    unittest.main()
