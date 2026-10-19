"""Authenticated callbacks that keep Agent tools and application data on Server."""

import hmac
import inspect
import os
from collections import defaultdict, deque

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel


router = APIRouter(prefix="/internal/v1/agent-executions", tags=["agent-internal"])
_executions = {}


class ToolCall(BaseModel):
    arguments: dict


def _authorized(authorization: str | None):
    token = os.environ.get("GNOSYS_AGENT_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not token or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "Invalid Agent service token")


def register_execution(execution_id: str, spec, mounted_tools=None):
    tools = {}
    for tool in list(getattr(spec, "tools", [])) + list(mounted_tools or []):
        tools[tool.name] = tool.handler
    for skill in getattr(spec, "skills", []):
        for tool in skill.tools:
            tools[tool.name] = tool.handler
    _executions[execution_id] = {"tools": tools, "submit": spec.submit.validator if spec.submit else None,
                                 "results": defaultdict(deque)}


def unregister_execution(execution_id: str):
    _executions.pop(execution_id, None)


def take_tool_result(execution_id: str, name: str):
    execution = _executions.get(execution_id)
    if not execution:
        return None
    results = execution["results"][name]
    return results.popleft() if results else None


@router.post("/{execution_id}/tools/{tool_name}", include_in_schema=False)
async def invoke_tool(execution_id: str, tool_name: str, call: ToolCall,
                      authorization: str | None = Header(default=None)):
    _authorized(authorization)
    execution = _executions.get(execution_id)
    handler = execution["tools"].get(tool_name) if execution else None
    if not handler:
        raise HTTPException(404, "Unknown or expired Agent tool")
    result = handler(call.arguments)
    if inspect.isawaitable(result):
        result = await result
    execution["results"][tool_name].append(result)
    return {"result": result}


@router.post("/{execution_id}/submit", include_in_schema=False)
async def validate_submission(execution_id: str, call: ToolCall,
                              authorization: str | None = Header(default=None)):
    _authorized(authorization)
    execution = _executions.get(execution_id)
    validator = execution["submit"] if execution else None
    if not validator:
        raise HTTPException(404, "Unknown or expired Agent submission")
    try:
        result = validator(call.arguments)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        raise HTTPException(422, str(exc)) from None
    return {"result": result}
