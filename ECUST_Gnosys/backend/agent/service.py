"""Standalone Agent execution service. Run with `uv run agent-service.py`."""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from agents import SQLiteSession
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from agent.protocol import ExecutionContract, ExecutionEvent, ExecutionReceipt, ExecutionSnapshot
from agent.runtime.loop import run_task
from agent.runtime.sdk import build_model
from agent.runtime.task import (
    ExecutionContext,
    LoopBudget,
    SkillSpec,
    SubmitSpec,
    TaskSpec,
    ToolSpec,
)


ROOT = Path(__file__).resolve().parents[2]
STATE_DB = Path(os.environ.get("GNOSYS_AGENT_STATE_DB", ROOT / "database" / "agent-state.db"))
TOKEN = os.environ.get("GNOSYS_AGENT_TOKEN", "")
ACTIVE: dict[str, asyncio.Task] = {}
CANCEL: dict[str, asyncio.Event] = {}
app = FastAPI(title="Gnosys Agent Service", version="1.0.0")


def _connect():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS executions(
            id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL,
            result_json TEXT, error TEXT, requests INTEGER NOT NULL DEFAULT 0,
            tool_calls INTEGER NOT NULL DEFAULT 0, mounted_tools_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS execution_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
            stage TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_execution_events ON execution_events(execution_id,id);
    """)
    return db


def _auth(authorization: str | None):
    if not TOKEN:
        raise HTTPException(503, "GNOSYS_AGENT_TOKEN is not configured")
    provided = authorization.removeprefix("Bearer ") if authorization else ""
    if not hmac.compare_digest(provided, TOKEN):
        raise HTTPException(401, "Invalid service token")


def _request_hash(body: ExecutionContract) -> str:
    data = body.model_dump(mode="json")
    data["config"]["api_key"] = ""
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(row) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        execution_id=row["id"], status=row["status"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"], requests=row["requests"], tool_calls=row["tool_calls"],
        mounted_tools=json.loads(row["mounted_tools_json"]),
    )


async def _post_callback(body: ExecutionContract, suffix: str, payload: dict):
    async with httpx.AsyncClient(timeout=body.config.request_timeout) as client:
        response = await client.post(
            body.callback_url.rstrip("/") + suffix,
            json=payload,
            headers={"Authorization": "Bearer " + TOKEN},
        )
    if response.is_error:
        try:
            detail = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Server 工具回调失败 ({response.status_code}): {detail[:500]}")
    return response.json().get("result")


def _tool_specs(body: ExecutionContract) -> tuple[list[ToolSpec], list[SkillSpec]]:
    def make_tool(contract):
        async def call(args):
            return await _post_callback(
                body, "/tools/" + quote(contract.name, safe=""), {"arguments": args}
            )
        return ToolSpec(contract.name, contract.description, contract.parameters, call, contract.required)

    tools = [make_tool(tool) for tool in body.tools]
    skills = [SkillSpec(skill.name, skill.description, skill.instructions,
                         [make_tool(tool) for tool in skill.tools]) for skill in body.skills]
    return tools, skills


async def _run(body: ExecutionContract, cancelled: asyncio.Event):
    db = _connect()
    session = None
    previous = None
    budget = LoopBudget(
        body.max_requests, body.max_tool_calls, time.monotonic() + body.timeout_seconds,
        requests=body.requests_used, tool_calls=body.tool_calls_used,
    )
    mounted_tools = []

    async def emit(stage, message, data=None):
        # Tool results may include source passages; keep those transient and out of Agent storage.
        persisted = dict(data or {})
        persisted.pop("result", None)
        persisted.pop("args", None)
        with db:
            db.execute("INSERT INTO execution_events(execution_id,stage,message,data_json) VALUES(?,?,?,?)",
                       (body.execution_id, stage, message,
                        json.dumps(persisted, ensure_ascii=False) if persisted else None))
            db.execute("UPDATE executions SET requests=?,tool_calls=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (budget.requests, budget.tool_calls, body.execution_id))

    try:
        config = body.config
        model_config = type("ModelConfig", (), {
            "base_url": config.base_url, "model": config.model,
            "api_key": config.api_key.get_secret_value(),
            "request_timeout": config.request_timeout, "max_requests": config.max_requests,
            "max_tool_calls": config.max_tool_calls, "run_timeout": config.run_timeout,
            "max_retries": config.max_retries, "session_id": config.session_id,
            "disable_thinking": config.disable_thinking,
        })()
        tools, skills = _tool_specs(body)
        submit = None
        if body.submit:
            async def validate_and_submit(args):
                result = await _post_callback(body, "/submit", {"arguments": args})
                if not isinstance(result, dict):
                    raise ValueError("Server 的提交校验必须返回对象")
                return result
            submit = SubmitSpec(body.submit.name, body.submit.description,
                                body.submit.parameters, validate_and_submit, body.submit.required)

        task = TaskSpec(
            name=body.task_name, prompt=body.prompt, materials=body.materials,
            submit=submit, tools=tools, skills=skills, allowed_skills=body.allowed_skills,
            request_limit=body.request_limit, stream=body.stream,
            tool_limit_action=body.tool_limit_action,
        )
        if body.session_key:
            session = SQLiteSession(body.session_key, db_path=str(STATE_DB),
                                    sessions_table="agent_sessions", messages_table="agent_items")
            previous = await session.get_items()
        with db:
            db.execute("UPDATE executions SET status='running' WHERE id=?", (body.execution_id,))
        result = await run_task(task, ExecutionContext(
            model=build_model(model_config), config=model_config, cancelled=cancelled,
            emit=emit, budget=budget, session=session, mounted_tools=mounted_tools,
        ))
        with db:
            db.execute("UPDATE executions SET status='succeeded',result_json=?,requests=?,tool_calls=?,mounted_tools_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), budget.requests, budget.tool_calls,
                        json.dumps([tool.name for tool in mounted_tools]), body.execution_id))
        await emit("execution_finished", "Agent 执行完成", {"status": "succeeded"})
    except (Exception, asyncio.CancelledError) as exc:
        if session and previous is not None:
            try:
                await session.clear_session()
                if previous:
                    await session.add_items(previous)
            except Exception:
                pass
        status = "canceled" if cancelled.is_set() else "failed"
        with db:
            db.execute("UPDATE executions SET status=?,error=?,requests=?,tool_calls=?,mounted_tools_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (status, str(exc)[:1500], budget.requests, budget.tool_calls,
                        json.dumps([tool.name for tool in mounted_tools]), body.execution_id))
        await emit("execution_finished", "Agent 执行结束", {"status": status, "error": str(exc)[:500]})
    finally:
        if session:
            session.close()
        ACTIVE.pop(body.execution_id, None)
        CANCEL.pop(body.execution_id, None)
        db.close()


@app.on_event("startup")
async def recover_executions():
    db = _connect()
    with db:
        db.execute("UPDATE executions SET status='interrupted',error='Agent 服务重启，任务已中断' WHERE status IN ('queued','running')")
    db.close()


@app.get("/health")
async def health():
    return {"ok": True, "service": "gnosys-agent", "protocol": "1.0"}


@app.delete("/agent/v1/sessions/{session_key:path}")
async def delete_session(session_key: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    session = SQLiteSession(session_key, db_path=str(STATE_DB),
                            sessions_table="agent_sessions", messages_table="agent_items")
    try:
        await session.clear_session()
    finally:
        session.close()
    return {"ok": True}


@app.post("/agent/v1/executions", response_model=ExecutionReceipt)
async def create_execution(body: ExecutionContract, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if not body.config.api_key.get_secret_value():
        raise HTTPException(422, "缺少模型 API 密钥")
    request_hash = _request_hash(body)
    db = _connect()
    try:
        existing = db.execute("SELECT request_hash,status FROM executions WHERE id=?", (body.execution_id,)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "execution_id 已用于不同请求")
            return ExecutionReceipt(execution_id=body.execution_id, status="queued")
        with db:
            db.execute("INSERT INTO executions(id,request_hash,status) VALUES(?,?,'queued')",
                       (body.execution_id, request_hash))
    finally:
        db.close()
    signal = asyncio.Event()
    CANCEL[body.execution_id] = signal
    ACTIVE[body.execution_id] = asyncio.create_task(_run(body, signal), name=f"agent-{body.execution_id}")
    return ExecutionReceipt(execution_id=body.execution_id, status="queued")


@app.get("/agent/v1/executions/{execution_id}", response_model=ExecutionSnapshot)
async def get_execution(execution_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    db = _connect()
    try:
        row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Execution not found")
        return _snapshot(row)
    finally:
        db.close()


@app.post("/agent/v1/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    signal = CANCEL.get(execution_id)
    if signal:
        signal.set()
    return {"ok": True, "requested": signal is not None}


@app.get("/agent/v1/executions/{execution_id}/events")
async def execution_events(execution_id: str, request: Request, after: int = 0,
                           authorization: str | None = Header(default=None)):
    _auth(authorization)
    db = _connect()
    if not db.execute("SELECT 1 FROM executions WHERE id=?", (execution_id,)).fetchone():
        db.close()
        raise HTTPException(404, "Execution not found")
    db.close()
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = after

    async def stream():
        nonlocal cursor
        while True:
            db = _connect()
            rows = db.execute("SELECT * FROM execution_events WHERE execution_id=? AND id>? ORDER BY id",
                              (execution_id, cursor)).fetchall()
            state = db.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()
            db.close()
            for row in rows:
                cursor = row["id"]
                item = ExecutionEvent(id=cursor, stage=row["stage"], message=row["message"],
                                      data=json.loads(row["data_json"]) if row["data_json"] else None)
                yield f"id: {cursor}\nevent: update\ndata: {item.model_dump_json()}\n\n"
            if not state or state["status"] not in ("queued", "running"):
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
