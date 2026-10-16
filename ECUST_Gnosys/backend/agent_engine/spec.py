"""引擎无关的任务描述。

两个基座（自写的 agent_core / smolagents）吃同一份 TaskSpec，
差别只在于谁去跑那个循环。
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


ToolHandler = Callable[[dict], Awaitable[Any]]
EventWriter = Callable[[str, str, "dict | None"], Awaitable[None]]

# smolagents 的 inputs 只认这几个类型名；OpenAI 的 JSON Schema 同名，子键要丢掉
_SCALAR_TYPES = {"string", "integer", "number", "boolean", "object", "array", "any", "null"}


@dataclass
class ToolSpec:
    """一个可以被模型调用的工具。handler 是 async，两边的调用方式不同。"""

    name: str
    description: str
    parameters: dict            # JSON Schema 的 properties
    handler: ToolHandler
    required: list[str] = field(default_factory=list)

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": self.parameters,
                           "required": self.required, "additionalProperties": False},
        }}

    def smol_inputs(self) -> dict:
        """smolagents 的 inputs 格式：{"名": {"type": ..., "description": ...}}，不认 items 等子键。"""
        out = {}
        for name, schema in self.parameters.items():
            typ = schema.get("type", "any")
            out[name] = {"type": typ if typ in _SCALAR_TYPES else "any",
                         "description": schema.get("description", name)}
        return out


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

    def as_tool(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters, self._never_called, self.required)

    async def _never_called(self, args):        # validator 由引擎直接调用，不走 handler
        raise AssertionError("submit 工具不应通过 handler 调用")

    def smol_inputs(self) -> dict:
        return self.as_tool().smol_inputs()


@dataclass
class SkillSpec:
    """一个技能包：instructions 是 SKILL.md，tools 是该技能提供的工具。"""

    name: str
    description: str
    instructions: str
    tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class LoopBudgetSpec:
    max_requests: int = 12          # 模型请求次数上限
    max_tool_calls: int = 20        # 工具调用次数上限
    timeout_seconds: float = 300.0  # 整任务时限
    request_limit: int | None = None  # 本阶段的请求上限（多阶段共享同一份预算时用）


@dataclass
class TaskSpec:
    layer: str                      # "L1" / "L2" / "L3" / "L4"
    prompt: str                     # 系统提示（角色 + 规范）
    materials: dict                 # 输入材料，作为第一条 user 消息
    submit: SubmitSpec
    tools: list[ToolSpec] = field(default_factory=list)       # 本任务直接可用的工具
    skills: list[SkillSpec] = field(default_factory=list)     # 按需加载的技能包
    allowed_skills: list[str] = field(default_factory=list)
    budget: LoopBudgetSpec = field(default_factory=LoopBudgetSpec)

    def skill(self, name: str) -> SkillSpec:
        for s in self.skills:
            if s.name == name:
                return s
        raise KeyError(name)
