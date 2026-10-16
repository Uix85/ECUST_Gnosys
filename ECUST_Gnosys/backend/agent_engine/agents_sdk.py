"""OpenAI Agents SDK 基座（生产路径）。

与 legacy 里的两个旧基座（自写循环 / smolagents）吃同一份 TaskSpec，差别只在谁跑那个循环。

这个基座比 smolagents 省掉的地方（都是读 SDK 源码核实过的机制）：
  * async 原生：Runner.run 是协程，工具直接 async，不需要跨线程桥接
  * 不需要"占 final_answer 名字"：tool_use_behavior 可声明「哪个工具的返回值即最终输出」
    （run_internal/turn_resolution.py 的 check_for_final_output_from_tools 支持
     "stop_on_first_tool" / {"stop_at_tool_names": [...]} / 自定义 callable）
  * 不需要自己接超时重试：AsyncOpenAI 上直接配 timeout / max_retries
  * 取消走 asyncio：整段 run 被 cancel 掉，工具执行中途也能停（旧基座只能在边界上查）
  * 会话记忆有内建 Session：复合循环（生成↔修订共享历史）不用自己搭
  * 请求级设置（tool_choice / extra_body）是 ModelSettings 的正式字段
  * max_turns 超了抛 MaxTurnsExceeded（不是静默的 RunResult.state）

仍然要自己搭的（SDK 同样没有）：
  * 三重预算（请求数 / 工具次数 / 总时限）—— 只有 max_turns
  * 事件观测（SDK 的 tracing 是往 OpenAI 上传的，我们用它自己的 RunHooks 转发到事件表）
  * skill 机制（没有内建）
"""

import asyncio
import contextlib
import json
import time
from typing import Any, Callable

from agents import (Agent, FunctionTool, ModelSettings, RunHooks, Runner,
                    ToolsToFinalOutputResult, set_tracing_disabled)
from agents.exceptions import MaxTurnsExceeded
from agents.memory.session import SessionABC

from agent_core import AgentCancelled, AgentError, BudgetExceeded, LoopBudget

from .prompts import build_instructions
from .spec import EventWriter, TaskSpec, ToolSpec

# SDK 默认把 trace 上传到 OpenAI（我们没有那个 key，会一直报警）。测试路径不会走到
# build_model，所以在这里一并关掉。
set_tracing_disabled(True)


def build_model(config):
    """按 .env 配置造 Agents SDK 的模型对象。

    默认的 Responses API 不适用于第三方中转端点，所以显式用 ChatCompletions 通道；
    超时/重试/自定义头都在 AsyncOpenAI 客户端上配（这也是旧基座 client_kwargs 的等价物）。
    """
    from agents import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI

    set_tracing_disabled(True)      # 默认会把 trace 传到 OpenAI，我们没有那个 key
    client_kwargs = {"timeout": config.request_timeout, "max_retries": config.max_retries}
    if config.session_id:           # opencode.ai 的 Go 路由要求带这个头，否则 MissingSessionID
        client_kwargs["default_headers"] = {"x-opencode-session": config.session_id}
    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, **client_kwargs)
    return OpenAIChatCompletionsModel(model=config.model, openai_client=client)


def model_settings(config) -> ModelSettings:
    """请求级设置。tool_choice=required 防止模型回散文导致空转，但与 thinking 模式冲突。"""
    kwargs: dict[str, Any] = {}
    if config.disable_thinking:
        kwargs["extra_body"] = {"enable_thinking": False}
    kwargs["tool_choice"] = "required" if config.disable_thinking else "auto"
    return ModelSettings(**kwargs)


