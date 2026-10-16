# reader3 Agent 功能合并进 ECUST_Gnosys 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把 reader3（`D:/Programs/reader3/ECUST_Reader3`，HEAD `100b9dc`）领先的 Agent 能力合并进 `ECUST_Gnosys/`，按后者既有的包结构与 `ToolSpec` 惯用法落位。

**架构：** reader3 是平铺模块 + OpenAI dict 工具；ECUST_Gnosys 是 `backend/{agent,reader,db,api}` 包化结构 + `ToolSpec`/闭包工具 + `run_service` 三段拆分。合并方式是「保形移植」：逻辑照搬 reader3，接口改写成 ECUST_Gnosys 的形状，段落链路行为零变化。

**技术栈：** Python 3.10+ / uv / FastAPI / SQLite / openai-agents（Agents SDK）。

**规格：** `docs/superpowers/specs/2026-10-17-reader3-agent-merge-design.md`

---

## 文件结构

**新建**

| 路径 | 职责 |
|---|---|
| `ECUST_Gnosys/backend/agent/selection.py` | L4 输入拦截：脏划词 → 规范 `(term, 位置)` |
| `ECUST_Gnosys/backend/agent/index.py` | L0 术语提名与索引（纯统计，零模型） |
| `ECUST_Gnosys/backend/agent/concept_tools.py` | L4 技能与工具，产出 `list[SkillSpec]` |
| `ECUST_Gnosys/backend/agent/concept.py` | L4 任务定义（map / locate）与校验 |
| `ECUST_Gnosys/backend/agent/outline.py` | L3 章-节-子节层级识别与自适应窗口 |
| `ECUST_Gnosys/backend/agent/skills/concept-mapping/SKILL.md` | 建图技能规程 |
| `ECUST_Gnosys/backend/agent/skills/concept-reading/SKILL.md` | 定位技能规程 |
| `ECUST_Gnosys/tools/{agent,concept,platform,semantic,structure}/*.py` | 分区自检脚本（7 个） |
| `ECUST_Gnosys/docs/概念层_设计_v1.md` | 概念层设计与实施现状 |
| `ECUST_Gnosys/docs/superpowers/` | 本规格与计划 |

**修改**

| 路径 | 改动 |
|---|---|
| `backend/db/schema.py` | 追加四张表 DDL + `_DROPPED_TABLES` / `_drop_unusable_tables` / `_ensure_columns` |
| `backend/reader/import_epub.py` | 补 `_extract_headings()` 并写 `chapters.heading_json` |
| `backend/agent/book.py` | 补 `_is_sentence_end()` / `split_sentences()` |
| `backend/agent/tasks.py` | `sentence_count()` 改用 `split_sentences()` |
| `backend/agent/tools.py` | 补 `read_layer_note` / `read_glossary`；search 子词兜底；verify 省略号分段 |
| `backend/agent/engine.py` | 补 `LoopBudgetSpec` + 可选 `TaskSpec.budget`；`AgentTask.budget` 可选 |
| `backend/agent/skills/book-reading/SKILL.md` | 补 `read_layer_note` 与 `expanded` 两条说明 |
| `frontend/templates/reader.html` | 补 `isSentenceEnd()` 并使 `SENT_END` 不再含 ASCII 句点 |
| `README.md` | 目录结构补新模块与 `tools/` |
| `books/*/book.db` | schema 迁移 + `heading_json` 回填 |

---

### 任务 1：规格与计划文档

**文件：**
- 创建：`docs/superpowers/specs/2026-10-17-reader3-agent-merge-design.md`
- 创建：`docs/superpowers/plans/2026-10-17-reader3-agent-merge.md`

- [ ] **步骤 1：写规格**（差异清单、改写规则、engine 扩展、迁移、验证、风险）
- [ ] **步骤 2：规格自检**：无占位符、章节无矛盾、范围可用一个计划覆盖、无二义
- [ ] **步骤 3：写本计划**，含每个任务的落点、命令与判据
- [ ] **步骤 4：Commit**

```bash
git add docs/superpowers
git commit -m "docs: reader3 agent 合并规格与实现计划"
```

### 任务 2：schema 四表与迁移

**文件：**
- 修改：`ECUST_Gnosys/backend/db/schema.py`

- [ ] **步骤 1：抄入四张表 DDL**

