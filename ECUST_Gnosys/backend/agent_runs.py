"""Persistent paragraph semantic runs and their HTTP API."""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from agent_book import (book_db, current_baseline, fingerprint, get_paragraph,
                        logical_paragraphs, semantic_needed)
from agent_config import load_config
from agent_core import AgentCancelled, AgentError, BudgetExceeded, LoopBudget
from agent_engine import build_engine_model, run_task
from agent_tasks import paragraph as paragraph_task
from book_service import BOOKS_DIR


router = APIRouter(prefix="/api/agent/{book_id}/runs", tags=["agent"])
_run_tasks: dict[tuple[str, str], asyncio.Task] = {}
_cancel_signals: dict[tuple[str, str], asyncio.Event] = {}
_deleting_books: set[str] = set()
_parallel: asyncio.Semaphore | None = None


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


# ── 任务定义（prompt / 输出结构 / 校验）已移到 agent_tasks.paragraph。
#    这里保留别名，供 _create_run 与测试沿用原来的名字。 ──
_sentence_count = paragraph_task.sentence_count
_validate_candidate = paragraph_task.validate_candidate
_validate_review = paragraph_task.validate_review
_spec = paragraph_task.spec_text
_base_prompt = paragraph_task.base_prompt


async def _loop_event(db, run_id, task_id, stage, message, data=None):
    _event(db, run_id, task_id, stage, message, data)


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


def _engine_model(config):
    """构建基座需要的模型对象；测试通过替换本函数注入假模型。"""
    return build_engine_model(config)


async def _process_paragraph(book_id: str, db, run: dict, task: dict, cancelled: asyncio.Event,
                             config):
    task_id = task["id"]
    budget = LoopBudget(config.max_requests, config.max_tool_calls, time.monotonic() + config.run_timeout)
    paragraph = get_paragraph(book_id, task["chapter_idx"], task["paragraph_index"])
    ledger = {(task["chapter_idx"], task["paragraph_index"]): [paragraph["text"]]}
    if paragraph["previous"]:
        ledger[(task["chapter_idx"], task["paragraph_index"] - 1)] = [paragraph["previous"]]
    if paragraph["next"]:
        ledger[(task["chapter_idx"], task["paragraph_index"] + 1)] = [paragraph["next"]]
    skills = paragraph_task.skills(book_id, ledger, run.get("skill_snapshot"))
    materials = {
        "target": {"material_type": "original_text", "text": paragraph["text"]},
        "previous": {"material_type": "original_text", "text": paragraph["previous"]},
        "next": {"material_type": "original_text", "text": paragraph["next"]},
        "position": {"chapter_idx": task["chapter_idx"], "chapter_title": paragraph["chapter_title"],
                     "paragraph_index": task["paragraph_index"], "fingerprint": task["fingerprint"]},
        "existing_note": {"material_type": "existing_note", "snapshot": json.loads(task["baseline_json"])},
    }
    spec, current_spec_version = paragraph_task.spec_text(run["spec_type"])
    spec_version = run.get("spec_version") or current_spec_version
    prompt = run.get("prompt_template") or paragraph_task.base_prompt(spec)
    model = _engine_model(config)

    async def emit(stage, message, data=None):
        await _loop_event(db, run["id"], task_id, stage, message, data)

    async def execute(task_spec, session=None):
        """三个阶段共用同一份预算与同一个事件写入器，只有任务定义不同。

        session 决定要不要复用会话历史：生成与修订传同一个 dict（复合循环），
        复核不传（独立上下文）。
        """
        return await run_task(task_spec, event=emit, cancelled=cancelled, model=model,
                              budget=budget, session=session, config=config)

    # 生成与修订共用：材料只在第一轮进 prompt，修订时模型带着自己的探索记忆改稿
    write_session: dict = {}

    await emit("generate", "开始生成解释")
    generated = await execute(paragraph_task.generate_spec(
        prompt=prompt, materials=materials, ledger=ledger, skills=skills), write_session)
    _save_task(db, task_id, candidate_json=_json(generated))
    await _loop_event(db, run["id"], task_id, "validate", "程序结构核验通过", {"sentence_count": len(generated["sentences"])})

    async def review(candidate, stage):
        await emit(stage, "开始独立复核")
        return await execute(paragraph_task.review_spec(
            materials=paragraph_task.review_materials(materials, spec, candidate), skills=skills))

    try:
        review_result = await review(generated, "review")
    except (AgentError, BudgetExceeded):
        review_result = {"approved": False, "issues": ["独立复核未完成"], "alternatives": []}
    if not review_result["approved"]:
        _save_task(db, task_id, review_json=_json(review_result))
        if budget.requests < budget.max_requests and budget.tool_calls < budget.max_tool_calls and not cancelled.is_set():
            try:
                await emit("revise", "按复核意见修订一次")
                generated = await execute(paragraph_task.revise_spec(
                    prompt=paragraph_task.revise_prompt(prompt),
                    materials=paragraph_task.revision_followup(review_result),
                    ledger=ledger, skills=skills), write_session)
                _save_task(db, task_id, candidate_json=_json(generated))
                review_result = await review(generated, "review_after_revision")
            except (AgentError, BudgetExceeded):
                review_result = {"approved": False, "issues": review_result.get("issues", []) + ["修订后复核未完成"], "alternatives": review_result.get("alternatives", [])}

    if cancelled.is_set():
        raise AgentCancelled("任务已取消")
    task = _task_for_read(db, task_id)
    task["book_folder"] = book_id
    if not _baseline_matches(db, task):
        state, status = "conflict", "conflict"
        review_result = {**review_result, "conflict": "原文或已有解释在任务运行期间发生变化"}
    elif review_result["approved"]:
        state, status = "published", "published"
    else:
        state, status = "pending", "pending"
    note_id = _persist_note(db, task, {**run, "spec_version": spec_version}, generated, review_result, state)
    _save_task(db, task_id, status=status, review_json=_json(review_result), error=None)
    await _loop_event(db, run["id"], task_id, status, {
        "published": "通过复核并发布", "pending": "已保存为待核稿", "conflict": "发现原文或解释冲突，候选稿已保留",
    }[status], {"note_id": note_id, "review": review_result})


