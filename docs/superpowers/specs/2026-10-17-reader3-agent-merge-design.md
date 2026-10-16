# reader3 新增 Agent 功能合并进 ECUST_Gnosys 设计

> 状态：已通过设计评审（2026-10-17）。本规格由用户批准的方案 1「保形移植」细化而成。

## 1. 背景

两条并行开发线，共享同一 GitHub 仓库 `Uix85/ECUST_Reader3`，各自从 `1e23f56 First commit` 分叉：

| 代号 | 位置 | 内容 | 状态 |
|---|---|---|---|
| A | `D:/Programs/reader3/ECUST_Reader3` | reader3 开发仓，`backend/` 平铺模块 | 本地 `main` 领先 origin/main 61 个提交，HEAD `100b9dc` |
| B | `D:/Programs/ECUST_Reader3` | 工作区仓，应用位于 `ECUST_Gnosys/`，`backend/` 已包化 | 分支 `合并旧版新增功能` = `6da4ce6` |

B 的 `ECUST_Gnosys/backend/agent/` 是 A 在**段落（语义层）那一条线**上的重构版：同样的行为，改成
`ToolSpec` + 闭包工具、`run_service`/`runs`/`run_api` 三段拆分、`agent_engine` 归并进单个 `engine.py`。
此后 A 继续推进，新增 L4 概念层、L3 结构层，并修了若干行为缺陷，B 未跟进。

本设计把 A 的领先部分合并进 B，落位遵循 B 的风格，**不改段落链路行为**。

## 2. 目标与非目标

**目标**

1. B 与 A `100b9dc` 在 Agent 相关能力上对齐。
2. 保持 B 的包结构与惯用法（`backend/{agent,reader,db,api}`、`ToolSpec`、闭包工具）。
3. 现有 3 个书库完成 schema 迁移与 `chapters.heading_json` 回填。

**非目标**

1. 不把概念层任务接进运行 API。A 里概念层只有 `tools/concept/check_concept_flow.py` 能驱动，本次保持 1:1。
2. 不迁 `tests/`（用户明确不选）。
3. 不迁 `backend/legacy/`（自研 loop + provider）；B 当初有意只留 `openai-agents` SDK。
4. 不迁 `agent_config.retry_base_delay`：A 里它只被 `legacy/provider.py` 与 `tests/` 使用，生产基座是把
   `max_retries` 直接交给 `AsyncOpenAI`（B 已一致），属死配置。
5. 不接线 L3 的 `section_context()`：A 作者自己标注「尚未接线」，B 保持同样状态。

## 3. 差异清单（逐项对账结果）

### 3.1 已同步模块里被漏掉的行为修复

| # | A 的改动 | B 现状 | 落点 |
|---|---|---|---|
| 1 | `agent_book._is_sentence_end()` + `split_sentences()`（句读统一，三处同规则） | 无这两个函数 | `agent/book.py` |
| 2 | `agent_tasks/paragraph.sentence_count()` 用 `split_sentences()` | 自带的旧正则版本 | `agent/tasks.py` |
| 3 | `read_layer_note` 工具（单独读全书层/章节层，不截断） | 无 | `agent/tools.py` |
| 4 | `read_glossary` 工具（查译名卡/前提追溯库，书目知识） | 无 | `agent/tools.py`（依赖 `agent/index.py`） |
| 5 | `search_book_text` 的子词兜底 `_sub_terms()`，命中标 `expanded: true` | 无 | `agent/tools.py` |
| 6 | `verify_source_quote` 对省略号引文 `A……B` 分段核验 | 无 | `agent/tools.py` |
| 7 | `skills/book-reading/SKILL.md` 的 `read_layer_note` 与 `expanded` 两条说明 | 缺这两条 | `agent/skills/book-reading/SKILL.md` |
| 8 | `reader.html` 的 `isSentenceEnd()`（ASCII 句点单独判，与后端同规则） | 旧版 `SENT_END.test()` | `frontend/templates/reader.html` |

第 7 条会改变 skill 快照指纹（`skill_version`），属预期：历史 run 的指纹与新快照不同，
`_baseline_matches` 只做比较不做阻断。

### 3.2 A 新增、B 完全没有的模块

| A | 行数 | B 目标位置 |
|---|---|---|
| `backend/selection_gate.py` | 222 | `agent/selection.py` |
| `backend/book_outline.py` | 264 | `agent/outline.py` |
| `backend/agent_index.py` | 370 | `agent/index.py` |
| `backend/agent_skills_concept.py` | 275 | `agent/concept_tools.py` |
| `backend/agent_tasks/concept.py` | 364 | `agent/concept.py` |
| `skills/concept-mapping/SKILL.md` | — | `agent/skills/concept-mapping/SKILL.md` |
| `skills/concept-reading/SKILL.md` | — | `agent/skills/concept-reading/SKILL.md` |
| `tools/{agent,concept,platform,semantic,structure}/` 7 个脚本 | — | `tools/`（同分区） |
| `docs/概念层_设计_v1.md` | 666 | `ECUST_Gnosys/docs/` |