从 A 的 `backend/schema.py:149-162` 起抄 `concept_occurrence`、`concept_sense`、`term_candidate`、
`term_scan` 的 `CREATE TABLE` / `CREATE INDEX`，追加到 B 的 `SCHEMA_SQL` 末尾（`agent_notes` 之后）。

- [ ] **步骤 2：抄入迁移函数**

A 的 `_DROPPED_TABLES`、`_drop_unusable_tables()`、`_ensure_columns()` 原样落在
B 的 `_table_ddl()` 之后、`_migrate_layer_tables()` 之前。

- [ ] **步骤 3：接线 `get_db()`**

```python
conn.executescript(SCHEMA_SQL)
_migrate_layer_tables(conn)   # 旧骨架库（层表字段不同）按当前定义重建
_drop_unusable_tables(conn)   # 坐标系不对的旧表（concept_occurrences）
_ensure_columns(conn)         # 后加的列（concept_sense.aliases_json）
conn.commit()
```

- [ ] **步骤 4：验证迁移在真库上生效**

运行：`cd ECUST_Gnosys && uv run python -c "import sys;sys.path.insert(0,'backend');from db.schema import get_db,close_db;import glob;d=sorted(glob.glob('books/*/book.db'))[0];db=get_db(d);print([r[0] for r in db.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'concept%'\")]);close_db(d)"`

预期：列出 `concept_layer, concept_occurrence, concept_sense`，且 stderr/输出里有
`[schema] 丢弃不可用的旧表：concept_occurrences`。

- [ ] **步骤 5：Commit**

```bash
git add ECUST_Gnosys/backend/db/schema.py
git commit -m "feat(schema): L4 概念层数据表，丢弃坐标系不符的 concept_occurrences"
```

### 任务 3：入库写入章节层级

**文件：**
- 修改：`ECUST_Gnosys/backend/reader/import_epub.py`

- [ ] **步骤 1：加 `_extract_headings()`**（A `backend/reader3.py:175-194` 原样，import 无需改）
- [ ] **步骤 2：改 INSERT**：`'[]'` → `_extract_headings(final_html)`，并把 A 的注释一并换上
- [ ] **步骤 3：验证**：跑 `tools/platform/backfill_heading_levels.py`（任务 7 落地后）报告「已有/补上/无标题可补」三项
- [ ] **步骤 4：Commit**

```bash
git add ECUST_Gnosys/backend/reader/import_epub.py
git commit -m "fix(入库): 章节层级写入 heading_json"
```

### 任务 4：分句器统一

**文件：**
- 修改：`ECUST_Gnosys/backend/agent/book.py`（补 45 行：`_SENT_END_HARD`、`_SENT_TAIL`、`_is_sentence_end`、`split_sentences`）
- 修改：`ECUST_Gnosys/backend/agent/tasks.py`（`sentence_count` 改用 `split_sentences`）
- 修改：`ECUST_Gnosys/frontend/templates/reader.html`（`isSentenceEnd`）

- [ ] **步骤 1：把 A `agent_book.py` 的四个分句定义原样抄进 `agent/book.py`**
- [ ] **步骤 2：`tasks.py` 的 `sentence_count()` 改写成 A 的版本**

```python
def sentence_count(text: str) -> int:
    return len(split_sentences(text))
```

（A 的实际写法以 A `agent_tasks/paragraph.py:25-39` 为准，逐行照抄。）

- [ ] **步骤 3：`reader.html`** 用 A 的 `SENT_END = /[。！？!?]/`、`TAIL`、`isSentenceEnd()` 替换旧的三行
- [ ] **步骤 4：验证**：`uv run tools/semantic/check_semantic_para.py "<精神现象学书目录>"` 全 ✅
- [ ] **步骤 5：Commit**

```bash
git add ECUST_Gnosys/backend/agent/book.py ECUST_Gnosys/backend/agent/tasks.py ECUST_Gnosys/frontend/templates/reader.html
git commit -m "fix(分句): 分句器统一到 agent.book.split_sentences"
```

### 任务 5：补齐 book-reading 工具集

**文件：**
- 修改：`ECUST_Gnosys/backend/agent/tools.py`
- 修改：`ECUST_Gnosys/backend/agent/skills/book-reading/SKILL.md`

