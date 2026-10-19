"""终端 Agent 的 OpenAI Agents SDK 适配层。

这里只负责一次 SDK turn：构造 Agent、挂载 submit_goal（task 模式）、
用 SQLiteSession 续接历史，并通过 run_streamed 把文本和工具调用打到终端。
Goal/Chat 的唤醒逻辑在 loop.py，不在这里。
"""

import agent.config
import agent.models
import agent.stores
import agents
import openai
import openai.types.responses

# 本地终端不需要向 OpenAI 上报 trace；关掉也能避免无 key 时的导出警告。
agents.set_tracing_disabled(True)


class SDKFunctionTools(list):
    """兼容当前 SDK 对 `agent.tools.reading` 的访问。

    即使没有工具，SDK 在执行工具计划时也会读这个属性；普通 list 会报
    AttributeError。这里让空工具列表也能通过。
    """

    @property
    def reading(self):
        """返回自身，满足 SDK 的 `tools.reading` 接口。"""
        return self


class SDKAgentCore:
    """一次 SDK turn 的执行器：Agent + SQLiteSession + Runner。"""

    def __init__(
        self,
        settings: agent.config.Settings,
        store: agent.stores.SQLiteStore,
        session_id: str,
    ):
        """构造模型客户端和持久化 session；每个 session_id 一条对话历史。"""
        self.session_id = session_id
        client_kwargs = {
            "api_key": settings.api_key,
            "timeout": settings.request_timeout,
            "base_url": settings.base_url
        }

        # 统一走 Chat Completions，避免依赖 Responses API 的供应商差异。
        self.model = agents.OpenAIChatCompletionsModel(
            model=settings.model,
            openai_client=openai.AsyncOpenAI(**client_kwargs),
        )
        # SDK 自己读写对话历史；表名与业务 contexts 分开，避免混用。
        self.session = agents.SQLiteSession(
            session_id,
            db_path=str(store.path),
            sessions_table="terminal_sessions",
            messages_table="terminal_messages",
        )

    async def run_turn(
        self,
        instructions: str,
        text: str,
        *,
        goal_mode: bool = False,
    ) -> agent.models.AgentResult:
        """执行一次 Agent turn：prompt 由 loop 拼好，core 只负责跑 Runner。"""
        submitted = {"done": False, "summary": None}

        if goal_mode:
            @agents.function_tool
            def submit_goal(summary: str) -> str:
                """Call only when the goal is fully satisfied."""
                # 只记录局部状态；真正结束 Runner 由 stop_when_submitted 决定。
                submitted["done"] = True
                submitted["summary"] = summary
                return "goal recorded"

            def stop_when_submitted(_context, _results):
                return agents.ToolsToFinalOutputResult(
                    is_final_output=submitted["done"],
                    final_output=submitted["summary"],
                )

            tools = SDKFunctionTools([submit_goal])
            tool_behavior = stop_when_submitted
        else:
            tools = SDKFunctionTools()
            tool_behavior = "run_llm_again"

        sdk_agent = agents.Agent(
            name="terminal-agent",
            instructions=instructions,
            model=self.model,
            tools=tools,
            tool_use_behavior=tool_behavior,
        )
        # SDK 内部会反复执行“模型 → 工具 → 模型”，直到最终输出。
        result = agents.Runner.run_streamed(sdk_agent, text, session=self.session)

        printed = False
        async for event in result.stream_events():
            # 只转发模型生成的文本增量；其余 SDK 事件不打印。
            if event.type == "raw_response_event" and isinstance(
                event.data, openai.types.responses.ResponseTextDeltaEvent
            ):
                print(event.data.delta, end="", flush=True)
                printed = True
            # 工具调用只打印名字，避免把大段工具输出刷到终端。
            elif (
                event.type == "run_item_stream_event"
                and event.item.type == "tool_call_item"
            ):
                name = getattr(event.item.raw_item, "name", "tool")
                print(f"\n[tool] {name}", flush=True)

        # task 已交卷时用 submit_goal 的 summary；否则用 Runner 的最终文本。
        output = (
            submitted["summary"]
            if goal_mode and submitted["done"]
            else result.final_output
        )
        # 流式文本已经逐字打印过；这里只补换行。若模型只调工具没输出文本，
        # 则把最终 summary 补打出来。
        if printed:
            print()
        elif output:
            print(output)

        # task 模式告诉 loop：这一轮是否已经 satisfied；chat 不需要。
        return agent.models.AgentResult(
            output=output,
            context_updates={"satisfied": submitted["done"]} if goal_mode else {},
        )

    def close(self) -> None:
        """关闭 SDK session 的 SQLite 连接；CLI 退出时调用。"""
        self.session.close()
