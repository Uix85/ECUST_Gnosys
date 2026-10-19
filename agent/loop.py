"""Goal 驱动的 Agent 主循环（独立架构样例）。

Chat 入口使用 `mode="chat"`，不创建 Goal；Layer/任务入口使用
`mode="task"`，传入一个 Goal。当前版本不做 Goal 嵌套：task 模式
只反复执行同一个 Goal，直到满足。

Goal 上的 store 负责持久化，具体 agent 对象负责模型与工具调用。本文件不连接
ECUST_Gnosys，也不直接执行 SQL。
"""

import agent.models
import json
import typing

LoopMode = typing.Literal["chat", "task"]

async def agent_loop(
    mode: LoopMode,
    agent: typing.Any,
    root_goal: agent.models.Goal | None = None,
    session_id: str | None = None,
    context_store: typing.Any | None = None,
    user_input: str | None = None,
    max_goal_rounds: int = 10,
    ) -> typing.Any:
    """Agent 主循环：跑一轮 Agent，停止后检查还有没有未完成 Goal。

    * task：启动前把 root_goal 写入它的 store；之后每轮取一个未完成 Goal 执行。
    * chat：不预加 Goal，也不检查 Goal；Agent 停止后直接返回。
    * task 在 Agent 停止后检查未完成 Goal，有就再唤醒，因此 task 相当于 chat 的无头模式。
    * 未完成 Goal 最多唤醒 max_goal_rounds 次，避免模型一直不交卷。
    """
    if mode == "task":
        store = root_goal.store
        await store.add_goal(root_goal)
        goal = await store.next_unfinished_goal()
        if goal is None:
            return None
        # task 的 context 在 while 里按 goal.id 加载，这里不预置。
    else:
        store = None
        context = await context_store.load_context(session_id)
        goal = None

    goal_rounds = 0

    while True:
        if goal is not None and mode == "task":
            context = await store.load_context(goal.id)

        if goal is not None:
            text = (
                f"Goal: {goal.objective}\n"
                f"Acceptance: {json.dumps(goal.acceptance, ensure_ascii=False)}\n"
                f"Input: {json.dumps(goal.input, ensure_ascii=False)}"
            )
            instructions = (
                "Complete the goal described by the user. When it is fully "
                "satisfied, call submit_goal with a concise summary."
            )
            if context:
                instructions += (
                    "\nContext: "
                    + json.dumps(context, ensure_ascii=False, default=str)
                )
            goal_mode = True
        else:
            text = user_input or "Hello"
            instructions = "You are a terminal assistant. Be concise."
            if context:
                instructions += (
                    "\nContext: "
                    + json.dumps(context, ensure_ascii=False, default=str)
                )
            goal_mode = False

        result = await agent.run_turn(instructions, text, goal_mode=goal_mode)

        context.update(result.context_updates)

        if goal is not None:
            await store.complete_goal(goal.id, result)

        if mode == "chat":
            await context_store.save_context(session_id, context)

        # Agent 停止后才检查未完成 Goal：有就再唤醒，没有就结束。
        goal = await store.next_unfinished_goal() if store else None
        if goal is None:
            return result.output

        goal_rounds += 1
        if goal_rounds >= max_goal_rounds:
            return result.output
