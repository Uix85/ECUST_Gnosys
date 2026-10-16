"""Database and paragraph-processing details for agent runs."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from agent.book import (book_db, current_baseline, fingerprint, get_paragraph,
                        logical_paragraphs, semantic_needed)
from agent.config import load_config
from agent.engine import (
    AgentTask,
    AgentCancelled,
    AgentError,
    LoopBudget,
    build_model,
)
from agents import SQLiteSession
import agent.tasks as paragraph_task
from reader.book_service import BOOKS_DIR


_deleting_books: set[str] = set()


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _event(db, run_id: str, task_id: str | None, stage: str, message: str, data=None):
    db.execute(
        "INSERT INTO agent_run_events(run_id,task_id,stage,message,data_json,created_at) VALUES(?,?,?,?,?,?)",
        (run_id, task_id, stage, message, _json(data) if data else None, _now()),
    )
    db.execute("UPDATE agent_runs SET updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()


def _save_task(db, task_id: str, **fields):
    assignments = ",".join(f"{name}=?" for name in fields)
    values = list(fields.values()) + [_now(), task_id]
    db.execute(f"UPDATE agent_run_tasks SET {assignments}, updated_at=? WHERE id=?", values)
    db.commit()


def _task_for_read(db, task_id):
    row = db.execute("SELECT * FROM agent_run_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise AgentError("段落任务记录不存在")
    return dict(row)


def _baseline_matches(db, task: dict) -> bool:
    try:
        original = json.loads(task["baseline_json"])
        para = get_paragraph(task["book_folder"], task["chapter_idx"], task["paragraph_index"])
    except Exception:
        return False
    if para["fingerprint"] != task["fingerprint"]:
        return False
    return current_baseline(db, task["chapter_idx"], para) == original


def _persist_note(db, task: dict, run: dict, candidate: dict, review: dict, state: str) -> str:
    note_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO agent_notes(id,chapter_idx,paragraph_index,fingerprint,source_text,short,detail,sentences,"
        "review_json,baseline_json,state,model,spec_version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (note_id, task["chapter_idx"], task["paragraph_index"], task["fingerprint"], task["source_text"],
         candidate["short"], candidate["detail"], _json(candidate["sentences"]), _json(review),
         task["baseline_json"], state, run["model"], run["spec_version"], _now()),
    )
    db.commit()
    return note_id


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

        self.skills = paragraph_task.skills(book_id, self.ledger, run.get("skill_snapshot"))
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
        self.spec, current_version = paragraph_task.spec_text(run["spec_type"])
        self.spec_version = run.get("spec_version") or current_version
        self.prompt = run.get("prompt_template") or paragraph_task.base_prompt(self.spec)
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
            paragraph_task.generate_spec(
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
            paragraph_task.revise_spec(
                prompt=paragraph_task.revise_prompt(self.prompt),
                materials=paragraph_task.revision_followup(review),
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
        return await self._execute(paragraph_task.review_spec(
            materials=paragraph_task.review_materials(self.materials, self.spec, candidate),
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

def preserve_candidate(db, run: dict, task_id: str, error: Exception, *, canceled: bool):
    run_id = run["id"]
    task = _task_for_read(db, task_id)
    message = "任务已取消" if canceled else str(error)[:2000]
    if task.get("candidate_json"):
        try:
            candidate = json.loads(task["candidate_json"])
            issue = "复核前任务被取消" if canceled else f"复核未完成：{str(error)[:500]}"
            review = {"approved": False, "issues": [issue], "alternatives": []}
            note_id = _persist_note(db, task, run, candidate, review, "pending")
            _save_task(
                db,
                task_id,
                status="pending",
                review_json=_json(review),
                error="任务已取消，候选稿待核" if canceled else message,
            )
            _event(
                db,
                run_id,
                task_id,
                "pending",
                "取消前生成的候选稿已保存为待核稿" if canceled else "候选稿已保存为待核稿",
                {"note_id": note_id},
            )
            return
        except Exception:
            pass

    status = "canceled" if canceled else "failed"
    _save_task(db, task_id, status=status, error=message)
    if not canceled:
        _event(db, run_id, task_id, "failed", str(error)[:500])


def start_run(db, run_id: str):
    row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    run = dict(row)
    run.update(json.loads(run["request_json"]))
    db.execute("UPDATE agent_runs SET status='running',updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()
    _event(db, run_id, None, "run", "段落任务开始")
    tasks = db.execute(
        "SELECT * FROM agent_run_tasks WHERE run_id=? AND status='queued' "
        "ORDER BY chapter_idx,paragraph_index",
        (run_id,),
    ).fetchall()
    return run, [dict(task) for task in tasks]


def mark_task_running(db, task_id: str) -> None:
    _save_task(db, task_id, status="running", error=None)


def finish_run(db, run_id: str, cancelled: bool) -> None:
    rows = db.execute("SELECT status FROM agent_run_tasks WHERE run_id=?", (run_id,)).fetchall()
    statuses = [row["status"] for row in rows]
    if cancelled or any(status in ("queued", "running") for status in statuses):
        status = "canceled"
    elif any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status in ("pending", "conflict") for status in statuses):
        status = "needs_review"
    else:
        status = "completed"
    db.execute("UPDATE agent_runs SET status=?,updated_at=? WHERE id=?", (status, _now(), run_id))
    db.commit()
    _event(db, run_id, None, "run", f"任务结束：{status}")


def cancel_remaining_tasks(db, run_id: str) -> None:
    db.execute(
        "UPDATE agent_run_tasks SET status='canceled',updated_at=? WHERE run_id=? AND status='queued'",
        (_now(), run_id),
    )
    db.execute("UPDATE agent_runs SET status='canceled',updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()
    _event(db, run_id, None, "canceled", "任务已取消")


def fail_run(db, run_id: str, error: Exception) -> None:
    db.execute(
        "UPDATE agent_runs SET status='failed',error=?,updated_at=? WHERE id=?",
        (str(error)[:2000], _now(), run_id),
    )
    db.commit()
    _event(db, run_id, None, "failed", str(error)[:500])


def serialize_run(db, run_id: str):
    run = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    tasks = db.execute(
        "SELECT id,chapter_idx,paragraph_index,fingerprint,status,error,review_json,updated_at "
        "FROM agent_run_tasks WHERE run_id=? ORDER BY chapter_idx,paragraph_index", (run_id,),
    ).fetchall()
    counts = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return {"id": run["id"], "status": run["status"], "model": run["model"],
            "created_at": run["created_at"], "updated_at": run["updated_at"], "error": run["error"],
            "counts": counts, "tasks": [dict(t) for t in tasks]}


def prepare_run(
    book_id: str, request: dict, selected_refs: list[dict] | None = None
) -> tuple[dict, str | None]:
    if book_id in _deleting_books:
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
            if (note and note.get("state") in ("published", "accepted")) or baseline.get("legacy"):
                continue
        candidates.append((para, baseline))
    if not candidates:
        return ({"id": None, "existing": False, "estimated_count": 0,
                 "skipped_existing": len(refs), "status": "nothing_to_do"}, None)
    config = load_config()
    spec, spec_version = paragraph_task.spec_text(spec_type)
    prompt_template = request.get("prompt_template") or paragraph_task.base_prompt(spec)
    skill_snapshot = request.get("skill_snapshot")
    if skill_snapshot is None:
        skill_snapshot = paragraph_task.load_book_reading_skill()
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


def cancel_run_record(book_id: str, run_id: str, signal=None) -> dict:
    db = book_db(book_id)
    if not db.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Run not found")
    if signal:
        signal.set()
    db.execute(
        "UPDATE agent_runs SET status='canceling',updated_at=? "
        "WHERE id=? AND status IN ('queued','running')",
        (_now(), run_id),
    )
    db.commit()
    _event(db, run_id, None, "canceling", "收到取消请求")
    return {"ok": True, "run_id": run_id}


def mark_book_deleting(book_id: str) -> None:
    _deleting_books.add(book_id)


def allow_book_runs(book_id: str) -> None:
    _deleting_books.discard(book_id)


def recover_interrupted_runs():
    if not os.path.isdir(BOOKS_DIR):
        return
    for folder in os.listdir(BOOKS_DIR):
        path = os.path.join(BOOKS_DIR, folder, "book.db")
        if not folder.endswith("_data") or not os.path.isfile(path):
            continue
        try:
            db = book_db(folder)
            rows = db.execute("SELECT id FROM agent_runs WHERE status IN ('queued','running','canceling')").fetchall()
            if rows:
                db.execute("UPDATE agent_runs SET status='interrupted',updated_at=? WHERE status IN ('queued','running','canceling')", (_now(),))
                db.execute("UPDATE agent_run_tasks SET status='interrupted',updated_at=? WHERE status IN ('queued','running')", (_now(),))
                db.commit()
        except Exception:
            continue


