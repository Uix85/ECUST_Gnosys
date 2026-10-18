"""Schedule runs and keep their background task loops."""

import asyncio
import json
import uuid

from fastapi import HTTPException

from agent.tools.book import book_db, get_paragraph
from agent.runtime.config import load_config
from agent.runtime.task import AgentCancelled
from agent.runs import records
from agent.runs.paragraph import ParagraphRun, prepare_run
from reader.workspace import baseline, dumps, now, validate_target

# 进程内任务注册表：键为 (book_id, run_id)。后台协程结束后会自行移除，
# 因此这里的条目只代表仍在运行（或尚未清理）的任务。
_run_tasks: dict[tuple[str, str], asyncio.Task] = {}
# 每个 run 一个取消信号；置位后由 worker/段落循环协作式退出。
_cancel_signals: dict[tuple[str, str], asyncio.Event] = {}
# 全局段落并发上限，首次运行时按配置惰性创建；所有书、所有 run 共用。
_parallel: asyncio.Semaphore | None = None


async def _run_worker(book_id: str, run_id: str, cancelled: asyncio.Event) -> None:
    """后台串行执行一个 run 内的全部段落任务，并收敛其终态。

    段落之间共享取消信号和全局并发信号量；单个段落失败只会把候选稿保留
    为待核稿，不会中断后续段落。收到取消则停止派发剩余任务，并交由
    AgentCancelled 分支标记其余排队任务。终态写库后从注册表移除自身。
    """
    global _parallel
    db = None
    try:
        config = load_config()
        # 信号量绑定首次运行时的配置，之后不再随配置热更新。
        if _parallel is None:
            _parallel = asyncio.Semaphore(max(1, config.max_parallel))
        db = book_db(book_id)
        started = records.start_run(db, run_id)
        if not started:
            # run 记录已不存在（例如被删除），直接结束。
            return
        run, tasks = started

        for task in tasks:
            if cancelled.is_set():
                break
            records.mark_task_running(db, task["id"])
            try:
                async with _parallel:
                    await ParagraphRun(book_id, db, run, task, cancelled, config).run()
            except AgentCancelled as error:
                # 取消属于整条 run 的终止信号：先保留当前候选稿，再向上传播。
                records.preserve_candidate(db, run, task["id"], error, canceled=True)
                raise
            except Exception as error:
                # 单段失败不阻断后续段落，仅把任务落为待核/失败。
                records.preserve_candidate(db, run, task["id"], error, canceled=False)

        records.finish_run(db, run_id, cancelled.is_set())
    except AgentCancelled:
        try:
            records.cancel_remaining_tasks(db or book_db(book_id), run_id)
        except Exception:
            pass
    except Exception as error:
        try:
            records.fail_run(db or book_db(book_id), run_id, error)
        except Exception:
            pass
    finally:
        _run_tasks.pop((book_id, run_id), None)
        _cancel_signals.pop((book_id, run_id), None)


async def _layer_worker(book_id: str, run_id: str, signal: asyncio.Event) -> None:
    """执行长任务（L1/L2/L4）并在异常时补齐终态。

    与 `_run_worker` 不同，整条 run 只对应一个 `LayerRun`。失败/取消时除了
    标记 run 状态，还会把已生成的候选稿保存为待核 draft，避免丢失模型输出。
    """
    run = None
    try:
        from agent.runs.layer import LayerRun

        run = LayerRun(book_id, run_id, signal)
        await run.run()
    except (Exception, asyncio.CancelledError) as error:
        db = book_db(book_id)
        # 取消信号可能先于异常到达，两者任一成立都视为取消。
        status = "canceled" if signal.is_set() or isinstance(error, AgentCancelled) else "failed"
        if run and run.candidate:
            run.save_draft({"approved": False, "issues": [str(error)]}, "pending")
        with db:
            db.execute(
                "UPDATE agent_runs SET status=?,error=?,updated_at=? WHERE id=?",
                (status, str(error)[:1500], now(), run_id),
            )
        records._event(db, run_id, None, status, str(error)[:500])
    finally:
        _run_tasks.pop((book_id, run_id), None)
        _cancel_signals.pop((book_id, run_id), None)


