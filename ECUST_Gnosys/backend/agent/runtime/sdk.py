"""Agents SDK 适配：模型、工具、技能和执行事件。"""

import json
import inspect
from dataclasses import replace
from typing import Any

from agents import Agent, FunctionTool, ModelSettings, RunHooks, ToolsToFinalOutputResult, set_tracing_disabled

from agent.runtime.task import ExecutionContext, TaskSpec, ToolSpec

# 关闭 SDK 自带 tracing：本项目只走自建 HTTP/SSE 链路，不需要向 OpenAI 上报执行轨迹。
set_tracing_disabled(True)


class SDKFunctionTools(list):
    """兼容当前 SDK 对 tools.reading 的访问。

    SDK 内部会把工具容器当作带 `.reading` 属性的对象来读取工具列表；
    这里直接返回自身，使普通 list 也能满足该访问方式。
    """

    @property
    def reading(self):
        """返回自身，兼容 SDK 以 `tools.reading` 读取工具列表。"""
        return self


def build_model(config):
    """按 AgentConfig 构造 OpenAI 兼容客户端与 Chat Completions 模型。

    `session_id` 非空时作为 x-opencode-session 请求头透传（部分中转/订阅路由强制要求）。
    """
    from agents import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI

    kwargs = {"timeout": config.request_timeout, "max_retries": config.max_retries}
    if config.session_id:
        kwargs["default_headers"] = {"x-opencode-session": config.session_id}
    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key, **kwargs)
    return OpenAIChatCompletionsModel(model=config.model, openai_client=client)


def model_settings(config, *, structured: bool) -> ModelSettings:
    """构造模型参数。

    关闭思考模式时透传 `enable_thinking=False`；结构化任务在同样条件下强制
    `tool_choice="required"`，确保模型必须走 submit 工具而不是只回文本。
    """
    disable_thinking = getattr(config, "disable_thinking", False)
    return ModelSettings(
        extra_body={"enable_thinking": False} if disable_thinking else None,
        tool_choice="required" if structured and disable_thinking else "auto",
    )


def _as_text(value: Any) -> str:
    """把工具结果统一成字符串：SDK 只接受文本作为工具输出。"""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class ExecutionHooks(RunHooks):
    """在 LLM 调用前后维护预算计数并向前端推送模型事件。"""

    def __init__(self, context: ExecutionContext):
        """保存执行上下文；预算计数在每次 LLM 调用时从 context.budget 读取。"""
        self.context = context

    async def on_llm_start(self, _context, _agent, _prompt, _items):
        """请求前检查预算并计入一次请求；超限直接抛错终止本次运行。"""
        context, budget = self.context, self.context.budget
        budget.check_request(context.cancelled)
        budget.requests += 1
        await context.emit("model_start", f"第 {budget.requests} 步", {"step": budget.requests, "kind": "llm"})

    async def on_llm_end(self, _context, _agent, response):
        """累计 token 用量并在模型返回后汇报累计值。"""
        budget = self.context.budget
        usage = getattr(response, "usage", None)
        budget.record_usage({
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        })
        await self.context.emit("model_end", f"模型返回（累计 {budget.total_tokens} tokens）", {
            "request": budget.requests,
            "input_tokens": budget.input_tokens,
            "output_tokens": budget.output_tokens,
            "total_tokens": budget.total_tokens,
        })


