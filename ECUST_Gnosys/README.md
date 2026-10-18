# ECUST Gnosys

一个本地运行的 EPUB/TXT 阅读工作台。目录、全文搜索、书签、四层 Markdown 笔记、Agent 对话和原文引用共用书内坐标；对话支持多层 `@章节` 补全、划线引用和流式 Markdown 渲染。页码/段落坐标由入库时建好的**定位表**（`book_nodes` / `book_paragraphs`）统一供给。书籍与阅读记录保存在本机，网页服务默认监听 `127.0.0.1:8123`。

## 快速开始

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。在项目根目录运行：

```bash
uv sync
uv run server.py
```

打开 <http://127.0.0.1:8123/>。启动时会检查 8123 端口；若已被占用，会结束占用该端口的进程后继续启动。可以在书库页面上传 EPUB/TXT，也可以从命令行导入 EPUB：

```bash
uv run backend/reader/import_epub.py "/path/to/book.epub"
```

导入的书籍保存在 database/books/<书名>_data/，每本书的数据位于该目录的 book.db。导入时会从正文的
`<h1>`-`<h6>` 抽出章节层级写入 `chapters.heading_json`；txt 源没有标题标签，由
`backend/agent/tools/context.py` 的文本行模式兜底推断。

入库后建议建一次定位表（章 → 标题区 → 段，见 [定位系统设计](docs/定位系统_设计_v1.md)）：

```bash
uv run python tools/platform/build_locator.py --book <书名>_data --apply
uv run python tools/platform/check_locator.py     # 与现解逐字一致 / 幂等 / 标题质量
```

定位表是 L2 章节层、L3 语义层、L4 概念层的共同地基；没建定位的老书会自动回退到渲染管线现解，不会报错。

### 离线验收

集成测试使用确定性离线模型，通过真实 Agents SDK 执行工具和流式调用；测试书库创建在临时目录中，不会打开 database/books/ 中的个人书籍。无需 API 密钥：

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

浏览器脚本使用本地确定性模型替身，不调用真实模型；当前验收覆盖 390/1024/1440 像素视口、页面内任务失败重试/取消/待核稿采纳，以及临时书籍导入和删除。

原有无模型自检仍可单独运行：

```bash
uv run python backend/agent/tools/context.py      # L3 结构层纯函数自检
uv run python backend/agent/runtime/config.py     # 设置页的校验/落盘/密钥脱敏自检
uv run python backend/agent/layers/l4_selection.py # L4 划词拦截自检
uv run python backend/reader/locator.py           # 定位层纯函数自检（标题树与段落归属）
uv run python tests/test_chapter_layer.py         # L2 章节层单测（validator / 渲染 / 定位树）
uv run python tools/structure/check_outline.py    # L3 层级识别与窗口统计
uv run python tools/platform/check_locator.py     # 定位层自检（与现解逐字一致、幂等、标题质量）
uv run python tools/structure/check_chapter_flow.py   # L2 章节层无模型自检（材料→校验→渲染→TaskSpec）
uv run python tools/concept/check_concept_flow.py # L4 概念层端到端（拦截→材料→工具→校验→TaskSpec）
uv run python tools/semantic/check_semantic_para.py   # 语义层数据对账（分句口径）
uv run python tools/platform/check_chapter_units.py   # 章单位对齐自检
uv run python tools/concept/build_term_index.py       # L0 术语提名与索引落库
```

定位层与章节层需要一个真模型（或用 `--model` 指定）：

```bash
uv run python tools/platform/build_locator.py --apply        # 建/重建定位表（章→标题区→段）
uv run python tools/structure/run_chapter_layer.py --book <书> --chapter 2            # 只看产出
uv run python tools/structure/run_chapter_layer.py --book <书> --chapter 2 --apply    # 写库（先导出旧稿）
```

## AI 配置

阅读器本身可以先启动；使用 Agent 前，需要配置兼容 OpenAI Chat Completions 工具调用的模型服务。打开 http://127.0.0.1:8123/settings（书库与阅读页右上角也有「设置」），填写服务地址、模型名和 API 密钥后保存，数据写入 database/settings.db，下一次 Agent 调用即生效，无需重启。API 密钥只写入本机数据库文件，该文件权限设为当前用户可读写。

