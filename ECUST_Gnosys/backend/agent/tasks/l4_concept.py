"""L4 概念层的任务定义（建图 / 定位两套）。

设计依据：docs/概念层_设计_v1.md §5（终止标准）、§9（维度与侧重）、§12.6（任务与预算）。

只描述三件事：给模型什么（materials）、要它交什么（submit + schema）、怎么算合格（validator）。
调度与持久化不在这里。

主线是「为每个术语维护一张意义地图」，所以两套任务的**判断目标不同**：
  map_spec    （后台、可缓存）建图：有哪些义项、各自管到哪、有哪些维度、按文本怎么演化
  locate_spec （前台、要快）  定位：当前位置读的是哪个义项、侧重哪一面；证据不足就上报
"""

import json

from agent.tools.book import book_db, get_paragraph, logical_paragraphs, normalize_text
from agent.runtime.task import LoopBudgetSpec, SubmitSpec, TaskSpec
from agent.tools.concept import concept_skills

READING_MAX = 200          # 弹窗长度上限（待实测调整）：只约束单条，条目数不限（前端折叠）
DEFINITION_NOTE_MAX = 600
DIM_NAME_MAX = 10
WHY_MAX = 60
MAP_REQUEST_LIMIT = 10
LOCATE_REQUEST_LIMIT = 4

DEF_KINDS = ("explicit", "inferred", "external")
CONFUSIONS = ("translation", "idiom_shift", "anachronism", "author_reversal", "author_stipulation")
SCOPES = ("book", "chapter", "paragraph")
STANCES = ("normal", "boundary")
REASON_KINDS = ("explicit_marker", "misreading_risk", "minority_usage", "argumentative_need")

MAP_FIELDS = ("has_definition", "definition_note", "confusion_types", "senses", "quotes")
SENSE_FIELDS = ("reading", "common_reading", "scope", "range_note", "evidence",
                "evolves_to_next", "distinction_note", "dimensions", "emphasis_here")
READING_FIELDS = ("reading", "common_reading", "emphasis_here", "stance", "quotes", "needs_mapping")


# ── 共用校验 ────────────────────────────────────────────

def _check_quote(quote: str, sources: list[str]) -> None:
    """引文必须非空且逐字出现在本次已读原文中。"""
    if not (quote or "").strip():
        raise ValueError("引文不能为空")
    if not any(quote in source for source in sources):
        raise ValueError(f"引文未在本任务已读取的原文中逐字找到：{quote[:60]}"
                         "（核验不通过就改用自己的话复述，或去掉引号）")


def _check_position(book_id: str, ci: int, pi: int) -> None:
    """证据位置必须是本章真实存在的段落（章/段下标均为非负整数）。"""
    if not isinstance(ci, int) or not isinstance(pi, int) or ci < 0 or pi < 0:
        raise ValueError("位置必须是 chapter_idx / paragraph_index 两个非负整数")
    _, paragraphs, _ = logical_paragraphs(book_id, ci)
    if pi >= len(paragraphs):
        raise ValueError(f"该位置不存在：第 {ci} 章第 {pi} 段（本章共 {len(paragraphs)} 段）")


def _check_emphasis(emphasis, sources: list[str]) -> None:
    """侧重：默认沉默；一旦出现就必须给理由和可核证据（设计 §9.3）。"""
    if emphasis in (None, {}, []):
        return
    if not isinstance(emphasis, dict):
        raise ValueError("emphasis_here 必须是对象或空")
    for key in ("dimension", "why", "reason_kind", "evidence"):
        if not emphasis.get(key):
            raise ValueError(f"写了侧重就必须给全 dimension/why/reason_kind/evidence（缺 {key}）")
    if emphasis["reason_kind"] not in REASON_KINDS:
        raise ValueError(f"reason_kind 必须是 {'/'.join(REASON_KINDS)}")
    if len(emphasis["why"]) > WHY_MAX:
        raise ValueError(f"侧重理由超过 {WHY_MAX} 字，请压缩成一句话")
    for item in emphasis["evidence"]:
        _check_quote(item.get("quote", ""), sources)


