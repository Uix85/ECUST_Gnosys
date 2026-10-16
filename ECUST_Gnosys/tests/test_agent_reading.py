import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "backend"))
sys.path.insert(0, str(TESTS_DIR))

import agent_book
import agent_runs
from scripted_model import ScriptedModel, request_text          # noqa: E402
from schema import close_db, get_db


BOOK_HTML = (
    "<p>重复的第一段，有<strong>非BMP字符𠮷</strong>和重要说明。第二句补充上下文。<br>"
    "重复的第一段，有<strong>非BMP字符𠮷</strong>和重要说明。第二句补充上下文。</p>"
)


class Script:
    """脚本容器 + 请求记录（假模型从这里取动作、往这里写记录）。"""
    script = []
    calls = []

    @classmethod
    def reset(cls, script=None):
        cls.script[:] = script or []
        cls.calls.clear()

    @classmethod
    def holder(cls):
        return {"script": cls.script, "calls": cls.calls}


class AgentReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book_id = "fixture_data"
        self.folder = Path(self.temp.name) / self.book_id
        self.folder.mkdir()
        self.db_path = str(self.folder / "book.db")
        self.db = get_db(self.db_path)
        self.db.execute(
            "INSERT INTO books(title,processed_at,total_chaps,total_paras) VALUES(?,?,?,?)",
            ("Fixture", "2026-10-16", 1, 2),
        )
        self.db.commit()
        self.path_patch = patch.object(agent_book, "BOOKS_DIR", self.temp.name)
        self.path_patch.start()
        self.html_patch = patch.object(agent_book, "chapter_html", return_value=({"title": "Fixture chapter"}, BOOK_HTML, 1))
        self.html_patch.start()
        Script.reset()
        # 生产基座是 Agents SDK，需要模型对象 → 这里注入脚本化假模型
        self.engine_patch = patch.object(
            agent_runs, "_engine_model", lambda config: ScriptedModel(Script.holder()))
        self.engine_patch.start()

    async def asyncTearDown(self):
        for (book_id, _), task in list(agent_runs._run_tasks.items()):
            if book_id == self.book_id:
                await asyncio.gather(task, return_exceptions=True)
        self.engine_patch.stop()
        self.html_patch.stop()
        self.path_patch.stop()
        close_db(self.db_path)
        self.temp.cleanup()

    async def test_logical_positions_keep_duplicate_paragraphs_separate(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        self.assertEqual([p["paragraph_index"] for p in paragraphs], [0, 1])
        self.assertEqual(paragraphs[0]["text"], paragraphs[1]["text"])
        self.assertEqual(paragraphs[0]["fingerprint"], paragraphs[1]["fingerprint"])
        self.assertIn("𠮷", paragraphs[0]["text"])

    async def test_exact_quote_validation_and_legacy_note_merge(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        source = paragraphs[0]["text"]
        candidate = {"short": "要点。", "detail": "原文明确说“重复的第一段”。", "sentences": ["", "说明。"]}
        self.assertEqual(agent_runs._validate_candidate(candidate, [source]), candidate)
        candidate["detail"] = "原文说“未经核验的引文”。"
        with self.assertRaisesRegex(ValueError, "引文未在"):
            agent_runs._validate_candidate(candidate, [source])
        self.db.execute(
            "INSERT INTO semantic_layer(book_id,text_hash,content,kind,source_text) VALUES(?,?,?,?,?)",
            (1, agent_book.text_hash(source), "旧解释", "para", source),
        )
        self.db.execute(
            "INSERT INTO agent_notes(id,chapter_idx,paragraph_index,fingerprint,source_text,short,detail,sentences,review_json,baseline_json,state,model,spec_version,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("note-position-1", 0, 1, paragraphs[1]["fingerprint"], source, "新位置解释", "新位置细节", "[]", "{}", "{}", "published", "test", "v1", "2026-10-16T00:00:00.000001"),
        )
        self.db.commit()
        items = agent_book.list_paragraph_notes(self.book_id, 0)
        self.assertEqual([item["origin"] for item in items], ["legacy", "agent"])
        self.assertEqual(items[0]["short"], "旧解释")
        self.assertEqual(items[1]["short"], "新位置解释")
        agent_book.invalidate_generated_notes(self.db, source)
        items = agent_book.list_paragraph_notes(self.book_id, 0)
        self.assertEqual([item["origin"] for item in items], ["legacy", "legacy"])

    async def test_generation_review_and_pending_adoption_conflict(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        source = paragraphs[0]["text"]
        # One target sentence and one second sentence; preserve an empty slot for the first.
        Script.script[:] = [
            ("load_skill", {"name": "book-reading"}),
            ("read_book_paragraph", {"chapter_idx": 0, "paragraph_index": 0}),
            ("verify_source_quote", {"quote": "重复的第一段", "chapter_idx": 0, "paragraph_index": 0}),
            ("submit_result", {"short": "解释要点。", "detail": "解释其上下文。", "sentences": ["", "补充第二句。"]}),
            ("submit_review", {"approved": False, "issues": ["需补足原文的论证关系"], "alternatives": ["另一种读法"]}),
            ("submit_result", {"short": "修订要点。", "detail": "修订后的具体解释。", "sentences": ["", "说明第二句的关系。"]}),
            ("submit_review", {"approved": False, "issues": ["仍缺少依据"], "alternatives": []}),
        ]
        run = agent_runs._create_run(self.book_id, {"paragraph_ref": {
            "chapter_idx": 0, "paragraph_index": 0, "fingerprint": paragraphs[0]["fingerprint"]},
            "mode": "overwrite", "spec_type": "general"})
        worker = agent_runs._run_tasks[(self.book_id, run["id"])]
        await asyncio.wait_for(worker, 2)
        state = self.db.execute("SELECT status FROM agent_runs WHERE id=?", (run["id"],)).fetchone()["status"]
        self.assertEqual(state, "needs_review")
        task = self.db.execute("SELECT * FROM agent_run_tasks WHERE run_id=?", (run["id"],)).fetchone()
        self.assertEqual(task["status"], "pending")
        note = self.db.execute("SELECT * FROM agent_notes WHERE state='pending'").fetchone()
        self.assertIsNotNone(note)
        self.assertEqual(json.loads(note["sentences"]), ["", "说明第二句的关系。"])
        # A concurrent manual note edit changes the baseline and blocks publication.
        self.db.execute(
            "INSERT INTO semantic_layer(book_id,text_hash,content,kind,source_text,updated_at) VALUES(?,?,?,?,?,?)",
            (1, agent_book.text_hash(source), "manual edit", "para", source, "later"),
        )
        self.db.commit()
        with self.assertRaises(Exception) as caught:
            await agent_runs.adopt_pending(self.book_id, run["id"], {"task_id": task["id"]})
        self.assertEqual(getattr(caught.exception, "status_code", None), 409)
        self.assertEqual(self.db.execute("SELECT state FROM agent_notes WHERE id=?", (note["id"],)).fetchone()["state"], "conflict")

    async def test_approved_result_publishes_and_tools_keep_call_ids(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        Script.script[:] = [
            ("submit_result", {"short": "要点说明。", "detail": "具体说明。", "sentences": ["第一句解释。", "第二句解释。"]}),
            ("submit_review", {"approved": True, "issues": [], "alternatives": ["存在另一种读法"]}),
        ]
        run = agent_runs._create_run(self.book_id, {"paragraph_ref": {
            "chapter_idx": 0, "paragraph_index": 1, "fingerprint": paragraphs[1]["fingerprint"]},
            "mode": "fill_missing", "spec_type": "general"})
        await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, run["id"])], 2)
        self.assertEqual(self.db.execute("SELECT status FROM agent_runs WHERE id=?", (run["id"],)).fetchone()["status"], "completed")
        note = self.db.execute("SELECT state,review_json FROM agent_notes WHERE chapter_idx=0 AND paragraph_index=1").fetchone()
        self.assertEqual(note["state"], "published")
        self.assertIn("另一种读法", note["review_json"])
        # 材料与工具结果都要进到模型看到的上下文里
        last = request_text(Script.calls[-1])
        self.assertIn("material_type", last)
        self.assertGreater(len(last), 100)

    async def test_retry_only_reprocesses_unsuccessful_paragraphs(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        run = agent_runs._create_run(self.book_id, {"paragraph_ref": {
            "chapter_idx": 0, "paragraph_index": 0, "fingerprint": paragraphs[0]["fingerprint"]},
            "mode": "overwrite", "spec_type": "general"})
        await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, run["id"])], 2)
        self.assertEqual(self.db.execute("SELECT status FROM agent_run_tasks WHERE run_id=?", (run["id"],)).fetchone()["status"], "failed")
        Script.script[:] = [
            ("submit_result", {"short": "要点说明。", "detail": "具体说明。", "sentences": ["第一句解释。", "第二句解释。"]}),
            ("submit_review", {"approved": True, "issues": [], "alternatives": []}),
        ]
        retried = await agent_runs.retry_run(self.book_id, run["id"])
        self.assertEqual(retried["estimated_count"], 1)
        await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, retried["id"])], 2)
        self.assertEqual(self.db.execute("SELECT status FROM agent_run_tasks WHERE run_id=?", (retried["id"],)).fetchone()["status"], "published")

    async def test_fastapi_create_and_detail_routes(self):
        _, paragraphs, _ = agent_book.logical_paragraphs(self.book_id, 0)
        Script.script[:] = [
            ("submit_result", {"short": "要点说明。", "detail": "具体说明。", "sentences": ["第一句解释。", "第二句解释。"]}),
            ("submit_review", {"approved": True, "issues": [], "alternatives": []}),
        ]
        app = FastAPI()
        app.include_router(agent_runs.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://reader.test") as client:
            response = await client.post(f"/api/agent/{self.book_id}/runs", json={
                "paragraph_ref": {"chapter_idx": 0, "paragraph_index": 0, "fingerprint": paragraphs[0]["fingerprint"]},
                "mode": "overwrite", "spec_type": "general",
            })
            self.assertEqual(response.status_code, 200, response.text)
            run_id = response.json()["id"]
            await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, run_id)], 2)
            detail = await client.get(f"/api/agent/{self.book_id}/runs/{run_id}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["status"], "completed")
            self.assertEqual(detail.json()["counts"], {"published": 1})


if __name__ == "__main__":
    unittest.main()
