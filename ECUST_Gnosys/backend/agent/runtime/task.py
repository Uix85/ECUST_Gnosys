"""Agent 执行请求、执行上下文和共享预算。"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from agents.memory.session import Session


class AgentError(RuntimeError):
    """Agent 执行期错误基类；上层可据此把失败统一归一为任务级错误。"""


class BudgetExceeded(AgentError):
    """预算（请求次数 / 工具次数 / 总时限）耗尽；属于可预期的终止，不是程序缺陷。"""


class AgentCancelled(AgentError):
    """用户取消或上层关闭事件流导致的终止。"""


@dataclass
class LoopBudget:
    """一次任务（或多阶段流程共享）的预算上限与实时用量。

    约定：
    - `max_requests` / `max_tool_calls` 是硬上限，`requests` / `tool_calls` 是已消耗计数；
    - `deadline` 是 `time.monotonic()` 的绝对时刻，不是剩余秒数；
    - `check_*` 只做校验、不推进计数，计数由调用方（hooks / 工具包装）显式累加，
      这样“检查”和“消耗”可以分别放在合适的时机；
    - 同一实例可在多个 Task 之间复用，从而让多阶段流程共享剩余额度。
    """

    max_requests: int
    max_tool_calls: int
    deadline: float
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入与输出 token 之和。"""
        return self.input_tokens + self.output_tokens

    def record_usage(self, usage: dict | None) -> None:
        """累加 SDK 返回的用量；同时兼容 Responses（input/output）与 Chat Completions（prompt/completion）命名。"""
        usage = usage or {}
        self.input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    def check_running(self, cancelled: asyncio.Event) -> None:
        """只检查取消与总时限，不消耗额度；超时抛 BudgetExceeded。"""
        if cancelled.is_set():
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("任务超过总时限")

    def check_request(self, cancelled: asyncio.Event) -> None:
        """发起模型请求前的检查（取消 / 超时 / 请求数上限），不自增计数。"""
        self.check_running(cancelled)
        if self.requests >= self.max_requests:
            raise BudgetExceeded("模型请求次数已达上限")

    def check_tool(self, cancelled: asyncio.Event) -> None:
        """执行工具前的检查（取消 / 超时 / 工具数上限），不自增计数。"""
        self.check_running(cancelled)
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("工具调用次数已达上限")


ToolHandler = Callable[[dict], Awaitable[Any]]
EventWriter = Callable[[str, str, dict | None], Awaitable[None]]


@dataclass
class LoopBudgetSpec:
    """默认预算；多阶段流程可以通过 ExecutionContext 传入共享预算。

    `request_limit` 用于在通用 `max_requests` 之上再给某个阶段单独收紧请求数，
    None 表示不额外限制（实际轮次由 loop.run_task 取两者较小值）。
    """

    max_requests: int = 12
    max_tool_calls: int = 20
    timeout_seconds: float = 300.0
    request_limit: int | None = None


@dataclass
class ToolSpec:
    """注册给模型的一个工具。

    `parameters` 是 JSON Schema 的 properties 段（不含 object 外壳），
    `required` 列出必填参数名；`handler` 接收解析后的参数字典并返回任意可序列化结果。
    """

    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    required: list[str] = field(default_factory=list)


@dataclass
class SubmitSpec:
    """通过 validator 后终止执行，返回规范化的结构化结果。

    name 必须以 `submit_` 开头，SDK 侧据此把该工具视为终止工具；
    validator 接收模型给出的参数并返回规范化字典，抛异常即表示退回给模型重试。
    """

    name: str
    description: str
    parameters: dict
    validator: Callable[[dict], dict]
    required: list[str] = field(default_factory=list)

    def __post_init__(self):
        """校验提交工具名，保证 SDK 侧能把它识别为终止工具。"""
        if not self.name.startswith("submit_"):
            raise ValueError("SubmitSpec.name 必须以 submit_ 开头")


@dataclass
class SkillSpec:
    """可被 `load_skill` 按需启用的技能。

    `instructions` 是加载时返回给模型的规程文本；`tools` 只在该技能被加载后才挂载，
    因此未加载的技能不会占用模型的工具列表和工具调用预算。
    """

    name: str
    description: str
    instructions: str
    tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class TaskSpec:
    """一个 Agent 任务的静态定义，由运行器（loop.run_task）消费。

    契约：
    - `prompt` 作为 Agent instructions，`materials` 序列化后作为用户输入；
    - `submit` 非空时，只有 submit 工具写入的结果才算成功，模型的自由文本不作为最终输出；
    - `tools` 是初始挂载工具，`skills` 中未被 `allowed_skills` 列出的技能不可加载；
    - `request_limit` / `budget` / `stream` / `tool_limit_action` 控制本次运行的
      额外请求上限、预算来源、是否流式消费以及工具预算耗尽时的行为
      （`fail` 抛错，`finish` 卸下工具让模型基于已有材料作答）。
    """

    name: str
    prompt: str
    materials: dict
    submit: SubmitSpec | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    skills: list[SkillSpec] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    request_limit: int | None = None
    budget: LoopBudgetSpec | None = None
    stream: bool = False
    tool_limit_action: Literal["fail", "finish"] = "fail"

    def skill(self, name: str) -> SkillSpec:
        """按名称查找技能；不存在时抛 KeyError（调用方已先校验 allowed_skills）。"""
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise KeyError(name)


@dataclass
class ExecutionContext:
    """单次执行的输入与跨任务共享状态。

    字段：
    - `model`：SDK 模型实例（必填，缺失时 run_task 直接报错）；
    - `config`：AgentConfig，用于取默认预算和模型参数；
    - `cancelled`：取消信号，预算检查与工具执行都会读取；
    - `emit`：事件写入器 `(stage, message, payload)`，负责向前端/SSE 推送进度；
    - `budget`：预置即共享。若上层（多阶段流程）已传入，run_task 直接复用其剩余额度；
      为 None 时才按 TaskSpec.budget 构造，缺失 spec 则回退到 config 的默认值。
      共享优先于任务自带预算，避免子任务重置额度；
    - `session`：SDK 会话，非空且已有历史时继续同一段对话；
    - `mounted_tools`：动态加载的技能工具会追加到这里，供后续阶段复用。
    """

    model: Any
    config: Any
    cancelled: asyncio.Event
    emit: EventWriter
    budget: LoopBudget | None = None
    session: "Session | None" = None
    mounted_tools: list[ToolSpec] | None = None
