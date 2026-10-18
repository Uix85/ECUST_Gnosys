"""L0 术语提名：扫一本书，产出候选词并落库（零模型）。

用法：
    uv run python tools/concept/build_term_index.py                     # 全部有书库的书写一遍
    uv run python tools/concept/build_term_index.py "精神现象学…_data"   # 只跑一本
    uv run python tools/concept/build_term_index.py --top 40            # 多打印一些候选

产出：
    term_candidate  候选词（含信号、章覆盖度、频次、排名、人工确认状态）
    term_scan       每个候选的全书分布缓存（划词时秒级返回，不必重扫）

设计依据：docs/概念层_设计_v1.md 第 11 节。这个脚本的作用是**让你看一眼提名词质量**：
噪声多不多、盗版站广告有没有混进来、长词优先有没有生效。看不过去就调
agent/index.py 里的阈值（MIN_TOTAL / MIN_PARAS / FREQ_SIGNAL / COVERAGE_RATIO）。
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def books() -> list[str]:
    base = ROOT / "database" / "books"
    return [p.name for p in sorted(base.iterdir())
            if p.is_dir() and (p / "book.db").is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description="L0 术语提名（零模型）")
    parser.add_argument("book", nargs="?", help="书库目录名；省略则跑全部")
    parser.add_argument("--top", type=int, default=25, help="打印前 N 个候选")
    parser.add_argument("--no-write", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    from agent.tools import index as agent_index

    targets = [args.book] if args.book else books()
    for name in targets:
        print(f"\n{'=' * 68}\n{name}")
        started = time.monotonic()
        try:
            items = agent_index.nominate(name)
        except Exception as exc:
            print(f"  [跳过] {exc}")
            continue
        elapsed = time.monotonic() - started
        print(f"  提名 {len(items)} 个词，耗时 {elapsed:.1f}s")
        if not args.no_write:
            summary = agent_index.build(name)
            print(f"  已落库：by_signal={summary['by_signal']}，章数={summary['chapters']}")
        print(f"  {'排名':>4} {'词':<14} {'章覆盖':>6} {'频次':>6}  信号")
        for item in items[:args.top]:
            print(f"  {item['rank']:>4} {item['term']:<14} {item['chapter_count']:>6} "
                  f"{item['total']:>6}  {'+'.join(item['signals'])}")
        # 质量提示：一眼能看出的两类噪声
        garbage = [i for i in items if any(c.isdigit() for c in i["term"])]
        ads = [i for i in items if "书友" in i["term"] or "读书" in i["term"] or "小编" in i["term"]]
        if garbage:
            print(f"  含数字的候选（应剔除）：{[i['term'] for i in garbage[:5]]}")
        if ads:
            print(f"  疑似盗版站文本（应剔除）：{[i['term'] for i in ads[:5]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
