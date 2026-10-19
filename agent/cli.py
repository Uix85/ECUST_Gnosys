"""Terminal REPL for the Goal/Chat agent."""

import argparse
import asyncio
import json
import sys
import uuid

import agent.config
import agent.core
import agent.loop
import agent.models
import agent.stores


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal Goal/Chat agent")
    parser.add_argument("--session", default=None, help="chat session id")
    parser.add_argument("--db", default=None, help="SQLite database path")
    parser.add_argument("--model", default=None, help="model name")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--goal", default=None, help="run one headless goal and exit")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = agent.config.load_settings(
        session_id=args.session,
        db_path=args.db,
        model=args.model,
        base_url=args.base_url,
    )
    if not settings.api_key:
        print(
            "缺少 API key：先设置环境变量，例如：\n"
            "  set -a; source agent/.env; set +a\n"
            "  # 或 export AGENT_API_KEY=...",
            file=sys.stderr,
        )
        return 1

    store = agent.stores.SQLiteStore(settings.db_path)
    core = agent.core.SDKAgentCore(settings, store, settings.session_id)

    def new_core(session_id: str) -> agent.core.SDKAgentCore:
        return agent.core.SDKAgentCore(settings, store, session_id)

    try:
        print(f"terminal agent | model={settings.model} | session={core.session_id}")
        print("输入 /help 查看命令，/exit 退出。")

        if args.goal:
            goal = agent.models.Goal(
                id=uuid.uuid4().hex,
                objective=args.goal,
                acceptance={},
                store=store,
            )
            await agent.loop.agent_loop(
                "task",
                agent=core,
                root_goal=goal,
            )
            return 0

        while True:
            try:
                line = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                print(
                    "/help            命令列表\n"
                    "/new             新建会话\n"
                    "/session <id>    切换会话\n"
                    "/context         查看当前 session 的业务 context\n"
                    "/goal <目标>     无头执行一个 Goal\n"
                    "/exit            退出"
                )
                continue
            if line == "/new":
                core.close()
                core = new_core(uuid.uuid4().hex[:8])
                print(f"新会话：{core.session_id}")
                continue
            if line.startswith("/session "):
                session_id = line.split(maxsplit=1)[1].strip()
                if session_id:
                    core.close()
                    core = new_core(session_id)
                    print(f"切换会话：{core.session_id}")
                continue
            if line == "/context":
                context = await store.load_context(core.session_id)
                print(json.dumps(context, ensure_ascii=False, indent=2))
                continue
            if line.startswith("/goal "):
                objective = line.split(maxsplit=1)[1].strip()
                if objective:
                    goal = agent.models.Goal(
                        id=uuid.uuid4().hex,
                        objective=objective,
                        acceptance={},
                        store=store,
                    )
                    await agent.loop.agent_loop(
                        "task",
                        agent=core,
                        root_goal=goal,
                    )
                continue

            await agent.loop.agent_loop(
                "chat",
                agent=core,
                session_id=core.session_id,
                context_store=store,
                user_input=line,
            )

        return 0
    finally:
        core.close()
        store.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        print("\nbye")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
