# ECUST Gnosys

一个本地运行的 EPUB 阅读器，提供章节阅读、阅读笔记和段落级 AI 解释。书籍与阅读记录保存在本机，网页服务默认监听 `127.0.0.1:8123`。

## 快速开始

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。在项目根目录运行：

```bash
uv sync
uv run server.py
```

打开 <http://127.0.0.1:8123/>。可以在书库页面上传 EPUB/TXT，也可以从命令行导入 EPUB：

```bash
uv run backend/reader/import_epub.py "/path/to/book.epub"
```

导入的书籍保存在 `books/<书名>_data/`，每本书的数据位于该目录的 `book.db`。

## AI 配置

阅读器本身可以先启动；使用段落 Agent 前，需要配置兼容 OpenAI Chat Completions 工具调用的模型服务。将 `.env.example` 复制为 `.env`，按需修改模型和服务地址，再在项目根目录创建 `.env.secrets`：

```dotenv
AGENT_API_KEY=你的_API_密钥
```

`.env.secrets` 已加入 Git 忽略规则。配置读取优先级为：进程环境变量、`.env.secrets`、`.env`。`.env.example` 中可调整的选项包括：

| 变量 | 用途 |
| --- | --- |
| `AGENT_API_BASE_URL` | Chat Completions 服务地址 |
| `AGENT_MODEL` | 使用的模型名称 |
| `AGENT_REQUEST_TIMEOUT` | 单次请求超时秒数 |
| `AGENT_MAX_REQUESTS` | 每段解释的模型请求上限 |
| `AGENT_MAX_TOOL_CALLS` | 每段解释的工具调用上限 |
| `AGENT_RUN_TIMEOUT` | 每段任务的最长运行秒数 |
| `AGENT_MAX_RETRIES` | 请求失败后的重试次数 |
| `AGENT_MAX_PARALLEL` | 同时运行的段落任务数 |
| `AGENT_SESSION_ID` | 可选的 `x-opencode-session` 请求头 |

## Agent 执行流程

`backend/agent/runs.py` 保留批次调度和段落任务的 `for` 循环，负责启动、取消和等待任务。每段的生成、复核、必要时修订及结果保存由 `run_service.py` 处理；`engine.py` 使用 OpenAI Agents SDK 的 `Runner.run` 执行单个 Agent 的模型与工具调用流程。

复核通过的解释会发布到正文旁；未通过的结果会保存在待核稿中，供读者审阅和采纳。任务状态和进度事件也保存在对应书籍的数据库里。

## 目录结构

```text
server.py                         根目录启动入口
backend/
  api/server.py                   FastAPI 应用、网页路由和服务装配
  agent/
    runs.py                       批次调度、取消和清晰的段落循环
    run_service.py                单段流程、运行状态与结果持久化
    engine.py                     Agents SDK 执行、预算和取消控制
    tasks.py                      Agent 任务定义、提示词和校验
    tools.py                      阅读与原文核对工具
    book.py                       Agent 使用的书籍和段落数据
    config.py                     环境配置
    skills/book-reading/          阅读 Agent 使用的 Skill
    run_api.py                    Agent HTTP 接口
  reader/                         EPUB 导入、阅读服务、章节处理和数据模型
  db/schema.py                    SQLite 表结构和连接管理
frontend/
  templates/                      阅读器网页模板
  static/                          浏览器静态资源
books/                             本地导入的书籍及其数据库
docs/                              项目规范、研究材料和章节文档
```

`backend/server.py` 是兼容启动入口；日常启动使用项目根目录的 `server.py`。

## 许可证

[MIT](LICENSE)