def create_run(book_id: str, request: dict, selected_refs: list[dict] | None = None) -> dict:
    """创建段落运行。

    `prepare_run` 负责校验、去重与写库；只有它真正产生了新 run（`run_id`
    非空）时，这里才登记取消信号并启动后台 worker。命中已有任务或无需
    处理时直接返回其响应。
    """
    response, run_id = prepare_run(book_id, request, selected_refs)
    if run_id is None:
        return response

    signal = asyncio.Event()
    key = (book_id, run_id)
    _cancel_signals[key] = signal
    _run_tasks[key] = asyncio.create_task(_run_worker(book_id, run_id, signal), name=f"agent-run-{run_id}")
    return response


def create_layer_run(book_id: str, request: dict) -> dict:
    """创建 L1/L2/L4 长任务，按目标段落去重排队中的 run。

    校验顺序：书未在删除 → 目标合法 → 层受支持 → 规范类型有效 → L4 术语
    门禁通过 → 已配置 API Key。L4 会先用门禁修正 term 并补 fingerprint，
    再重新计算 `note_key`。同一 `note_key` 已有活动 run 时直接复用其 id。
    """
    if records.is_book_deleting(book_id):
        raise HTTPException(409, "此书正在删除")
    db = book_db(book_id)
    layer, target = request.get("layer"), request.get("target", {})
    key = validate_target(book_id, layer, target)
    if layer not in ("L1", "L2", "L4"):
        raise HTTPException(422, "此接口扩展处理 L1/L2/L4")
    if request.get("spec_type", "general") not in ("general", "hegel_helin"):
        raise HTTPException(422, "规范无效")
    if layer == "L4":
        from dataclasses import asdict
        from agent.tasks.l4_selection import gate

        paragraph = get_paragraph(book_id, target["chapter_idx"], target["paragraph_index"])
        decision = gate(target["term"], paragraph["text"])
        if decision.action not in ("pass", "corrected"):
            raise HTTPException(422, asdict(decision))
        # 门禁可能修正术语，因此需要以修正后的目标重新计算去重 key。
        target = {**target, "term": decision.term, "fingerprint": paragraph["fingerprint"]}
        key = validate_target(book_id, layer, target)
    config = load_config()
    if not config.api_key:
        raise HTTPException(422, "请配置 AGENT_API_KEY")
    request = {**request, "target": target, "note_key": key}
    # 只与仍在排队/运行/取消中的 run 去重；已结束的 run 允许重新发起。
    for row in db.execute("SELECT id,request_json FROM agent_runs WHERE status IN ('queued','running','canceling')"):
        if json.loads(row["request_json"]).get("note_key") == key:
            return {"id": row["id"], "existing": True}

    run_id, timestamp = uuid.uuid4().hex, now()
    book_row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    with db:
        db.execute(
            "INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?)",
            (run_id, book_row["id"], "queued", dumps(request), config.model, timestamp, timestamp, None),
        )
        # 先冻结当前原文/解释作为基线，后续发布与采纳都以此校验冲突。
        db.execute(
            "INSERT INTO layer_results(run_id,baseline_json) VALUES(?,?)",
            (run_id, dumps(baseline(book_id, layer, target))),
        )
    signal = asyncio.Event()
    run_key = (book_id, run_id)
    _cancel_signals[run_key] = signal
    _run_tasks[run_key] = asyncio.create_task(_layer_worker(book_id, run_id, signal), name="layer-" + run_id)
    return {"id": run_id, "status": "queued", "layer": layer, "target": target}


def cancel_run(book_id: str, run_id: str) -> dict:
    """请求取消段落运行：置位信号并把状态改为 canceling。"""
    return records.cancel_run_record(book_id, run_id, _cancel_signals.get((book_id, run_id)))


def cancel_layer_run(book_id: str, run_id: str) -> dict:
    """请求取消长任务并返回其当前快照。

    置位信号后立即序列化当前记录；真正的终止与草稿保留由 `_layer_worker`
    的异常分支完成。
    """
    signal = _cancel_signals.get((book_id, run_id))
    if signal:
        signal.set()
    return records.serialize_layer_run(book_db(book_id), run_id)


async def cancel_book_runs(book_id: str) -> None:
    """删除书前取消并等待该书所有后台 run 退出。

    先标记书为删除中，阻止新 run 创建；再置位取消信号，最后 gather 等待
    后台任务收尾（异常被吞掉，因为此时书库即将删除）。
    """
    records.mark_book_deleting(book_id)
    active = [(key, task) for key, task in list(_run_tasks.items()) if key[0] == book_id]
    for key, _ in active:
        signal = _cancel_signals.get(key)
        if signal:
            signal.set()
    if active:
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)

