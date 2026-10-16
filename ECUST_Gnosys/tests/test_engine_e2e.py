"""端到端：一段语义层任务跑完整链路（生产基座 smolagents）。

用假模型驱动，不需要 API key：
    uv run python -m unittest tests.test_engine_e2e -v

验证的是「整条链」而不是循环本身：
  建任务 → 生成 → 程序校验 → 独立复核 → 落库(agent_notes) → 事件留痕 → 用量记账

还有一个测试专门盯复合循环的设计意图：
  生成与修订复用同一个 agent（记忆延续），复核每次都是独立上下文。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "backend"))
sys.path.insert(0, str(TESTS_DIR))

import agent_book                                          # noqa: E402
import agent_runs                                          # noqa: E402
from scripted_model import ScriptedModel, new_script          # noqa: E402
from schema import close_db, get_db                        # noqa: E402

BOOK_HTML = (
    "<p>重复的第一段，有<strong>非BMP字符𠮷</strong>和重要说明。第二句补充上下文。<br>"
    "重复的第一段，有<strong>非BMP字符𠮷</strong>和重要说明。第二句补充上下文。</p>"
)

CANDIDATE = {"short": "要点", "detail": "整段大意", "sentences": ["第一句转述", "第二句转述"]}
REVIEW = {"approved": True, "issues": [], "alternatives": []}
# 复核不过 → 修订 → 再复核。修订稿用另一份内容，才能验证落库的确实是修订后的稿
REJECT = {"approved": False, "issues": ["第二句缺少原文依据"], "alternatives": []}
REVISED = {"short": "修好的要点", "detail": "修好后的大意", "sentences": ["第一句转述", "第二句转述"]}
# 脚本用真实工具名（Agents SDK 用 tool_use_behavior 声明终止工具，不需要改名）
HAPPY_SCRIPT = [("submit_result", CANDIDATE), ("submit_review", REVIEW)]
REVISE_SCRIPT = [("submit_result", CANDIDATE), ("submit_review", REJECT),
                 ("submit_result", REVISED), ("submit_review", REVIEW)]
USAGE = {"prompt_tokens": 120, "completion_tokens": 30}

# 假模型从这里取动作、往这里写请求记录
REC = new_script()


class EngineEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book_id = "fixture_data"
        folder = Path(self.temp.name) / self.book_id
        folder.mkdir()
        self.db_path = str(folder / "book.db")
        self.db = get_db(self.db_path)
        self.db.execute("INSERT INTO books(title,processed_at,total_chaps,total_paras) VALUES(?,?,?,?)",
                        ("Fixture", "2026-10-16", 1, 2))
        self.db.commit()

        self.patches = [
            patch.object(agent_book, "BOOKS_DIR", self.temp.name),
            patch.object(agent_book, "chapter_html",
                         return_value=({"title": "Fixture chapter"}, BOOK_HTML, 1)),
        ]
        for p in self.patches:
            p.start()
        # 生产用的是 OpenAIServerModel；测试注入等价的假模型
        self.patches.append(patch.object(agent_runs, "_engine_model",
                                         lambda config: ScriptedModel(REC)))
        self.patches[-1].start()

    async def asyncTearDown(self):
        for (book_id, _), task in list(agent_runs._run_tasks.items()):
            if book_id == self.book_id:
                await asyncio.gather(task, return_exceptions=True)
        for p in reversed(self.patches):
            p.stop()
        close_db(self.db_path)
        self.temp.cleanup()

    async def test_full_paragraph_chain(self):
        REC["script"][:] = HAPPY_SCRIPT
        REC["calls"].clear()

        run = agent_runs._create_run(self.book_id, {"paragraph_ref": {
            "chapter_idx": 0, "paragraph_index": 0}, "mode": "fill_missing"})
        self.assertTrue(run.get("id"), run)
        await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, run["id"])], timeout=30)

        # ① 落库：一条已发布的语义层解释，内容就是模型交的
        notes = self.db.execute(
            "SELECT chapter_idx,paragraph_index,short,detail,sentences,state FROM agent_notes").fetchall()
        self.assertEqual(len(notes), 1, notes)
        note = dict(notes[0])
        self.assertEqual(note["state"], "published")
        self.assertEqual(note["short"], CANDIDATE["short"])
        self.assertEqual(note["detail"], CANDIDATE["detail"])
        self.assertEqual(json.loads(note["sentences"]), CANDIDATE["sentences"])

        # ② 任务状态
        status = self.db.execute("SELECT status FROM agent_runs WHERE id=?", (run["id"],)).fetchone()
        self.assertEqual(status["status"], "completed")

        # ③ 事件留痕：三个阶段 + 重复核
        stages = [row["stage"] for row in self.db.execute(
            "SELECT stage FROM agent_run_events WHERE run_id=? ORDER BY id", (run["id"],))]
        for expected in ("generate", "review", "published", "model_response"):
            self.assertIn(expected, stages, f"缺少 {expected} 事件：{stages}")

        # ④ 用量记账（生成 1 次 + 复核 1 次）
        usage = [json.loads(row["data_json"]) for row in self.db.execute(
            "SELECT data_json FROM agent_run_events WHERE run_id=? AND stage='model_response' ORDER BY id",
            (run["id"],))]
        total = usage[-1]["total_tokens"]
        self.assertEqual(total, (USAGE["prompt_tokens"] + USAGE["completion_tokens"]) * 2,
                         f"token 统计不对：{usage}")

        # ⑤ 请求数：生成 + 复核各一次，没有多余往返
        self.assertEqual(len(REC["calls"]), 2,
                         f"模型请求数应为 2，实际 {len(REC['calls'])}")

    async def test_revision_reuses_agent_but_review_does_not(self):
        """复合循环的设计意图：修订复用生成阶段的 agent，复核永远开新的。"""
        REC["script"][:] = REVISE_SCRIPT
        REC["calls"].clear()

        run = agent_runs._create_run(self.book_id, {"paragraph_ref": {
            "chapter_idx": 0, "paragraph_index": 0}, "mode": "fill_missing"})
        await asyncio.wait_for(agent_runs._run_tasks[(self.book_id, run["id"])], timeout=30)

        # ① 落库的是**修订后**的稿
        note = dict(self.db.execute(
            "SELECT short,state FROM agent_notes").fetchone())
        self.assertEqual(note["short"], REVISED["short"], note)
        self.assertEqual(note["state"], "published")

        # ② 四个阶段各一次请求：生成 / 复核 / 修订 / 修后再复核
        self.assertEqual(len(REC["calls"]), 4,
                         f"模型请求数应为 4，实际 {len(REC['calls'])}")

        # ③ 复用意图逐阶段核对：只有修订那一阶段是复用的
        #    过滤掉 data_json 为空的事件（任务是级事件，不是某阶段的）
        runs = [json.loads(row["data_json"]) for row in self.db.execute(
            "SELECT data_json FROM agent_run_events WHERE run_id=? AND stage='run' "
            "AND data_json IS NOT NULL ORDER BY id", (run["id"],))]
        self.assertEqual([r["reused"] for r in runs], [False, False, True, False], runs)

        # ④ 复用确实携上了记忆：修订那次的上下文比生成那次长得多
        #    （历史里既有原文、上一稿与自己的工具调用记录）
        self.assertGreater(len(REC["calls"][2]["input"]), len(REC["calls"][0]["input"]) + 2,
                           "修订阶段没有带上生成阶段的历史")
        # ⑤ 复核是独立上下文：它的输入不比生成那次长
        self.assertLessEqual(len(REC["calls"][1]["input"]), len(REC["calls"][0]["input"]) + 2,
                             "复核阶段不该复用生成阶段的历史")


if __name__ == "__main__":
    unittest.main()
