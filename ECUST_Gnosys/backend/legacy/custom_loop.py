"""早期自写基座的循环（备份，未接入生产路径）。

生产路径走 agent_engine/smol.py（smolagents）。
保留它是为了能一行切回去对照，以及 agent 基座出问题时的退路。
"""

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from agent_core import AgentCancelled, AgentError, BudgetExceeded, LoopBudget


ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ModelRequest = Callable[[list[dict], list[dict], asyncio.Event], Awaitable[dict]]
EventWriter = Callable[[str, str, "dict | None"], Awaitable[None]]


async def run_tool_loop(
    *,
    prompt: str,
    materials: dict,
    validator: Callable[[dict], dict],
    allowed_skills: list[str],
    skill_registry: dict[str, dict],
    budget: LoopBudget,
    cancelled: asyncio.Event,
    request_model: ModelRequest,
    event: EventWriter,
    task_tools: list[dict],
    task_handlers: dict[str, ToolHandler],
    request_limit: int | None = None,
) -> dict:
    """Run until a validated task-specific submit tool returns a result."""
    advertised = [
        {"name": name, "description": skill_registry[name]["description"]}
        for name in allowed_skills if name in skill_registry
    ]
    messages = [
        {"role": "system", "content": prompt + "\n\n可按需加载的技能：" + json.dumps(advertised, ensure_ascii=False)},
        {"role": "user", "content": json.dumps(materials, ensure_ascii=False)},
    ]
    load_tool = {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "读取一个允许使用的技能规程，并启用该技能提供的工具。",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        },
    }
    tools = [load_tool, *task_tools]
    handlers = dict(task_handlers)
    loaded_skills: set[str] = set()
    submit_name = next((tool["function"]["name"] for tool in task_tools if tool["function"]["name"].startswith("submit_")), None)
    if not submit_name:
        raise ValueError("task_tools must include a submit_* result tool")

    async def load_skill(args: dict) -> dict:
        name = args.get("name")
        if name not in allowed_skills or name not in skill_registry:
            raise ValueError("该技能不在本任务允许列表中")
        skill = skill_registry[name]
        if name not in loaded_skills:
            messages.insert(1, {"role": "system", "content": skill["instructions"]})
            loaded_skills.add(name)
            for tool in skill["tools"]:
                if tool["function"]["name"] not in handlers:
                    tools.append(tool)
                    handlers[tool["function"]["name"]] = skill["handlers"][tool["function"]["name"]]
        await event("skill", f"已加载技能：{skill['title']}", {"skill": name})
        return {"loaded": name, "tools": [t["function"]["name"] for t in skill["tools"]]}

    while True:
        budget.check(cancelled, request_limit)
        budget.requests += 1
        await event("model_request", f"模型请求 {budget.requests}/{budget.max_requests}", {"request": budget.requests})
        response = await request_model(messages, tools, cancelled)
        if cancelled.is_set():
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= budget.deadline:
            raise BudgetExceeded("任务超过总时限")
        choices = response.get("choices") or []
        if not choices:
            raise AgentError("模型返回了空响应")
        budget.record_usage(response.get("usage"))
        await event(
            "model_response",
            f"模型返回（累计 {budget.total_tokens} tokens）",
            {"request": budget.requests, "input_tokens": budget.input_tokens,
             "output_tokens": budget.output_tokens, "total_tokens": budget.total_tokens},
        )
        assistant = choices[0].get("message") or {}
        calls = assistant.get("tool_calls") or []
        # Keep the assistant tool-call message intact so every tool_call_id has a matching reply.
        messages.append({
            "role": "assistant",
            "content": assistant.get("content"),
            "tool_calls": calls,
        })
        if not calls:
            raise AgentError("模型未调用提交结果工具")
        submitted = None
        for call in calls:
            budget.check_running(cancelled)     # 上一个工具可能跑很久，这里补一次检查
            budget.tool_calls += 1
            if budget.tool_calls > budget.max_tool_calls:
                raise BudgetExceeded("工具调用次数已达上限")
            call_id = call.get("id", "")
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                if name == "load_skill":
                    result = await load_skill(args)
                elif name == submit_name or name in handlers:
                    ref = {key: args[key] for key in ("chapter_idx", "paragraph_index", "offset", "chars", "limit", "query") if key in args}
                    await event("tool", f"执行工具：{name}", {"tool": name, "reference": ref})
                    if name == submit_name:
                        result = validator(args)
                        submitted = result
                    else:
                        result = await handlers[name](args)
                    refs = []
                    result_type = None
                    if isinstance(result, dict):
                        result_type = result.get("material_type")
                        if result.get("reference"):
                            refs.append(result["reference"])
                    elif isinstance(result, list):
                        refs = [item["reference"] for item in result if isinstance(item, dict) and item.get("reference")]
                        if refs:
                            result_type = "original_text"
                    if refs:
                        await event("material", "读取了带位置的书内材料", {"material_type": result_type, "references": refs})
                else:
                    raise ValueError(f"未授权的工具：{name}")
                tool_content = {"ok": True, "result": result}
            except (ValueError, TypeError, KeyError) as exc:
                tool_content = {"ok": False, "error": str(exc)}
                await event("tool_error", str(exc), {"tool": name})
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(tool_content, ensure_ascii=False),
            })
        if submitted is not None:
            return submitted

