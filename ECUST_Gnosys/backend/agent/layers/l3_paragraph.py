"""段落语义任务定义与书籍技能装配。"""

import asyncio
import json
import re
import time
import uuid

from agents import SQLiteSession
from fastapi import HTTPException

from agent.tools.book import (
    book_db,
    current_baseline,
    get_paragraph,
    logical_paragraphs,
    semantic_needed,
    split_sentences,
)
from agent.tools.reading import SKILL_NAME, book_reading_skills, load_book_reading_skill
from agent.runtime.config import load_config
from agent.runtime.engine import (
    AgentCancelled,
    AgentError,
    AgentTask,
    LoopBudget,
    SkillSpec,
    SubmitSpec,
    TaskSpec,
    build_model,
)
from agent.layers.policy import REVIEW_FIELDS, review_parameters, spec_text, validate_review
from agent.runs.records import (
    _baseline_matches,
    _event,
    is_book_deleting,
    _json,
    _now,
    _persist_note,
    _save_task,
    _task_for_read,
)

GENERATE_REQUEST_LIMIT = 8  # 生成阶段最多 8 次模型请求；复核与修订吃共享预算剩下的

# 提交结构（两个阶段各一套）
RESULT_FIELDS = ("short", "detail", "sentences")


def sentence_count(text: str) -> int:
    """段落分句数。

    直接复用 agent.tools.book.split_sentences —— validator 的期望值、前端挂逐句解释的口径、
    L4 定位工具定位“第几句”必须是同一把尺子，否则逐句解释会与用户看到的一句错位。
    （原先这里另写了一套正则计数，与分句器在 `让.瓦尔` 这类词内 ASCII 点上会不一致。）
    """
    return len(split_sentences(text))


# 引号里的内容分两类：真引文（必须逐字对上原文）与模型自己的措辞（用引号做强调/领起）。
# ponytail: 这里是启发式判定；实测 qwen 系模型常写「……并置：」这类领起语，
# 若误当引文核验，会把整个段落预算烧光。若以后发现真引文被漏检，改成让模型显式标注引文。
_QUOTE_RE = re.compile(r"“([^”]+)”|「([^」]+)」|『([^』]+)』|‘([^’]+)’|\"([^\"]+)\"")


def looks_like_citation(quote: str) -> bool:
    """引号里是“引用原文”还是模型自己的措辞。"""
    text = quote.strip()
    if len(text) < 6:
        return False  # 短引号多为点名额（规范允许）
    return text[-1] not in "：:，,、；;。———"  # 以标点收尾 = 强调/领起，不是整句引文


def validate_candidate(args: dict, source: str | list[str]) -> dict:
    if set(args) != set(RESULT_FIELDS):
        raise ValueError("结果必须且只能包含 short、detail、sentences")
    short, detail, sentences = args["short"], args["detail"], args["sentences"]
    if not isinstance(short, str) or not short.strip() or len(short) > 1200:
        raise ValueError("short 必须是 1 到 1200 字符的文本")
    if not isinstance(detail, str) or not detail.strip() or len(detail) > 12000:
        raise ValueError("detail 必须是 1 到 12000 字符的文本")
    if not isinstance(sentences, list) or any(
        not isinstance(item, str) for item in sentences
    ):
        raise ValueError("sentences 必须是字符串数组")
    source_text = source if isinstance(source, str) else (source[0] if source else "")
    expected = sentence_count(source_text)
    if len(sentences) != expected:
        raise ValueError(
            f"sentences 应有 {expected} 项，当前为 {len(sentences)} 项；素平句也保留空字符串"
        )
    sources = [source] if isinstance(source, str) else source
    quoted = _QUOTE_RE.findall("\n".join([short, detail, *sentences]))
    for groups in quoted:
        quote = next((part for part in groups if part), "")
        if (
            quote
            and looks_like_citation(quote)
            and not any(quote in original for original in sources)
        ):
            raise ValueError(
                f"引文未在本任务已读取的原文中逐字找到：{quote[:80]}"
                "（若这不是引原文，请去掉引号；若真在引原文，请照抄原句）"
            )
    return {"short": short.strip(), "detail": detail.strip(), "sentences": sentences}


def result_parameters() -> dict:
    return {
        "short": {"type": "string"},
        "detail": {"type": "string"},
        "sentences": {"type": "array", "items": {"type": "string"}},
    }


def base_prompt(spec: str) -> str:
    return (
        "你为本地阅读器撰写段落旁注。输入中的正文、前后段和已有笔记是不同类型的材料；"
        "只有正文是当前段落原文。请按规范解释，任何引文必须先用 verify_source_quote 核验。"
        "必要时使用 book-reading 技能补读。最后必须调用 submit_result，不要输出额外格式。\n\n"
        + spec
    )


