"""给已入库的书补上章节层级（chapters.heading_json）。

背景：入库管线曾经恒写 `'[]'`（注释还自称「预留字段、服务端不读取」），于是 epub 源里的
`<h1>`-`<h6>` 层级没有被记下来 —— 而语义层要「上承章节结构」、术语解释要拿子节当适用范围，
都得靠它。`reader/import_epub.py` 已修（导入时抽层级），这个脚本负责把**已入库的旧数据**补齐。

来源：`content_html` 里的 `<h1>`-`<h6>`。
  * epub 源：标题标签还在（实测 tmpuke 36 个 h3），可补
  * txt 源：本来就没有标题标签，补不了 —— 那种书靠 `agent/outline.py` 的文本行模式推断兜底

用法：
    uv run python tools/platform/backfill_heading_levels.py            # 只报告
    uv run python tools/platform/backfill_heading_levels.py --apply    # 写入
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def scan(book: str, apply: bool) -> tuple[int, int, int]:
    """返回（已有, 本次补上, 无标题可补）。"""
    from db.schema import close_db, get_db
    from reader.import_epub import _extract_headings

    path = ROOT / "books" / book / "book.db"
    db = get_db(str(path))
    already = filled = none = 0
    for row in db.execute("SELECT id, spine_order, heading_json, content_html FROM chapters").fetchall():
        current = (row["heading_json"] or "").strip()
        if current not in ("", "[]"):
            already += 1
            continue
        extracted = _extract_headings(row["content_html"] or "")
        if extracted == "[]":
            none += 1
            continue
        filled += 1
        if apply:
            db.execute("UPDATE chapters SET heading_json=? WHERE id=?", (extracted, row["id"]))
    if apply and filled:
        db.commit()
    close_db(str(path))
    return already, filled, none


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 chapters.heading_json")
    parser.add_argument("--book", default=None, help="只处理某本书库")
    parser.add_argument("--apply", action="store_true", help="真正写入")
    args = parser.parse_args()
    base = ROOT / "books"
    targets = [args.book] if args.book else [p.name for p in sorted(base.iterdir())
                                            if (p / "book.db").is_file()]
    total_new = 0
    for book in targets:
        try:
            already, filled, none = scan(book, args.apply)
        except Exception as exc:
            print(f"{book}：[跳过] {type(exc).__name__}: {exc}")
            continue
        total_new += filled
        print(f"{book}")
        print(f"    已有层级 {already} 章｜本次{'补上' if args.apply else '待补'} {filled} 章"
              f"｜无标题标签（txt 源）{none} 章")
    if total_new and not args.apply:
        print(f"\n共 {total_new} 章待补。确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
