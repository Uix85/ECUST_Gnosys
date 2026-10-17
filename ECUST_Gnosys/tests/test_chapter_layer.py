"""章节层（L2）单测：合格线只查形式、渲染是机械的、定位树的区间语义。

不碰模型、不写库（写库路径由 tools/structure/run_chapter_layer.py 在试点时走）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from reader import locator  # noqa: E402
from agent.layers import l2_chapter as ct  # noqa: E402


def nodes_fixture():
    return [
        {"node_idx": 1, "kind": "chapter", "level": 0, "parent_idx": None, "title": "第二章",
         "para_from": 0, "para_to": 9, "synthetic": 0},
        {"node_idx": 2, "kind": "section", "level": 2, "parent_idx": 1, "title": "[一、甲]",
         "para_from": 2, "para_to": 4, "synthetic": 0},
        {"node_idx": 3, "kind": "section", "level": 2, "parent_idx": 1, "title": "[二、乙]",
         "para_from": 5, "para_to": 9, "synthetic": 0},
    ]


def good_args():
    return {
        "chapter": {"problem": "问题", "forecast": "预告", "ascent": "上升", "carry_over": "承",
                    "hand_over": "启"},
        "nodes": [
            {"node_id": 2, "role": "definition", "purpose": "立出三个环节", "weight": "core",
             "focus": "判决点", "argument": "X→Y", "steps": ["一", "二"], "to_next": "逼出乙"},
            {"node_id": 3, "role": "argument", "purpose": "把甲拆掉", "weight": "support"},
        ],
        "turning_points": [
            {"node_id": 2, "label": "章首判定", "pos": 2, "text": "证书"},
            {"node_id": 3, "label": "转折点一", "pos": 5, "text": "撕毁"},
            {"node_id": 3, "label": "转折点二", "pos": 9, "text": "收束"},
        ],
        "handoff": ["收获"],
    }


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.nodes = nodes_fixture()
        self.sources = ["甲段的原文", "乙段的原文"]

    def test_accepts_good(self):
        ct.validate_chapter(good_args(), self.sources, self.nodes)

    def test_rejects_missing_node(self):
        a = good_args()
        a["nodes"].pop()
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, self.sources, self.nodes)

    def test_rejects_unknown_node_id(self):
        a = good_args()
        a["nodes"][0]["node_id"] = 99
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, self.sources, self.nodes)

    def test_role_and_weight_are_closed_sets(self):
        for field, bad in (("role", "论点"), ("weight", "major")):
            a = good_args()
            a["nodes"][0][field] = bad
            with self.assertRaises(ValueError):
                ct.validate_chapter(a, self.sources, self.nodes)

    def test_core_needs_argument_and_steps(self):
        a = good_args()
        a["nodes"][0]["steps"] = ["只有一步"]
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, self.sources, self.nodes)

    def test_quote_must_be_in_sources(self):
        a = good_args()
        a["nodes"][0]["quote"] = "书里没有"
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, self.sources, self.nodes)
        b = good_args()
        b["nodes"][0]["quote"] = "甲段的原文"
        ct.validate_chapter(b, self.sources, self.nodes)

    def test_caps_are_display_budgets(self):
        a = good_args()
        a["nodes"][0]["purpose"] = "字" * 61
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, self.sources, self.nodes)

    def test_weight_distribution_is_not_checked(self):
        """侧重是判断：代码不许查条数/字数（设计 §6 的"明确不查"清单）。"""
        a = good_args()
        for item in a["nodes"]:
            item.update(weight="core", argument="X→Y", steps=["一", "二"])
        ct.validate_chapter(a, self.sources, self.nodes)          # 全核心也通过
        a["nodes"] = [dict(item, weight="minor") for item in a["nodes"]]
        ct.validate_chapter(a, self.sources, self.nodes)          # 全次要也通过

    def test_internal_node_needs_to_next(self):
        nodes = nodes_fixture() + [
            {"node_idx": 4, "kind": "section", "level": 3, "parent_idx": 2, "title": "[（1）丙]",
             "para_from": 3, "para_to": 4, "synthetic": 0}]
        a = good_args()
        a["nodes"].append({"node_id": 4, "role": "transition", "purpose": "子块", "weight": "minor"})
        a["nodes"][0].pop("to_next")                              # 父块（节点2）是内部节点
        with self.assertRaises(ValueError):
            ct.validate_chapter(a, ["甲段的原文"], nodes)


class TestRender(unittest.TestCase):
    def test_render_contains_every_node(self):
        md = ct.render_markdown({"title": "第二章　知觉", "nodes_source": nodes_fixture(), **good_args()})
        self.assertIn("# 第二章　知觉", md)
        self.assertIn("[一、甲]", md)
        self.assertIn("[二、乙]", md)
        self.assertIn("**作用**：立出三个环节", md)
        self.assertIn("**重点**：判决点", md)
        self.assertIn("1. 一", md)
        self.assertIn("关键论证转折点", md)
        self.assertIn("章末收获", md)

    def test_cut_breaks_at_punctuation(self):
        cut = ct._cut("①②③④⑤⑥⑦⑧⑨⑩" * 12, 20)
        self.assertLessEqual(len(cut), 21)
        self.assertTrue(cut.endswith("…") or len(cut) <= 20)


class TestLocatorTree(unittest.TestCase):
    def test_tree_ranges_include_subtree(self):
        nodes = locator.build_tree("第二章", [
            {"level": 0, "title": "第二章", "paragraph_index": 0},
            {"level": 2, "title": "[一、甲]", "paragraph_index": 2},
            {"level": 2, "title": "[二、乙]", "paragraph_index": 5}], 10, 0, 0)
        self.assertEqual((nodes[0]["para_from"], nodes[0]["para_to"]), (0, 9))
        self.assertEqual((nodes[1]["para_from"], nodes[1]["para_to"]), (2, 4))
        assigned = locator.assign_paragraphs(nodes, 10)
        self.assertEqual(assigned[0]["node_idx"], nodes[0]["node_idx"])   # 章首段落直接挂章节点
        self.assertEqual(assigned[6]["seg_in_node"], 1)                   # 乙区内第 2 段

    def test_chapter_node_is_synthetic_when_unmarked(self):
        nodes = locator.build_tree("序言", [], 5, 0, 0)
        self.assertEqual(nodes[0]["synthetic"], 1)
        self.assertIsNone(nodes[0]["head_para"])


if __name__ == "__main__":
    unittest.main()