class SDKExecution:
    """每次调用独有的工具挂载、提交结果和 SDK Agent 状态。

    工具错误分为两层：
    - 协议层问题（JSON 非法、参数不是对象、validator 退回）以
      `{"ok": false, "error": ...}` 文本返回给模型，让模型自行修正后重试；
    - 处理函数内部的异常先上报 tool_error 事件，再重新抛出，由 run_task 终止任务。
    """

    def __init__(self, task: TaskSpec, context: ExecutionContext):
        """构造时立即组装 Agent；之后的 load_skill 只做增量挂载。"""
        self.task, self.context = task, context
        self.submitted = None
        self.tool_seq = 0
        self.agent = self._build_agent()
        self.hooks = ExecutionHooks(context)

    def _make_tool(self, spec, handler=None):
        """把 ToolSpec 包装成 SDK FunctionTool，集中处理解析、预算、事件与错误分层。"""

        async def invoke(_context, raw_args):
            """执行一次工具调用：解析参数、检查预算、发事件并把结果转成 SDK 文本。"""
            # 每次调用独立编号，便于前端把 start/end/error 事件配对。
            self.tool_seq += 1
            data = {"id": self.tool_seq, "tool": spec.name, "args": {}, "status": "running"}
            context, budget = self.context, self.context.budget
            budget.check_running(context.cancelled)
            # finish 策略：预算耗尽时不报错，而是卸下所有工具并恢复 tool_choice=auto，
            # 迫使模型停止调用工具、基于已读材料作答。
            if budget.tool_calls >= budget.max_tool_calls and self.task.tool_limit_action == "finish":
                try:
                    args = json.loads(raw_args or "{}")
                    if isinstance(args, dict):
                        data["args"] = args
                except (ValueError, TypeError):
                    pass
                self.agent.tools = SDKFunctionTools()
                self.agent.model_settings = replace(self.agent.model_settings, tool_choice="auto")
                message = "工具预算已用完，基于已读材料回答"
                await context.emit("tool_budget_exhausted", message, {"tool": spec.name, "exhausted": True})
                await context.emit("tool_error", message, {**data, "status": "blocked", "error": message})
                return _as_text({"ok": False, "error": message + "；停止调用工具，材料不足请明确说明。"})
            budget.check_tool(context.cancelled)
            budget.tool_calls += 1
            try:
                args = json.loads(raw_args or "{}")
                if not isinstance(args, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                data["args"] = args
                await context.emit("tool_start", f"执行工具：{spec.name}", data.copy())
                result = await (handler or spec.handler)(args)
            except (ValueError, TypeError) as exc:
                # 协议层错误：作为工具结果回传，模型可以修正参数后重试。
                message = f"参数不是合法 JSON：{exc}" if isinstance(exc, json.JSONDecodeError) else str(exc)
                await context.emit("tool_error", message, {**data, "status": "error", "error": message})
                return _as_text({"ok": False, "error": message})
            except Exception as exc:
                # 处理函数自身失败：记录事件后继续抛出，避免模型误以为工具只是参数问题。
                await context.emit("tool_error", str(exc), {**data, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
                raise
            await context.emit("tool_end", f"工具完成：{spec.name}", {**data, "status": "ok", "result": result})
            return _as_text(result)

        return FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema={"type": "object", "properties": spec.parameters, "required": spec.required, "additionalProperties": False},
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    async def _submit(self, args):
        """submit 工具：validator 通过则记录结果并触发终止；失败以 ValueError 退回模型重试。"""
        try:
            submitted = self.task.submit.validator(args)
            if inspect.isawaitable(submitted):
                submitted = await submitted
        except Exception as exc:
            raise ValueError(f"提交被退回：{exc}") from exc
        self.submitted = submitted
        await self.context.emit("tool_ok", "提交通过校验", {"keys": sorted(submitted)})
        return {"ok": True}

    async def _load_skill(self, args):
        """按名称加载允许的技能，只挂载尚未存在的工具；重复加载是幂等的。"""
        name = (args.get("name") or "").strip()
        if name not in self.task.allowed_skills:
            raise ValueError(f"该技能不在本任务允许列表中：{name!r}")
        skill = self.task.skill(name)
        existing = {tool.name for tool in self.agent.tools}
        new_tools = [tool for tool in skill.tools if tool.name not in existing]
        self.agent.tools.extend(self._make_tool(tool) for tool in new_tools)
        if self.context.mounted_tools is not None:
            self.context.mounted_tools.extend(new_tools)
        return {"description": skill.description, "instructions": skill.instructions, "tools": [tool.name for tool in new_tools]}

    def _build_agent(self):
        """组装 SDK Agent：任务工具 + 共享已挂载工具 + submit/load_skill，按名称去重。"""
        # 同名工具后者覆盖前者，因此上下文里已挂载的工具优先于任务自带的同名定义。
        specs = {tool.name: tool for tool in self.task.tools + (self.context.mounted_tools or [])}
        tools = [self._make_tool(tool) for tool in specs.values()]
        if self.task.submit is not None:
            tools.append(self._make_tool(self.task.submit, self._submit))
        if self.task.allowed_skills:
            tools.append(self._make_tool(ToolSpec(
                "load_skill", "读取技能规程并启用工具。可用技能：" + "/".join(self.task.allowed_skills),
                {"name": {"type": "string", "description": "技能名"}}, self._load_skill, ["name"],
            )))
        kwargs = {}
        if self.task.submit is not None:
            # 由回调决定何时结束：只有 submit 成功才返回最终输出。
            kwargs["tool_use_behavior"] = self._stop_when_submitted
        return Agent(
            name=self.task.name,
            instructions=self.task.prompt,
            model=self.context.model,
            tools=SDKFunctionTools(tools),
            model_settings=model_settings(self.context.config, structured=self.task.submit is not None),
            **kwargs,
        )

    def _stop_when_submitted(self, _context, _tool_results):
        """提交成功即视为最终输出，SDK 据此结束运行。"""
        return ToolsToFinalOutputResult(is_final_output=self.submitted is not None, final_output=self.submitted)

    async def consume_stream(self, result):
        """消费 SDK 流式事件，仅把文本/推理增量映射为前端事件，其余事件忽略。"""
        async for event in result.stream_events():
            if event.type != "raw_response_event":   # 生命周期类事件不转发
                continue
            kind = getattr(event.data, "type", "")
            if kind == "response.output_text.delta":
                await self.context.emit("text_delta", "", {"text": event.data.delta})
            elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                text = getattr(event.data, "delta", "")
                if text:
                    await self.context.emit("reasoning_delta", "", {"text": text})
