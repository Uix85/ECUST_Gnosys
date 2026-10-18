"""所有 Agent 任务共用的执行入口。模型与工具迭代由 SDK Runner 完成。"""

import asyncio
import json
import time

from agents import Runner
from agents.exceptions import MaxTurnsExceeded

from agent.runtime.sdk import SDKExecution
from agent.runtime.task import AgentCancelled, AgentError, BudgetExceeded, ExecutionContext, LoopBudget, TaskSpec


async def _with_cancellation(awaitable, context):
    """让 runner 协程与取消事件、总时限三者赛跑。

    谁先完成决定结果：取消事件 → AgentCancelled；deadline 到期 → BudgetExceeded；
    runner 正常完成 → 返回其结果（runner 内部异常原样抛出）。finally 中取消并回收
    未完成的协程，避免流式读取或后台请求留下悬挂任务。
    """
    runner = asyncio.create_task(awaitable)
    cancel = asyncio.create_task(context.cancelled.wait())
    try:
        done, _ = await asyncio.wait(
            (runner, cancel),
            timeout=max(0, context.budget.deadline - time.monotonic()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel in done:
            raise AgentCancelled("任务已取消")
        if runner in done:
            return runner.result()
        raise BudgetExceeded("任务超过总时限")
    finally:
        for task in (runner, cancel):
            if not task.done():
                task.cancel()
        await asyncio.gather(runner, cancel, return_exceptions=True)


async def run_task(task: TaskSpec, context: ExecutionContext) -> str | dict:
    """执行单个任务，返回最终输出（str）或 submit 的规范化结果（dict）。

    预算优先复用 context.budget（上层共享的剩余额度）；没有时才用 TaskSpec.budget，
    再缺省则回退到 config 的默认值，因此多阶段流程不会在每个子任务重置额度。
    有 submit 的任务只认 submit 工具写入的结果：模型若只输出文本而未提交，视为失败。
    SDK 抛出的异常在此归一为 AgentError 子类，调用方只需处理统一错误类型。
    """
    if context.model is None:
        raise ValueError("Agents SDK 需要传入 model")
    if context.budget is None:
        spec, config = task.budget, context.config
        context.budget = LoopBudget(
            spec.max_requests if spec else getattr(config, "max_requests", 12),
            spec.max_tool_calls if spec else getattr(config, "max_tool_calls", 20),
            time.monotonic() + (spec.timeout_seconds if spec else getattr(config, "run_timeout", 300.0)),
        )
    context.budget.check_request(context.cancelled)
    # 剩余请求数；request_limit 只会在该值之上进一步收紧，不会放宽。
    turns = context.budget.max_requests - context.budget.requests
    limit = task.request_limit if task.request_limit is not None else (task.budget.request_limit if task.budget else None)
    if limit is not None:
        turns = min(turns, limit)
    if turns <= 0:
        raise BudgetExceeded("模型请求次数已达上限")

    execution = SDKExecution(task, context)
    result = None
    try:
        # 会话已有条目说明这是续写，事件文案用于前端区分“新开”与“继续”。
        reused = bool(context.session and await context.session.get_items())
        await context.emit("run", "复用同一段历史继续执行" if reused else "Agents SDK 开始执行", {"name": task.name, "reused": reused})
        kwargs = dict(session=context.session, hooks=execution.hooks, max_turns=turns)
        payload = json.dumps(task.materials, ensure_ascii=False)
        if task.stream:
            result = Runner.run_streamed(execution.agent, payload, **kwargs)
            await _with_cancellation(execution.consume_stream(result), context)
        else:
            result = await _with_cancellation(Runner.run(execution.agent, payload, **kwargs), context)
        if task.submit is not None:
            if execution.submitted is None:
                raise AgentError("模型未调用提交结果工具")
            return execution.submitted
        return result.final_output
    except MaxTurnsExceeded as exc:
        raise BudgetExceeded("步数用尽仍未完成任务") from exc
    except asyncio.CancelledError:
        # 只有确实收到取消信号才转成 AgentCancelled，否则保留原始取消语义向上传播。
        if context.cancelled.is_set():
            raise AgentCancelled("任务已取消") from None
        raise
    except AgentError:
        raise
    except Exception as exc:
        # SDK 常把工具内抛出的异常包一层再抛；若 cause 已是 AgentError 则还原它，
        # 避免预算/取消类错误被泛化成“SDK 执行失败”。
        if context.cancelled.is_set():
            raise AgentCancelled("任务已取消") from exc
        if isinstance(exc.__cause__, AgentError):
            raise exc.__cause__ from None
        raise AgentError(f"Agents SDK 执行失败：{type(exc).__name__}: {exc}") from exc
    finally:
        # 流式运行提前结束或被取消时，SDK 的生成器仍可能挂起；显式 cancel 并耗尽
        # 剩余事件，确保连接和后台任务被回收（忽略清理阶段的任何异常）。
        if task.stream and result is not None:
            result.cancel()
            try:
                async for _ in result.stream_events():
                    pass
            except (Exception, asyncio.CancelledError):
                pass