Agent 设置只从 `database/settings.db` 读取和保存。未单独配置的选项使用内置默认值；修改设置后下一次 Agent 调用立即生效，无需重启。

下面列出可配置选项：

| 设置项 | 用途 |
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

`backend/agent/runs/loop.py` 是后台任务的调度入口，保留任务遍历、执行和取消的主线；`runs/layer.py` 提供长任务共用的执行上下文与复核发布流程；`runs/records.py` 管理任务、候选稿和事件的数据库读写与重启恢复。各层的任务定义放在 `layers/`，可复用的书籍阅读能力放在 `tools/`。Agent 运行接口与聊天 HTTP/SSE 路由集中在 `api.py`；`chat.py` 管理对话执行、SDK 会话和恢复。对话使用 Agents SDK 流式 Runner 与 SQLiteSession；笔记、候选稿、任务状态和事件保存在书籍数据库中。

复核通过的解释会发布到正文旁；未通过的结果会保存在待核稿中，供读者审阅和采纳。任务状态和进度事件也保存在对应书籍的数据库里。

### 分层与当前接线状态

| 层 | 模块 | 状态 |
| --- | --- | --- |
| 定位层（地基） | `reader/locator.py`，入口 `tools/platform/build_locator.py` | 入库产物：`book_nodes` 标题树 + `book_paragraphs` 段落真相；`logical_paragraphs`／`locate()` 优先读表，老书回退现解 |
| L1 全书层 | `agent/layers/l1_book.py` + `agent/runs/layer.py` | 按段落分块覆盖全书，再归纳章节关系；保存覆盖进度和证据 |
| L2 章节层 | `agent/layers/l2_chapter.py`，入口 `tools/structure/run_chapter_layer.py` | 沿定位树的节点逐个判断（作用/重点/论证/去向），结构化 `blocks_json` 落库并渲染 Markdown；未过线不入库 |
| L2 章节摘要接口 | `agent/layers/l2_chapter.py` + `agent/layers/l1_book.py` | Agent API 保留章级总纲、论证区块、关键转折和承接关系的旧摘要流程 |
| L3 语义层（段落解释） | `agent/layers/l3_paragraph.py` + `agent/tools/reading.py` + `agent/runs/records.py` | 阅读页可触发段落、详细及逐句解释；使用章节结构和自适应上下文 |
| L0 术语索引（录入期提名） | `agent/layers/l0_index.py`，入口 `tools/concept/build_term_index.py` | 零模型统计，手动跑 |
| L4 概念层（划词 → 意义地图/定位） | `agent/layers/l4_selection.py` + `agent/tools/concept.py` + `agent/layers/l4_concept.py` | 阅读页可触发；保存术语地图、位置解释及带指纹的原文证据 |
| Agent 对话 | `agent/chat.py` + `agent/tools/reading.py` | 按书持久化；支持划线、多层级 `@章节`、取消/重试、来源跳转和 Markdown 导出 |

概念层的判定标准写在任务提示（`agent/layers/l4_concept.py`）里，`agent/skills/concept-*/SKILL.md` 只讲工具用法 —— 按需加载的技能不参与判定。

## 目录结构

`agent/runtime` 放各任务共用的模型配置和执行底座；`agent/tools` 放书籍访问、上下文组装和可供 Agent 调用的工具；`agent/layers` 按 L0–L4 放具体任务定义与校验；`agent/runs` 只处理后台调度、通用运行生命周期和数据库记录；`agent/api.py` 装配 Agent 与聊天 HTTP 路由，`agent/chat.py` 管对话执行；`agent/skills` 放按需提供给 Agent 的操作规程。