class MemorySession(SessionABC):
    """进程内会话：复合循环里让生成与修订共享同一段历史，不落盘。"""

    def __init__(self) -> None:
        self._items: list[Any] = []

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return self._items[-limit:] if limit else list(self._items)

    async def add_items(self, items: list[Any]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Any | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


def _params_schema(tspec: ToolSpec) -> dict:
    return {"type": "object", "properties": tspec.parameters,
            "required": tspec.required, "additionalProperties": False}


def _as_text(result: Any) -> Any:
    """工具结果给模型看的形态：字符串直通，其余按 JSON 给（比 Python repr 好读）。"""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _material_refs(result: Any) -> dict | None:
    """从工具结果里挑出"读了哪些带位置的材料"，供前端与统计使用。"""
    refs, material_type = [], None
    if isinstance(result, dict):
        material_type = result.get("material_type")
        if result.get("reference"):
            refs.append(result["reference"])
    elif isinstance(result, list):
        refs = [item["reference"] for item in result
                if isinstance(item, dict) and item.get("reference")]
        if refs:
            material_type = "original_text"
    return {"material_type": material_type, "references": refs} if refs else None


async def _with_watchdog(coro, *, budget: LoopBudget, cancelled: Any):
    """跑一段 run，同时盯着取消/时限/预算；越界就把整段 run 取消掉。

    撤销的是整个 asyncio 任务，所以即使模型正在等一个扫全书的工具，也会当场停下。
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if done:
                return task.result()
            if cancelled.is_set():
                raise AgentCancelled("任务已取消")
            if time.monotonic() >= budget.deadline:
                raise BudgetExceeded("任务超过总时限")
            if budget.requests > budget.max_requests:
                raise BudgetExceeded("模型请求次数已达上限")
            if budget.tool_calls > budget.max_tool_calls:
                raise BudgetExceeded("工具调用次数已达上限")
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        raise


async def run(
    spec: TaskSpec,
    *,
    model: Any,
    event: EventWriter,
    cancelled: Any,
    budget: LoopBudget | None = None,
    session: dict[str, Any] | None = None,
    config: Any = None,
) -> dict[str, Any]:
    """跑一个阶段。

    传同一个 session 就复用同一段历史（复合循环：生成与修订共享记忆，材料只进一次）；
    复核不传 session，保持独立上下文。
    """
    if model is None:
        raise ValueError("Agents SDK 基座需要传入 model")
    if budget is None:
        budget = LoopBudget(spec.budget.max_requests, spec.budget.max_tool_calls,
                            time.monotonic() + spec.budget.timeout_seconds)
    cfg = spec.budget
    state: dict[str, Any] = {"submitted": None}
    agent_ref: dict[str, Any] = {}

    def emit(stage: str, message: str, data: dict | None = None):
        """事件写入是 async 的，而 hooks / 工具都是 async，所以直接 await，不像旧基座
        要跨线程投递（那样会有丢事件与乱序的风险）。"""
        return event(stage, message, data)

    # ---------- 工具 ----------
    def make_tool(tspec: ToolSpec, call: Callable[[dict], Any]) -> FunctionTool:
        async def invoke(_ctx, args_json: str):
            try:
                args = json.loads(args_json or "{}")
            except json.JSONDecodeError as exc:
                return json.dumps({"ok": False, "error": f"参数不是合法 JSON：{exc}"}, ensure_ascii=False)
            return _as_text(await call(args))
        return FunctionTool(name=tspec.name, description=tspec.description,
                            params_json_schema=_params_schema(tspec),
                            on_invoke_tool=invoke, strict_json_schema=False)

    async def call_handler(tool_spec: ToolSpec, args: dict):
        budget.check_running(cancelled)
        return await tool_spec.handler(args)

    async def submit_handler(args: dict):
        """交卷。validator 不通过时**返回**错误而不是抛异常 —— 抛异常会被 SDK 统一
        包装成通用错误文本，返回结构化错误能让模型看到具体是哪一条不合格。"""
        try:
            validated = spec.submit.validator(args)
        except ValueError as exc:
            await emit("tool_error", f"提交被退回：{str(exc)[:200]}", {"error": str(exc)[:200]})
            return {"ok": False, "error": str(exc)}
        state["submitted"] = validated
        return {"ok": True}

    async def load_skill_handler(args: dict):
        name = args.get("name")
        if name not in spec.allowed_skills:
            raise ValueError("该技能不在本任务允许列表中")
        skill = spec.skill(name)
        agent = agent_ref["agent"]
        existing = {tool.name for tool in agent.tools}
        mounted = [make_tool(t, lambda a, s=t: call_handler(s, a)) for t in skill.tools
                   if t.name not in existing]
        agent.tools.extend(mounted)      # SDK 每轮重新读取 agent.tools，下一轮即生效
        return {"instructions": skill.instructions, "tools": [t.name for t in mounted]}

    tools = [make_tool(t, lambda a, s=t: call_handler(s, a)) for t in spec.tools]
    tools.append(make_tool(spec.submit.as_tool(), submit_handler))
    tools.append(make_tool(
        ToolSpec("load_skill", "读取一个允许使用的技能规程，并启用该技能提供的工具。",
                 {"name": {"type": "string", "description": "技能名"}},
                 load_skill_handler, ["name"]), load_skill_handler))

    def stop_when_submitted(_context, _tool_results) -> ToolsToFinalOutputResult:
        """SDK 的终止判定：只有**通过校验的提交**才算最终输出。

        SDK 自带的 "stop_at_tool_names" 只看工具名，校验失败也照停 —— 这里按状态判定，
        所以"被退回"的提交不会让 run 结束。
        """
        if state["submitted"] is not None:
            return ToolsToFinalOutputResult(is_final_output=True, final_output=state["submitted"])
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

    # ---------- 观测 ----------
    class Hooks(RunHooks):
        def check(self) -> None:
            """每步开头检查一次 —— 与旧基座的粒度对齐（旧基座在循环边界上查）。
            长时工具跑起来时的中止由 _with_watchdog 负责。"""
            if cancelled.is_set():
                raise AgentCancelled("任务已取消")
            if time.monotonic() >= budget.deadline:
                raise BudgetExceeded("任务超过总时限")
            if budget.requests > budget.max_requests:
                raise BudgetExceeded("模型请求次数已达上限")
            if budget.tool_calls > budget.max_tool_calls:
                raise BudgetExceeded("工具调用次数已达上限")

        async def on_llm_start(self, _ctx, _agent, _system_prompt, _input_items) -> None:
            self.check()
            budget.requests += 1
            await emit("step", f"第 {budget.requests} 步", {"step": budget.requests, "kind": "llm"})

        async def on_llm_end(self, _ctx, _agent, response) -> None:
            usage = getattr(response, "usage", None)
            budget.record_usage({"input_tokens": getattr(usage, "input_tokens", 0) or 0,
                                 "output_tokens": getattr(usage, "output_tokens", 0) or 0})
            await emit("model_response", f"模型返回（累计 {budget.total_tokens} tokens）",
                       {"request": budget.requests, "input_tokens": budget.input_tokens,
                        "output_tokens": budget.output_tokens, "total_tokens": budget.total_tokens})

        async def on_tool_start(self, _ctx, _agent, tool) -> None:
            self.check()
            budget.tool_calls += 1
            await emit("tool", f"执行工具：{tool.name}", {"tool": tool.name})

        async def on_tool_end(self, _ctx, _agent, tool, result) -> None:
            refs = _material_refs(result)
            if refs:
                await emit("material", "读取了带位置的书内材料", refs)

    # ---------- 会话与 agent ----------
    # reused 要在建会话之前算：session 里已经有 memory 键 = 前面阶段已经把历史写进去了
    reused = bool(session and session.get("memory"))
    memory = None
    if session is not None:
        memory = session.setdefault("memory", MemorySession())
    agent_kwargs: dict[str, Any] = {}
    if config is not None:
        agent_kwargs["model_settings"] = model_settings(config)
    agent = Agent(
        name=f"reader3-{spec.layer}",
        instructions=build_instructions(spec, tool_budget=cfg.max_tool_calls),
        tools=tools,
        model=model,
        tool_use_behavior=stop_when_submitted,
        **agent_kwargs,
    )
    agent_ref["agent"] = agent

    remaining = max(0, budget.max_requests - budget.requests)
    if cfg.request_limit:
        remaining = min(remaining, cfg.request_limit)
    await emit("run", "复用同一段历史继续执行" if reused else "Agents SDK 基座开始执行",
               {"layer": spec.layer, "reused": reused})
    try:
        result = await _with_watchdog(
            Runner.run(agent, json.dumps(spec.materials, ensure_ascii=False),
                       max_turns=max(1, remaining), hooks=Hooks(), session=memory),
            budget=budget, cancelled=cancelled)
    except (AgentError, BudgetExceeded, AgentCancelled):
        raise
    except MaxTurnsExceeded as exc:
        raise BudgetExceeded("步数用尽仍未交出合规结果") from exc
    except asyncio.CancelledError:
        if cancelled.is_set():
            raise AgentCancelled("任务已取消") from None
        raise
    except Exception as exc:
        if cancelled.is_set():
            raise AgentCancelled("任务已取消") from exc
        raise AgentError(f"Agents SDK 基座执行失败：{type(exc).__name__}: {exc}") from exc

    if state["submitted"] is None:
        output = getattr(result, "final_output", None)
        if isinstance(output, dict) and {"short", "detail", "sentences"} <= set(output):
            return output                       # 兜底：模型直接以结构化文本交卷
        raise AgentError("模型未调用提交结果工具")
    return state["submitted"]
