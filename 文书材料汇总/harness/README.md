# Harness 设计

状态：设计稿 v1，2026-10-24。本文定义契约与落地顺序；代码尚未按此实现。

## 一句话

Harness 是模型-工具循环之外的外层执行壳：把目标（Goal）变成可恢复的多轮执行，把一次模型会话变成可流式、可取消的 Turn；Server 把两类执行暴露成带幂等、预算和 SSE 的 HTTP 接口。

## 三层职责

| 层 | 现有实现 | 负责 | 不负责 |
|---|---|---|---|
| Turn | `runtime/task.py`、`runtime/sdk.py`、`runtime/loop.py` | 一次 `run_task`：模型 ↔ 工具循环、submit 终止、预算计数、SDK 会话 | Goal 唤醒、跨轮上下文、HTTP 生命周期 |
| Harness | `agent/loop.py`（原型） | Goal 唤醒、上下文、轮次、终态、会话回滚 | 书籍、笔记、审核、发布 |
| Server | `agent/service.py`（扩展） | 生命周期、幂等、并发、SSE、执行状态持久化 | 模型实现、业务数据模型 |

业务 Server（ECUST_Gnosys）仍然拥有书库、阅读记录、run/task、待核稿和发布。Harness 只接收任务描述和工具契约，工具执行通过带认证的回调回到业务 Server。

## 与 PRD / 架构图对齐

对照 `产品需求文档（PRD）(1).md` 与 `未命名绘图(1).drawio`：

| PRD / 架构图 | Harness 落点 | 边界 |
|---|---|---|
| Server 与 Agent 分离、HTTP+SSE、共享令牌 | D5/D6，[server.md](server.md) | Harness 不连业务库 |
| 四层 + Chat | [reader-integration.md](reader-integration.md) 映射表 | 业务编排仍在 `runs/*`、`chat.py` |
| Agent：任务契约与上下文 | Goal `spec_json` + `context` | 上下文 ≤ 16 KiB |
| Agent：工具与技能 | `ToolContract` / `SkillContract` | 工具执行回调业务 Server |
| Agent：Loop 循环规则 | `run_goal` / `run_chat_turn` | Goal 轮次、聊天单轮 |
| Agent：预算与终止机制 | `max_requests` / `max_tool_calls` / `max_tokens` / 超时 / submit | Token 预算在本轮模型返回后结算 |
| Agent：证据与复核 | 业务回调维护任务级证据索引；Harness 只传 `run_id/turn_id/call_id` | Harness 不解析原文、不存工具结果 |
| Agent：记忆机制 | SDK 会话 + `update_goal_context` + 会话游标 | 不做向量记忆 |
| Agent：模型配置与成本分配 | 每个 Goal/Turn 独立 `config` + usage | 小模型/大模型由业务按阶段选择 |
| Agent：文本内容预处理 | 业务导入管线（`reader/import_*`、`locator`） | Harness 不解析书籍 |
| 模型服务（多模型） | 请求级 `config`，按阶段切换 | Harness 不选模型 |
| 每轮调用一个工具 | Goal 模式 `parallel_tool_calls=False` | 聊天保持模型默认，可并行 |
| L3 生成/修订复用同一会话 | 两个 Goal 传同一个 `session_key`；复核用独立 key | 共享会话的阶段必须串行 |
| 单次结果不作质量结论、运行记录可复现 | Harness 存事件/用量；业务存证据索引、候选稿和校验结果 | 不把原文和工具结果复制进执行状态库 |

## 现状与缺口

| 代码 | 现在能做什么 | 缺口 |
|---|---|---|
| `agent/loop.py` | task/chat 两种模式、Goal 反复唤醒、SQLite 持久化 | `complete_goal` 无条件完成；context 更新不可见；无预算/轮次持久化；只打终端 |
| `runtime/task.py`、`runtime/loop.py`、`runtime/sdk.py` | 单轮执行、submit 终止、预算、技能、流式事件 | 不暴露 `submitted`；没有 Goal/Turn 资源 |
| `backend/agent/service.py` | `/agent/v1/executions`、SSE、取消、重启恢复、工具回调 | 一次请求只跑一轮；没有 Goal；聊天另走一套 |
| `backend/agent/chat.py` | 会话、消息、流式回答、取消、恢复 | 自己构造 SDK Runner，取消/预算/会话与 execution 路径重复 |
| `backend/agent/runs/*` | L1–L4 的业务调度、复核、发布 | 每个阶段各自调用一次 execution，外层语义不进 Harness |

