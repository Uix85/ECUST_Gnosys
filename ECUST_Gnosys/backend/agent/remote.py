"""Execute Agent tasks locally or through the versioned Agent HTTP service."""

import asyncio
import json
import os
import time
import uuid
from urllib.parse import quote

import httpx

from api.agent_bridge import register_execution, take_tool_result, unregister_execution
from agent.runtime.task import AgentCancelled, AgentError, ExecutionContext


def _tool(tool):
    return {"name": tool.name, "description": tool.description,
            "parameters": tool.parameters, "required": tool.required}


def _budget_spec(spec, config, budget):
    declared = getattr(spec, "budget", None)
    timeout = (declared.timeout_seconds if declared else config.run_timeout)
    return {
        "max_requests": budget.max_requests,
        "max_tool_calls": budget.max_tool_calls,
        "timeout_seconds": max(0.1, min(timeout, budget.deadline - time.monotonic())),
        "requests_used": budget.requests,
        "tool_calls_used": budget.tool_calls,
    }


async def _local(spec, config, cancelled, emit, budget, session, mounted_tools, model=None):
    if hasattr(spec, "layer"):
        from agent.runtime.engine import AgentTask, build_model
        model = model or build_model(config)
        return await AgentTask(spec, model=model, event=emit, cancelled=cancelled,
                               budget=budget, session=session, mounted_tools=mounted_tools,
                               config=config).run()

    from agent.runtime.loop import run_task
    from agent.runtime.sdk import build_model
    model = model or build_model(config)
    return await run_task(spec, ExecutionContext(
        model=model, config=config, cancelled=cancelled, emit=emit,
        budget=budget, session=session, mounted_tools=mounted_tools,
    ))


