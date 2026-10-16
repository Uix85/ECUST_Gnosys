"""自写基座：把 TaskSpec 喂给现有的 agent_core.run_tool_loop。

这一层只是翻译，不改 agent_core 的任何行为 —— 现有 1424 段语义层的执行路径就是它。
"""

import time
from typing import Any, Awaitable, Callable

from agent_core import LoopBudget
from agent_engine.spec import EventWriter, TaskSpec

from .custom_loop import run_tool_loop


async def run(
    spec: TaskSpec,
    *,
    request_model: Callable[[list[dict], list[dict], Any], Awaitable[dict]],
    event: EventWriter,
    cancelled: Any,
    budget=None,
) -> dict[str, Any]:
    if budget is None:
        budget = LoopBudget(spec.budget.max_requests, spec.budget.max_tool_calls,
                            time.monotonic() + spec.budget.timeout_seconds)
    registry = {
        s.name: {"title": s.name, "description": s.description,
                 "instructions": s.instructions,
                 "tools": [t.openai_schema() for t in s.tools],
                 "handlers": {t.name: t.handler for t in s.tools}}
        for s in spec.skills
    }
    handlers = {t.name: t.handler for t in spec.tools}
    return await run_tool_loop(
        prompt=spec.prompt,
        materials=spec.materials,
        validator=spec.submit.validator,
        allowed_skills=spec.allowed_skills,
        skill_registry=registry,
        budget=budget,
        cancelled=cancelled,
        request_model=request_model,
        event=event,
        task_tools=[spec.submit.as_tool().openai_schema()],
        task_handlers=handlers,
        request_limit=spec.budget.request_limit,
    )