def _check_dimensions(dimensions) -> None:
    """维度可省略；一旦给出，每项必须有 name 且不超过弹窗宽度。"""
    if dimensions in (None, [], {}):
        return
    if not isinstance(dimensions, list):
        raise ValueError("dimensions 必须是数组")
    for dim in dimensions:
        name = (dim or {}).get("name", "")
        if not name:
            raise ValueError("dimensions 每项都要有 name")
        if len(name) > DIM_NAME_MAX:
            raise ValueError(f"维度名超过 {DIM_NAME_MAX} 字（弹窗放不下）：{name}")


def _evidence_chapters(evidence: list, sources: list[str], book_id: str) -> set:
    """校验每条证据并返回它覆盖的章集合（用于判断「全书一致」是否真有跨章依据）。

    引文要同时过账本与当前原文两道关：账本证明模型确实读过，原文比对防止引用已变更的文本。
    """
    chapters = set()
    for item in evidence:
        _check_position(book_id, item.get("chapter_idx"), item.get("paragraph_index"))
        _check_quote(item.get("quote", ""), sources)
        original = get_paragraph(book_id, item["chapter_idx"], item["paragraph_index"])["text"]
        _check_quote(item.get("quote", ""), [original])
        chapters.add(item["chapter_idx"])
    return chapters


# ── 建图：validator ─────────────────────────────────────

def validate_map(args: dict, sources: list[str], book_id: str,
                 term_chapter_count: int | None = None) -> dict:
    """建图结果的结构与语义不变量。

    核心约束：义项（含义不同）不能与维度（同一含义的不同侧面）混为一谈；
    scope=book 必须有跨两章证据（该词本就只在一章出现时豁免）；多义时演化链
    只能有一个终点、每项都要写 distinction_note 与 range_note，防止把同一含义
    拆成多个义项。判定好坏不在 validator，这里只查可枚举/可计数的事实。
    """
    if set(args) != set(MAP_FIELDS):
        raise ValueError("结果字段必须且只能包含：" + "、".join(MAP_FIELDS))
    if args["has_definition"] not in DEF_KINDS:
        raise ValueError(f"has_definition 必须是 {'/'.join(DEF_KINDS)}"
                         "（explicit=书中给出定义 / inferred=从使用中归纳 / external=当通行术语用）")
    if args["has_definition"] == "explicit" and not (args["definition_note"] or "").strip():
        raise ValueError("声称书中有明确定义时，必须用 definition_note 指出定义内容与位置")
    if len(args["definition_note"] or "") > DEFINITION_NOTE_MAX:
        raise ValueError(f"definition_note 超过 {DEFINITION_NOTE_MAX} 字")

    types = args["confusion_types"]
    if not isinstance(types, list) or not types or any(t not in CONFUSIONS for t in types):
        raise ValueError(f"confusion_types 必须是 {'/'.join(CONFUSIONS)} 组成的非空数组")

    senses = args["senses"]
    if not isinstance(senses, list) or not senses:
        raise ValueError("senses 不能为空（至少要给出当前位置读的是什么）")

    ends = 0
    for i, sense in enumerate(senses):
        if set(sense) != set(SENSE_FIELDS):
            raise ValueError(f"第 {i + 1} 个义项字段必须且只能包含：" + "、".join(SENSE_FIELDS))
        if not (sense["reading"] or "").strip() or not (sense["common_reading"] or "").strip():
            raise ValueError(f"第 {i + 1} 个义项的 reading 与 common_reading 都不能为空")
        if len(sense["reading"]) > READING_MAX:
            raise ValueError(f"第 {i + 1} 个义项 reading 超过 {READING_MAX} 字")
        if sense["scope"] not in SCOPES:
            raise ValueError(f"第 {i + 1} 个义项 scope 必须是 {'/'.join(SCOPES)}")
        if sense["scope"] != "book" and not (sense["range_note"] or "").strip():
            raise ValueError(f"第 {i + 1} 个义项范围不是全书时，必须用 range_note 说明它管到哪")
        if not isinstance(sense["evolves_to_next"], bool):
            raise ValueError("evolves_to_next 必须是布尔值")
        _check_dimensions(sense["dimensions"])
        _check_emphasis(sense["emphasis_here"], sources)

        chapters = _evidence_chapters(sense["evidence"] or [], sources, book_id)
        if not chapters:
            raise ValueError(f"第 {i + 1} 个义项必须给出原文证据")
        # 反偷懒：声称全书一致必须跨章两处证据；例外：该词本来就只出现在一章
        if sense["scope"] == "book" and len(chapters) < 2:
            if not (term_chapter_count is not None and term_chapter_count <= 1):
                raise ValueError(f"第 {i + 1} 个义项要判全书一致，必须给出跨两章的引文证据；"
                                 "证据不足就改判 chapter 或 paragraph")
        if len(senses) > 1 and not (sense["distinction_note"] or "").strip():
            raise ValueError(f"第 {i + 1} 个义项缺少 distinction_note：必须说明它与相邻义项"
                             "为何是不同含义，而不是同一个含义的不同维度")
        if not sense["evolves_to_next"]:
            ends += 1

    if len(senses) > 1:
        if ends != 1:
            raise ValueError("多个义项时演化链只能有一个终点（最后一个义项的 evolves_to_next=false）")
        if senses[-1]["evolves_to_next"]:
            raise ValueError("最后一个义项的 evolves_to_next 必须是 false")
        for i, sense in enumerate(senses):
            if not (sense["range_note"] or "").strip():
                raise ValueError(f"第 {i + 1} 个义项缺少 range_note：多义时必须逐个说明管到哪")

    for quote in args["quotes"]:
        _check_quote(quote, sources)
    return args


