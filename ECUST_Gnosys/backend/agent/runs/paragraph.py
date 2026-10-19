"""Run paragraph generation, independent review, revision, and persistence."""

import asyncio
import json
import os
import time
import uuid
from dataclasses import replace

from agents import SQLiteSession
from fastapi import HTTPException

from agent.tools.book import (
    book_db, current_baseline, get_paragraph, logical_paragraphs, semantic_needed,
)
from agent.tools.reading import book_reading_skills, load_book_reading_skill
from agent.tools.context import material_refs
from agent.runtime.config import load_config
from agent.runtime.task import AgentCancelled, AgentError, LoopBudget
from agent.remote import execute_agent
from agent.tasks.l3_paragraph import (
    base_prompt, generate_spec, review_materials, review_spec, revise_prompt,
    revise_spec, revision_followup,
)
from agent.tasks.policy import build_instructions, spec_text
from agent.runs.records import (
    _baseline_matches, _event, is_book_deleting, _json, _now, _persist_note,
    _save_task, _task_for_read,
)


class ParagraphRun:
    """运行单段的生成、复核、修订和保存流程。"""

    async def run(self) -> None:
        """按生成 → 复核 → 必要时修订 → 落库的顺序执行单段任务。

        任何一步抛出的取消异常都会向上传播，由调度器统一收尾；无论成功
        还是失败，最终都会关闭写作会话。
        """
        try:
            candidate = await self._generate()
            review = await self._review_candidate(candidate)
            candidate, review = await self._revise_if_needed(candidate, review)
            await self._save(candidate, review)
        finally:
            self.write_session.close()

    def __init__(self, book_id: str, db, run: dict, task: dict,
                 cancelled: asyncio.Event, config):
        """构建单段执行上下文：预算、取消信号、材料与模型/会话。"""
        self.book_id = book_id
        self.db = db
        self.run_record = run
        self.task_id = task["id"]
        self.cancelled = cancelled
        self.config = config
        # 预算在整段流程（生成/修订/两次复核）内共享，deadline 为绝对时间。
        self.budget = LoopBudget(
            config.max_requests,
            config.max_tool_calls,
            time.monotonic() + config.run_timeout,
        )

        self.paragraph = get_paragraph(book_id, task["chapter_idx"], task["paragraph_index"])
        ci, pi = task["chapter_idx"], task["paragraph_index"]
        # ledger 收集目标段及相邻段原文，工具读取时据此拼接连续上下文。
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
        # 同语境段落也要补进 ledger，保证工具能读取到它们。
        for context_pi in self.materials['chapter_structure'].get('same_context_paragraphs', []):
            self.ledger.setdefault((ci, context_pi), []).append(get_paragraph(book_id, ci, context_pi)['text'])
        self.spec, current_version = spec_text(run["spec_type"])
        # 运行记录里冻结了创建时的规范版本，避免中途切换版本导致口径不一致。
        self.spec_version = run.get("spec_version") or current_version
        self.prompt = run.get("prompt_template") or base_prompt(self.spec)
        if os.environ.get("GNOSYS_AGENT_MODE", "local") != "http":
            from agent.runtime.sdk import build_model
            self.model = build_model(config)
        else:
            self.model = None
        self.write_session = SQLiteSession(f"{self.task_id}-writing")
        self.write_tools = []

    async def _emit(self, stage: str, message: str, data=None) -> None:
        """直接写入运行事件（不经过 SDK 事件映射）。"""
        _event(self.db, self.run_record["id"], self.task_id, stage, message, data)

    async def _agent_event(self, stage: str, message: str, data=None) -> None:
        """把 SDK 生命周期事件映射为前端可消费的阶段。

        模型与工具事件会改名后转发；tool_end 额外抽取引用的材料，单独记
        一条 material 事件并吞掉原始 tool_end（避免重复展示）。
        """
        if stage == "run":
            data = {**(data or {}), "layer": "L3"}
        if stage == "tool_end":
            refs = material_refs((data or {}).get("result"))
            if refs:
                await self._emit("material", "读取了带位置的书内材料", refs)
            return
        stage = {"model_start": "step", "model_end": "model_response", "tool_start": "tool"}.get(stage, stage)
        await self._emit(stage, message, data)

    async def _execute(self, task_spec, *, session=None, mounted_tools=None):
        """注入工具预算说明并在共享预算/取消信号下调用 run_task。"""
        task_spec = replace(task_spec, prompt=build_instructions(
            task_spec, tool_budget=self.budget.max_tool_calls,
        ))
        return await execute_agent(
            task_spec, self.config, emit=self._agent_event, cancelled=self.cancelled,
            budget=self.budget, session=session,
            session_key=f"{self.task_id}-writing" if session else None,
            mounted_tools=mounted_tools, model=self.model,
        )

    async def _generate(self) -> dict:
        """生成候选稿并立即持久化。

        先保存再继续，确保后续复核/修订失败时仍能恢复候选稿；结构校验由
        generate_spec 的 submit validator 在 run_task 内完成。
        """
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
        """按复核意见修订一次，复用写作会话与工具以保留上下文。"""
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
        """独立复核候选稿；`stage` 区分首次复核与修订后复核。"""
        await self._emit(stage, "开始独立复核")
        return await self._execute(review_spec(
            materials=review_materials(self.materials, self.spec, candidate),
            skills=self.skills,
        ))

    async def _review_candidate(self, candidate: dict) -> dict:
        """复核失败时降级为“未通过”，但取消必须继续向上传播。"""
        try:
            return await self._review(candidate, "review")
        except AgentCancelled:
            raise
        except AgentError:
            return {"approved": False, "issues": ["独立复核未完成"], "alternatives": []}

    async def _revise_if_needed(self, candidate: dict, review: dict) -> tuple[dict, dict]:
        """未通过且仍有预算时修订并再次复核。

        预算耗尽或已取消则直接返回原候选与原复核；修订/复核报错时保留
        原复核意见并追加说明，不丢弃已生成的候选稿。
        """
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
        """落库前最终检查，并决定 published / pending / conflict。

        baseline 不匹配（原文或已有解释在运行期间变化）一律判 conflict；
        仅复核通过才发布，并同步更新 workspace_notes 的正文与版本号。
        """
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
    """校验请求、解析目标段落、去重并持久化一条新的段落 run。

    返回值约定：`(response, None)` 表示未创建新 run（命中已有任务、无需
    解释或全部跳过）；`(response, run_id)` 表示已写库、可由调用方启动。
    解析顺序为显式 `selected_refs` → 单个 `paragraph_ref` → 章节/章节范围；
    每个目标都会复核指纹、过滤无需解释的段落，并按 (章节, 段落, 指纹)
    与活动任务去重。
    """
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
        # 未给出具体段落时，按章节或章节区间展开为候选段落。
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
        # 调用方显式带了指纹就必须与当前原文一致，否则视为并发修改。
        if ref.get("fingerprint") and ref["fingerprint"] != para["fingerprint"]:
            raise HTTPException(status_code=409, detail={"message": "原文指纹已变化", "chapter_idx": ci, "paragraph_index": pi})
        if not semantic_needed(para["text"]):
            continue
        # 同一段落在活动 run 中已有任务时，直接复用该 run（幂等）。
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
        # fill_missing 模式下跳过已有已发布/已接受解释、旧版或工作区解释的段落。
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
