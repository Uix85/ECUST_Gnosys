"""Canonical reading positions and user-owned notes shared by UI and agents."""

import json
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response
from bs4 import BeautifulSoup

from agent.book import book_db, fingerprint, get_paragraph, logical_paragraphs, normalize_text, list_paragraph_notes
from reader.book_service import load_book_cached, _get_toc_pages, _build_chapters
from reader.import_epub import clean_html_content

router = APIRouter(prefix='/api/workspace/{book_id}', tags=['workspace'])


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def chapters(book_id):
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(404, '书籍不存在')
    return _build_chapters(_get_toc_pages(book_id, book))


def outline_nodes(book_id):
    from agent.outline import structure
    nodes = []
    for ci, chapter in enumerate(chapters(book_id)):
        _, paras, _ = logical_paragraphs(book_id, ci)
        root = {'id': f'c{ci}', 'parent_id': None, 'title': chapter['title'], 'path': [chapter['title']],
                'chapter_idx': ci, 'paragraph_index': 0, 'end': len(paras), 'depth': 0,
                'fingerprint': fingerprint('\n'.join(p['text'] for p in paras))}
        nodes.append(root)
        stack = [(None, root)]
        heads = structure(book_id, ci)['headings']
        for i, head in enumerate(heads):
            pi, level = head['paragraph_index'], head['level']
            if pi == 0 and normalize_text(head['title']) == normalize_text(chapter['title']):
                continue
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            end = next((h['paragraph_index'] for h in heads[i+1:] if h['level'] <= level), len(paras))
            parent = stack[-1][1]
            node = {'id': f'c{ci}:p{pi}', 'parent_id': parent['id'], 'title': head['title'],
                    'path': parent['path'] + [head['title']], 'chapter_idx': ci, 'paragraph_index': pi,
                    'end': end, 'depth': parent['depth'] + 1,
                    'fingerprint': fingerprint('\n'.join(p['text'] for p in paras[pi:end]))}
            nodes.append(node)
            stack.append((level, node))
    return nodes


def reference(book_id, value):
    if not isinstance(value, dict):
        raise HTTPException(422, '引用格式不正确')
    if value.get('node_id'):
        node = next((n for n in outline_nodes(book_id) if n['id'] == value['node_id']), None)
        if not node:
            raise HTTPException(409, '目录引用已失效')
        result = {**node, 'node_id': node['id'], 'kind': 'node', 'label': ' - '.join(node['path'])}
    else:
        ci, pi = value.get('chapter_idx'), value.get('paragraph_index')
        if type(ci) is not int or type(pi) is not int or min(ci, pi) < 0:
            raise HTTPException(422, '引用需要有效的章与段落位置')
        p = get_paragraph(book_id, ci, pi)
        snapshot = value.get('quote') or p['text']
        if not isinstance(snapshot, str) or not snapshot or snapshot not in p['text']:
            raise HTTPException(409, '划线内容不在该段原文中，请重新划选')
        result = {'kind': 'selection', 'chapter_idx': ci, 'paragraph_index': pi,
                  'fingerprint': p['fingerprint'], 'quote': snapshot, 'label': snapshot[:60]}
    if value.get('fingerprint') and value['fingerprint'] != result['fingerprint']:
        raise HTTPException(409, '原文已变化，引用失效；快照已保留')
    return result


def check_references(book_id, values):
    if not isinstance(values, list) or len(values) > 20:
        raise HTTPException(422, '一次最多 20 处引用')
    return [reference(book_id, v) for v in values]


def note_key(layer, target):
    if layer == 'L1':
        return 'L1:book'
    if layer == 'L2':
        return f"L2:{target['chapter_idx']}"
    if layer in ('L3', 'L4'):
        return f"{layer}:{target['chapter_idx']}:{target['paragraph_index']}" + (':' + normalize_text(target['term']) if layer == 'L4' else '')
    if layer == 'chat':
        return 'chat:' + target['message_id']
    raise HTTPException(422, '未知层级')


def _chapter_count(book_id):
    """章数：有定位表就直接问它（L3 逐条校验时不用重跑渲染管线切章）。"""
    from reader import locator
    if locator.has_locator(book_id):
        return locator.chapter_count(book_id)
    return len(chapters(book_id))


def validate_target(book_id, layer, target):
    if not isinstance(target, dict):
        raise HTTPException(422, 'target 必须是对象')
    if layer in ('L2', 'L3', 'L4'):
        ci = target.get('chapter_idx')
        if type(ci) is not int or ci < 0 or ci >= _chapter_count(book_id):
            raise HTTPException(422, '章节位置无效')
    if layer in ('L3', 'L4'):
        reference(book_id, target)
    if layer == 'L4' and (not isinstance(target.get('term'), str) or not target['term'].strip()):
        raise HTTPException(422, '需要术语')
    if layer == 'chat':
        if not book_db(book_id).execute("SELECT 1 FROM chat_messages WHERE id=? AND role='assistant'", (target.get('message_id'),)).fetchone():
            raise HTTPException(404, '回答不存在')
    try:
        return note_key(layer, target)
    except KeyError as exc:
        raise HTTPException(422, '笔记目标不完整') from exc


