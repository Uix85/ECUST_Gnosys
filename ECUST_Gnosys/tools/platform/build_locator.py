"""重建某本书（或全部书）的定位表：章 → 标题区 → 段。

定位是入库产物（见 docs/定位系统_设计_v1.md），改动判定规则后**只重跑这一步**，
不需要重新解析正文。

用法：
    uv run python tools/platform/build_locator.py                  # 全部书：预演，只报告
    uv run python tools/platform/build_locator.py --apply          # 全部书写入
    uv run python tools/platform/build_locator.py --book tmpuke_eepn_data --apply
    uv run python tools/platform/build_locator.py --book <书> --diff   # 与现有定位对比后再写
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from reader import locator  # noqa: E402
from reader.book_service import BOOKS_DIR  # noqa: E402


def all_books() -> list[str]:
    return [d for d in sorted(os.listdir(BOOKS_DIR))
            if os.path.isfile(os.path.join(BOOKS_DIR, d, "book.db"))]


def show(book_id: str, verbose: bool) -> None:
    rep = locator.build(book_id, apply=False)
    print(f"\n{book_id}")
    print(f"  章 {rep['n_chapters']}｜节点 {rep['n_nodes']}｜段 {rep['n_paragraphs']}")
    if verbose:
        for r in rep["report"]:
            extra = f"｜漏标题 {r['n_skipped']}" if r["n_skipped"] else ""
            print(f"    ci={r['chapter_idx']:3d} 段={r['n_paragraphs']:4d} 节点={r['n_nodes']:3d} "
                  f"{r['heading_source']:5s} {r['title'][:30]!r}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description="重建定位表（章→标题区→段）")
    ap.add_argument("--book", default=None, help="书文件夹名；缺省=全部书")
    ap.add_argument("--apply", action="store_true", help="写入（缺省只预演）")
    ap.add_argument("--diff", action="store_true", help="写入前先与现有定位对比段数与节点数")
    ap.add_argument("--verbose", action="store_true", help="逐章报告")
    args = ap.parse_args()

    books = [args.book] if args.book else all_books()
    for b in books:
        if args.diff:
            before_nodes = len(locator.load_nodes(b))
            before_paras = sum(len(locator.load_paragraphs(b, ci) or [])
                               for ci in range(locator.chapter_count(b)))
            after = locator.build(b, apply=False)
            print(f"\n{b}\n  节点 {before_nodes} → {after['n_nodes']}"
                  f"｜段 {before_paras} → {after['n_paragraphs']}")
            if before_nodes != after["n_nodes"]:
                print("  ⚠ 节点数变了：确认这是规则改动带来的，而不是误判")
        if args.apply:
            rep = locator.build(b, apply=True)
            print(f"{b}: 章 {rep['n_chapters']}｜节点 {rep['n_nodes']}｜段 {rep['n_paragraphs']}（已写入）")
        else:
            show(b, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