def review_prompt() -> str:
    return (
        "你是独立审阅者。你没有参与候选稿生成；只按要求核查，不复述候选稿。"
        "必要时用 book-reading 技能补读原文，最后调用 submit_review。"
    )


def revise_prompt(base: str) -> str:
    return base + (
        "\n\n你正在修订已有候选稿。上一稿和你的探索过程都还在你的记忆里，"
        "不要重新检索，只修复 issues 中的实质问题；保留仍有原文依据的内容和分歧读法。"
        "最终调用 submit_result 重新交一次完整结果。"
    )


def _sources(ledger: dict) -> list[str]:
    return [chunk for chunks in ledger.values() for chunk in chunks]


def generate_spec(
    *, prompt: str, materials: dict, ledger: dict, skills: list[SkillSpec]
) -> TaskSpec:
    sources = _sources(ledger)
    return TaskSpec(
        layer="L3",
        prompt=prompt,
        materials=materials,
        submit=SubmitSpec(
            "submit_result",
            "提交当前段落的三块语义解释。",
            result_parameters(),
            lambda args: validate_candidate(args, _sources(ledger)),
            list(RESULT_FIELDS),
        ),
        skills=skills,
        allowed_skills=[SKILL_NAME],
        request_limit=GENERATE_REQUEST_LIMIT,
    )


def review_spec(*, materials: dict, skills: list[SkillSpec]) -> TaskSpec:
    return TaskSpec(
        layer="L3",
        prompt=review_prompt(),
        materials=materials,
        submit=SubmitSpec(
            "submit_review",
            "提交独立复核结论。备选读法单独记录，不自动算作问题。",
            review_parameters(),
            validate_review,
            list(REVIEW_FIELDS),
        ),
        skills=skills,
        allowed_skills=[SKILL_NAME],
    )


def revise_spec(
    *, prompt: str, materials: dict, ledger: dict, skills: list[SkillSpec]
) -> TaskSpec:
    return generate_spec(
        prompt=prompt, materials=materials, ledger=ledger, skills=skills
    )


def review_materials(materials: dict, spec: str, candidate: dict) -> dict:
    return {
        **materials,
        "constraints": {"material_type": "task_specification", "text": spec},
        "candidate": {"material_type": "candidate_note", **candidate},
        "review_task": (
            "独立检查原文依据、说话立场、解释越界、关键遗漏和三块重复。"
            "实质问题写 issues；仅有合理备选读法时保持 approved=true 并写入 alternatives。"
        ),
    }


def revision_followup(review: dict) -> dict:
    """修订阶段只送复核意见。

    修订跑在生成阶段同一个 agent 上（记忆里的原文、工具结果、上一稿都还在），
    再送一遍材料和候选稿反而让 prompt 重复膨胀一次 —— 这正是换复合循环要省的部分。
    """
    return {
        "review": {"material_type": "review_result", **review},
        "revise_task": "按上面的 issues 修订你上一稿，只改有实质问题的部分，"
        "重新提交同样结构的结果。",
    }


