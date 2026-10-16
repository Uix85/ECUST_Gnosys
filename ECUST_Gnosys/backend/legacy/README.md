# legacy —— 两个已下线 Agent 基座的备份（未接入生产路径）

**生产路径是 OpenAI Agents SDK**（`backend/agent_engine/agents_sdk.py`）。本目录里的文件
不再被 `backend/agent_runs.py` 调用，只作为备份保留：出问题时能对照/回退，而且测试仍在
跑它们，保证备份不腐坏（见 `tests/test_engine_parity.py`，三个基座吃同一份 TaskSpec、
行为必须一致）。

| 文件 | 是什么 | 什么时候下线的 |
|---|---|---|
| `agents_sdk` 之前的基座 ↓ | | |
| `smol_engine.py` | smolagents 的 `ToolCallingAgent` 适配（含它专属的提示词模板与 `final_answer` 占名） | 组长决定换 Agents SDK 时 |
| 最早的基座 ↓ | | |
| `provider.py` | OpenAI 兼容的 httpx 客户端（含重试退避、用量记账、会话头） | 换 smolagents 时 |
| `custom_loop.py` | `run_tool_loop` —— 自写的工具循环（含 skill 加载、材料账本、三重预算） | 换 smolagents 时 |
| `custom_engine.py` | 把 `TaskSpec` 适配成上两者的执行器 | 换 smolagents 时 |

## 备份腐坏的风险点

`provider.py` 里的重试/超时/用量、`smol_engine.py` 里的 `client_kwargs`，都只对各自的
基座生效。**生产基座（Agents SDK）对应能力在 `agents_sdk.build_model()` 里** ——
超时/重试/自定义头都配在 `AsyncOpenAI` 客户端上。以后往备份里加新能力，记得同步到生产。

## 依赖

`smol_engine.py` 需要 `smolagents`，它依赖 `openai` SDK。装 Agents SDK 时
`openai` 被从 2.54 升到 3.31，归档基座是否仍可用没有专门回归验证过 ——
真要用它回退，先跑一遍 `tests/test_engine_parity.py`。

## 想切回去怎么办

`agent_engine/__init__.py` 的 `run_task` 改成调用对应基座的 `run`，
并让它收自己需要的模型对象（`custom_engine` 收 `request_model=client.complete`，
`smol_engine` 收 `model=smolagents` 的模型）即可。
