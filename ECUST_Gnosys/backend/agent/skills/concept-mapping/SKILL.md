# concept-mapping

本技能只讲**工具怎么用**。判断标准（义项怎么分、范围怎么写、侧重何时写）在任务提示里。

## 工具与用法

| 工具 | 用法要点 |
|---|---|
| `find_term_occurrences` | 只给**分布 + 抽样**，不是全部出现。`distribution` 是每章计数 —— 疏密断层是分簇线索；`samples` 是均匀抽样，不要把它当全量 |
| `find_definition_candidates` | **召回候选，不是判定**。它扫「出现段 ± `window`」并对锚点词机械打分；返回里的 `note` 会再提醒一次。`chapter_range` 按你对"是否全书一致"的判断给 |
| `read_paragraphs_at` | 一次最多 4 段完整文本。做比较/代回测试前必须用它读完整段，摘要不够 |
| `read_concept_entry` | 读该词已有义项与别名。已有编号不要重造 |
| `read_glossary` | 查这个词是不是本书规范里的术语。返回的是书目知识，不是原文证据 |
| `search_book_text` / `read_book_paragraph` | 补读用。`search` 命中里 `expanded: true` 表示是**子词**匹配（「自在自为」→「自在」），不是整词逐字出现 |
| `verify_source_quote` | 引文核验。带省略号的引文会**分段核验**；有不命中项就改用自己的话，不要反复重试 |

## 工具结果的读法

- **材料账本**：只有读原文的工具会记账，`verify_source_quote` 与最终校验**只认账本**。没读过的原文不能引用。
- `locate_term_context` 的 `note_state: null`（或 `read_layer_note` 的 `available: false`）= 该层还没有内容，是正常降级，不是错误。
- `existing_senses` 为空 = 这是第一次为这个词建图，正常。

## 红线

- 引文逐字取自**本次已读原文**；核验不过就改用自己的话。
- 不许上网、不许看别的书、不许引用没读过的内容。
- 同一工具同一参数不要重复调用；能交出合规结果就立刻交卷。
