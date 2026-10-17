"""定位层自检（无模型）：证明"把定位提前到入库"没有改变读取行为，且真的修好了标题。

    uv run python tools/platform/check_locator.py

断言：
  1. 逐章段落数与"现解 HTML"完全一致（不改行为）
  2. 重复构建幂等（指纹全等）
  3. 抽查 20 段：locate() 取回的文本与现解文本逐字一致
  4. epub 书的标题数确实回来了（修复前只读到章首那一个 spine 块）
  5. 鱼尾括号【…】不再被当标题（曾一口吞掉序言 122 段）
  6. 指纹反查位置可用
  7. 每段的 node_idx 都指向真实节点，seg_in_node 从 0 连续递增
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from reader import locator  # noqa: E402
from agent.book import split_paragraphs  # noqa: E402
from reader.book_service import (BOOKS_DIR, _build_chapters, _chapter_slice_anchors,  # noqa: E402
                                 _get_toc_pages, _get_unified_html_cached, _slice_content,
                                 load_book_cached)

OK, BAD = "  ✅", "  ❌"
TXT = "精神现象学 黑格尔着 (精神现象学 黑格尔着.txt) (z-library.sk, 1lib.sk, z-lib.sk)_data"
EPUB = "tmpuke_eepn_data"


class Checker:
    def __init__(self):
        self.ok = self.bad = 0

    def check(self, label, cond, detail=""):
        print((OK if cond else BAD) + f" {label}" + (f"  {detail}" if detail else ""))
        self.ok += bool(cond)
        self.bad += not cond


def live_paragraphs(book_id: str, ci: int) -> list[str]:
    """现解路径（旧实现）：统一 HTML → 章切片 → 切段。"""
    book = load_book_cached(book_id)
    pages = _get_toc_pages(book_id, book)
    chapters = _build_chapters(pages)
    a, b = _chapter_slice_anchors(chapters, pages, book, book_id, ci)
    return split_paragraphs(_slice_content(_get_unified_html_cached(book_id, book), a, b))


def main() -> int:
    c = Checker()
    books = [b for b in (EPUB, TXT) if os.path.isfile(os.path.join(BOOKS_DIR, b, "book.db"))]
    for book in books:
        print(f"\n{'=' * 66}\n{book}")
        if not locator.has_locator(book):
            print("  （没有定位表，先跑 build_locator.py --apply）")
            continue

        # 1 + 3. 与现解一致（逐章段数 + 抽段逐字比对）
        mismatch, sampled, total = [], 0, 0
        n_ch = locator.chapter_count(book)
        for ci in range(n_ch):
            want = live_paragraphs(book, ci)
            rows = locator.load_paragraphs(book, ci) or []
            total += len(rows)
            if len(want) != len(rows):
                mismatch.append((ci, len(want), len(rows)))
                continue
            for pi in (0, len(rows) // 2, len(rows) - 1):
                if 0 <= pi < len(rows):
                    sampled += 1
                    got = locator.locate(book, ci, pi)
                    if got["text"] != want[pi]:
                        mismatch.append((ci, pi, "文本不一致"))
        c.check("逐章段落数与现解一致、抽段逐字一致", not mismatch,
                f"共 {spam(total)} 段，抽查 {sampled} 段" + (f"，问题 {mismatch[:3]}" if mismatch else ""))

        # 2. 幂等
        before = {(ci, p["para_idx"]): p["fingerprint"] for ci in range(n_ch)
                  for p in (locator.load_paragraphs(book, ci) or [])}
        locator.build(book, apply=True)
        after = {(ci, p["para_idx"]): p["fingerprint"] for ci in range(n_ch)
                 for p in (locator.load_paragraphs(book, ci) or [])}
        c.check("重复构建幂等（指纹全等）", before == after,
                f"{len(before)} → {len(after)}")

        # 7. 节点指向与段内序号
        nodes = locator.load_nodes(book)
        idx = {n["node_idx"] for n in nodes}
        bad_node = bad_seg = 0
        for ci in range(n_ch):
            seg = {}
            for p in locator.load_paragraphs(book, ci) or []:
                if p["node_idx"] not in idx:
                    bad_node += 1
                want = seg.get(p["node_idx"], 0)
                if p["seg_in_node"] != want:
                    bad_seg += 1
                seg[p["node_idx"]] = want + 1
        c.check("每段都指向真实节点、seg_in_node 连续", not bad_node and not bad_seg,
                f"坏指向 {bad_node}｜序号错 {bad_seg}")

        # 6. 指纹反查
        p0 = (locator.load_paragraphs(book, 0) or [{}])[0]
        hits = locator.find_by_fingerprint(book, p0.get("fingerprint", "")) if p0 else []
        c.check("指纹可反查位置", bool(hits), f"{len(hits)} 处")

        # 4/5. 标题质量
        titles = [n["title"] for n in nodes]
        if book == EPUB:
            c.check("epub 标题确实回来了（修复前每章只 1 个）", len(nodes) >= 50,
                    f"节点 {len(nodes)} 个（修复前约 24）")
            ch2 = [n for n in nodes if n["chapter_idx"] == 2]
            c.check("导言章拿到了子标题（第一节/第二节…）", len(ch2) >= 6,
                    f"ci=2 节点 {len(ch2)} 个（修复前 1 个）")
        c.check("鱼尾括号【…】不当标题", not [t for t in titles if t.startswith("【")])
        if book == TXT:                      # epub 源的标题来自 h 标签，不含方括号批注
            c.check("方括号批注仍是标题", any(t.startswith("[") for t in titles))
    print(f"\n{'=' * 66}\n通过 {c.ok} 项，失败 {c.bad} 项")
    return 1 if c.bad else 0


def spam(n: int) -> str:
    return f"{n:,}"


if __name__ == "__main__":
    raise SystemExit(main())
