"""跑一章的章节层（L2）。

    uv run python tools/structure/run_chapter_layer.py --book <书> --chapter 2            # 只看产出
    uv run python tools/structure/run_chapter_layer.py --book <书> --chapter 2 --apply    # 写库
    uv run python tools/structure/run_chapter_layer.py --book <书> --all                  # 逐章（不写库）

写库前会自动把旧内容导出到 tmp/legacy_chapter_layer/<书>/<章>.md 作对照基线。
质量关由人把（见设计 §8.2）：未过线的章不要 --apply。
"""

import argparse
import asyncio
import dataclasses
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from reader import locator  # noqa: E402
from agent.tools.book import logical_paragraphs  # noqa: E402
from agent.runtime.config import load_config  # noqa: E402
from agent.runtime.engine import AgentTask, build_model  # noqa: E402
from agent.layers import l2_chapter as ct  # noqa: E402

LEGACY_DIR = os.path.join(ROOT, "tmp", "legacy_chapter_layer")


def export_legacy(book_id: str, chapter_idx: int) -> str | None:
    old = ct.existing_l2(book_id, chapter_idx)
    if not old.strip():
        return None
    path = os.path.join(LEGACY_DIR, book_id, f"{chapter_idx:02d}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(old)
    return path


async def run_one(book_id: str, chapter_idx: int, *, apply: bool, config) -> dict:
    nodes = [n for n in locator.load_nodes(book_id) if n["chapter_idx"] == chapter_idx]
    if not nodes:
        raise SystemExit(f"第 {chapter_idx} 章没有定位节点：先跑 tools/platform/build_locator.py --apply")
    materials, read_sources = ct.chapter_materials(book_id, chapter_idx)
    spec = ct.chapter_spec(book_id=book_id, materials=materials, read_sources=read_sources, nodes=nodes)
    model = build_model(config)

    async def emit(stage, message, data=None):
        print(f"  [{stage}] {message}")

    result = await AgentTask(spec, model=model, event=emit, cancelled=asyncio.Event(),
                             config=config).run()
    if not isinstance(result, dict):
        raise SystemExit(f"没拿到结构化结果：{result!r}")
    title = logical_paragraphs(book_id, chapter_idx)[0].get("title", "")
    print(f"\n──── 第 {chapter_idx} 章《{title}》产出 ────")
    print(ct.render_markdown({"title": title, "nodes_source": nodes, **result}))
    if apply:
        legacy = export_legacy(book_id, chapter_idx)
        if legacy:
            print(f"（旧稿已导出：{legacy}）")
        ct.save_chapter_layer(book_id, chapter_idx, result, nodes,
                              model_version=getattr(config, "model", ""))
        print(f"已写入 chapter_layer（chapter_idx={chapter_idx}）")
    return result


async def main_async(args) -> int:
    config = load_config()
    if args.model:
        config = dataclasses.replace(config, model=args.model)
    print(f"模型：{config.model}｜base={config.base_url}")
    chapters = []
    if args.all:
        chapters = list(range(locator.chapter_count(args.book)))
    else:
        chapters = [args.chapter]
    for ci in chapters:
        print(f"\n{'=' * 66}\n第 {ci} 章")
        try:
            await run_one(args.book, ci, apply=args.apply, config=config)
        except Exception as exc:                       # 单章失败不影响后续
            print(f"  ❌ 失败：{type(exc).__name__}: {exc}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="跑一章的章节层（L2）")
    ap.add_argument("--book", required=True)
    ap.add_argument("--chapter", type=int, default=2)
    ap.add_argument("--all", action="store_true", help="逐章跑（配合 --apply 会写库）")
    ap.add_argument("--apply", action="store_true", help="写库（写前导出旧稿）")
    ap.add_argument("--model", default=None)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
