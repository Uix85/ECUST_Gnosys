"""生产与 legacy 共用的 Agent 基础类型：异常与运行预算。

工具循环本身已移到 backend/legacy/custom_loop.py。
"""

import asyncio
import time
from dataclasses import dataclass


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
    input_tokens: int = 0        # 从模型响应 usage 累计（供成本核算）
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record_usage(self, usage: dict | None) -> None:
        usage = usage or {}
        self.input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    def check(self, cancelled: asyncio.Event, request_limit: int | None = None) -> None:
        if cancelled.is_set():
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("任务超过总时限")
        if self.requests >= min(self.max_requests, request_limit or self.max_requests):
            raise BudgetExceeded("模型请求次数已达上限")

    def check_running(self, cancelled: asyncio.Event) -> None:
        """工具执行期间的检查：不被工具自己吞掉截止时间。"""
        if cancelled.is_set():
            raise AgentCancelled("任务已取消")
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("任务超过总时限")


