"""L1/L2/L4 runs: complete coverage, checked sources, review, revision, and drafts."""

import asyncio
import json
import time
import uuid

from fastapi import HTTPException
from agent.book import book_db, get_paragraph, logical_paragraphs, fingerprint
from agent.config import load_config
from agent.engine import AgentTask, AgentCancelled, LoopBudget, SubmitSpec, TaskSpec, build_model
from agent.run_service import _event, _now, _json
from agent.tools import book_reading_tools
from agent.tasks import validate_review, review_parameters, spec_text
from reader.workspace import chapters, baseline, get_note, save_note, validate_target, dumps, now

ACTIVE = {}
CANCEL = {}
SECTIONS = {
    'L1': ['核心问题', '思想地图', '论证主线', '章节关系'],
    'L2': ['章级总纲', '论证区块', '关键转折', '承上启下'],
}
RESULT_PARAMS = {
    'content': {'type': 'string'},
    'sources': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'chapter_idx': {'type': 'integer'}, 'paragraph_index': {'type': 'integer'}, 'quote': {'type': 'string'}},
        'required': ['chapter_idx', 'paragraph_index', 'quote'], 'additionalProperties': False}},
}


def validate_result(book_id, value, ledger, sections=()):
    if set(value) != {'content', 'sources'} or not isinstance(value['content'], str) or not value['content'].strip():
        raise ValueError('提交 content（Markdown）和 sources（原文位置及逐字引文），正文不能为空')
    import re
    for section in sections:
        if not re.search(r'^#{1,6}[^\n]*' + re.escape(section), value['content'], re.M):
            raise ValueError('Markdown 缺少结构标题：' + section)
    if not isinstance(value['sources'], list) or not value['sources']:
        raise ValueError('至少提供一处可核验的原文证据')
    for item in value['sources']:
        ci, pi, quote = item.get('chapter_idx'), item.get('paragraph_index'), item.get('quote')
        if type(ci) is not int or type(pi) is not int or not isinstance(quote, str) or not quote:
            raise ValueError('sources 需要 chapter_idx、paragraph_index 和非空 quote')
        if not any(quote in source for source in ledger.get((ci, pi), [])):
            raise ValueError('引文不在该位置的已读原文中')
        paragraph = get_paragraph(book_id, ci, pi)
        if quote not in paragraph['text']:
            raise ValueError('原文已变化或引文位置不正确')
        item['fingerprint'] = paragraph['fingerprint']
    return value


def markdown(result):
    lines = [result['content']]
    if result.get('sources'):
        lines.append('\n\n### 原文依据\n')
        for ref in result['sources']:
            ci, pi = ref['chapter_idx'], ref['paragraph_index']
            lines.append(f"- [第 {ci+1} 章 · 第 {pi+1} 段](#source={ci}:{pi})：{ref['quote']}")
    return '\n'.join(lines)


def is_layer_run(db, run_id):
    return db.execute('SELECT 1 FROM layer_results WHERE run_id=?', (run_id,)).fetchone() is not None


