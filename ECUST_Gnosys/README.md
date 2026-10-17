# ECUST Gnosys

一个本地运行的 EPUB/TXT 阅读工作台。目录、全文搜索、书签、四层 Markdown 笔记、Agent 对话和原文引用共用书内坐标；对话支持多层 `@章节` 补全、划线引用和流式 Markdown 渲染。书籍与阅读记录保存在本机，网页服务默认监听 `127.0.0.1:8123`。

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

导入的书籍保存在 `books/<书名>_data/`，每本书的数据位于该目录的 `book.db`。导入时会从正文的
`<h1>`-`<h6>` 抽出章节层级写入 `chapters.heading_json`；txt 源没有标题标签，由
`backend/agent/outline.py` 的文本行模式兜底推断。

### 离线验收

集成测试使用确定性离线模型，通过真实 Agents SDK 执行工具和流式调用；测试书库创建在临时目录中，不会打开 `books/` 中的个人书籍。无需 API 密钥：

```bash
uv run python tests/test_workspace.py
```

测试覆盖迁移备份及回滚、EPUB/TXT 导入、坐标与指纹、笔记冲突、四层 Agent、完整上下文覆盖、复核/修订/待核稿、取消/失败/重试、会话历史和恢复。浏览器端到端测试也只用临时书库：

```bash
uv run python tests/browser_server.py
```

另开终端并连接 Ego Browser 后运行：

```bash
ego-browser nodejs < tests/browser_e2e.mjs
```

浏览器报告和桌面视口截图保存在 [docs/acceptance](docs/acceptance/)（脚本同时写入 `/tmp`）。更完整的逐项结果见 [验收记录](docs/implementation_acceptance.md)。

原有无模型自检仍可单独运行：

```bash
uv run python backend/agent/outline.py            # L3 结构层纯函数自检
uv run python backend/agent/selection.py          # L4 划词拦截自检
uv run python tools/structure/check_outline.py    # L3 层级识别与窗口统计
uv run python tools/concept/check_concept_flow.py # L4 概念层端到端（拦截→材料→工具→校验→TaskSpec）
uv run python tools/semantic/check_semantic_para.py   # 语义层数据对账（分句口径）
uv run python tools/platform/check_chapter_units.py   # 章单位对齐自检
uv run python tools/concept/build_term_index.py       # L0 术语提名与索引落库
```

## AI 配置

阅读器本身可以先启动；使用 Agent 前，需要配置兼容 OpenAI Chat Completions 工具调用的模型服务。将 `.env.example` 复制为 `.env`，按需修改模型和服务地址，再在项目根目录创建 `.env.secrets`：

```dotenv
AGENT_API_KEY=你的_API_密钥
```

`.env.secrets` 已加入 Git 忽略规则。配置读取优先级为：进程环境变量、`.env.secrets`、`.env`。`.env.example` 中可调整的选项包括：

| 变量 | 用途 |
| --- | --- |
| `AGENT_API_BASE_URL` | OpenAI-compatible Chat Completions 服务地址 |
| `AGENT_MODEL` | 使用的模型名称 |
| `AGENT_REQUEST_TIMEOUT` | 单次请求超时秒数 |
| `AGENT_MAX_REQUESTS` | 单个 Agent 任务的模型请求上限 |
| `AGENT_MAX_TOOL_CALLS` | 单个 Agent 任务的工具调用上限 |
| `AGENT_RUN_TIMEOUT` | 单个 Agent 任务的最长运行秒数 |
| `AGENT_MAX_RETRIES` | 请求失败后的重试次数 |
| `AGENT_MAX_PARALLEL` | 同时运行的段落任务数 |
| `AGENT_SESSION_ID` | 可选的 `x-opencode-session` 请求头 |

## Agent 执行流程

`backend/agent/runs.py` 保留段落任务的调度循环；`run_service.py` 管理 L3 单段解释的生成、复核、修订及发布。`layer_runs.py` 管理 L1 全书覆盖汇总、L2 章节论证结构和 L4 概念地图/定位。`chat.py` 管理按书保存的 Agent 会话、只读阅读工具和 SSE 流。对话使用 Agents SDK 流式 Runner 与 SQLiteSession；笔记、候选稿、任务状态和事件保存在书籍数据库中。

复核通过的解释会发布到正文旁；未通过的结果会保存在待核稿中，供读者审阅和采纳。任务状态和进度事件也保存在对应书籍的数据库里。

### 分层与当前接线状态

| 层 | 模块 | 状态 |
| --- | --- | --- |
| L1 全书层 | `layer_runs.py` | 按段落分块覆盖全书，再归纳章节关系；保存覆盖进度和证据 |
| L2 章节层 | `layer_runs.py` + `outline.py` | 归纳章级总纲、论证区块、关键转折和承接关系 |
| L3 语义层（段落解释） | `tasks.py` + `tools.py` + `run_service.py` | 阅读页可触发段落、详细及逐句解释；使用章节结构和自适应上下文 |
| L0 术语索引（录入期提名） | `index.py`，入口 `tools/concept/build_term_index.py` | 零模型统计，手动跑 |
| L4 概念层（划词 → 意义地图/定位） | `selection.py` + `concept_tools.py` + `concept.py` + `layer_runs.py` | 阅读页可触发；保存术语地图、位置解释及带指纹的原文证据 |
| Agent 对话 | `chat.py` + `tools.py` | 按书持久化；支持划线、多层级 `@章节`、取消/重试、来源跳转和 Markdown 导出 |

概念层的判定标准写在任务提示（`concept.py`）里，`agent/skills/concept-*/SKILL.md` 只讲工具用法 —— 按需加载的技能不参与判定。

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
    book.py                       书籍、段落数据与句子切分
    config.py                     环境配置
    index.py                      L0 录入期术语提名与索引
    selection.py                  L4 输入拦截（划词 → 规范词条与位置）
    concept_tools.py              L4 概念层技能与工具
    concept.py                    L4 任务定义（建图 / 定位）
    outline.py                    L3 章-节-子节层级与自适应上下文窗口
    run_api.py                    Agent HTTP 接口
    skills/book-reading/          阅读 Agent 使用的 Skill
    skills/concept-mapping/       L4 建图技能规程
    skills/concept-reading/       L4 定位技能规程
  reader/                         EPUB 导入、阅读服务、章节处理和数据模型
  db/schema.py                    SQLite 表结构和连接管理
tools/                            开发期自检脚本（零模型，随时可跑）
  agent/check_agent_endpoint.py   端点与 tool calling 连通性
  concept/build_term_index.py     L0 术语提名与索引落库
  concept/check_concept_flow.py   L4 概念层无模型端到端验证
  concept/clean_concept_placeholder.py  清理占位污染词条
  platform/check_chapter_units.py 章单位对齐自检
  platform/rebuild_chapter_paragraphs.py 章单位对齐（--apply 写入）
  platform/backfill_heading_levels.py    补齐 chapters.heading_json
  semantic/check_semantic_para.py 语义层数据对账
  semantic/run_batch.py           真模型批量跑（自动备份还原 book.db）
  structure/check_outline.py      L3 结构层自检
frontend/
  templates/                      阅读器网页模板
  static/                          浏览器静态资源
books/                             本地导入的书籍及其数据库
docs/                              项目规范、研究材料和章节文档
  reading_semantics.md            术语规范（译名卡 / 前提追溯库，供 read_glossary）
  概念层_设计_v1.md               L4 概念层设计与实施现状
```

`backend/server.py` 是兼容启动入口；日常启动使用项目根目录的 `server.py`。

## 许可证

[MIT](LICENSE)
