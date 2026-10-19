"""Read-only, resumable book conversations and their persisted lifecycle."""

import asyncio
import json
import time
import uuid

from agents import SQLiteSession
from fastapi import HTTPException

from agent.tools.book import book_db, get_paragraph
from agent.runtime.config import load_config
from agent.runtime.loop import run_task
from agent.runtime.sdk import build_model
from agent.runtime.task import ExecutionContext, LoopBudget
from agent.tasks.chat import chat_task
from agent.tools.context import material_refs
from reader.workspace import check_references, dumps, now

# 进程内正在回答的任务：键为 (book_id, session_id, message_id)。
# 仅用于取消/清理；真正的状态以数据库中的 chat_messages 为准。
ACTIVE = {}
# 每个回答一个取消信号，停止接口置位后由 run_task 协作式退出。
SIGNALS = {}


def session_record(db, session_id):
    """读取对话记录；不存在时返回 404，避免后续操作出现悬空引用。"""
    row = db.execute('SELECT * FROM chat_sessions WHERE id=?', (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, '对话不存在')
    return dict(row)


def sdk_session(book_id, session_id):
    """创建映射到书库文件的 SDK 会话。

    SDK 历史写入独立表 `sdk_chat_sessions` / `sdk_chat_items`，与业务表
    隔离，便于失败时整体回滚。
    """
    db_path = book_db(book_id).execute('PRAGMA database_list').fetchone()[2]
    return SQLiteSession(session_id, db_path=db_path, sessions_table='sdk_chat_sessions', messages_table='sdk_chat_items')


def event(db, message_id, kind, data):
    """追加一条对话流事件（思考、工具、增量文本等），供前端按游标拉取。"""
    with db:
        db.execute('INSERT INTO chat_events(message_id,kind,data_json) VALUES(?,?,?)', (message_id, kind, dumps(data)))


def _message_trace(db, message_id):
    """把思考/工具事件还原成已完成的执行轨迹；旧消息没有这些事件时返回空轨迹。"""
    reasoning, tools, by_id = [], [], {}
    rows = db.execute(
        'SELECT kind,data_json FROM chat_events WHERE message_id=? ORDER BY id', (message_id,)
    ).fetchall()
    for row in rows:
        if row['kind'] not in ('reasoning', 'tool'):
            continue
        try:
            data = json.loads(row['data_json'])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if row['kind'] == 'reasoning':
            text = data.get('text')
            if isinstance(text, str) and text:
                reasoning.append(text)
            continue
        tid = data.get('id')
        item = by_id.get(tid)
        if item is None:
            item = {'id': tid, 'tool': data.get('tool') or '',
                    'args': data.get('args') or {}, 'status': data.get('status') or 'running'}
            if tid is not None:
                by_id[tid] = item
            tools.append(item)
        else:
            if data.get('tool'):
                item['tool'] = data['tool']
            if 'args' in data:
                item['args'] = data.get('args') or {}
            if data.get('status'):
                item['status'] = data['status']
        if data.get('error'):
            item['error'] = data['error']
    return {'reasoning': ''.join(reasoning), 'tools': tools}


def message_item(book_id, row):
    """把消息行展开为 API 视图：校验引用、补充事件游标与执行轨迹。

    引用按当前原文重新校验（valid 标记只影响展示，不修改库中记录）；
    event_cursor 供前端断线后从上次位置继续拉取事件。
    """
    item = dict(row)
    refs = json.loads(item.pop('references_json'))
    for ref in refs:
        try:
            check_references(book_id, [ref])
            ref['valid'] = True
        except HTTPException:
            ref['valid'] = False
    db = book_db(book_id)
    item['event_cursor'] = db.execute('SELECT COALESCE(MAX(id),0) FROM chat_events WHERE message_id=?', (item['id'],)).fetchone()[0]
    item['references'] = refs
    if item.get('role') == 'assistant':
        item['trace'] = _message_trace(db, item['id'])
    return item


def start_message(book_id, session_id, body, *, retry_of=None):
    """校验并登记一条用户消息，同时排队一个异步回答。

    幂等性由调用方提供的 `request_id` 保证：相同编号 + 相同内容直接返回
    已有回答；编号相同但内容不同则 409。同一对话同时只允许一个
    queued/running 的回答，避免历史交错。成功后分别写入 user/assistant
    两行，并启动后台协程。
    """
    db = book_db(book_id)
    session_record(db, session_id)
    config = load_config()
    if not config.api_key:
        raise HTTPException(422, '请配置 AGENT_API_KEY')
    content = body.get('content', '')
    if not isinstance(content, str) or len(content) > 30000:
        raise HTTPException(422, '消息过长或格式错误')
    refs = check_references(book_id, body.get('references', []))
    if not content.strip() and not refs:
        raise HTTPException(422, '请输入问题或添加原文引用')
    request_id = body.get('request_id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise HTTPException(422, '需要 request_id 以避免重复执行')
    # 幂等重放：同一 request_id 的用户消息已存在时不再重复建任务。
    prior = db.execute("SELECT * FROM chat_messages WHERE session_id=? AND request_id=? AND role='user'", (session_id, request_id)).fetchone()
    if prior:
        if prior['content'] != content or json.loads(prior['references_json']) != refs:
            raise HTTPException(409, '同一请求编号的内容不同')
        response = db.execute('SELECT * FROM chat_messages WHERE session_id=? AND request_id=?', (session_id, 'answer:' + request_id)).fetchone()
        return {'message_id': response['id'], 'status': response['status'], 'existing': True}
    # 单活跃回答约束：已有排队/运行中的回答时拒绝新问题。
    if db.execute("SELECT 1 FROM chat_messages WHERE session_id=? AND role='assistant' AND status IN ('queued','running')", (session_id,)).fetchone():
        raise HTTPException(409, '此对话正在回答，请先停止或等待完成')
    user_id, answer_id = uuid.uuid4().hex, uuid.uuid4().hex
    timestamp = now()
    with db:
        db.execute('INSERT INTO chat_messages(id,session_id,role,content,references_json,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (user_id, session_id, 'user', content, dumps(refs), 'completed', request_id, timestamp))
        db.execute('INSERT INTO chat_messages(id,session_id,role,content,references_json,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (answer_id, session_id, 'assistant', '', '[]', 'queued', 'answer:' + request_id, now()))
        db.execute('UPDATE chat_sessions SET updated_at=? WHERE id=?', (now(), session_id))
    key = (book_id, session_id, answer_id)
    signal = SIGNALS[key] = asyncio.Event()
    ACTIVE[key] = asyncio.create_task(answer(book_id, session_id, answer_id, content, refs, signal), name='chat-' + answer_id)
    return {'message_id': answer_id, 'user_id': user_id, 'status': 'queued', 'retry_of': retry_of}


async def answer(book_id, session_id, message_id, content, refs, signal):
    """执行一次对话回答，并把过程增量持久化到消息与事件表。

    流式文本每次增量都会更新 `chat_messages.content` 并追加 delta 事件，
    因此进程中断时已生成内容仍可展示。失败/取消时回滚 SDK 会话历史到
    本轮开始前的快照，避免半截工具调用污染后续上下文；展示文本保留。
    """
    db, session, previous = book_db(book_id), None, None
    text = ''
    try:
        config = load_config()
        budget = LoopBudget(config.max_requests, config.max_tool_calls, time.monotonic() + config.run_timeout)
        ledger = {}
        spec = chat_task(book_id, content, refs, config, ledger)

        async def emit(kind, message, data):
            """把 SDK 事件翻译为对话事件，并持续累积流式回答文本。"""
            nonlocal text
            data = data or {}
            if kind == 'model_start':
                event(db, message_id, 'status', {'message': '正在组织回答'})
            elif kind == 'tool_start':
                event(db, message_id, 'status', {'message': '正在检索或核对原文', 'tool': data.get('tool')})
                event(db, message_id, 'tool', data)
            elif kind in ('tool_end', 'tool_error'):
                event(db, message_id, 'tool', {key: value for key, value in data.items() if key != 'result'})
                if kind == 'tool_end':
                    material = material_refs(data.get('result'))
                    if material:
                        event(db, message_id, 'sources', material)
            elif kind == 'tool_budget_exhausted':
                event(db, message_id, 'status', {'message': message, **data})
            elif kind == 'text_delta':
                delta = data.get('text', '')
                text += delta
                with db:
                    db.execute('UPDATE chat_messages SET content=? WHERE id=?', (text, message_id))
                event(db, message_id, 'delta', {'text': delta})
            elif kind == 'reasoning_delta':
                if data.get('text'):
                    event(db, message_id, 'reasoning', {'text': data['text']})

        session = sdk_session(book_id, session_id)
        # 保存本轮开始前的 SDK 历史，失败时据此回滚。
        previous = await session.get_items()
        with db:
            db.execute("UPDATE chat_messages SET status='running' WHERE id=?", (message_id,))
        result = await run_task(spec, ExecutionContext(
            model=build_model(config), config=config, cancelled=signal,
            emit=emit, budget=budget, session=session,
        ))
        if not text and isinstance(result, str):
            text = result
        sources = []
        for ci, pi in ledger:
            paragraph = get_paragraph(book_id, ci, pi)
            sources.append({'chapter_idx': ci, 'paragraph_index': pi,
                            'fingerprint': paragraph['fingerprint'], 'kind': 'selection',
                            'quote': paragraph['text'][:240], 'label': f'第 {ci+1} 章 · 第 {pi+1} 段'})
        with db:
            db.execute("UPDATE chat_messages SET content=?,references_json=?,status='completed',error=NULL WHERE id=?", (text, dumps(sources), message_id))
        event(db, message_id, 'completed', {'status': 'completed'})
    except (Exception, asyncio.CancelledError) as exc:
        # Failed turns do not leave half a tool call in model history. Display text stays in SQLite.
        if session and previous is not None:
            await session.clear_session()
            if previous:
                await session.add_items(previous)
        status = 'canceled' if signal.is_set() else 'failed'
        with db:
            db.execute('UPDATE chat_messages SET content=?,status=?,error=? WHERE id=?', (text, status, str(exc)[:1500], message_id))
        event(db, message_id, 'completed', {'status': status, 'error': str(exc)[:500]})
    finally:
        if session:
            session.close()
        ACTIVE.pop((book_id, session_id, message_id), None)
        SIGNALS.pop((book_id, session_id, message_id), None)


def recover(book_id):
    """启动时把重启前未完成的回答标记为 interrupted。

    已流式写入的正文保留，前端可提示用户重试；状态不会停留在
    queued/running，避免新消息被“单活跃回答”规则永久阻塞。
    """
    db = book_db(book_id)
    with db:
        db.execute("UPDATE chat_messages SET status='interrupted',error='服务重启，未完成回答已保留，可重试' WHERE status IN ('queued','running')")