def serialize(db, run_id):
    row = db.execute('SELECT r.*,x.candidate_json,x.review_json,x.coverage_json FROM agent_runs r JOIN layer_results x ON r.id=x.run_id WHERE r.id=?', (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    request = json.loads(row['request_json'])
    draft = db.execute('SELECT id,state FROM layer_drafts WHERE run_id=? ORDER BY updated_at DESC LIMIT 1', (run_id,)).fetchone()
    return {**dict(row), 'request': request, 'layer': request['layer'], 'target': request.get('target', {}),
            'coverage': json.loads(row['coverage_json']), 'candidate': json.loads(row['candidate_json'] or 'null'),
            'review': json.loads(row['review_json'] or 'null'), 'draft': dict(draft) if draft else None, 'tasks': [], 'counts': {}}


def create(book_id, request):
    db = book_db(book_id)
    layer, target = request.get('layer'), request.get('target', {})
    key = validate_target(book_id, layer, target)
    if layer not in ('L1', 'L2', 'L4'):
        raise HTTPException(422, '此接口扩展处理 L1/L2/L4')
    if request.get('spec_type', 'general') not in ('general', 'hegel_helin'):
        raise HTTPException(422, '规范无效')
    if layer == 'L4':
        from agent.selection import gate
        from dataclasses import asdict
        p = get_paragraph(book_id, target['chapter_idx'], target['paragraph_index'])
        decision = gate(target['term'], p['text'])
        if decision.action not in ('pass', 'corrected'):
            raise HTTPException(422, asdict(decision))
        target = {**target, 'term': decision.term, 'fingerprint': p['fingerprint']}
        key = validate_target(book_id, layer, target)
    config = load_config()
    if not config.api_key:
        raise HTTPException(422, '请配置 AGENT_API_KEY')
    request = {**request, 'target': target, 'note_key': key}
    for row in db.execute("SELECT id,request_json FROM agent_runs WHERE status IN ('queued','running','canceling')"):
        if json.loads(row['request_json']).get('note_key') == key:
            return {'id': row['id'], 'existing': True}
    run_id, timestamp = uuid.uuid4().hex, now()
    bid = db.execute('SELECT id FROM books LIMIT 1').fetchone()[0]
    with db:
        db.execute('INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?)', (run_id, bid, 'queued', dumps(request), config.model, timestamp, timestamp, None))
        db.execute('INSERT INTO layer_results(run_id,baseline_json) VALUES(?,?)', (run_id, dumps(baseline(book_id, layer, target))))
    signal = CANCEL[(book_id, run_id)] = asyncio.Event()
    ACTIVE[(book_id, run_id)] = asyncio.create_task(worker(book_id, run_id, signal), name='layer-' + run_id)
    return {'id': run_id, 'status': 'queued', 'layer': layer, 'target': target}


class LayerRun:
    def __init__(self, book_id, run_id, signal):
        self.book_id, self.run_id, self.signal = book_id, run_id, signal
        self.db = book_db(book_id)
        self.row = self.db.execute('SELECT * FROM agent_runs WHERE id=?', (run_id,)).fetchone()
        self.request = json.loads(self.row['request_json'])
        self.layer, self.target = self.request['layer'], self.request.get('target', {})
        self.config = load_config()
        self.model = build_model(self.config)
        self.ledger, self.coverage, self.originals = {}, [], {}
        self.candidate = None
        self.budget = LoopBudget(self.config.max_requests, self.config.max_tool_calls, time.monotonic() + self.config.run_timeout)
        self.spec = spec_text(self.request.get('spec_type', 'general'))[0]

    async def event(self, stage, message, data=None):
        _event(self.db, self.run_id, None, stage, message, data)

    async def execute(self, prompt, materials, *, review=False, spec=None, final=False):
        # Each coverage chunk has the configured request/tool allowance; the run shares its deadline.
        self.budget.requests = self.budget.tool_calls = 0
        if spec is None:
            submit = SubmitSpec('submit_review' if review else 'submit_result', '提交复核' if review else '提交有位置证据的 Markdown',
                                review_parameters() if review else RESULT_PARAMS,
                                validate_review if review else lambda x: validate_result(self.book_id, x, self.ledger, SECTIONS.get(self.layer, []) if final else []),
                                ['approved', 'issues', 'alternatives'] if review else ['content', 'sources'])
            spec = TaskSpec(self.layer, prompt, materials, submit, tools=book_reading_tools(self.book_id, self.ledger)['tools'])
        return await AgentTask(spec, model=self.model, event=self.event, cancelled=self.signal, budget=self.budget, config=self.config).run()

    def remember(self, ci, paras):
        for p in paras:
            self.ledger.setdefault((ci, p['paragraph_index']), []).append(p['text'])
            self.originals[(ci, p['paragraph_index'])] = p['fingerprint']

    def persist(self):
        with self.db:
            self.db.execute('UPDATE layer_results SET candidate_json=?,coverage_json=? WHERE run_id=?',
                            (dumps(self.candidate) if self.candidate else None, dumps(self.coverage), self.run_id))

    async def summarize(self):
        from agent.outline import chapter_outline
        indices = list(range(len(chapters(self.book_id)))) if self.layer == 'L1' else [self.target['chapter_idx']]
        summaries = []
        for ci in indices:
            chapter, paras, _ = logical_paragraphs(self.book_id, ci)
            chunks, current, size = [], [], 0
            for p in paras:
                # Every character is covered, including a paragraph larger than a model window.
                for start in range(0, len(p['text']), 8000):
                    fragment = {**p, 'text': p['text'][start:start+8000], 'offset': start}
                    if size + len(fragment['text']) > 8000 and current:
                        chunks.append(current)
                        current, size = [], 0
                    current.append(fragment)
                    size += len(fragment['text'])
            if current:
                chunks.append(current)
            for k, chunk in enumerate(chunks):
                self.budget.check_running(self.signal)
                self.remember(ci, chunk)
                await self.event('coverage', f"读取第 {ci+1} 章 · {k+1}/{len(chunks)} 块", {'chapter_idx': ci, 'chunk': k, 'total_chunks': len(chunks)})
                material = {'chapter_idx': ci, 'title': chapter['title'], 'outline': chapter_outline(self.book_id, ci),
                            'source': [{'paragraph_index': p['paragraph_index'], 'offset': p['offset'], 'text': p['text']} for p in chunk]}
                result = await self.execute('依据这块完整原文进行分析。必须用 Markdown 标题分别写出：' + '、'.join(SECTIONS[self.layer]) + '。标注证据位置，区分原文与解释。论证区块中的每个区块用 [原文](#source=章序:段序) 指向其范围起点，编号从0开始。\n' + self.spec, material, final=True)
                summaries.append({'chapter_idx': ci, 'chunk': k, 'result': result})
                self.coverage.append({'chapter_idx': ci, 'chunk': k, 'total_chunks': len(chunks),
                                      'chars': sum(len(p['text']) for p in chunk), 'references': [{'chapter_idx': ci, 'paragraph_index': p['paragraph_index'], 'fingerprint': p['fingerprint']} for p in chunk], 'result': result})
                self.candidate = {'content': '\n\n'.join(x['result']['content'] for x in summaries),
                                  'sources': [s for x in summaries for s in x['result']['sources']]}
                self.persist()
        if len(summaries) == 1:
            return self.candidate
        while len(summaries) > 4:
            reduced = []
            for start in range(0, len(summaries), 4):
                group = summaries[start:start+4]
                result = await self.execute('合并这些已覆盖的原文块分析，保留核心问题、论证、章节关系、差异及可核验来源。不得扩大覆盖范围。', {'covered_chunks': group})
                reduced.append({'result': result})
            summaries = reduced
        purpose = ('全书层：核心问题、思想地图、论证主线、章节关系。必须依据所有章的完整覆盖记录，不能用目录抽样冒充全书分析。'
                   if self.layer == 'L1' else '章节层：章级总纲、按原文范围组织论证区块、关键转折及承上启下关系。每个区块给可定位的原文证据。')
        return await self.execute(purpose + '\n必须包含这些 Markdown 标题：' + '、'.join(SECTIONS[self.layer]) + '。论证区块逐个附上可跳转的原文位置链接。\n按块分析是参考，逐字引文必须回原文核对。输出 Markdown。\n' + self.spec, {'covered_chunks': summaries}, final=True)

    async def concept(self, review=None):
        from agent.concept import locate_spec, locate_materials, map_spec, map_materials
        from agent.concept_tools import _distribution
        term, ci, pi = self.target['term'], self.target['chapter_idx'], self.target['paragraph_index']
        dist = _distribution(self.book_id, term)
        cache_key = 'concept-map:' + term
        cached = self.db.execute('SELECT value_json FROM reader_state WHERE key=?', (cache_key,)).fetchone()
        mapping = json.loads(cached[0]) if cached and not review else None
        # Mapping cache carries source fingerprints; changed text invalidates it.
        if mapping and any(get_paragraph(self.book_id, r['chapter_idx'], r['paragraph_index'])['fingerprint'] != r['fingerprint'] for r in mapping['references']):
            mapping = None
        senses = mapping['map']['senses'] if mapping else None
        material = locate_materials(self.book_id, term, ci, pi, self.ledger, map_senses=senses)
        result = await self.execute('', {}, spec=locate_spec(book_id=self.book_id, materials=material, ledger=self.ledger, has_map=bool(mapping)))
        if not mapping or result['needs_mapping']:
            material = map_materials(term, ci, pi, dist['total'], dist['chapters'], senses)
            if review:
                material['revision_issues'] = review
            mapped = await self.execute('', {}, spec=map_spec(book_id=self.book_id, materials=material, ledger=self.ledger, term_chapter_count=len(dist['chapters'])))
            refs = []
            for sense in mapped['senses']:
                for r in sense['evidence']:
                    refs.append({**r, 'fingerprint': get_paragraph(self.book_id, r['chapter_idx'], r['paragraph_index'])['fingerprint']})
            mapping = {'map': mapped, 'references': refs}
            self.candidate = {'content': '\n\n'.join(s['reading'] for s in mapped['senses']), 'sources': [{k: r[k] for k in ('chapter_idx', 'paragraph_index', 'quote')} for r in refs]}
            self.persist()
            material = locate_materials(self.book_id, term, ci, pi, self.ledger, map_senses=mapped['senses'])
            result = await self.execute('', {}, spec=locate_spec(book_id=self.book_id, materials=material, ledger=self.ledger, has_map=True))
        if result['needs_mapping']:
            raise ValueError('补读建图后仍无足够证据；请查看待核材料')
        self.mapping = mapping
        for r in mapping['references']:
            self.remember(r['chapter_idx'], [get_paragraph(self.book_id, r['chapter_idx'], r['paragraph_index'])])
        parts = [f"## {term}\n\n{result['reading']}", f"### 日常读法\n{result['common_reading']}",
                 '### 定义与证据性质\n' + mapping['map']['has_definition'] + '\n' + (mapping['map']['definition_note'] or ''), '### 意义地图']
        for i, sense in enumerate(mapping['map']['senses'], 1):
            parts.append(f"{i}. {sense['reading']}\n\n适用范围：{sense['scope']} {sense['range_note'] or ''}\n\n演化：{'连接下一义项' if sense['evolves_to_next'] else '终点'}；{sense['distinction_note'] or ''}")
        return {'content': '\n\n'.join(parts), 'sources': [{k: r[k] for k in ('chapter_idx', 'paragraph_index', 'quote')} for r in mapping['references']]}

    async def run(self):
        with self.db:
            self.db.execute("UPDATE agent_runs SET status='running',updated_at=? WHERE id=?", (now(), self.run_id))
        self.candidate = await (self.concept() if self.layer == 'L4' else self.summarize())
        self.persist()
        review = await self.execute('你是独立复核者。核查内容有原文依据、覆盖范围没有夸大、范围/论证结构准确、每个引文位置正确。只提交 approved/issues/alternatives。',
                                    {'candidate': self.candidate, 'coverage': [{k:v for k,v in x.items() if k != 'result'} for x in self.coverage]}, review=True)
        if not review['approved']:
            if self.layer == 'L4':
                self.candidate = await self.concept(review)
            else:
                self.candidate = await self.execute('只修订指出的实质问题，保留可靠内容，交完整 Markdown 与 sources。\n' + self.spec,
                                                    {'candidate': self.candidate, 'review': review}, final=True)
            self.persist()
            review = await self.execute('独立复核修订稿，仅提交实质问题；检查原文与位置。', {'candidate': self.candidate}, review=True)
        with self.db:
            self.db.execute('UPDATE layer_results SET review_json=? WHERE run_id=?', (dumps(review), self.run_id))
        state = 'pending'
        expected = json.loads(self.db.execute('SELECT baseline_json FROM layer_results WHERE run_id=?', (self.run_id,)).fetchone()[0])
        if review['approved']:
            try:
                for (ci, pi), fp in self.originals.items():
                    if get_paragraph(self.book_id, ci, pi)['fingerprint'] != fp:
                        raise HTTPException(409, '原文已变化；候选稿已保留')
                save_note(self.book_id, self.layer, self.target, markdown(self.candidate), expected, self.row['model'])
                state = 'published'
                if self.layer == 'L4':
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO reader_state VALUES(?,?)', ('concept-map:' + self.target['term'], dumps(self.mapping)))
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                state = 'conflict'
        self.save_draft(review, state)
        status = 'completed' if state == 'published' else 'needs_review'
        with self.db:
            self.db.execute('UPDATE agent_runs SET status=?,updated_at=? WHERE id=?', (status, now(), self.run_id))
        await self.event(status, '生成已完成' if state == 'published' else '候选稿已保留，等待读者复核')

    def save_draft(self, review, state):
        expected = self.db.execute('SELECT baseline_json FROM layer_results WHERE run_id=?', (self.run_id,)).fetchone()[0]
        with self.db:
            self.db.execute('INSERT INTO layer_drafts VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (uuid.uuid4().hex, self.run_id, self.request['note_key'], self.layer, dumps(self.target), dumps(self.candidate), dumps(review), expected, state, now()))


async def worker(book_id, run_id, signal):
    run = None
    try:
        run = LayerRun(book_id, run_id, signal)
        await run.run()
    except (Exception, asyncio.CancelledError) as exc:
        db = book_db(book_id)
        status = 'canceled' if signal.is_set() or isinstance(exc, AgentCancelled) else 'failed'
        if run and run.candidate:
            run.save_draft({'approved': False, 'issues': [str(exc)]}, 'pending')
        with db:
            db.execute('UPDATE agent_runs SET status=?,error=?,updated_at=? WHERE id=?', (status, str(exc)[:1500], now(), run_id))
        _event(db, run_id, None, status, str(exc)[:500])
    finally:
        ACTIVE.pop((book_id, run_id), None)
        CANCEL.pop((book_id, run_id), None)


def cancel(book_id, run_id):
    signal = CANCEL.get((book_id, run_id))
    if signal:
        signal.set()
    return serialize(book_db(book_id), run_id)


def adopt(book_id, run_id, draft_id):
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