async def execute_agent(spec, config, *, cancelled, emit, budget, session=None,
                        session_key=None, mounted_tools=None, model=None):
    """Run one Agent stage; HTTP mode keeps model execution out of Server."""
    if os.environ.get("GNOSYS_AGENT_MODE", "local") != "http":
        return await _local(spec, config, cancelled, emit, budget, session, mounted_tools, model)
    if session is not None and not session_key:
        raise AgentError("HTTP Agent 执行需要稳定的 session_key")

    from agent.protocol import ExecutionContract

    execution_id = uuid.uuid4().hex
    register_execution(execution_id, spec, mounted_tools)
    agent_url = os.environ.get("GNOSYS_AGENT_URL", "http://127.0.0.1:8124").rstrip("/")
    server_url = os.environ.get("GNOSYS_SERVER_URL", "http://127.0.0.1:8123").rstrip("/")
    token = os.environ.get("GNOSYS_AGENT_TOKEN", "")
    if not token:
        unregister_execution(execution_id)
        raise AgentError("HTTP Agent 模式需要 GNOSYS_AGENT_TOKEN")

    raw_budget = _budget_spec(spec, config, budget)
    submit = getattr(spec, "submit", None)
    skills = getattr(spec, "skills", [])
    task_name = getattr(spec, "name", None) or getattr(spec, "layer", "agent-task")
    request = ExecutionContract(
        execution_id=execution_id,
        task_name=task_name,
        prompt=spec.prompt,
        materials=spec.materials,
        submit=_tool(submit) if submit else None,
        tools=[_tool(t) for t in getattr(spec, "tools", []) + list(mounted_tools or [])],
        skills=[{"name": s.name, "description": s.description, "instructions": s.instructions,
                 "tools": [_tool(t) for t in s.tools]} for s in skills],
        allowed_skills=getattr(spec, "allowed_skills", []),
        request_limit=getattr(spec, "request_limit", None),
        max_requests=raw_budget["max_requests"], max_tool_calls=raw_budget["max_tool_calls"],
        timeout_seconds=raw_budget["timeout_seconds"], requests_used=raw_budget["requests_used"],
        tool_calls_used=raw_budget["tool_calls_used"], stream=getattr(spec, "stream", False),
        tool_limit_action=getattr(spec, "tool_limit_action", "fail"),
        callback_url=f"{server_url}/internal/v1/agent-executions/{execution_id}",
        session_key=session_key,
        config={"base_url": config.base_url, "model": config.model, "api_key": config.api_key,
                "request_timeout": config.request_timeout, "max_requests": config.max_requests,
                "max_tool_calls": config.max_tool_calls, "run_timeout": config.run_timeout,
                "max_retries": config.max_retries, "session_id": config.session_id,
                "disable_thinking": config.disable_thinking},
    )
    headers = {"Authorization": "Bearer " + token}

    async def consume():
        async with httpx.AsyncClient(timeout=None) as client:
            payload = request.model_dump(mode="json")
            # Pydantic masks SecretStr in JSON mode; reveal it only in the outbound request.
            payload["config"]["api_key"] = config.api_key
            response = await client.post(agent_url + "/agent/v1/executions", json=payload,
                                         headers=headers, timeout=15)
            response.raise_for_status()
            cursor = 0
            async with client.stream("GET", agent_url + f"/agent/v1/executions/{execution_id}/events",
                                     params={"after": cursor}, headers=headers) as stream:
                stream.raise_for_status()
                event_id = None
                event_name = "message"
                data_lines = []
                async for line in stream.aiter_lines():
                    if line.startswith("id:"):
                        event_id = line[3:].strip()
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line:
                        if event_name == "end":
                            break
                        if event_name == "update" and data_lines:
                            item = json.loads("\n".join(data_lines))
                            cursor = int(event_id or item.get("id", cursor))
                            data = item.get("data") or {}
                            if item["stage"] == "tool_end":
                                data["result"] = take_tool_result(execution_id, data.get("tool", ""))
                            await emit(item["stage"], item.get("message", ""), data)
                        event_id, event_name, data_lines = None, "message", []
            result = await client.get(agent_url + f"/agent/v1/executions/{execution_id}", headers=headers)
            result.raise_for_status()
            return result.json()

    stream_task = asyncio.create_task(consume())
    cancel_task = asyncio.create_task(cancelled.wait())
    try:
        done, _ = await asyncio.wait((stream_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done and cancelled.is_set():
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(agent_url + f"/agent/v1/executions/{execution_id}/cancel", headers=headers)
                await asyncio.wait_for(stream_task, timeout=15)
            except (httpx.HTTPError, asyncio.TimeoutError):
                stream_task.cancel()
            raise AgentCancelled("任务已取消")
        snapshot = stream_task.result()
        budget.requests = snapshot.get("requests", budget.requests)
        budget.tool_calls = snapshot.get("tool_calls", budget.tool_calls)
        if mounted_tools is not None:
            available = {tool.name: tool for skill in skills for tool in skill.tools}
            existing = {tool.name for tool in mounted_tools}
            mounted_tools.extend(available[name] for name in snapshot.get("mounted_tools", [])
                                 if name in available and name not in existing)
        if snapshot["status"] == "canceled":
            raise AgentCancelled(snapshot.get("error") or "任务已取消")
        if snapshot["status"] != "succeeded":
            raise AgentError(snapshot.get("error") or f"Agent 执行状态：{snapshot['status']}")
        result = snapshot.get("result")
        if submit:
            validator = submit.validator
            result = validator(result)
        return result
    except httpx.HTTPError as exc:
        raise AgentError(f"Agent 服务连接失败：{exc}") from exc
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        if not stream_task.done():
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
        unregister_execution(execution_id)


async def delete_remote_session(session_key: str):
    """Delete SDK history owned by the Agent service when its chat is deleted."""
    if os.environ.get("GNOSYS_AGENT_MODE", "local") != "http":
        return
    token = os.environ.get("GNOSYS_AGENT_TOKEN", "")
    base = os.environ.get("GNOSYS_AGENT_URL", "http://127.0.0.1:8124").rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.delete(base + "/agent/v1/sessions/" + quote(session_key, safe=""),
                                       headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