# ── 定位：validator ─────────────────────────────────────

def validate_reading(args: dict, sources: list[str]) -> dict:
    """定位结果的结构校验。

    needs_mapping=true 是合法出口：证据不足时允许只上报、不硬猜（此时不校验 reading），
    由系统转入后台建图。needs_mapping=false 则必须给出读法与日常读法。
    """
    if set(args) != set(READING_FIELDS):
        raise ValueError("结果字段必须且只能包含：" + "、".join(READING_FIELDS))
    if not isinstance(args["needs_mapping"], bool):
        raise ValueError("needs_mapping 必须是布尔值")
    if args["stance"] not in STANCES:
        raise ValueError(f"stance 必须是 {'/'.join(STANCES)}（boundary = 正处于义项过渡处）")
    _check_emphasis(args["emphasis_here"], sources)
    for quote in args["quotes"]:
        _check_quote(quote, sources)
    if args["needs_mapping"]:
        return args                                     # 证据不足，上报由系统建图，不算失败
    if not (args["reading"] or "").strip():
        raise ValueError("不能既不给读法也不上报证据不足（needs_mapping）")
    if len(args["reading"]) > READING_MAX:
        raise ValueError(f"reading 超过 {READING_MAX} 字")
    if not (args["common_reading"] or "").strip():
        raise ValueError("common_reading 不能为空（日常语言里这个词是什么意思）")
    return args


# ── 提示词 ─────────────────────────────────────────────

def map_prompt() -> str:
    """建图阶段的判定标准。

    ⚠ 这些规则**必须放 prompt**（每任务必读）。曾经写在 SKILL.md 里，而 load_skill 是模型
    自愿调的 —— 没调就永远看不到判定标准，只看得到摘要。技能只负责“工具怎么用”。
    """
    return ("你在为一部社科著作建立某个术语的「意义地图」。地图会被反复使用，"
            "决定读者之后每次划词看到的解释，所以宁可保守，也不要臆断。\n\n"
            "## 1. 先分清三件事（最常见的错误是把维度当义项）\n"
            "- 义项：含义不同的单元（“距离”作为社会形式）\n"
            "- 维度：同一义项内部的面（空间/社会/心理/审美）—— 写进 dimensions\n"
            "- 侧重：某一次使用中凸显的那一面 —— 只在门槛内写 emphasis_here\n"
            "一旦把维度拆成义项，后续所有划词都跟着错。\n\n"
            "## 2. 代回测试（三档）\n"
            "把 A 处的读法代回 B 处那句：\n"
            "- 成立且不矛盾 → 同一义项、同一维度（侧重可能不同）\n"
            "- 不成立但不矛盾，换个角度就通 → **同一义项、不同维度**，写进 dimensions，不得拆成义项\n"
            "- 不成立且互相矛盾 → 新义项\n\n"
            "## 3. 范围与演化\n"
            "- 每个义项都要给 scope（book/chapter/paragraph）；不是 book 就必须用 range_note 说明管到哪。\n"
            "- **填 book 必须给出跨两章的引文证据**（该词本来只出现在一章时除外）；给不出就老实填 chapter/paragraph。\n"
            "- 义项按首现位置排序，evolves_to_next 串成链，多义时只能有一个终点。\n"
            "- 多义时每个义项都要写 distinction_note：为何是不同含义而非不同维度。\n\n"
            "## 4. 侧重：默认沉默\n"
            "原则上不写。只有满足下列任一条才写 emphasis_here，并给 reason_kind 与引文：\n"
            "- explicit_marker（作者此处有元语言标记）/ misreading_risk（不指出会被读成别的意思）\n"
            "- minority_usage（与全书分布明显不同）/ argumentative_need（该句论点靠这个面成立）\n"
            "维度名 ≤10 字，理由 ≤60 字。\n\n"
            "## 5. 其余字段\n"
            "- has_definition：explicit（书中给出定义）/ inferred（从使用中归纳）/ external（书中无定义，当通行术语用）。"
            "填 explicit 要用 definition_note 指出内容与位置。\n"
            "- confusion_types（可多选）：translation / idiom_shift / anachronism / author_reversal / author_stipulation。"
            "填了 idiom_shift 时 common_reading 必须写出日常读法做对照。\n"
            "- 引文必须逐字取自本次已读原文。最后调用 submit_concept_map。")