async def _run_worker_impl(book_id: str, run_id: str, cancelled: asyncio.Event):
    global _parallel
    config = load_config()
    if _parallel is None:
        _parallel = asyncio.Semaphore(max(1, config.max_parallel))
    db = book_db(book_id)
    row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return
    run = dict(row)
    run.update(json.loads(run["request_json"]))
    db.execute("UPDATE agent_runs SET status='running',updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()
    _event(db, run_id, None, "run", "段落任务开始", None)
    try:
        tasks = db.execute("SELECT * FROM agent_run_tasks WHERE run_id=? AND status='queued' ORDER BY chapter_idx,paragraph_index", (run_id,)).fetchall()
        for raw_task in tasks:
            if cancelled.is_set():
                break
            task = dict(raw_task)
            _save_task(db, task["id"], status="running", error=None)
            try:
                async with _parallel:
                    await asyncio.wait_for(_process_paragraph(book_id, db, run, task, cancelled, config), timeout=config.run_timeout)
            except AgentCancelled:
                refreshed = _task_for_read(db, task["id"])
                if refreshed.get("candidate_json"):
                    try:
                        candidate = json.loads(refreshed["candidate_json"])
                        review = {"approved": False, "issues": ["复核前任务被取消"], "alternatives": []}
                        note_id = _persist_note(db, refreshed, run, candidate, review, "pending")
                        _save_task(db, task["id"], status="pending", review_json=_json(review), error="任务已取消，候选稿待核")
                        _event(db, run_id, task["id"], "pending", "取消前生成的候选稿已保存为待核稿", {"note_id": note_id})
                    except Exception:
                        _save_task(db, task["id"], status="canceled", error="任务已取消")
                else:
                    _save_task(db, task["id"], status="canceled", error="任务已取消")
                raise AgentCancelled("任务已取消")
            except Exception as exc:
                refreshed = _task_for_read(db, task["id"])
                if refreshed.get("candidate_json"):
                    try:
                        candidate = json.loads(refreshed["candidate_json"])
                        review = {"approved": False, "issues": [f"复核未完成：{str(exc)[:500]}"], "alternatives": []}
                        note_id = _persist_note(db, refreshed, run, candidate, review, "pending")
                        _save_task(db, task["id"], status="pending", review_json=_json(review), error=str(exc)[:2000])
                        _event(db, run_id, task["id"], "pending", "候选稿已保存为待核稿", {"note_id": note_id})
                    except Exception:
                        _save_task(db, task["id"], status="failed", error=str(exc)[:2000])
                        _event(db, run_id, task["id"], "failed", str(exc)[:500])
                else:
                    _save_task(db, task["id"], status="failed", error=str(exc)[:2000])
                    _event(db, run_id, task["id"], "failed", str(exc)[:500])
        rows = db.execute("SELECT status FROM agent_run_tasks WHERE run_id=?", (run_id,)).fetchall()
        statuses = [r["status"] for r in rows]
        if cancelled.is_set():
            final = "canceled"
        elif any(status in ("queued", "running") for status in statuses):
            final = "canceled"
        elif any(status == "failed" for status in statuses):
            final = "failed"
        elif any(status in ("pending", "conflict") for status in statuses):
            final = "needs_review"
        else:
            final = "completed"
        db.execute("UPDATE agent_runs SET status=?,updated_at=? WHERE id=?", (final, _now(), run_id))
        db.commit()
        _event(db, run_id, None, "run", f"任务结束：{final}")
    except AgentCancelled:
        db.execute("UPDATE agent_run_tasks SET status='canceled',updated_at=? WHERE run_id=? AND status='queued'", (_now(), run_id))
        db.execute("UPDATE agent_runs SET status='canceled',updated_at=? WHERE id=?", (_now(), run_id))
        db.commit()
        _event(db, run_id, None, "canceled", "任务已取消")
    except Exception as exc:
        db.execute("UPDATE agent_runs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), run_id))
        db.commit()
        _event(db, run_id, None, "failed", str(exc)[:500])
    finally:
        _run_tasks.pop((book_id, run_id), None)
        _cancel_signals.pop((book_id, run_id), None)


async def _run_worker(book_id: str, run_id: str, cancelled: asyncio.Event):
    try:
        await _run_worker_impl(book_id, run_id, cancelled)
    except Exception as exc:
        try:
            db = book_db(book_id)
            db.execute("UPDATE agent_runs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), run_id))
            db.commit()
            _event(db, run_id, None, "failed", str(exc)[:500])
        except Exception:
            pass
    finally:
        _run_tasks.pop((book_id, run_id), None)
        _cancel_signals.pop((book_id, run_id), None)


def _serialize_run(db, run_id: str):
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


def _create_run(book_id: str, request: dict, selected_refs: list[dict] | None = None) -> dict:
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
            _, paras, total = logical_paragraphs(book_id, chapter_idx)
            for para in paras:
                if semantic_needed(para["text"]):
                    refs.append({"chapter_idx": chapter_idx, "paragraph_index": para["paragraph_index"],
                                 "fingerprint": para["fingerprint"], "source_text": para["text"]})
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
            return {"id": active["run_id"], "existing": True, "task_id": active["task_id"],
                    "estimated_count": len(refs)}
        baseline = current_baseline(db, ci, para)
        if mode == "fill_missing":
            note = baseline.get("agent")
            if (note and note.get("state") in ("published", "accepted")) or baseline.get("legacy"):
                continue
        candidates.append((para, baseline))
    if not candidates:
        return {"id": None, "existing": False, "estimated_count": 0, "skipped_existing": len(refs), "status": "nothing_to_do"}
    config = load_config()
    spec, spec_version = _spec(spec_type)
    prompt_template = request.get("prompt_template") or _base_prompt(spec)
    skill_snapshot = request.get("skill_snapshot")
    if skill_snapshot is None:
        skill_snapshot = (Path(__file__).resolve().parents[1] / "skills" / "book-reading" / "SKILL.md").read_text(encoding="utf-8")
    tool_source = (Path(__file__).resolve().parent / "agent_skills.py").read_text(encoding="utf-8")
    skill_version = fingerprint(skill_snapshot + tool_source)
    run_id = uuid.uuid4().hex
    now = _now()
    run_request = {"mode": mode, "spec_type": spec_type, "estimated_count": len(candidates),
                   "spec_version": spec_version, "prompt_template": prompt_template,
                   "prompt_version": fingerprint(prompt_template), "skill_version": skill_version,
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
    signal = asyncio.Event()
    _cancel_signals[(book_id, run_id)] = signal
    task = asyncio.create_task(_run_worker(book_id, run_id, signal), name=f"agent-run-{run_id}")
    _run_tasks[(book_id, run_id)] = task
    return {"id": run_id, "existing": False, "estimated_count": len(candidates), "status": "queued"}


@router.post("")
async def create_run(book_id: str, body: dict = Body(...)):
    result = _create_run(book_id, body)
    if result.get("id") and result.get("status") != "nothing_to_do" and not result.get("existing"):
        return result
    return result


@router.get("")
async def list_runs(book_id: str, limit: int = Query(50, ge=1, le=200)):
    db = book_db(book_id)
    ids = db.execute("SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [_serialize_run(db, row["id"]) for row in ids]}


@router.get("/pending")
async def list_pending(book_id: str):
    db = book_db(book_id)
    rows = db.execute(
        "SELECT t.id AS task_id,t.run_id,t.chapter_idx,t.paragraph_index,t.source_text,t.review_json,"
        "n.id AS note_id,n.short,n.detail,n.sentences,n.state,n.updated_at "
        "FROM agent_run_tasks t JOIN agent_notes n ON n.chapter_idx=t.chapter_idx "
        "AND n.paragraph_index=t.paragraph_index AND n.fingerprint=t.fingerprint "
        "WHERE t.status IN ('pending','conflict') AND n.state IN ('pending','conflict') "
        "AND n.updated_at=(SELECT MAX(n2.updated_at) FROM agent_notes n2 WHERE n2.chapter_idx=n.chapter_idx "
        "AND n2.paragraph_index=n.paragraph_index AND n2.fingerprint=n.fingerprint AND n2.state IN ('pending','conflict')) "
        "ORDER BY n.updated_at DESC"
    ).fetchall()
    return {"items": [dict(r, sentences=json.loads(r["sentences"] or "[]"),
                            review=json.loads(r["review_json"] or "{}")) for r in rows]}


@router.get("/{run_id}")
async def get_run(book_id: str, run_id: str):
    return _serialize_run(book_db(book_id), run_id)


@router.get("/{run_id}/events")
async def run_events(book_id: str, run_id: str, after: int = Query(0, ge=0)):
    db = book_db(book_id)
    if not db.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Run not found")

    async def stream():
        cursor = after
        while True:
            rows = db.execute("SELECT * FROM agent_run_events WHERE run_id=? AND id>? ORDER BY id", (run_id, cursor)).fetchall()
            for row in rows:
                cursor = row["id"]
                payload = {"id": row["id"], "task_id": row["task_id"], "stage": row["stage"],
                           "message": row["message"], "data": json.loads(row["data_json"]) if row["data_json"] else None,
                           "created_at": row["created_at"]}
                yield f"id: {cursor}\nevent: update\ndata: {_json(payload)}\n\n"
            state = db.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not state or state["status"] not in ("queued", "running"):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{run_id}/cancel")
async def cancel_run(book_id: str, run_id: str):
    db = book_db(book_id)
    if not db.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Run not found")
    signal = _cancel_signals.get((book_id, run_id))
    if signal:
        signal.set()
    db.execute("UPDATE agent_runs SET status='canceling',updated_at=? WHERE id=? AND status IN ('queued','running')", (_now(), run_id))
    db.commit()
    _event(db, run_id, None, "canceling", "收到取消请求")
    return {"ok": True, "run_id": run_id}


@router.post("/{run_id}/retry")
async def retry_run(book_id: str, run_id: str):
    db = book_db(book_id)
    previous = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not previous:
        raise HTTPException(status_code=404, detail="Run not found")
    if previous["status"] in ("queued", "running", "canceling"):
        raise HTTPException(status_code=409, detail="运行中的任务不能重试")
    failed = db.execute("SELECT * FROM agent_run_tasks WHERE run_id=? AND status!='published'", (run_id,)).fetchall()
    refs = [{"chapter_idx": r["chapter_idx"], "paragraph_index": r["paragraph_index"], "fingerprint": r["fingerprint"]} for r in failed]
    if not refs:
        return {"id": None, "status": "nothing_to_do", "estimated_count": 0}
    old = json.loads(previous["request_json"])
    return _create_run(book_id, old, selected_refs=refs)


@router.post("/{run_id}/adopt")
async def adopt_pending(book_id: str, run_id: str, body: dict = Body(...)):
    task_id = body.get("task_id")
    if not isinstance(task_id, str):
        raise HTTPException(status_code=422, detail="task_id 必须是字符串")
    db = book_db(book_id)
    row = db.execute("SELECT * FROM agent_run_tasks WHERE id=? AND run_id=?", (task_id, run_id)).fetchone()
    if not row or row["status"] not in ("pending", "conflict"):
        raise HTTPException(status_code=404, detail="待核稿不存在")
    task = dict(row)
    paragraph = get_paragraph(book_id, task["chapter_idx"], task["paragraph_index"])
    note = db.execute(
        "SELECT * FROM agent_notes WHERE chapter_idx=? AND paragraph_index=? AND fingerprint=? "
        "AND state IN ('pending','conflict') ORDER BY updated_at DESC LIMIT 1",
        (task["chapter_idx"], task["paragraph_index"], task["fingerprint"]),
    ).fetchone()
    if not note:
        raise HTTPException(status_code=404, detail="候选稿记录不存在")
    if paragraph["fingerprint"] != task["fingerprint"] or current_baseline(db, task["chapter_idx"], paragraph) != json.loads(note["baseline_json"]):
        db.execute("UPDATE agent_run_tasks SET status='conflict',updated_at=? WHERE id=?", (_now(), task_id))
        db.execute("UPDATE agent_notes SET state='conflict',updated_at=? WHERE id=?", (_now(), note["id"]))
        db.commit()
        raise HTTPException(status_code=409, detail="原文或已有解释在任务运行期间发生变化；候选稿已保留")
    db.execute("UPDATE agent_notes SET state='published',updated_at=? WHERE id=?", (_now(), note["id"]))
    db.execute("UPDATE agent_run_tasks SET status='published',updated_at=? WHERE id=?", (_now(), task_id))
    remaining = db.execute("SELECT status FROM agent_run_tasks WHERE run_id=? AND id!=?", (run_id, task_id)).fetchall()
    if all(item["status"] == "published" for item in remaining):
        db.execute("UPDATE agent_runs SET status='completed',updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()
    _event(db, run_id, task_id, "adopted", "读者采纳待核稿", {"note_id": note["id"]})
    return {"ok": True, "note_id": note["id"]}


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


async def cancel_book_runs(book_id: str):
    _deleting_books.add(book_id)
    active = [(key, task) for key, task in list(_run_tasks.items()) if key[0] == book_id]
    for key, _ in active:
        signal = _cancel_signals.get(key)
        if signal:
            signal.set()
    if active:
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)


def allow_book_runs(book_id: str):
    _deleting_books.discard(book_id)