```text
server.py                         根目录启动入口
backend/
  api/server.py                   FastAPI 应用、网页路由和服务装配
  api/port_cleanup.py             启动前定位并结束占用 8123 的进程
  agent/
    __init__.py                   Agent 包入口
    runtime/                      跨任务共享的运行基础设施
      __init__.py                 Python 子包标记
      config.py                   环境配置、模型设置读取与保存
      engine.py                   Agents SDK 执行、预算、工具和取消控制
    api.py                        Agent 运行与聊天 HTTP/SSE 路由
    chat.py                       对话执行、SDK 会话、工具调用与恢复
    runtime/                      跨任务共享的运行基础设施
      __init__.py                 Python 子包标记
      config.py                   环境配置、模型设置读取与保存
      engine.py                   Agents SDK 执行、预算、工具和取消控制
    tools/                        书籍访问、上下文和 Agent 工具
      __init__.py                 Python 子包标记
      book.py                     书籍数据库、段落访问、文本处理与指纹
      context.py                  章节结构、自适应上下文窗口
      reading.py                  阅读工具、Skill 装配与原文核对能力
      concept.py                  L4 概念专用工具与 Skill 装配
    layers/                       按 L0–L4 组织的任务定义和判定规则
      __init__.py                 Python 子包标记
      l0_index.py                 L0 录入期术语提名与索引
      l1_book.py                  L1 全书覆盖；也提供 L2 旧摘要流程共用的覆盖分析
      l2_chapter.py               L2 章节结构任务与旧摘要接口
      l3_paragraph.py              L3 段落生成、复核、修订和发布前处理
      l4_concept.py               L4 意义地图与当前位置解释任务
      l4_selection.py             L4 划词拦截与规范词条定位
      policy.py                   各层共用的复核、证据和 Markdown 结果规则
    runs/                         后台任务调度、通用生命周期和记录
      __init__.py                 Python 子包标记
      loop.py                     任务创建、主循环、后台 worker 和取消
      layer.py                    L1/L2/L4 共用执行上下文、预算、复核与发布
      records.py                  运行/任务/候选稿/事件持久化与重启恢复
    skills/book-reading/          阅读 Agent 使用的 Skill
    skills/concept-mapping/       L4 建图技能规程
    skills/concept-reading/       L4 定位技能规程
  reader/                         EPUB 导入、阅读服务、章节处理和数据模型
    locator.py                    定位层：章→标题区→段的唯一真相（book_nodes / book_paragraphs）
  db/schema.py                    SQLite 表结构和连接管理
tools/                            开发期自检脚本（零模型，随时可跑）
  agent/check_agent_endpoint.py   端点与 tool calling 连通性
  concept/build_term_index.py     L0 术语提名与索引落库
  concept/check_concept_flow.py   L4 概念层无模型端到端验证
  concept/clean_concept_placeholder.py  清理占位污染词条
  platform/check_chapter_units.py 章单位对齐自检
  platform/build_locator.py      定位表构建（--apply 写入）
  platform/check_locator.py      定位层自检（逐字一致 / 幂等 / 标题质量）
  platform/rebuild_chapter_paragraphs.py 章单位对齐（--apply 写入）
  platform/backfill_heading_levels.py    补齐 chapters.heading_json
  semantic/check_semantic_para.py 语义层数据对账
  semantic/run_batch.py           真模型批量跑（自动备份还原 book.db）
  structure/check_outline.py      L3 结构层自检
  structure/check_chapter_flow.py L2 章节层无模型自检
  structure/run_chapter_layer.py L2 真模型跑一章（--apply 写库）
frontend/
  templates/                      阅读器网页模板
  static/                          浏览器静态资源
database/                           应用数据
  books/                           本地导入的书籍及其数据库
  settings.db                      Agent 模型设置与密钥
docs/                              项目规范、研究材料和章节文档
  reading_semantics.md            术语规范（译名卡 / 前提追溯库，供 read_glossary）
  定位系统_设计_v1.md             定位层设计（四层的地基：一条主坐标 + 一份结构事实）
  章节层_设计_v1.md               L2 章节层设计（材料/判定标准/合格线/验收）
  概念层_设计_v1.md               L4 概念层设计与实施现状
```

`backend/server.py` 是兼容启动入口；日常启动使用项目根目录的 `server.py`。

## 许可证

[MIT](LICENSE)
