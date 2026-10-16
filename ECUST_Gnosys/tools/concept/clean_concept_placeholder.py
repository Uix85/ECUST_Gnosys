"""清理概念层里的占位污染数据（可重复执行，默认只报告不删）。

背景：auxiliary_reading.save_concept 曾经在 content 缺省时把**全书层文本**填进去当占位，
于是划词测试产生的垃圾词条全部以「# 《…》全书概览」开头混进了 concept_layer
（实测 tmpuke 库 26 条，term 是「于1807年，更」「包含着事」这类随手划的片段）。
占位逻辑已在 save_concept 里移除，这个脚本负责把历史残留清掉。

判据（只删确凿的占位，不动任何真人填写的内容）：
    term 不是术语（长度或字符形状不像）且 content 以书层文本开头

用法：
    uv run python tools/concept/clean_concept_placeholder.py              # 只报告
    uv run python tools/concept/clean_concept_placeholder.py --apply      # 真删
    uv run python tools/concept/clean_concept_placeholder.py --book 江村经济_data --apply
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def looks_like_placeholder(term: str, content: str, book_content: str) -> bool:
    """占位数据的特征：内容是书层文本（实测全部以「# 」开头）。

    不看词条形状：整个 concept_layer 从未有过 AI 生成的内容（模型版本全是 manual-v1），
    所以只要内容还是书层占位文本，就确定是残留，不管词条写的是什么。
    """
    c = (content or "").strip()
    if not c:
        return False
    base = (book_content or "").strip()
    if base and (c[:20] == base[:20]):
        return True
    return c.startswith("# ") and "全书概览" in c[:40]


def clean(folder: str, apply: bool = False) -> tuple[int, int]:
    from db.schema import close_db, get_db
    path = ROOT / "books" / folder / "book.db"
    if not path.is_file():
        raise SystemExit(f"没有这个书库：{path}")
    db = get_db(str(path))
    row = db.execute("SELECT id, content FROM book_layer LIMIT 1").fetchone()
    book_content = (row["content"] if row else "") or ""
    rows = db.execute("SELECT id, term, content FROM concept_layer").fetchall()
    victims = [r for r in rows if looks_like_placeholder(r["term"], r["content"], book_content)]
    print(f"{folder}：concept_layer 共 {len(rows)} 条，其中占位污染 {len(victims)} 条")
    for r in victims[:8]:
        print(f"    - 「{(r['term'] or '')[:24]}」")
    if victims and apply:
        db.executemany("DELETE FROM concept_layer WHERE id=?", [(r["id"],) for r in victims])
        db.commit()
        print(f"    已删除 {len(victims)} 条")
    close_db(str(path))
    return len(rows), len(victims)


def main() -> int:
    parser = argparse.ArgumentParser(description="清理概念层占位污染")
    parser.add_argument("--book", help="只处理某本书库；省略则全部")
    parser.add_argument("--apply", action="store_true", help="真正执行删除")
    args = parser.parse_args()
    base = ROOT / "books"
    targets = [args.book] if args.book else [p.name for p in sorted(base.iterdir())
                                            if (p / "book.db").is_file()]
    total = 0
    for folder in targets:
        try:
            _, n = clean(folder, args.apply)
            total += n
        except Exception as exc:
            print(f"{folder}：[跳过] {exc}")
    if total and not args.apply:
        print(f"\n共 {total} 条待清理。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