def locate_prompt(has_map: bool) -> str:
    """定位阶段的判定标准（同上：必须放 prompt，不能藏在按需加载的技能里）。"""
    head = ("你在为读者解释他在阅读器里划出的那个词。输入里已经给了这一句、整段原文、"
            "邻近段落，以及该段的语义层解释。\n\n"
            "红线：语义层内容是上一层生成的解释，**只能当参考，不能当原文依据**；"
            "引文只能取自原文。\n\n")
    if has_map:
        head += ("## 你的任务：在地图上定位\n"
                 "该词已有意义地图（见 materials.map）：判断这一处读的是哪个义项、侧重哪一面，"
                 "**不要改动地图**。\n"
                 "若两个义项都说得通、这一处正处在过渡上 → stance 填 boundary，不要硬选一个"
                 "（这通常是最有教学价值的位置）。\n"
                 "若地图没有覆盖这一处（该义项的证据只落在别章，而这里读法说不通）→ "
                 "说明理由并 needs_mapping=true。\n")
    else:
        head += ("## 该词还没有意义地图\n"
                 "只能做局部判断：reading 必须能由这一句和邻近段落支撑。\n"
                 "若要看全书分布或作者的定义段落才能判断 → needs_mapping=true、reading 留空，"
                 "由系统后台建图。**不许硬猜**：猜错的解释会直接显示给读者，比暂时不给更糟。\n")
    return head + ("\n## 侧重：默认沉默\n"
                   "只有满足下列任一条才写 emphasis_here，并给 reason_kind 与引文：\n"
                   "- explicit_marker（作者此处有元语言标记）/ misreading_risk（不指出会被读成别的意思）\n"
                   "- minority_usage（与别处明显不同）/ argumentative_need（这一句的论点靠这个面成立）\n"
                   "维度名 ≤10 字，理由 ≤60 字。\n\n"
                   "## 输出\n"
                   "- reading：**这个词在这一句里**怎么读（≤200 字）。别写整段概括，那是语义层的活。\n"
                   "- common_reading：它在日常语言里是什么意思。\n"
                   "- stance：normal 或 boundary；needs_mapping：证据不足时 true。\n"
                   "- 引文逐字取自本次已读原文。最后调用 submit_concept_reading。")


# ── materials 组装（保证引文核验的账本不变量） ──────────

