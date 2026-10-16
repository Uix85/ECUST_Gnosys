"""Agent run HTTP endpoints."""

import asyncio
import json

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from agent.book import book_db, current_baseline, get_paragraph
from agent.run_service import (
    _event,
    _json,
    _now,
    serialize_run,
)
from agent.runs import cancel_run as request_cancel, create_run as schedule_run

router = APIRouter(prefix="/api/agent/{book_id}/runs", tags=["agent"])


@router.post("")
async def create_run(book_id: str, body: dict = Body(...)):
    return schedule_run(book_id, body)


@router.get("")
async def list_runs(book_id: str, limit: int = Query(50, ge=1, le=200)):
    db = book_db(book_id)
    ids = db.execute("SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [serialize_run(db, row["id"]) for row in ids]}


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
    return serialize_run(book_db(book_id), run_id)


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
    return request_cancel(book_id, run_id)


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
    return schedule_run(book_id, old, selected_refs=refs)


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