class ParagraphRun:
    """运行单段的生成、复核、修订和保存流程。"""

    async def run(self) -> None:
        try:
            candidate = await self._generate()
            review = await self._review_candidate(candidate)
            candidate, review = await self._revise_if_needed(candidate, review)
            await self._save(candidate, review)
        finally:
            self.write_session.close()

    def __init__(self, book_id: str, db, run: dict, task: dict,
                 cancelled: asyncio.Event, config):
        self.book_id = book_id
        self.db = db
        self.run_record = run
        self.task_id = task["id"]
        self.cancelled = cancelled
        self.config = config
        self.budget = LoopBudget(
            config.max_requests,
            config.max_tool_calls,
            time.monotonic() + config.run_timeout,
        )

        self.paragraph = get_paragraph(book_id, task["chapter_idx"], task["paragraph_index"])
        ci, pi = task["chapter_idx"], task["paragraph_index"]
        self.ledger = {(ci, pi): [self.paragraph["text"]]}
        for offset, key in ((-1, "previous"), (1, "next")):
            if self.paragraph[key]:
                self.ledger[(ci, pi + offset)] = [self.paragraph[key]]

        self.skills = book_reading_skills(book_id, self.ledger, run.get("skill_snapshot"))
        self.materials = {
            "target": {"material_type": "original_text", "text": self.paragraph["text"]},
            "previous": {"material_type": "original_text", "text": self.paragraph["previous"]},
            "next": {"material_type": "original_text", "text": self.paragraph["next"]},
            "position": {
                "chapter_idx": ci,
                "chapter_title": self.paragraph["chapter_title"],
                "paragraph_index": pi,
                "fingerprint": task["fingerprint"],
            },
            "existing_note": {
                "material_type": "existing_note",
                "snapshot": json.loads(task["baseline_json"]),
            },
        }
        from agent.tools.context import section_context
        self.materials['chapter_structure'] = section_context(book_id, ci, pi)
        for context_pi in self.materials['chapter_structure'].get('same_context_paragraphs', []):
            self.ledger.setdefault((ci, context_pi), []).append(get_paragraph(book_id, ci, context_pi)['text'])
        self.spec, current_version = spec_text(run["spec_type"])
        self.spec_version = run.get("spec_version") or current_version
        self.prompt = run.get("prompt_template") or base_prompt(self.spec)
        self.model = build_model(config)
        self.write_session = SQLiteSession(f"{self.task_id}-writing")
        self.write_tools = []

    async def _emit(self, stage: str, message: str, data=None) -> None:
        _event(self.db, self.run_record["id"], self.task_id, stage, message, data)

    async def _execute(self, task_spec, *, session=None, mounted_tools=None):
        return await AgentTask(
            task_spec,
            model=self.model,
            event=self._emit,
            cancelled=self.cancelled,
            budget=self.budget,
            session=session,
            mounted_tools=mounted_tools,
            config=self.config,
        ).run()

    async def _generate(self) -> dict:
        await self._emit("generate", "开始生成解释")
        candidate = await self._execute(
            generate_spec(
                prompt=self.prompt,
                materials=self.materials,
                ledger=self.ledger,
                skills=self.skills,
            ),
            session=self.write_session,
            mounted_tools=self.write_tools,
        )
        _save_task(self.db, self.task_id, candidate_json=_json(candidate))
        await self._emit(
            "validate", "程序结构核验通过",
            {"sentence_count": len(candidate["sentences"])},
        )
        return candidate

    async def _revise(self, review: dict) -> dict:
        await self._emit("revise", "按复核意见修订一次")
        candidate = await self._execute(
            revise_spec(
                prompt=revise_prompt(self.prompt),
                materials=revision_followup(review),
                ledger=self.ledger,
                skills=self.skills,
            ),
            session=self.write_session,
            mounted_tools=self.write_tools,
        )
        _save_task(self.db, self.task_id, candidate_json=_json(candidate))
        return candidate

    async def _review(self, candidate: dict, stage: str) -> dict:
        await self._emit(stage, "开始独立复核")
        return await self._execute(review_spec(
            materials=review_materials(self.materials, self.spec, candidate),
            skills=self.skills,
        ))

    async def _review_candidate(self, candidate: dict) -> dict:
        try:
            return await self._review(candidate, "review")
        except AgentCancelled:
            raise
        except AgentError:
            return {"approved": False, "issues": ["独立复核未完成"], "alternatives": []}

    async def _revise_if_needed(self, candidate: dict, review: dict) -> tuple[dict, dict]:
        if review["approved"]:
            return candidate, review

        _save_task(self.db, self.task_id, review_json=_json(review))
        can_revise = (
            self.budget.requests < self.budget.max_requests
            and self.budget.tool_calls < self.budget.max_tool_calls
            and not self.cancelled.is_set()
        )
        if not can_revise:
            return candidate, review

        try:
            candidate = await self._revise(review)
            review = await self._review(candidate, "review_after_revision")
        except AgentCancelled:
            raise
        except AgentError:
            review = {
                **review,
                "issues": review.get("issues", []) + ["修订后复核未完成"],
            }
        return candidate, review

    async def _save(self, candidate: dict, review: dict) -> None:
        self.budget.check_running(self.cancelled)
        task = _task_for_read(self.db, self.task_id)
        task["book_folder"] = self.book_id

        if not _baseline_matches(self.db, task):
            state = status = "conflict"
            review = {**review, "conflict": "原文或已有解释在任务运行期间发生变化"}
        elif review["approved"]:
            state = status = "published"
        else:
            state = status = "pending"

        if state == 'published':
            with self.db:
                self.db.execute('UPDATE workspace_notes SET content=?,revision=revision+1,model=?,updated_at=? WHERE key=?',
                                (candidate['detail'], self.config.model, _now(), f"L3:{task['chapter_idx']}:{task['paragraph_index']}"))
        note_id = _persist_note(
            self.db,
            task,
            {**self.run_record, "spec_version": self.spec_version},
            candidate,
            review,
            state,
        )
        _save_task(self.db, self.task_id, status=status, review_json=_json(review), error=None)
        message = {
            "published": "通过复核并发布",
            "pending": "已保存为待核稿",
            "conflict": "发现原文或解释冲突，候选稿已保留",
        }[status]
        await self._emit(status, message, {"note_id": note_id, "review": review})