- [ ] **步骤 1：补 `_sub_terms()` 与 search 的子词兜底**（A `agent_skills.py:60-77`、search handler 的 `expanded` 分支）
- [ ] **步骤 2：补 `read_layer_note` handler 与注册**（A `agent_skills.py:210-238`、289-315）
- [ ] **步骤 3：补 `read_glossary` handler 与注册**（A `agent_skills.py:239-262`），import 用 `from agent.index import glossary_entries`
- [ ] **步骤 4：补 `verify_quote` 的省略号分段核验**（A `agent_skills.py:263-286`）
- [ ] **步骤 5：SKILL.md 补两条**（`read_layer_note` 段、`search_book_text` 子词兜底段），文字照抄 A
- [ ] **步骤 6：验证**：`uv run tools/agent/check_agent_endpoint.py` 里的工具清单与实现的 7 个工具名一致（无密钥时只核对清单，不发请求）
- [ ] **步骤 7：Commit**

```bash
git add ECUST_Gnosys/backend/agent/tools.py ECUST_Gnosys/backend/agent/skills/book-reading/SKILL.md
git commit -m "feat(tools): 补 read_layer_note / read_glossary / 子词兜底 / 省略号核验"
```

### 任务 6：engine 预算字段

**文件：**
- 修改：`ECUST_Gnosys/backend/agent/engine.py`

- [ ] **步骤 1：加 `LoopBudgetSpec`**（第三十行附近，`LoopBudget` 之后）
- [ ] **步骤 2：`TaskSpec` 加 `budget: LoopBudgetSpec | None = None`**
- [ ] **步骤 3：`AgentTask.__init__` 的 `budget` 改为 `LoopBudget | None = None`**，按 §5 三分支构造
- [ ] **步骤 4：`run()` 的 `request_limit` 解析**兼容 `spec.budget.request_limit`
- [ ] **步骤 5：验证**：`uv run server.py` 起得来；`python -c` 构造一次 `TaskSpec(layer=..., prompt=..., materials=..., submit=...)` 不报错
- [ ] **步骤 6：Commit**

```bash
git add ECUST_Gnosys/backend/agent/engine.py
git commit -m "feat(engine): TaskSpec 可声明 LoopBudgetSpec"
```

### 任务 7：L0 术语索引

**文件：**
- 创建：`ECUST_Gnosys/backend/agent/index.py`（A `backend/agent_index.py` 370 行）

- [ ] **步骤 1：整文件复制**，只改 import：`agent_book` → `agent.book`、`agent_config` → `agent.config`
- [ ] **步骤 2：确认 `ROOT_DIR` 语义**：A 的 `parents[1]`（backend 上一级）在 B 里同样指向 `ECUST_Gnosys/`
- [ ] **步骤 3：验证**：`uv run tools/concept/build_term_index.py` 在真书上跑通
- [ ] **步骤 4：Commit**

```bash
git add ECUST_Gnosys/backend/agent/index.py
git commit -m "feat(L0): 录入期术语提名与索引"
```

### 任务 8：L4 输入拦截

**文件：**
- 创建：`ECUST_Gnosys/backend/agent/selection.py`（A `backend/selection_gate.py` 222 行）

- [ ] **步骤 1：整文件复制**（无内部 import 依赖，只需确认无 `agent_*` 引用）
- [ ] **步骤 2：验证**：`uv run tools/concept/check_concept_flow.py` 的第 1 项（gate 收敛）通过
- [ ] **步骤 3：Commit**

```bash
git add ECUST_Gnosys/backend/agent/selection.py
git commit -m "feat(L4): 输入拦截 selection_gate"
```

### 任务 9：L4 概念层工具、任务与技能

**文件：**
- 创建：`ECUST_Gnosys/backend/agent/concept_tools.py`（A `backend/agent_skills_concept.py`）
- 创建：`ECUST_Gnosys/backend/agent/concept.py`（A `backend/agent_tasks/concept.py`）
- 创建：`ECUST_Gnosys/backend/agent/skills/concept-mapping/SKILL.md`
- 创建：`ECUST_Gnosys/backend/agent/skills/concept-reading/SKILL.md`

