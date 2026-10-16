"""Agent 执行引擎：任务结构、运行预算和 OpenAI Agents SDK。"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    RunHooks,
    Runner,
    SQLiteSession,
    ToolsToFinalOutputResult,
    set_tracing_disabled,
)
from agents.exceptions import MaxTurnsExceeded


class AgentError(RuntimeError):
    pass


class BudgetExceeded(AgentError):
    pass


class AgentCancelled(AgentError):
    pass


@dataclass
class LoopBudget:
    max_requests: int
    max_tool_calls: int
    deadline: float
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0  # 从模型响应 usage 累计（供成本核算）
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record_usage(self, usage: dict | None) -> None:
        usage = usage or {}
        self.input_tokens += int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )
        self.output_tokens += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )

    def check_running(self, cancelled: asyncio.Event) -> None:
        if cancelled.is_set():
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("任务超过总时限")

    def check_request(self, cancelled: asyncio.Event) -> None:
        self.check_running(cancelled)
        if self.requests >= self.max_requests:
            raise BudgetExceeded("模型请求次数已达上限")

    def check_tool(self, cancelled: asyncio.Event) -> None:
        self.check_running(cancelled)
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("工具调用次数已达上限")


ToolHandler = Callable[[dict], Awaitable[Any]]
EventWriter = Callable[[str, str, "dict | None"], Awaitable[None]]


@dataclass
class LoopBudgetSpec:
    """任务级预算声明。AgentTask 未收到显式 LoopBudget 时用它构造。

    段落链路由 run_service 持有一份跨阶段的共享预算，不走这里；
    概念层（L4）的 map / locate 两阶段各有自己的额度，由 TaskSpec 声明。
    """

    max_requests: int = 12  # 模型请求次数上限
    max_tool_calls: int = 20  # 工具调用次数上限
    timeout_seconds: float = 300.0  # 整任务时限
    request_limit: int | None = None  # 本阶段的请求上限（多阶段共享同一份预算时用）


@dataclass
class ToolSpec:
    """一个可以被模型调用的工具。"""

    name: str
    description: str
    parameters: dict  # JSON Schema 的 properties
    handler: ToolHandler
    required: list[str] = field(default_factory=list)

@dataclass
class SubmitSpec:
    """终止工具：模型调它 = 交卷。validator 通过返回规范化结果，否则抛 ValueError。"""

    name: str
    description: str
    parameters: dict
    validator: Callable[[dict], dict]
    required: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.name.startswith("submit_"):
            raise ValueError("SubmitSpec.name 必须以 submit_ 开头")

@dataclass
class SkillSpec:
    """一个技能包：instructions 是 SKILL.md，tools 是该技能提供的工具。"""

    name: str
    description: str
    instructions: str
    tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class TaskSpec:
    layer: str  # "L1" / "L2" / "L3" / "L4"
    prompt: str  # 系统提示（角色 + 规范）
    materials: dict  # 输入材料，作为第一条 user 消息
    submit: SubmitSpec
    tools: list[ToolSpec] = field(default_factory=list)  # 本任务直接可用的工具
    skills: list[SkillSpec] = field(default_factory=list)  # 按需加载的技能包
    allowed_skills: list[str] = field(default_factory=list)
    request_limit: int | None = None  # 本阶段上限；整个段落仍共用 LoopBudget
    budget: LoopBudgetSpec | None = None  # 任务级预算声明；缺省则用外部传入的 LoopBudget

    def skill(self, name: str) -> SkillSpec:
        for s in self.skills:
            if s.name == name:
                return s
        raise KeyError(name)


# 收尾规则单独成段，位置在领域提示词之前。
RULES = """## 收尾规则（按此执行，不要自己发明流程）
1. 每一轮只调用一个工具，不要在一轮里同时调多个。
2. 先读后写。读原文用 read_book_paragraph，检索用 search_book_text，读已有笔记用 read_existing_note。
3. 引文必须先用 verify_source_quote 核验过一次才写进结果。核验不通过就改用自己的话复述，
   不要反复重试同一句引文。
4. 同一工具 + 同一组参数不要重复调用第二次；已经有结论就往前走。
5. 整个任务最多 {tool_budget} 次工具调用，超了直接失败。用掉一半还没开始动笔，就直接动笔：
   宁可写得保守一些，也不要因为“材料读得不够”而交不出稿。
6. 只要你能写出一份符合规范的结果，立刻调用 {submit} 交卷 —— 交卷即任务结束，
   不要再补做一次核验或补充阅读。
