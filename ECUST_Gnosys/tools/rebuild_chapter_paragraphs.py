"""章单位对齐工具（see backend/chapter_units.py）

把 paragraphs 的归属对齐到“阅读器真正展示的章”（统一 HTML 的章切片），
修掉“一个 chunk 里没有顶级标题 → 该 chunk 的段落被算到下一章”的老问题
（《精神现象学》序言三、四节就是这样丢的）。

用法：
    uv run tools/rebuild_chapter_paragraphs.py                 # 预演（不改库，只报告）
    uv run tools/rebuild_chapter_paragraphs.py --apply         # 写入
    uv run tools/rebuild_chapter_paragraphs.py --apply "精神现象学..._data"   # 只处理指定书
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import chapter_units  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv[1:]
    only = args or None
    reports = chapter_units.align_all(apply=apply, only=only)
    for rep in reports:
        print(chapter_units.format_report(rep))
    if not apply:
        print('\n（预演模式，未改动数据库；确认无误后加 --apply 写入）')
    return 0 if all(not r.get('error') for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