### 3.3 数据层

| A | B 现状 |
|---|---|
| `concept_occurrence`、`concept_sense`、`term_candidate`、`term_scan` 四张表 | 都没有 |
| `_DROPPED_TABLES=("concept_occurrences",)` + `_drop_unusable_tables()` | 无（旧表 `concept_occurrences` 仍在） |
| `_ensure_columns()` 补 `concept_sense.aliases_json` | 无 |
| `reader3._extract_headings()` 写 `chapters.heading_json` | 恒写 `'[]'` |

`concept_occurrences` 指向物理段落坐标系（`paragraphs.id`），而 L3 / `agent_notes` / 前端高亮全用逻辑坐标
（`chapter_idx + paragraph_index + fingerprint`）；实测抽一章 38 个逻辑段，能在物理表逐字找到的只有 1 段，
且该表从未写入过任何数据。因此按 A 的做法直接丢弃，不搬运。

`tools/check_semantic_para.py` → `tools/semantic/check_semantic_para.py` 需要与后端 `split_sentences` 对齐（第 1 条）。

### 3.4 engine 的一处扩展

B 的 `TaskSpec` 没有 budget 字段：预算由 `run_service.ParagraphRun` 造一份共享 `LoopBudget` 传入。
A 的 `TaskSpec.budget: LoopBudgetSpec` 是可声明式的，且两套概念层 spec 各自声明不同预算
（map：12 请求/16 工具/300s；locate：5 请求/6 工具/120s），`tools/concept/check_concept_flow.py:253`
会读 `spec.budget.max_requests` 作为自检断言。

**决定（用户选 A）**：把 `LoopBudgetSpec` 与可选 `TaskSpec.budget` 补回 `agent/engine.py`，
`AgentTask.budget` 参数改为可选，缺省时按 `spec.budget` → `config` 构造。段落路径始终显式传入共享
budget，行为零变化。

## 4. 架构与改写规则

落位后 `ECUST_Gnosys/backend/agent/` 的结构：

```text
agent/
  book.py            书籍/段落数据与句子切分（split_sentences 为唯一分句实现）
  config.py          环境配置
  engine.py          Agents SDK 执行、TaskSpec/ToolSpec/LoopBudgetSpec、预算与取消
  tasks.py           段落（L3）任务定义、提示词、校验
  tools.py           book-reading 技能工具（7 个）
  runs.py            批次调度与取消
  run_service.py     单段流程、运行状态与结果持久化
  run_api.py         Agent HTTP 接口
  index.py           L0 术语提名与索引（纯统计，零模型）
  selection.py       L4 输入拦截：划词 → 规范 (term, 位置)
  concept_tools.py   L4 概念层技能与工具（concept-mapping / concept-reading）
  concept.py         L4 任务定义（map / locate）
  outline.py         L3 结构层：章-节-子节识别与自适应上下文窗口
  skills/
    book-reading/SKILL.md
    concept-mapping/SKILL.md
    concept-reading/SKILL.md
```

三条统一改写规则：

1. **import 改包路径**：`agent_book` → `agent.book`、`agent_config` → `agent.config`、
   `schema` → `db.schema`、`book_service` → `reader.book_service`、
   `agent_engine.spec` / `agent_core` → `agent.engine`。
2. **工具从 dict 改 `ToolSpec`**：A 的 `_tool(name, desc, props, required)` 返回 OpenAI schema 字典，
   handlers 单独放在 `raw["handlers"]` 字典里；B 的 `tools.py` 让 `_tool(...)` 直接返回 `ToolSpec`，
   handler 是同一作用域里的 async 闭包。概念层照 B 的形状改。
3. **技能装配返回 `SkillSpec`**：A 的 `agent_skills_concept.skill_specs()` 做 dict→ToolSpec 转换后返回
   `[SkillSpec]`；B 里这一步由 `tools.book_reading_tools()` 完成，`tasks.skills()` 直接取用。
   概念层对应写成 `concept_tools.concept_skills(book_id, ledger, mode) -> list[SkillSpec]`，
   技能目录用 `SKILLS_DIR = Path(__file__).resolve().parent / "skills"`（与 `tasks.py` 同约定）。

`ROOT_DIR`：A 的 `agent_config.ROOT_DIR = Path(__file__).resolve().parents[1]`（backend 的上一级 = 仓根）；
B 的 `config.py` 同样在 `backend/agent/` 下一层，`parents[1]` 仍是 `ECUST_Gnosys/`，语义不变。

## 5. engine 扩展的具体形态