def locate_materials(book_id: str, term: str, ci: int, pi: int, ledger: dict,
                     map_senses: list | None = None, glossary_entry: str | None = None,
                     window: int = 1) -> dict:
    """定位材料。把本段与邻段原文写进 ledger —— 引文核验只认账本，这个不变量必须在这里保证。"""
    from agent.tools.book import split_sentences, text_hash
    item = get_paragraph(book_id, ci, pi)
    text = item["text"]
    sentences = split_sentences(text)
    idx = next((i for i, s in enumerate(sentences) if term in s), None)
    ledger.setdefault((ci, pi), []).append(text)
    neighbors = []
    for off in range(1, window + 1):
        for sign in (-1, 1):
            try:
                nb = get_paragraph(book_id, ci, pi + sign * off)
            except Exception:
                continue
            neighbors.append({"paragraph_index": pi + sign * off, "text": nb["text"]})
            ledger.setdefault((ci, pi + sign * off), []).append(nb["text"])
    db = book_db(book_id)
    row = db.execute("SELECT content, detail, sentences FROM semantic_layer "
                     "WHERE text_hash=? AND kind='para'", (text_hash(text),)).fetchone()
    semantic = None
    if row:
        sents = json.loads(row["sentences"] or "[]")
        semantic = {"short": row["content"], "detail": row["detail"], "sentences": sents,
                    "sentence_explanation": (sents[idx] if idx is not None and idx < len(sents) else ""),
                    "quality": "参考，不是原文依据"}
    materials = {
        "target_term": term,
        "position": {"chapter_idx": ci, "paragraph_index": pi,
                     "chapter_title": item["chapter_title"], "fingerprint": item["fingerprint"]},
        "sentence": sentences[idx] if idx is not None else "",
        "sentence_index": idx,
        "paragraph_text": text,
        "neighbors": neighbors,
        "semantic_layer": semantic,
    }
    if map_senses:
        materials["map"] = {"existing_senses": map_senses}
    if glossary_entry:
        materials["glossary_entry"] = glossary_entry
    return materials


def map_materials(term: str, ci: int, pi: int, total: int, distribution: list,
                  existing_senses: list | None = None) -> dict:
    """建图材料：术语、当前位置、全书分布、既有义项（修订时用于对照/避免重复）。"""
    return {"target_term": term,
            "position": {"chapter_idx": ci, "paragraph_index": pi},
            "book_occurrence": {"total": total, "distribution": distribution},
            "existing_map": {"senses": existing_senses or []}}


# ── TaskSpec ────────────────────────────────────────────

def map_spec(*, book_id: str, materials: dict, ledger: dict, term_chapter_count: int | None) -> TaskSpec:
    """建图 TaskSpec（后台、可缓存）：允许较长的探索与补读。

    预算按「质量优先」给：12 次请求 / 16 次工具调用 / 300 秒，
    但要靠 request_limit 约束单次交卷前的模型请求轮数。
    """
    sources = [chunk for chunks in ledger.values() for chunk in chunks]
    return TaskSpec(
        name="L4.map",
        prompt=map_prompt(),
        materials=materials,
        submit=SubmitSpec("submit_concept_map", "提交该术语的意义地图（义项、范围、维度）。",
                          _map_parameters(),
                          lambda args: validate_map(args, [chunk for chunks in ledger.values() for chunk in chunks], book_id, term_chapter_count),
                          list(MAP_FIELDS)),
        skills=concept_skills(book_id, ledger, "mapping"),
        allowed_skills=["concept-mapping"],
        budget=LoopBudgetSpec(max_requests=12, max_tool_calls=16,
                              timeout_seconds=300, request_limit=MAP_REQUEST_LIMIT),
    )


def locate_spec(*, book_id: str, materials: dict, ledger: dict, has_map: bool) -> TaskSpec:
    """定位 TaskSpec（前台、要快）：只做局部判断，预算明显收紧。

    5 次请求 / 6 次工具调用 / 120 秒：读本段与邻段、必要时查一次地图即可，
    证据不够就走 needs_mapping 上报，不在前台拖时间。
    """
    sources = [chunk for chunks in ledger.values() for chunk in chunks]
    return TaskSpec(
        name="L4.locate",
        prompt=locate_prompt(has_map),
        materials=materials,
        submit=SubmitSpec("submit_concept_reading", "提交该处该怎么读，或上报证据不足。",
                          _reading_parameters(), lambda args: validate_reading(args, [chunk for chunks in ledger.values() for chunk in chunks]),
                          list(READING_FIELDS)),
        skills=concept_skills(book_id, ledger, "locating"),
        allowed_skills=["concept-reading"],
        budget=LoopBudgetSpec(max_requests=5, max_tool_calls=6,
                              timeout_seconds=120, request_limit=LOCATE_REQUEST_LIMIT),
    )