## 设计决策

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | Goal 是唯一的多轮任务单位；聊天一次用户消息 = 一个 Turn，不创建 Goal | 聊天的“目标”由用户下一句话决定，不需要服务端反复唤醒 |
| D2 | 只有 `submit_*` 通过 validator 才算成功；模型自然停下的文本不算完成 | 避免原型中“没交卷也被标记完成”的问题 |
| D3 | 复用 `TaskSpec` / `SubmitSpec` / `run_task`，只补 `ExecutionContext.submitted` | 单轮执行器已经覆盖预算、技能、工具错误分层，重写没有收益 |
| D4 | 跨轮上下文由显式工具 `update_goal_context` 写入，不从返回值隐式吸收 | 持久、可审计、模型能理解什么时候该保存进度 |
| D5 | 业务数据留在业务 Server；Harness 只存执行状态，工具经认证回调执行 | 书库和笔记的写入边界不变，Harness 可独立部署/测试 |
| D6 | 扩展现有 Agent Service（默认 `127.0.0.1:8124`），新路由挂在 `/agent/v1` 下，不新建端口和进程 | `dev.py`、令牌、状态库、SSE 轮询都能复用 |
| D7 | 回调按 `run_id` 注册：Goal 用 `goal_id`，聊天用 `turn_id`；Turn 只作为载荷字段 | Goal 跨多个 Turn 时业务注册一次即可，恢复后无需重注册 |
| D8 | 生命周期事件持久化；`text_delta`/`reasoning_delta` 合并后写入；API key、工具参数和工具结果不落库、不进事件流 | 兼顾可重放、SQLite 写放大和密钥安全 |
| D9 | 进程重启后只把未完成记录标为 `interrupted`，不自动续跑 | 避免重启后无人值守地产生模型费用；由业务显式 resume |
| D10 | v1 单机 SQLite + 进程内调度，不引入 Redis/队列 | 当前是本地阅读器，并发上限只有个位数 |
| D11 | `agent/loop.py` 的语义并入 `backend/agent/harness/runner.py`；根 `agent/` 只保留为终端客户端/样例，不再维护第二套循环 | 避免原型和产品实现分叉 |
| D12 | Goal 预算增加共享 `max_tokens`；在每次模型返回后结算，超限终止 | PRD 要求请求、工具、时限和 Token 四类预算 |
| D13 | Goal 模式设置 `parallel_tool_calls=False`，每个模型回合最多一个工具 | PRD 的生命周期按“每轮调用一个工具”登记证据 |
| D14 | 允许业务给多个 Goal 传同一个 `session_key`：L3 生成/修订共享，复核独立；共享阶段必须串行 | PRD 要求生成与修订复用会话、独立校验用单独会话 |
| D15 | 任务级原文证据索引和复核留在业务 Server；Harness 不持久化原文/工具结果 | 证据索引需要原文与指纹，只有业务 Server 持有 |

## 文档索引

| 文档 | 内容 |
|---|---|
| [harness.md](harness.md) | Goal/Turn 对象、状态机、循环、上下文、预算、工具回调、持久化、恢复 |
| [server.md](server.md) | HTTP/SSE 路由、请求响应模型、幂等、并发、配置、安全、兼容性 |
| [reader-integration.md](reader-integration.md) | 与 ECUST_Gnosys 的映射、业务改造点、迁移阶段、验收与风险 |

## 一句话边界

Harness 不做评测集、不做模型路由、不做多租户、不做分布式调度、不做业务审核。评测可以消费 Harness 的 Turn 事件和最终输出，但打分规则属于另一个系统。
