"""Shared execution context and review lifecycle for long reading runs."""

import json
import time
import uuid

from fastapi import HTTPException
from agent.tools.book import book_db, get_paragraph
from agent.runtime.config import load_config
from agent.runtime.engine import AgentTask, LoopBudget, SubmitSpec, TaskSpec, build_model
from agent.runs.records import _event
from agent.tools.reading import book_reading_tools
from agent.layers.policy import (
    RESULT_PARAMS,
    SECTIONS,
    markdown,
    review_parameters,
    spec_text,
    validate_result,
    validate_review,
)
from reader.workspace import dumps, now, save_note


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

    async def run(self):
        with self.db:
            self.db.execute("UPDATE agent_runs SET status='running',updated_at=? WHERE id=?", (now(), self.run_id))
        if self.layer == 'L4':
            from agent.layers.l4_concept import generate
        elif self.layer == 'L1':
            from agent.layers.l1_book import generate
        else:
            from agent.layers.l2_chapter import generate_legacy as generate
        self.candidate = await generate(self)
        self.persist()
        review = await self.execute('你是独立复核者。核查内容有原文依据、覆盖范围没有夸大、范围/论证结构准确、每个引文位置正确。只提交 approved/issues/alternatives。',
                                    {'candidate': self.candidate, 'coverage': [{k:v for k,v in x.items() if k != 'result'} for x in self.coverage]}, review=True)
        if not review['approved']:
            if self.layer == 'L4':
                from agent.layers.l4_concept import generate
                self.candidate = await generate(self, review)
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