def _map_parameters() -> dict:
    """建图交卷工具的入参形状（义项内部结构在此声明，模型不用猜）。"""
    evidence = {"type": "array", "items": {"type": "object", "properties": {
        "chapter_idx": {"type": "integer"}, "paragraph_index": {"type": "integer"},
        "quote": {"type": "string"}}}}
    sense = {"type": "object", "properties": {
        "reading": {"type": "string"}, "common_reading": {"type": "string"},
        "scope": {"type": "string"}, "range_note": {"type": "string"},
        "evolves_to_next": {"type": "boolean"}, "distinction_note": {"type": "string"},
        "evidence": evidence, "dimensions": {"type": "array"},
        "emphasis_here": {"type": "object"}}}
    return {"has_definition": {"type": "string"}, "definition_note": {"type": "string"},
            "confusion_types": {"type": "array", "items": {"type": "string"}},
            "senses": {"type": "array", "items": sense},
            "quotes": {"type": "array", "items": {"type": "string"}}}


def _reading_parameters() -> dict:
    """定位交卷工具的入参形状。"""
    return {"reading": {"type": "string"}, "common_reading": {"type": "string"},
            "emphasis_here": {"type": "object"}, "stance": {"type": "string"},
            "quotes": {"type": "array", "items": {"type": "string"}},
            "needs_mapping": {"type": "boolean"}}


async def generate(run, review=None):
    """L4 编排：优先用缓存地图做定位；地图缺失或覆盖不足时先建图再重新定位。

    缓存键是概念层的地图缓存，references 里带原文 fingerprint —— 原文变化即失效。
    建图后重新走一遍定位，是因为新地图可能改变「这一处读哪个义项」的判断。
    """
    from agent.tools.concept import _distribution
    term, ci, pi = run.target['term'], run.target['chapter_idx'], run.target['paragraph_index']
    dist = _distribution(run.book_id, term)
    cache_key = 'concept-map:' + term
    cached = run.db.execute('SELECT value_json FROM reader_state WHERE key=?', (cache_key,)).fetchone()
    mapping = json.loads(cached[0]) if cached and not review else None
    # Mapping cache carries source fingerprints; changed text invalidates it.
    if mapping and any(get_paragraph(run.book_id, r['chapter_idx'], r['paragraph_index'])['fingerprint'] != r['fingerprint'] for r in mapping['references']):
        mapping = None
    senses = mapping['map']['senses'] if mapping else None
    material = locate_materials(run.book_id, term, ci, pi, run.ledger, map_senses=senses)
    result = await run.execute('', {}, spec=locate_spec(book_id=run.book_id, materials=material, ledger=run.ledger, has_map=bool(mapping)))
    if not mapping or result['needs_mapping']:
        material = map_materials(term, ci, pi, dist['total'], dist['chapters'], senses)
        if review:
            material['revision_issues'] = review
        mapped = await run.execute('', {}, spec=map_spec(book_id=run.book_id, materials=material, ledger=run.ledger, term_chapter_count=len(dist['chapters'])))
        refs = []
        for sense in mapped['senses']:
            for r in sense['evidence']:
                refs.append({**r, 'fingerprint': get_paragraph(run.book_id, r['chapter_idx'], r['paragraph_index'])['fingerprint']})
        mapping = {'map': mapped, 'references': refs}
        run.candidate = {'content': '\n\n'.join(s['reading'] for s in mapped['senses']), 'sources': [{k: r[k] for k in ('chapter_idx', 'paragraph_index', 'quote')} for r in refs]}
        run.persist()
        material = locate_materials(run.book_id, term, ci, pi, run.ledger, map_senses=mapped['senses'])
        result = await run.execute('', {}, spec=locate_spec(book_id=run.book_id, materials=material, ledger=run.ledger, has_map=True))
    if result['needs_mapping']:
        raise ValueError('补读建图后仍无足够证据；请查看待核材料')
    run.mapping = mapping
    for r in mapping['references']:
        run.remember(r['chapter_idx'], [get_paragraph(run.book_id, r['chapter_idx'], r['paragraph_index'])])
    parts = [f"## {term}\n\n{result['reading']}", f"### 日常读法\n{result['common_reading']}",
             '### 定义与证据性质\n' + mapping['map']['has_definition'] + '\n' + (mapping['map']['definition_note'] or ''), '### 意义地图']
    for i, sense in enumerate(mapping['map']['senses'], 1):
        parts.append(f"{i}. {sense['reading']}\n\n适用范围：{sense['scope']} {sense['range_note'] or ''}\n\n演化：{'连接下一义项' if sense['evolves_to_next'] else '终点'}；{sense['distinction_note'] or ''}")
    return {'content': '\n\n'.join(parts), 'sources': [{k: r[k] for k in ('chapter_idx', 'paragraph_index', 'quote')} for r in mapping['references']]}
