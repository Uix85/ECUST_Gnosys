"""真模型批量跑：拿书里的真实段落跑一遍生产 Agent，报告产出与开销。

跑之前先 checkpoint 并备份 book.db，跑完还原 —— 不污染书库里的既有数据。

    uv run python tools/semantic/run_batch.py -n 2                  # 冒烟：2 个段落
    uv run python tools/semantic/run_batch.py -n 20 --chapter 1     # 小批量
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def default_book() -> str:
    """默认挑第一本有 book.db 的书；显式指定用 --book。"""
    for path in sorted((ROOT / "database" / "books").iterdir()):
        if path.is_dir() and (path / "book.db").is_file():
            return path.name
    raise SystemExit("database/books/ 下没找到带 book.db 的书")


def refs_for(book_id: str, chapter_idx: int, count: int) -> list[dict]:
    from agent.tools import book as agent_book

    _, paragraphs, _ = agent_book.logical_paragraphs(book_id, chapter_idx)
    picked = []
    for para in paragraphs:
        if agent_book.semantic_needed(para["text"]):
            picked.append({"chapter_idx": chapter_idx, "paragraph_index": para["paragraph_index"],
                           "fingerprint": para["fingerprint"]})
        if len(picked) >= count:
            break
    return picked


def collect(db, run_id: str, elapsed: float) -> dict:
    """从事件流里把这次跑的"账"读出来：状态分布、用量、被拒次数、解释膨胀比。"""
    statuses = {}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM agent_run_tasks WHERE run_id=? GROUP BY status",
                          (run_id,)):
        statuses[row["status"]] = row["n"]
    usage = {"responses": 0, "input": 0, "output": 0, "total": 0, "rejections": 0, "tools": {}}
    growth = []                       # 每次请求实际带入的输入量（历史膨胀曲线）
    # 注意：事件里的 input_tokens/output_tokens 是**累计值**（预算对象一直在累加），
    # 不是本次请求的量。而且同一 run 下多个任务的累计序列互相独立，
    # 所以必须按 task_id 分组做差分，不能直接相加也不能跨任务差分。
    last: dict[str, tuple[int, int]] = {}
    for row in db.execute("SELECT task_id, stage, data_json FROM agent_run_events "
                          "WHERE run_id=? ORDER BY task_id, id", (run_id,)):
        data = json.loads(row["data_json"]) if row["data_json"] else {}
        if row["stage"] == "model_response":
            prev_in, prev_out = last.get(row["task_id"], (0, 0))
            cum_in = data.get("input_tokens") or 0
            cum_out = data.get("output_tokens") or 0
            usage["responses"] += 1
            usage["input"] += cum_in - prev_in
            usage["output"] += cum_out - prev_out
            growth.append(cum_in - prev_in)
            last[row["task_id"]] = (cum_in, cum_out)
        elif row["stage"] == "tool_error":
            usage["rejections"] += 1
        elif row["stage"] == "tool":
            name = data.get("tool", "?")
            usage["tools"][name] = usage["tools"].get(name, 0) + 1
    usage["total"] = usage["input"] + usage["output"]
    rows = db.execute("SELECT length(source_text) s, length(short)+length(detail)+length(sentences) o "
                      "FROM agent_notes WHERE trim(ifnull(source_text,'')) <> ''").fetchall()
    values = [row["o"] / row["s"] for row in rows if row["s"]]
    errors = [row["error"][:200] for row in db.execute(
        "SELECT error FROM agent_run_tasks WHERE run_id=? AND error IS NOT NULL LIMIT 2", (run_id,))]
    return {"statuses": statuses, "usage": usage, "elapsed": round(elapsed, 1), "growth": growth,
            "notes": len(rows), "ratio": round(sum(values) / len(values), 2) if values else None,
            "errors": errors}


async def run_batch(book_id: str, chapter_idx: int, count: int) -> dict:
    sys.path.insert(0, str(BACKEND))
    from agent.tools import book as agent_book  # noqa: E402
    from agent.runs import loop as agent_runs   # noqa: E402
    from agent.runtime.config import load_config   # noqa: E402
    from db.schema import close_db, get_db  # noqa: E402

    db_path = ROOT / "database" / "books" / book_id / "book.db"
    refs = refs_for(book_id, chapter_idx, count)      # 先选段（只读），空就不动数据库
    if not refs:
        raise SystemExit(f"{book_id} 第 {chapter_idx} 章没有待处理段落（章号 0 起算，换个 --chapter）")
    # 临时文件一律进 tmp/（见 tmp/TEMP_README.md）：写在书库旁边的话，
    # 脚本被中断就会留下未还原的备份和改过的 book.db
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    backup = tmp_dir / f"tmp_{book_id}_book.db.batchbak"
    # 先 checkpoint：有 -wal 时直接 copy 主库会漏掉未落盘的数据
    checkpoint = get_db(str(db_path))
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    close_db(str(db_path))
    shutil.copy2(db_path, backup)
    try:
        db = get_db(str(db_path))
        db.execute("DELETE FROM agent_runs")          # 只清 agent 任务表，不动原文/旧语义层
        db.commit()
        config = load_config()
        print(f"模型={config.model} 端点={config.base_url} 段落数={len(refs)}", flush=True)
        run = agent_runs.create_run(book_id, {"mode": "overwrite", "spec_type": "hegel_helin"},
                                    selected_refs=refs)
        started = time.monotonic()
        await asyncio.wait_for(agent_runs._run_tasks[(book_id, run["id"])], timeout=1500)
        result = collect(db, run["id"], time.monotonic() - started)
        result["run_status"] = db.execute("SELECT status FROM agent_runs WHERE id=?",
                                          (run["id"],)).fetchone()["status"]
        close_db(str(db_path))
        return result
    finally:
        close_db(str(db_path))
        for suffix in ("-shm", "-wal"):      # 运行时文件，还原后重建即可
            side = Path(str(db_path) + suffix)
            if side.exists():
                side.unlink()
        shutil.move(str(backup), str(db_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="真模型批量跑（自动备份还原 book.db）")
    parser.add_argument("--book", default=default_book())
    parser.add_argument("--chapter", type=int, default=0)      # 章号 0 起算
    parser.add_argument("-n", "--count", type=int, default=20)
    args = parser.parse_args()

    print(f"书：{args.book}\n章：{args.chapter}\n段落数：{args.count}\n")
    result = asyncio.run(run_batch(args.book, args.chapter, args.count))
    usage = result["usage"]
    print("\n════════════ 生产 Agent 批量结果 ════════════")
    print(f"  运行状态    {result['run_status']}")
    print(f"  任务分布    {result['statuses']}")
    print(f"  模型请求数  {usage['responses']}")
    print(f"  总 token    {usage['total']}（入 {usage['input']} / 出 {usage['output']}）")
    print(f"  校验被拒    {usage['rejections']}")
    print(f"  工具调用    {usage['tools']}")
    print(f"  落库条数    {result['notes']}")
    print(f"  耗时(秒)    {result['elapsed']}")
    print(f"  解释/原文   {result['ratio']}")
    growth = result["growth"]
    if growth:
        curve = " ".join(f"{t // 1000}k" for t in growth[:14])
        print(f"  输入膨胀    每次请求实际带入：{curve}"
              + (" …" if len(growth) > 14 else ""))
        print(f"  最大单次    {max(growth) // 1000}k tokens")
    for err in result["errors"]:
        print(f"  失败原因    {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
