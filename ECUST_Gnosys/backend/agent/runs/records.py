"""Database and paragraph-processing details for agent runs."""

import json
import os
import uuid
from datetime import datetime

from fastapi import HTTPException

from agent.layers.policy import markdown
from agent.tools.book import book_db, current_baseline, get_paragraph
from agent.runtime.engine import AgentError
from reader.book_service import BOOKS_DIR
from reader.workspace import now, save_note


_deleting_books: set[str] = set()


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def is_book_deleting(book_id: str) -> bool:
    return book_id in _deleting_books


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




def is_layer_run(db, run_id):
    return db.execute('SELECT 1 FROM layer_results WHERE run_id=?', (run_id,)).fetchone() is not None

def serialize_layer_run(db, run_id):
    row = db.execute('SELECT r.*,x.candidate_json,x.review_json,x.coverage_json FROM agent_runs r JOIN layer_results x ON r.id=x.run_id WHERE r.id=?', (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    request = json.loads(row['request_json'])
    draft = db.execute('SELECT id,state FROM layer_drafts WHERE run_id=? ORDER BY updated_at DESC LIMIT 1', (run_id,)).fetchone()
    return {**dict(row), 'request': request, 'layer': request['layer'], 'target': request.get('target', {}),
            'coverage': json.loads(row['coverage_json']), 'candidate': json.loads(row['candidate_json'] or 'null'),
            'review': json.loads(row['review_json'] or 'null'), 'draft': dict(draft) if draft else None, 'tasks': [], 'counts': {}}

def adopt_layer_draft(book_id, run_id, draft_id):
    db = book_db(book_id)
    row = db.execute("SELECT * FROM layer_drafts WHERE id=? AND run_id=? AND state IN ('pending','conflict')", (draft_id, run_id)).fetchone()
    if not row:
        raise HTTPException(404, '待核稿不存在')
    target = json.loads(row['target_json'])
    result = json.loads(row['result_json'])
    # Recheck current text before manual adoption. Note baseline still protects manual edits.
    for source in result.get('sources', []):
        original = get_paragraph(book_id, source['chapter_idx'], source['paragraph_index'])
        if source['quote'] not in original['text'] or source.get('fingerprint', original['fingerprint']) != original['fingerprint']:
            raise HTTPException(409, '原文已变化；候选稿已保留')
    coverage = json.loads(db.execute('SELECT coverage_json FROM layer_results WHERE run_id=?', (run_id,)).fetchone()[0])
    for chunk in coverage:
        for ref in chunk.get('references', []):
            if get_paragraph(book_id, ref['chapter_idx'], ref['paragraph_index'])['fingerprint'] != ref['fingerprint']:
                raise HTTPException(409, '覆盖范围内原文已变化；候选稿已保留')
    note = save_note(book_id, row['layer'], target, markdown(result), json.loads(row['baseline_json']), 'reader-adopted')
    with db:
        db.execute("UPDATE layer_drafts SET state='published' WHERE id=?", (draft_id,))
        db.execute("UPDATE agent_runs SET status='completed',updated_at=? WHERE id=?", (now(), run_id))
    return note