```python
@dataclass
class LoopBudgetSpec:
    """任务级预算声明。AgentTask 未收到显式 LoopBudget 时用它构造。"""

    max_requests: int = 12
    max_tool_calls: int = 20
    timeout_seconds: float = 300.0
    request_limit: int | None = None   # 本阶段的请求上限（多阶段共享预算时用）


@dataclass
class TaskSpec:
    ...
    request_limit: int | None = None          # 既有字段，保持不变
    budget: LoopBudgetSpec | None = None      # 新增，可选
```

`AgentTask.__init__` 的 `budget: LoopBudget` → `budget: LoopBudget | None = None`：

```python
if budget is not None:
    self.budget = budget
elif spec.budget is not None:
    self.budget = LoopBudget(spec.budget.max_requests, spec.budget.max_tool_calls,
                             time.monotonic() + spec.budget.timeout_seconds)
else:
    self.budget = LoopBudget(config.max_requests, config.max_tool_calls,
                             time.monotonic() + config.run_timeout)
```

`run()` 里解析阶段请求上限（约 `engine.py:403-406`）：

```python
limit = self.spec.request_limit
if limit is None and self.spec.budget is not None:
    limit = self.spec.budget.request_limit
if limit is not None:
    max_turns = min(max_turns, limit)
```

`config` 为 `None` 时退化为 `12 / 20 / 300`（与 `LoopBudgetSpec` 默认值一致）。

## 6. 数据与迁移

1. `db/schema.py` 的 `SCHEMA_SQL` 追加四张表 DDL（照抄 A），并追加
   `_DROPPED_TABLES`、`_drop_unusable_tables()`、`_ensure_columns()`；
   `get_db()` 在 `_migrate_layer_tables()` 之后依次调用二者。
2. `reader/import_epub.py` 补 `_extract_headings()`，INSERT 章节时写入 `chapters.heading_json`。
3. 一次性回填：先 `uv run tools/platform/backfill_heading_levels.py` 出报告，备份 `book.db` 后
   `--apply`。txt 源没有 `<h1>-<h6>`，补不了，靠 `agent/outline.py` 的文本行模式兜底 —— 这是预期。

## 7. 验证

零模型成本、可重复：

| 命令 | 判据 |
|---|---|
| `uv run tools/structure/check_outline.py` | 层级树正确，`book_outline` 自检通过 |
| `uv run tools/concept/build_term_index.py` | 术语提名与索引在真书上跑通 |
| `uv run tools/concept/check_concept_flow.py` | 拦截 → 材料 → 工具 → 校验 → TaskSpec 全 ✅ |
| `uv run tools/semantic/check_semantic_para.py "<书目录>"` | 语义层数据对账全 ✅（分句与后端一致） |
| `uv run tools/platform/check_chapter_units.py` | 章节单元校验通过 |
| 首次连接旧库 | stderr 出现 `[schema] 丢弃不可用的旧表：concept_occurrences` 与 `[schema] 补列：concept_sense.aliases_json` |
| `uv run server.py` | 书库页/阅读页可打开，段落解释链路无回归 |

含模型的一次性验证：`uv run tools/agent/check_agent_endpoint.py`（本机无密钥时跳过，不作为验收条件）。

## 8. 风险与回滚

| 风险 | 处置 |
|---|---|
| `DROP TABLE concept_occurrences` 不可逆 | 回填前复制 `books/*/book.db` 备份；该表无数据，丢失面为零 |
| 改 `split_sentences` 影响已发布的 L3 解释对齐 | `tools/semantic/check_semantic_para.py` 逐条对账，作为回归闸门 |
| 改 SKILL.md 改变 `skill_version` | 只影响新 run 的指纹比较，不阻断既有数据 |
| 移植后 import 断链导致服务起不来 | 每个模块落位后立即跑对应 check 脚本，`server.py` 冒烟兜底 |

## 9. 交付方式

分支 `merge/reader3-agent`（自 `合并旧版新增功能` 分出），按系统分批提交，主题与 A 对齐：

1. `docs: reader3 agent 合并规格与实现计划`
2. `feat(schema): L4 概念层数据表，丢弃坐标系不符的 concept_occurrences`
3. `fix(入库): 章节层级写入 heading_json`
4. `fix(分句): 分句器统一到 agent.book.split_sentences`
5. `feat(tools): 补 read_layer_note / read_glossary / 子词兜底 / 省略号核验`
6. `feat(engine): TaskSpec 可声明 LoopBudgetSpec`
7. `feat(L0): 录入期术语提名与索引`
8. `feat(L4): 输入拦截 selection_gate`
9. `feat(L4): 概念层工具、任务定义与两套技能规程`
10. `feat(L3): 章-节-子节层级识别与自适应上下文窗口`
11. `chore(tools): 迁入分区自检脚本`
12. `docs: 概念层设计文档与 README 目录结构`
13. `chore(books): 书库随 schema 迁移与 heading 回填更新`

完成后合并进 `main` 并推送 `origin/main`。
