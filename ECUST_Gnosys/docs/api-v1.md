# Gnosys API v1

页面使用静态 HTML、CSS 和 JavaScript；页面数据通过版本化 HTTP API 获取。前端开发以 `/api/v1` 和 Agent 服务的 `/agent/v1` 为接口约定。

## Server API

默认地址是 `http://127.0.0.1:8123`。当前 OpenAPI 文档位于 [`/openapi.json`](http://127.0.0.1:8123/openapi.json)，交互式文档位于 [`/docs`](http://127.0.0.1:8123/docs)。OpenAPI 只发布 v1 路由；旧路径仍可访问以兼容现有脚本。

| 功能 | 路径 | 主要用途 |
|---|---|---|
| 书库 | `/api/v1/books`、`/api/v1/books/{book_id}` | 列表、书籍元数据和章节数 |
| 导入与删除 | `/api/v1/books/import`、`DELETE /api/v1/books/{book_id}` | `multipart/form-data` 的 `file` 字段导入 EPUB/TXT；删除本地书籍 |
| 正文与图片 | `/api/v1/books/{book_id}/chapters/{chapter_idx}/html`、`/api/v1/books/{book_id}/images/{image_name}` | 章节 HTML 和书内图片 |
| 阅读工作区 | `/api/v1/workspace/{book_id}/...` | 目录、章节段落、搜索、阅读位置、书签、引用校验和笔记 |
| Agent 任务 | `/api/v1/agent/{book_id}/runs...` | 创建/列出任务、待核稿、任务详情、取消、重试、采纳和事件流 |
| 对话 | `/api/v1/chat/{book_id}/...` | 会话、消息、重试、取消、导出和消息事件流 |
| 阅读层数据 | `/api/v1/layer/...` | L1–L4 内容读取与手动保存 |
| 设置 | `GET/PUT /api/v1/settings` | 读取设置；更新时提交 `{"values": {...}}` |

书库列表返回 `{"items":[{"id":"...","title":"...","author":"..."}]}`。书籍详情返回 `id`、`title`、`author`、`chapter_count` 和 `anchor_map`。工作区的阅读位置、目录节点、段落、笔记等字段以 `/openapi.json` 和现有接口实现为准。

JSON API 错误统一为：

```json
{"error":{"code":"invalid_request","message":"请求格式无效","details":[]}}
```

服务端事件流使用 SSE：每条 `update` 事件带递增 `id` 和 JSON `data`，结束时发送 `end` 事件。客户端可用 `after` 参数或 `Last-Event-ID` 续读。

## 独立 Agent 服务

默认地址是 `http://127.0.0.1:8124`，接口文档为该服务的 `/openapi.json`。协议路径为 `/agent/v1`，请求使用 `Authorization: Bearer <GNOSYS_AGENT_TOKEN>`。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/health` | 服务就绪检查 |
| `POST` | `/agent/v1/executions` | 提交一条执行请求，返回 `execution_id` 和 `queued` 状态 |
| `GET` | `/agent/v1/executions/{execution_id}` | 读取状态、结果和预算用量 |
| `GET` | `/agent/v1/executions/{execution_id}/events` | 读取 Agent 执行 SSE |
| `POST` | `/agent/v1/executions/{execution_id}/cancel` | 请求取消执行 |
| `DELETE` | `/agent/v1/sessions/{session_key}` | 清除 Agent 侧对话历史 |

执行请求模型在 `backend/agent/protocol.py` 中定义。Agent 只接收任务、模型配置和工具描述；需要读取书籍、保存结果或校验提交时，通过 Server 的内部回调完成。书籍、笔记、任务和对话业务记录归 Server 管理；Agent 的 SQLite 文件保存执行状态、事件和 SDK 会话。

## 本地启动

```bash
uv run dev.py
```

该命令分别启动 Agent 服务和 Server 进程，临时生成共享认证令牌，并在退出时关闭两个进程。`GNOSYS_SERVER_PORT`、`GNOSYS_AGENT_PORT` 和对应的 `GNOSYS_SERVER_URL`、`GNOSYS_AGENT_URL` 可用于调整本地地址。直接运行 `uv run server.py` 会使用兼容的进程内 Agent 模式；联调独立服务时使用 `dev.py`。

