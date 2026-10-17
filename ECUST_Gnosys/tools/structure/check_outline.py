"""章-节-子节层级自检与统计：看结构切得对不对、上下文窗口有多大。

用法：
    uv run python tools/check_outline.py                        # 全部有书库的书
    uv run python tools/check_outline.py --book 精神现象学…_data
    uv run python tools/check_outline.py --chapter 3 --tree     # 打印某一章的层级树

看三件事：
  1. 各层标题条数是否合理（不该把正文误判成标题）
  2. 标题行占多少段 —— 这些段不该再生成解释（现在会被当普通段落解释）
  3. 自适应上下文窗口的字数分布 —— 决定 prompt 会长多少
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def books() -> list[str]:
    return [p.name for p in sorted((ROOT / "books").iterdir())
            if p.is_dir() and (p / "book.db").is_file()]


def scan(book: str, chapter: int | None, tree: bool) -> None:
    from agent.tools import book as agent_book
    from agent.tools import outline as book_outline

    n_ch = 0
    while True:
        try:
            agent_book.logical_paragraphs(book, n_ch)
        except Exception:
            break
        n_ch += 1
    chapters = range(n_ch) if chapter is None else [chapter]

    per_level: dict[int, int] = {}
    heads = total = 0
    ctx: list[int] = []
    for ci in chapters:
        _ch, paragraphs, _t = agent_book.logical_paragraphs(book, ci)   # 每章只解析一次
        data = book_outline.structure(book, ci)
        for h in data["headings"]:
            per_level[h["level"]] = per_level.get(h["level"], 0) + 1
            heads += 1
        total += data["n_paragraphs"]
        for info in data["para_meta"].values():
            start, end = info["context_range"]
            ctx.append(sum(len(paragraphs[p]["text"]) for p in range(start, min(end, len(paragraphs)))))
        if tree:
            print(f"\n章 {ci}：{data['n_paragraphs']} 段，标题 {len(data['headings'])}")
            for h in data["headings"]:
                print("  " * max(0, h["level"] - 1) + f"- [L{h['level']}] {h['title'][:48]}")
    if not ctx:
        print("  （没有可统计的段落）")
        return
    ctx.sort()
    print(f"  段总数 {total}｜标题 {heads}（{heads * 100 // max(1, total)}%，这些不该生成解释）")
    print("  各层标题数：" + "  ".join(f"L{k}={v}" for k, v in sorted(per_level.items())))
    print(f"  自适应上下文窗口字数: 中位 {ctx[len(ctx) // 2]}  P90 {ctx[int(len(ctx) * .9)]}  最大 {ctx[-1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="章-节-子节层级自检")
    parser.add_argument("--book", default=None)
    parser.add_argument("--chapter", type=int, default=None)
    parser.add_argument("--tree", action="store_true")
    args = parser.parse_args()
    for name in ([args.book] if args.book else books()):
        print(f"\n{'=' * 70}\n{name}")
        scan(name, args.chapter, args.tree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
