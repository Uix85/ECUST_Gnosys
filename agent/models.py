"""Shared data objects for the agent package."""

import dataclasses
import typing


@dataclasses.dataclass(frozen=True)
class Goal:
    """唯一的业务工作对象。"""

    id: str
    objective: str
    acceptance: dict[str, typing.Any]
    input: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    status: str = "created"  # created / completed
    store: typing.Any | None = dataclasses.field(
        default=None, repr=False, compare=False
    )


@dataclasses.dataclass(frozen=True)
class AgentResult:
    """agent 对当前 Goal 的一次执行结果。"""

    output: typing.Any
    context_updates: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