7. {submit} 返回错误说明时，含义是“改一处再交一次”：按错误信息修改后重新提交，
   不要重新从头探索。"""


def build_instructions(spec, *, tool_budget: int) -> str:
    """将统一收尾规则与领域提示词组合起来。"""
    return (
        RULES.format(submit=spec.submit.name, tool_budget=tool_budget)
        + "\n\n"
        + spec.prompt
    )


# SDK 默认把 trace 上传到 OpenAI；本项目不使用该 tracing 后端。
set_tracing_disabled(True)


def build_model(config):
    """按 .env 配置造 Agents SDK 的模型对象。

    默认的 Responses API 不适用于第三方中转端点，所以显式用 ChatCompletions 通道；
    超时、重试和自定义头都在 AsyncOpenAI 客户端上配置。
    """
    from agents import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI

    client_kwargs = {
        "timeout": config.request_timeout,
        "max_retries": config.max_retries,
    }
    if config.session_id:  # opencode.ai 的 Go 路由要求带这个头，否则 MissingSessionID
        client_kwargs["default_headers"] = {"x-opencode-session": config.session_id}
    client = AsyncOpenAI(
        base_url=config.base_url, api_key=config.api_key, **client_kwargs
    )
    return OpenAIChatCompletionsModel(model=config.model, openai_client=client)


def model_settings(config) -> ModelSettings:
    """请求级设置。tool_choice=required 防止模型回散文导致空转，但与 thinking 模式冲突。"""
    kwargs: dict[str, Any] = {}
    if config.disable_thinking:
        kwargs["extra_body"] = {"enable_thinking": False}
    kwargs["tool_choice"] = "required" if config.disable_thinking else "auto"
    return ModelSettings(**kwargs)


def _params_schema(tspec: ToolSpec | SubmitSpec) -> dict:
    return {
        "type": "object",
        "properties": tspec.parameters,
        "required": tspec.required,
        "additionalProperties": False,
    }


def _as_text(result: Any) -> Any:
    """工具结果给模型看的形态：字符串直通，其余按 JSON 给（比 Python repr 好读）。"""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _material_refs(result: Any) -> dict | None:
    """从工具结果里挑出"读了哪些带位置的材料"，供前端与统计使用。"""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    refs, material_type = [], None
    if isinstance(result, dict):
        material_type = result.get("material_type")
        if result.get("reference"):
            refs.append(result["reference"])
    elif isinstance(result, list):
        refs = [
            item["reference"]
            for item in result
            if isinstance(item, dict) and item.get("reference")
        ]
        if refs:
            material_type = "original_text"
    return {"material_type": material_type, "references": refs} if refs else None


async def _run_with_cancellation(awaitable, *, cancelled, deadline):
    runner_task = asyncio.create_task(awaitable)
    cancel_task = asyncio.create_task(cancelled.wait())
    timeout = max(0, deadline - time.monotonic())
    try:
        done, _ = await asyncio.wait(
            (runner_task, cancel_task),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runner_task in done:
            return runner_task.result()
        if cancel_task in done:
            raise AgentCancelled("任务已取消")
        raise BudgetExceeded("任务超过总时限")
    finally:
        for task in (runner_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(runner_task, cancel_task, return_exceptions=True)


class AgentTaskHooks(RunHooks):
    def __init__(self, task: "AgentTask"):
        self.task = task

    async def on_llm_start(self, _context, _agent, _prompt, _items) -> None:
        budget = self.task.budget
        budget.check_request(self.task.cancelled)
        budget.requests += 1
        await self.task.event(
            "step", f"第 {budget.requests} 步", {"step": budget.requests, "kind": "llm"}
        )

    async def on_llm_end(self, _context, _agent, response) -> None:
        budget = self.task.budget
        usage = getattr(response, "usage", None)
        budget.record_usage(
            {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
        )
        await self.task.event(
            "model_response",
            f"模型返回（累计 {budget.total_tokens} tokens）",
            {
                "request": budget.requests,
                "input_tokens": budget.input_tokens,
                "output_tokens": budget.output_tokens,
                "total_tokens": budget.total_tokens,
            },
        )

    async def on_tool_start(self, _context, _agent, tool) -> None:
        budget = self.task.budget
        budget.check_tool(self.task.cancelled)
        budget.tool_calls += 1
        await self.task.event("tool", f"执行工具：{tool.name}", {"tool": tool.name})

    async def on_tool_end(self, _context, _agent, _tool, result) -> None:
        refs = _material_refs(result)
        if refs:
            await self.task.event("material", "读取了带位置的书内材料", refs)


class AgentTask:
    """把一个 TaskSpec 交给 Agents SDK 执行，并处理项目特有的交卷规则。"""

    def __init__(
        self,
        spec: TaskSpec,
        *,
        model: Any,
        event: EventWriter,
        cancelled: asyncio.Event,
        budget: LoopBudget | None = None,
        session: SQLiteSession | None = None,
        mounted_tools: list[ToolSpec] | None = None,
        config: Any = None,
    ):
        self.spec = spec
        self.model = model
        self.event = event
        self.cancelled = cancelled
        # 外部传入的共享预算优先；否则按 TaskSpec.budget → config → engine 默认值构造
        if budget is not None:
            self.budget = budget
        elif spec.budget is not None:
            self.budget = LoopBudget(
                spec.budget.max_requests,
                spec.budget.max_tool_calls,
                time.monotonic() + spec.budget.timeout_seconds,
            )
        else:
            self.budget = LoopBudget(
                getattr(config, "max_requests", 12),
                getattr(config, "max_tool_calls", 20),
                time.monotonic() + getattr(config, "run_timeout", 300.0),
            )
        self.session = session
        self.mounted_tools = mounted_tools
        self.config = config
        self.agent: Agent | None = None
        self.submitted: dict | None = None

    def _make_tool(
        self, spec: ToolSpec | SubmitSpec, handler: ToolHandler | None = None
    ) -> FunctionTool:
        async def invoke(_context, raw_args: str):
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {"ok": False, "error": f"参数不是合法 JSON：{exc}"},
                    ensure_ascii=False,
                )
            return _as_text(await (handler or spec.handler)(args))

        return FunctionTool(
            name=spec.name,
            description=spec.description,
            params_json_schema=_params_schema(spec),
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    async def _submit(self, args: dict) -> dict:
        try:
            self.submitted = self.spec.submit.validator(args)
        except ValueError as exc:
            message = str(exc)
            await self.event("tool_error", f"提交被退回：{message[:200]}", {"error": message[:200]})
            return {"ok": False, "error": message}
        return {"ok": True}

    async def _load_skill(self, args: dict) -> dict:
        name = args.get("name")
        if name not in self.spec.allowed_skills:
            raise ValueError("该技能不在本任务允许列表中")
        skill = self.spec.skill(name)
        existing = {tool.name for tool in self.agent.tools}
        mounted = [self._make_tool(tool) for tool in skill.tools if tool.name not in existing]
        self.agent.tools.extend(mounted)
        if self.mounted_tools is not None:
            self.mounted_tools.extend(
                tool for tool in skill.tools if tool.name not in existing
            )
        return {
            "description": skill.description,
            "instructions": skill.instructions,
            "tools": [tool.name for tool in mounted],
        }

    def _build_agent(self) -> Agent:
        specs = {spec.name: spec for spec in self.spec.tools + (self.mounted_tools or [])}
        tools = [self._make_tool(spec) for spec in specs.values()]
        tools.append(self._make_tool(self.spec.submit, self._submit))
        tools.append(
            self._make_tool(
                ToolSpec(
                    "load_skill",
                    "读取一个允许使用的技能规程，并启用该技能提供的工具。",
                    {"name": {"type": "string", "description": "技能名"}},
                    self._load_skill,
                    ["name"],
                )
            )
        )
        kwargs = {"model": self.model, "tools": tools}
        if self.config is not None:
            kwargs["model_settings"] = model_settings(self.config)
        agent = Agent(
            name=f"reader3-{self.spec.layer}",
            instructions=build_instructions(self.spec, tool_budget=self.budget.max_tool_calls),
            tool_use_behavior=self._stop_when_submitted,
            **kwargs,
        )
        self.agent = agent
        return agent

    def _stop_when_submitted(self, _context, _tool_results) -> ToolsToFinalOutputResult:
        return ToolsToFinalOutputResult(
            is_final_output=self.submitted is not None,
            final_output=self.submitted,
        )

    async def run(self) -> dict[str, Any]:
        if self.model is None:
            raise ValueError("Agents SDK 需要传入 model")
        max_turns = self.budget.max_requests - self.budget.requests
        limit = self.spec.request_limit
        if limit is None and self.spec.budget is not None:
            limit = self.spec.budget.request_limit
        if limit is not None:
            max_turns = min(max_turns, limit)
        if max_turns <= 0:
            raise BudgetExceeded("模型请求次数已达上限")

        reused = bool(self.session and await self.session.get_items())
        await self.event(
            "run",
            "复用同一段历史继续执行" if reused else "Agents SDK 开始执行",
            {"layer": self.spec.layer, "reused": reused},
        )
        try:
            await _run_with_cancellation(
                Runner.run(
                    self._build_agent(),
                    json.dumps(self.spec.materials, ensure_ascii=False),
                    max_turns=max_turns,
                    hooks=AgentTaskHooks(self),
                    session=self.session,
                ),
                cancelled=self.cancelled,
                deadline=self.budget.deadline,
            )
        except MaxTurnsExceeded as exc:
            raise BudgetExceeded("步数用尽仍未交出合规结果") from exc
        except asyncio.CancelledError:
            if self.cancelled.is_set():
                raise AgentCancelled("任务已取消") from None
            raise
        except AgentError:
            raise
        except Exception as exc:
            if self.cancelled.is_set():
                raise AgentCancelled("任务已取消") from exc
            raise AgentError(f"Agents SDK 执行失败：{type(exc).__name__}: {exc}") from exc

        if self.submitted is None:
            raise AgentError("模型未调用提交结果工具")
        return self.submitted
