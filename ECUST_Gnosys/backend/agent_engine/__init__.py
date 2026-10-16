"""生产用的 Agent 基座：OpenAI Agents SDK（`agent_engine/agents_sdk.py`）。

历史沿革（两个旧基座都在 `backend/legacy/`，见那里的 README）：
  legacy/custom_loop.py + custom_engine.py  早期自写的 Chat Completions 工具循环
  legacy/smol_engine.py                     中途换成的 smolagents

这里只做两件事：按 TaskSpec 跑一个任务、按配置造模型对象与请求设置。
"""


async def run_task(spec, *, event, cancelled, request_model=None, model=None, budget=None,
                   session=None, config=None):
    """执行一份 TaskSpec。

    budget 可外部传入 —— 多阶段（生成/复核/修订）共享同一份预算时用。
    session 也由外部持有：传入同一个 dict 就让多个阶段复用同一段会话历史，
    这是"复合循环"的接入口。复核阶段**不要**传 session，独立上下文是刻意的质量设计。
    config 用于构造请求级设置（tool_choice / extra_body）。

    request_model 保留在签名里：它是旧基座测试注入假模型的入口，生产路径不使用。
    """
    from . import agents_sdk
    return await agents_sdk.run(spec, model=model, event=event, cancelled=cancelled,
                                budget=budget, session=session, config=config)


def build_engine_model(config):
    """按配置造 Agents SDK 的模型对象。"""
    from .agents_sdk import build_model
    return build_model(config)