def _first_text_line(content):
    for line in (content or '').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            return line
    return ''


def _compose_l3_content(short, detail, sentences):
    """旧语义层 L3 的三段内容合成为一份可编辑 Markdown。"""
    short = (short or '').strip()
    detail = (detail or '').strip()
    sections = []
    if short:
        sections.append('## 简短解释\n\n' + short)
    if detail and detail != short:
        sections.append('## 详细解释\n\n' + detail)
    items = []
    for sentence in sentences or []:
        text = sentence.get('text', '') if isinstance(sentence, dict) else str(sentence)
        if text.strip():
            items.append(f'{len(items) + 1}. {text.strip()}')
    if items:
        sections.append('## 逐句解释\n\n' + '\n'.join(items))
    return '\n\n'.join(sections)


def get_note(book_id, layer, target):
    key = validate_target(book_id, layer, target)
    db = book_db(book_id)
    row = db.execute('SELECT * FROM workspace_notes WHERE key=?', (key,)).fetchone()
    if row:
        return {**dict(row), 'target': json.loads(row['target_json'])}
    content, legacy_version = '', ''
    if layer == 'L1':
        old = db.execute('SELECT content,updated_at FROM book_layer LIMIT 1').fetchone()
    elif layer == 'L2':
        from agent.tools import _chapter_layer_row
        bid = db.execute('SELECT id FROM books LIMIT 1').fetchone()[0]
        old = _chapter_layer_row(db, bid, target['chapter_idx'], book_id)
    else:
        old = None
    if old:
        content = old['content'] or ''
        legacy_version = fingerprint(content)
    if layer == 'L3':
        old = next((n for n in list_paragraph_notes(book_id, target['chapter_idx']) if n['paragraph_index'] == target['paragraph_index']), None)
        if old:
            content = _compose_l3_content(old.get('short'), old.get('detail'), old.get('sentences'))
            legacy_version = fingerprint(dumps(old))
    if layer == 'L4':
        old = db.execute('SELECT content FROM concept_layer WHERE term=?', (target['term'],)).fetchone()
        if old:
            content = old['content'] or ''
            legacy_version = fingerprint(content)
    return {'key': key, 'layer': layer, 'target': target, 'content': content, 'revision': 0,
            'legacy_version': legacy_version, 'updated_at': '', 'model': 'legacy' if old else ''}


def baseline(book_id, layer, target):
    note = get_note(book_id, layer, target)
    return {'revision': note['revision'], 'content': note['content'], 'legacy_version': note.get('legacy_version', '')}


def save_note(book_id, layer, target, content, expected, model='manual'):
    key = validate_target(book_id, layer, target)
    db = book_db(book_id)
    current = baseline(book_id, layer, target)
    if expected != current:
        raise HTTPException(409, {'message': '笔记已由其他操作修改，当前输入已保留。请另存或合并后重试。', 'current': get_note(book_id, layer, target)})
    if not isinstance(content, str) or len(content) > 2_000_000:
        raise HTTPException(422, '笔记内容过长或格式错误')
    with db:
        db.execute('INSERT INTO workspace_notes(key,layer,target_json,content,revision,updated_at,model) VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET content=excluded.content,revision=excluded.revision,updated_at=excluded.updated_at,model=excluded.model',
                   (key, layer, dumps(target), content, current['revision'] + 1, now(), model))
    return get_note(book_id, layer, target)


@router.post('/reference/check')
async def validate_references(book_id: str, body: dict = Body(...)):
    return {'items': check_references(book_id, body.get('references', []))}


@router.get('/outline')
async def get_outline(book_id: str):
    return {'items': outline_nodes(book_id)}


@router.get('/chapter/{chapter_idx}')
async def get_chapter(book_id: str, chapter_idx: int):
    from agent.book import chapter_html, paragraph_blocks
    chapter, paras, total = logical_paragraphs(book_id, chapter_idx)
    _, original_html, _ = chapter_html(book_id, chapter_idx)
    # 定位表的段落只存文本；正文 HTML 与标签名从同一套切段现取，按下标对齐（切段口径是同一个函数）
    blocks = paragraph_blocks(original_html)
    notes = {p['paragraph_index']: p for p in list_paragraph_notes(book_id, chapter_idx)}
    for i, p in enumerate(paras):
        block = blocks[i] if i < len(blocks) else {}
        p['html'] = str(clean_html_content(BeautifulSoup(p.get('html') or block.get('html', ''), 'html.parser')))
        p['tag'] = p.get('tag') or block.get('tag', 'p')
        p['note'] = notes.get(p['paragraph_index'])
        manual = book_db(book_id).execute('SELECT content,model FROM workspace_notes WHERE key=?', (f"L3:{chapter_idx}:{p['paragraph_index']}",)).fetchone()
        if manual and manual['model'] == 'manual':
            p['note'] = {'short': _first_text_line(manual['content']), 'detail': manual['content'], 'sentences': []}
    images = []
    soup = clean_html_content(BeautifulSoup(original_html, 'html.parser'))
    for img in soup.find_all('img'):
        if not img.find_parent(('p', 'li', 'blockquote')):
            images.append(str(img))
    return {'title': chapter['title'], 'chapter_idx': chapter_idx, 'total': total, 'paragraphs': paras, 'images': images}