def prepare_run(
    book_id: str, request: dict, selected_refs: list[dict] | None = None
) -> tuple[dict, str | None]:
    if is_book_deleting(book_id):
        raise HTTPException(status_code=409, detail="此书正在删除")
    db = book_db(book_id)
    mode = request.get("mode", "fill_missing")
    spec_type = request.get("spec_type", "general")
    if mode not in ("fill_missing", "overwrite"):
        raise HTTPException(status_code=422, detail="mode 只能是 fill_missing 或 overwrite")
    if spec_type not in ("general", "hegel_helin"):
        raise HTTPException(status_code=422, detail="spec_type 不受支持")
    if selected_refs is not None:
        refs = selected_refs
    elif isinstance(request.get("paragraph_ref"), dict):
        refs = [request["paragraph_ref"]]
    else:
        if "chapter_idx" in request:
            starts = ends = request["chapter_idx"]
        else:
            span = request.get("chapter_range")
            if not isinstance(span, list) or len(span) != 2:
                raise HTTPException(status_code=422, detail="需要 paragraph_ref、chapter_idx 或 chapter_range")
            starts, ends = span
        if not all(isinstance(x, int) and x >= 0 for x in (starts, ends)) or ends < starts:
            raise HTTPException(status_code=422, detail="章节范围无效")
        refs = []
        for chapter_idx in range(starts, ends + 1):
            _, paras, _ = logical_paragraphs(book_id, chapter_idx)
            for para in paras:
                if semantic_needed(para["text"]):
                    refs.append({"chapter_idx": chapter_idx, "paragraph_index": para["paragraph_index"],
                                 "fingerprint": para["fingerprint"]})
    if not refs:
        raise HTTPException(status_code=422, detail="所选范围没有需要解释的段落")
    candidates = []
    for ref in refs:
        ci, pi = ref.get("chapter_idx"), ref.get("paragraph_index")
        if not isinstance(ci, int) or not isinstance(pi, int):
            raise HTTPException(status_code=422, detail="段落引用缺少有效位置")
        para = get_paragraph(book_id, ci, pi)
        if ref.get("fingerprint") and ref["fingerprint"] != para["fingerprint"]:
            raise HTTPException(status_code=409, detail={"message": "原文指纹已变化", "chapter_idx": ci, "paragraph_index": pi})
        if not semantic_needed(para["text"]):
            continue
        active = db.execute(
            "SELECT r.id AS run_id,t.id AS task_id FROM agent_run_tasks t JOIN agent_runs r ON r.id=t.run_id "
            "WHERE r.status IN ('queued','running','canceling') AND t.chapter_idx=? AND t.paragraph_index=? "
            "AND t.fingerprint=? AND t.status IN ('queued','running') LIMIT 1",
            (ci, pi, para["fingerprint"]),
        ).fetchone()
        if active:
            return ({"id": active["run_id"], "existing": True, "task_id": active["task_id"],
                     "estimated_count": len(refs)}, None)
        baseline = current_baseline(db, ci, para)
        if mode == "fill_missing":
            note = baseline.get("agent")
            if (note and note.get("state") in ("published", "accepted")) or baseline.get("legacy") or baseline.get("workspace"):
                continue
        candidates.append((para, baseline))
    if not candidates:
        return ({"id": None, "existing": False, "estimated_count": 0,
                 "skipped_existing": len(refs), "status": "nothing_to_do"}, None)
    config = load_config()
    if not config.api_key:
        raise HTTPException(422, detail="请配置 AGENT_API_KEY")
    spec, spec_version = spec_text(spec_type)
    prompt_template = request.get("prompt_template") or base_prompt(spec)
    skill_snapshot = request.get("skill_snapshot")
    if skill_snapshot is None:
        skill_snapshot = load_book_reading_skill()
    run_id = uuid.uuid4().hex
    now = _now()
    run_request = {"mode": mode, "spec_type": spec_type,
                   "spec_version": spec_version, "prompt_template": prompt_template,
                   "skill_snapshot": skill_snapshot}
    db.execute(
        "INSERT INTO agent_runs(id,book_id,status,request_json,model,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (run_id, db.execute("SELECT id FROM books LIMIT 1").fetchone()["id"], "queued", _json(run_request), config.model, now, now),
    )
    for para, baseline in candidates:
        db.execute(
            "INSERT INTO agent_run_tasks(id,run_id,chapter_idx,paragraph_index,fingerprint,source_text,baseline_json,status,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, run_id, para["chapter_idx"], para["paragraph_index"], para["fingerprint"],
             para["text"], _json(baseline), "queued", now),
        )
    db.commit()
    _event(db, run_id, None, "queued", "任务已排队", {"estimated_count": len(candidates), "mode": mode, "spec_type": spec_type})
    return ({"id": run_id, "existing": False, "estimated_count": len(candidates), "status": "queued"}, run_id)