- [ ] **步骤 1：复制两个 SKILL.md** 并确认它们在 `agent/skills/` 下
- [ ] **步骤 2：`concept_tools.py`**
  - `from agent.book import ...`、`from agent.index import ...`（若用到 glossary）
  - `SKILLS_DIR = Path(__file__).resolve().parent / "skills"`
  - `_tool(...)` 改为返回 `ToolSpec`（照 `tools.py` 的 `_tool` 签名：多一个 `handler` 位置参数）
  - `book_reading_skill(...)` → `book_reading_tools(book_id, ledger)`（B 的工具集函数名）
  - `concept_skill(...)` 的 `tools` 直接用 `ToolSpec` 列表，删掉 `handlers` 字典与 `wanted` 过滤
  - `skill_specs(book_id, ledger, mode)` → `concept_skills(book_id, ledger, mode) -> list[SkillSpec]`，
    返回 `[SkillSpec(name, description, instructions, tools)]`
- [ ] **步骤 3：`concept.py`**
  - `from agent.book import ...`、`from agent.engine import LoopBudgetSpec, SubmitSpec, TaskSpec`、
    `from agent.concept_tools import concept_skills`
  - `skill_specs(...)` 调用点改 `concept_skills(...)`
- [ ] **步骤 4：验证**：`uv run tools/concept/check_concept_flow.py` 全 ✅，5 项断言逐条通过
- [ ] **步骤 5：Commit**

```bash
git add ECUST_Gnosys/backend/agent/concept_tools.py ECUST_Gnosys/backend/agent/concept.py ECUST_Gnosys/backend/agent/skills/concept-mapping ECUST_Gnosys/backend/agent/skills/concept-reading
git commit -m "feat(L4): 概念层工具、任务定义与两套技能规程"
```

### 任务 10：L3 结构层

**文件：**
- 创建：`ECUST_Gnosys/backend/agent/outline.py`（A `backend/book_outline.py` 264 行）

- [ ] **步骤 1：整文件复制**，import 改 `from agent.book import ...`
- [ ] **步骤 2：保留文件尾的 `assert` 自检**（A 用它做单文件自检）
- [ ] **步骤 3：验证**：`uv run python backend/agent/outline.py` 打印 `book_outline 自检通过`
- [ ] **步骤 4：Commit**

```bash
git add ECUST_Gnosys/backend/agent/outline.py
git commit -m "feat(L3): 章-节-子节层级识别与自适应上下文窗口"
```

### 任务 11：tools 分区自检脚本

**文件：**
- 创建：`ECUST_Gnosys/tools/agent/check_agent_endpoint.py`
- 创建：`ECUST_Gnosys/tools/concept/{build_term_index.py,check_concept_flow.py,clean_concept_placeholder.py}`
- 创建：`ECUST_Gnosys/tools/platform/{backfill_heading_levels.py,check_chapter_units.py,rebuild_chapter_paragraphs.py}`
- 创建：`ECUST_Gnosys/tools/semantic/{check_semantic_para.py,run_batch.py}`
- 创建：`ECUST_Gnosys/tools/structure/check_outline.py`
- 删除：`ECUST_Gnosys/tools/{check_chapter_units.py,check_semantic_para.py,rebuild_chapter_paragraphs.py}`（旧平铺位置）

- [ ] **步骤 1：复制 `tools/` 整个目录树**（连同 A 的 `README.md` 说明）
- [ ] **步骤 2：改内部 import**：`ROOT = Path(__file__).resolve().parents[2]` 保持（`tools/<系统>/x.py` → 仓根）；
  `sys.path.insert(0, str(ROOT / "backend"))` 保持；模块名改包路径（`agent_book` → `agent.book`、
  `reader3` → `reader.import_epub`、`schema` → `db.schema`、`agent_skills` → `agent.tools`、
  `agent_tasks` → `agent.concept`、`agent_skills_concept` → `agent.concept_tools`、
  `selection_gate` → `agent.selection`、`book_outline` → `agent.outline`）
- [ ] **步骤 3：`tools/README.md`** 若有，同步路径说明
- [ ] **步骤 4：验证**：逐个脚本 `uv run tools/<系统>/<脚本>.py` 全绿
- [ ] **步骤 5：Commit**

```bash
git add ECUST_Gnosys/tools
git commit -m "chore(tools): 迁入分区自检脚本"
```

### 任务 12：文档与 README

