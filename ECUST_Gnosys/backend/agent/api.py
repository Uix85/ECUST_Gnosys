"""Agent run and chat HTTP endpoints."""

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from agent.tools.book import book_db, current_baseline, get_paragraph
from agent.chat import ACTIVE, SIGNALS, message_item, sdk_session, session_record, start_message
from agent.runs.records import (
    _event,
    _json,
    _now,
    adopt_layer_draft,
    is_layer_run,
    serialize_layer_run,
    serialize_run,
)
from agent.runs.loop import (
    cancel_layer_run,
    cancel_run as request_cancel,
    create_layer_run,
    create_run as schedule_run,
)
from agent.remote import delete_remote_session
from reader.workspace import dumps, now

agent_router = APIRouter(prefix="/api/agent/{book_id}/runs", tags=["agent"])


@agent_router.post("")
async def create_run(book_id: str, body: dict = Body(...)):
    if body.get("layer", "L3") != "L3":
        return create_layer_run(book_id, body)
    return schedule_run(book_id, body)


@agent_router.get("")
async def list_runs(book_id: str, limit: int = Query(50, ge=1, le=200)):
    db = book_db(book_id)
    ids = db.execute("SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [(serialize_layer_run if is_layer_run(db, row["id"]) else serialize_run)(db, row["id"]) for row in ids]}


@agent_router.get("/pending")
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
    drafts = [dict(r, result=json.loads(r["result_json"]), target=json.loads(r["target_json"])) for r in db.execute("SELECT * FROM layer_drafts WHERE state IN ('pending','conflict') ORDER BY updated_at DESC")]
    return {"drafts": drafts, "items": [dict(r, sentences=json.loads(r["sentences"] or "[]"),
                            review=json.loads(r["review_json"] or "{}")) for r in rows]}


@agent_router.get("/{run_id}")
async def get_run(book_id: str, run_id: str):
    db = book_db(book_id)
    return (serialize_layer_run if is_layer_run(db, run_id) else serialize_run)(db, run_id)


@agent_router.get("/{run_id}/events")
async def run_events(book_id: str, run_id: str, request: Request, after: int = Query(0, ge=0)):
    db = book_db(book_id)
    if not db.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Run not found")

    async def stream():
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
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


@agent_router.post("/{run_id}/cancel")
async def cancel_run(book_id: str, run_id: str):
    if is_layer_run(book_db(book_id), run_id):
        return cancel_layer_run(book_id, run_id)
    return request_cancel(book_id, run_id)


@agent_router.post("/{run_id}/retry")
async def retry_run(book_id: str, run_id: str):
    db = book_db(book_id)
    previous = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not previous:
        raise HTTPException(status_code=404, detail="Run not found")
    if previous["status"] in ("queued", "running", "canceling"):
        raise HTTPException(status_code=409, detail="运行中的任务不能重试")
    if is_layer_run(db, run_id):
        return create_layer_run(book_id, json.loads(previous["request_json"]))
    failed = db.execute("SELECT * FROM agent_run_tasks WHERE run_id=? AND status!='published'", (run_id,)).fetchall()
    refs = [{"chapter_idx": r["chapter_idx"], "paragraph_index": r["paragraph_index"], "fingerprint": r["fingerprint"]} for r in failed]
    if not refs:
        return {"id": None, "status": "nothing_to_do", "estimated_count": 0}
    old = json.loads(previous["request_json"])
    return schedule_run(book_id, old, selected_refs=refs)


@agent_router.post("/{run_id}/adopt")
async def adopt_pending(book_id: str, run_id: str, body: dict = Body(...)):
    if is_layer_run(book_db(book_id), run_id):
        return adopt_layer_draft(book_id, run_id, body.get("draft_id"))
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
    db.execute('UPDATE workspace_notes SET content=?,revision=revision+1,model=?,updated_at=? WHERE key=?',
               (note['detail'], note['model'], _now(), f"L3:{task['chapter_idx']}:{task['paragraph_index']}"))
    db.execute("UPDATE agent_notes SET state='published',updated_at=? WHERE id=?", (_now(), note["id"]))
    db.execute("UPDATE agent_run_tasks SET status='published',updated_at=? WHERE id=?", (_now(), task_id))
    remaining = db.execute("SELECT status FROM agent_run_tasks WHERE run_id=? AND id!=?", (run_id, task_id)).fetchall()
    if all(item["status"] == "published" for item in remaining):
        db.execute("UPDATE agent_runs SET status='completed',updated_at=? WHERE id=?", (_now(), run_id))
    db.commit()
    _event(db, run_id, task_id, "adopted", "读者采纳待核稿", {"note_id": note["id"]})
    return {"ok": True, "note_id": note["id"]}




chat_router = APIRouter(prefix="/api/chat/{book_id}", tags=["chat"])

@chat_router.get('/sessions')
async def list_sessions(book_id: str):
    rows = book_db(book_id).execute(
        "SELECT s.* FROM chat_sessions s WHERE EXISTS "
        "(SELECT 1 FROM chat_messages m WHERE m.session_id=s.id) "
        "ORDER BY s.updated_at DESC")
    return {'items': [dict(r) for r in rows]}


@chat_router.post('/sessions')
async def create_session(book_id: str, body: dict = Body(default={})):
    ident, timestamp = uuid.uuid4().hex, now()
    title = str(body.get('title') or '新对话').strip()[:100] or '新对话'
    with book_db(book_id) as db:
        db.execute('INSERT INTO chat_sessions VALUES(?,?,?,?)', (ident, title, timestamp, timestamp))
    return {'id': ident, 'title': title}


@chat_router.patch('/sessions/{session_id}')
async def rename_session(book_id: str, session_id: str, body: dict = Body(...)):
    db = book_db(book_id)
    session_record(db, session_id)
    title = body.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise HTTPException(422, '名称需要 1–100 个字符')
    with db:
        db.execute('UPDATE chat_sessions SET title=?,updated_at=? WHERE id=?', (title.strip(), now(), session_id))
    return {'ok': True}


@chat_router.get('/sessions/{session_id}')
async def get_session(book_id: str, session_id: str):
    db = book_db(book_id)
    # 同一轮的用户/助手消息可能落在同一微秒；rowid 保证按插入顺序（用户在前）稳定输出。
    return {**session_record(db, session_id), 'messages': [message_item(book_id, r) for r in db.execute('SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at,rowid', (session_id,))]}


@chat_router.delete('/sessions/{session_id}')
async def delete_session(book_id: str, session_id: str):
    db = book_db(book_id)
    session_record(db, session_id)
    for key, signal in list(SIGNALS.items()):
        if key[:2] == (book_id, session_id):
            signal.set()
    pending = [task for key, task in list(ACTIVE.items()) if key[:2] == (book_id, session_id)]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if os.environ.get("GNOSYS_AGENT_MODE", "local") == "http":
        await delete_remote_session(f"chat:{book_id}:{session_id}")
    else:
        session = sdk_session(book_id, session_id)
        try:
            await session.clear_session()
        finally:
            session.close()
    with db:
        db.execute('DELETE FROM chat_sessions WHERE id=?', (session_id,))
    return {'ok': True}


@chat_router.post('/sessions/{session_id}/messages')
async def send(book_id: str, session_id: str, body: dict = Body(...)):
    return start_message(book_id, session_id, body)


@chat_router.post('/sessions/{session_id}/messages/{message_id}/retry')
async def retry(book_id: str, session_id: str, message_id: str, body: dict = Body(...)):
    db = book_db(book_id)
    session_record(db, session_id)
    row = db.execute("SELECT * FROM chat_messages WHERE id=? AND session_id=? AND role='assistant'", (message_id, session_id)).fetchone()
    if not row or row['status'] not in ('failed', 'canceled', 'interrupted'):
        raise HTTPException(409, '只能重试失败、停止或中断的回答')
    request_id = (row['request_id'] or '').removeprefix('answer:')
    user = db.execute("SELECT * FROM chat_messages WHERE session_id=? AND request_id=? AND role='user'", (session_id, request_id)).fetchone()
    return start_message(book_id, session_id, {'content': user['content'], 'references': json.loads(user['references_json']), 'request_id': body.get('request_id')}, retry_of=message_id)


@chat_router.post('/sessions/{session_id}/messages/{message_id}/cancel')
async def cancel(book_id: str, session_id: str, message_id: str):
    db = book_db(book_id)
    session_record(db, session_id)
    if not db.execute('SELECT 1 FROM chat_messages WHERE id=? AND session_id=?', (message_id, session_id)).fetchone():
        raise HTTPException(404, '消息不存在')
    signal = SIGNALS.get((book_id, session_id, message_id))
    if signal:
        signal.set()
    return {'ok': True}


@chat_router.get('/sessions/{session_id}/messages/{message_id}/events')
async def events(book_id: str, session_id: str, message_id: str, request: Request, after: int = Query(0, ge=0)):
    db = book_db(book_id)
    session_record(db, session_id)
    if not db.execute('SELECT 1 FROM chat_messages WHERE id=? AND session_id=?', (message_id, session_id)).fetchone():
        raise HTTPException(404, '消息不存在')
    try:
        cursor = max(after, int(request.headers.get('last-event-id', 0)))
    except ValueError:
        cursor = after

    async def stream():
        nonlocal cursor
        while True:
            for row in db.execute('SELECT * FROM chat_events WHERE message_id=? AND id>? ORDER BY id', (message_id, cursor)).fetchall():
                cursor = row['id']
                yield f"id: {cursor}\nevent: update\ndata: {dumps({'id': cursor, 'kind': row['kind'], 'data': json.loads(row['data_json'])})}\n\n"
            row = db.execute('SELECT status FROM chat_messages WHERE id=?', (message_id,)).fetchone()
            if not row or row['status'] not in ('queued', 'running'):
                yield 'event: end\ndata: {}\n\n'
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.3)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@chat_router.get('/sessions/{session_id}/export')
async def export(book_id: str, session_id: str):
    data = await get_session(book_id, session_id)
    lines = ['# ' + data['title']]
    for m in data['messages']:
        lines.append('\n## ' + ('读者' if m['role'] == 'user' else 'Agent') + '\n\n' + m['content'])
        for ref in m['references']:
            lines.append(f"\n> {ref.get('quote') or ref['label']}\n\n[原文](#source={ref['chapter_idx']}:{ref['paragraph_index']})")
        if m['status'] not in ('completed',):
            lines.append('\n状态：' + m['status'])
    return Response('\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="conversation.md"'})