@router.get('/search')
async def search(book_id: str, q: str = Query(..., min_length=1, max_length=200), limit: int = Query(50, ge=1, le=200)):
    items = []
    for ci in range(len(chapters(book_id))):
        chapter, paras, _ = logical_paragraphs(book_id, ci)
        for p in paras:
            if normalize_text(q) in normalize_text(p['text']):
                at = max(0, p['text'].lower().find(q.lower()) - 70)
                items.append({'chapter_idx': ci, 'paragraph_index': p['paragraph_index'], 'fingerprint': p['fingerprint'],
                              'title': chapter['title'], 'excerpt': p['text'][at:at+240]})
                if len(items) == limit:
                    return {'items': items, 'truncated': True}
    return {'items': items, 'truncated': False}


@router.get('/state')
async def get_state(book_id: str):
    row = book_db(book_id).execute("SELECT value_json FROM reader_state WHERE key='progress'").fetchone()
    return json.loads(row[0]) if row else {}


@router.put('/state')
async def put_state(book_id: str, body: dict = Body(...)):
    ref = reference(book_id, body)
    with book_db(book_id) as db:
        db.execute("INSERT OR REPLACE INTO reader_state VALUES('progress',?)", (dumps(ref),))
    return ref


@router.get('/bookmarks')
async def bookmarks(book_id: str):
    items = []
    for row in book_db(book_id).execute('SELECT * FROM bookmarks ORDER BY created_at DESC'):
        ref = json.loads(row['reference_json'])
        try:
            reference(book_id, ref)
            valid = True
        except HTTPException:
            valid = False
        items.append({**dict(row), 'reference': ref, 'valid': valid})
    return {'items': items}


@router.post('/bookmarks')
async def add_bookmark(book_id: str, body: dict = Body(...)):
    ref = reference(book_id, body.get('reference', {}))
    ident = uuid.uuid4().hex
    with book_db(book_id) as db:
        db.execute('INSERT INTO bookmarks VALUES(?,?,?,?)', (ident, dumps(ref), str(body.get('label') or ref['label'])[:200], now()))
    return {'id': ident}


@router.delete('/bookmarks/{bookmark_id}')
async def delete_bookmark(book_id: str, bookmark_id: str):
    with book_db(book_id) as db:
        db.execute('DELETE FROM bookmarks WHERE id=?', (bookmark_id,))
    return {'ok': True}


@router.get('/notes')
async def list_notes(book_id: str):
    db = book_db(book_id)
    items = [dict(row, target=json.loads(row['target_json'])) for row in db.execute('SELECT * FROM workspace_notes ORDER BY updated_at DESC')]
    keys = {n['key'] for n in items}

    def push(note):
        if note['key'] not in keys and note['content']:
            keys.add(note['key'])
            items.append(note)

    chapter_list = chapters(book_id)
    for layer, target in [('L1', {})] + [('L2', {'chapter_idx': ci}) for ci in range(len(chapter_list))]:
        push(get_note(book_id, layer, target))
    # L3 直接从已读出的段落笔记拼条目：逐条 get_note 会把每章的段落笔记重扫一遍（1425 条 × 每章 0.4s）
    for ci in range(len(chapter_list)):
        for p in list_paragraph_notes(book_id, ci):
            target = {'chapter_idx': ci, 'paragraph_index': p['paragraph_index'],
                      'fingerprint': p['fingerprint']}
            key = note_key('L3', target)
            if key in keys:
                continue
            keys.add(key)
            items.append({'key': key, 'layer': 'L3', 'target': target,
                          'content': p['detail'] or p['short'], 'revision': 0,
                          'legacy_version': fingerprint(dumps(p)), 'updated_at': '',
                          'model': 'legacy'})
    return {'items': items}


@router.post('/note/read')
async def read_note(book_id: str, body: dict = Body(...)):
    return get_note(book_id, body.get('layer'), body.get('target', {}))


@router.put('/note')
async def put_note(book_id: str, body: dict = Body(...)):
    return save_note(book_id, body.get('layer'), body.get('target', {}), body.get('content'), body.get('baseline'))


@router.post('/note/export')
async def export_note(book_id: str, body: dict = Body(...)):
    note = get_note(book_id, body.get('layer'), body.get('target', {}))
    return Response(note['content'], media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(note['key'] + '.md')})