**文件：**
- 创建：`ECUST_Gnosys/docs/概念层_设计_v1.md`（A `docs/概念层_设计_v1.md` 666 行，原样）
- 修改：`ECUST_Gnosys/README.md`

- [ ] **步骤 1：复制设计文档**
- [ ] **步骤 2：README 目录结构**补 `agent/` 新模块、`tools/`、`docs/概念层_设计_v1.md`；
  「Agent 执行流程」段落补一句概念层与结构层当前状态（有代码、未接运行 API）
- [ ] **步骤 3：Commit**

```bash
git add ECUST_Gnosys/docs/概念层_设计_v1.md ECUST_Gnosys/README.md
git commit -m "docs: 概念层设计文档与 README 目录结构"
```

### 任务 13：书库迁移与全量验证

- [ ] **步骤 1：备份**
```bash
cd ECUST_Gnosys && for d in books/*/book.db; do cp "$d" "$d.bak-merge"; done && ls books/*/*.bak-merge
```
- [ ] **步骤 2：回填章节层级（先报告）**：`uv run tools/platform/backfill_heading_levels.py`
- [ ] **步骤 3：回填（写入）**：`uv run tools/platform/backfill_heading_levels.py --apply`
- [ ] **步骤 4：schema 迁移触发并核对输出**：任选一本书 `uv run python -c "...get_db(...)"`，看到丢表与补列两行
- [ ] **步骤 5：跑全部零模型自检**

```bash
uv run python backend/agent/outline.py
uv run tools/structure/check_outline.py
uv run tools/concept/build_term_index.py
uv run tools/concept/check_concept_flow.py
uv run tools/semantic/check_semantic_para.py "<精神现象学书目录>"
uv run tools/platform/check_chapter_units.py
```

- [ ] **步骤 6：服务器冒烟**：`uv run server.py` → `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8123/` 得 200；
  打开一本书的章节页 200；`curl` 一次 `/agent/<book>/runs/pending` 不 500
- [ ] **步骤 7：备份文件不入库**：确认 `.gitignore` 已忽略 `*.bak-merge`，或移动出仓库
- [ ] **步骤 8：Commit 书库变化**

```bash
git add ECUST_Gnosys/books
git commit -m "chore(books): 书库随 schema 迁移与 heading 回填更新"
```

### 任务 14：合并 main 并推送

- [ ] **步骤 1：确认分支干净**：`git status --short` 只剩预期未跟踪项
- [ ] **步骤 2：合并**：`git checkout main && git merge --no-ff merge/reader3-agent -m "merge: reader3 新增 Agent 功能合并进 ECUST_Gnosys"`
- [ ] **步骤 3：推送**：`git push origin main`
- [ ] **步骤 4：核对**：`git log --oneline -3 origin/main` 含 merge 提交，`git status -sb` 显示与 origin/main 同步

---

## 自检

**1. 规格覆盖度**

| 规格章节 | 对应任务 |
|---|---|
| §3.1 第 1、2、8 条（分句） | 任务 4 |
| §3.1 第 3–7 条（工具与 SKILL） | 任务 5 |
| §3.2 selection / index / concept_* / outline / tools / docs | 任务 7、8、9、10、11、12 |
| §3.3 数据层 | 任务 2、3、13 |
| §3.4 engine 扩展 | 任务 6 |
| §7 验证 | 任务 13 |
| §9 交付方式 | 任务 14 |

**2. 占位符扫描**：无「待定 / TODO / 后续实现」；每个代码步骤都给了落点行号或确切改法。

**3. 类型一致性**：`LoopBudgetSpec`（任务 6 定义）在任务 9 的 `concept.py` 被引用；
`concept_skills`（任务 9 定义）与 `concept.py` 的调用点同名；`book_reading_tools`（B 既有）在任务 9 复用；
`agent.index.glossary_entries`（任务 7 定义）在任务 5 的 `read_glossary` 被引用 —— 因此**任务 5 与任务 7
存在循环依赖**：任务 5 的 `read_glossary` 需要 `agent/index.py`。实现顺序调整为 7 → 5（先索引后工具），
或任务 5 的 `read_glossary` 分步落地。实际执行按 **2 → 3 → 7 → 4 → 5 → 6 → 8 → 9 → 10 → 11 → 12 → 13 → 14**。
