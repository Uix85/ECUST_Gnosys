"""Schedule runs and keep their background task loops."""

import asyncio
import json
import uuid

from fastapi import HTTPException

from agent.tools.book import book_db, get_paragraph
from agent.runtime.config import load_config
from agent.runtime.engine import AgentCancelled
from agent.runs import records
from agent.layers.l3_paragraph import ParagraphRun, prepare_run
from reader.workspace import baseline, dumps, now, validate_target

_run_tasks: dict[tuple[str, str], asyncio.Task] = {}
_cancel_signals: dict[tuple[str, str], asyncio.Event] = {}
_parallel: asyncio.Semaphore | None = None


async def _run_worker(book_id: str, run_id: str, cancelled: asyncio.Event) -> None:
    global _parallel
    db = None
    try:
        config = load_config()
        if _parallel is None:
            _parallel = asyncio.Semaphore(max(1, config.max_parallel))
        db = book_db(book_id)
        started = records.start_run(db, run_id)
        if not started:
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
                records.preserve_candidate(db, run, task["id"], error, canceled=True)
                raise
            except Exception as error:
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
    run = None
    try:
        from agent.runs.layer import LayerRun

        run = LayerRun(book_id, run_id, signal)
        await run.run()
    except (Exception, asyncio.CancelledError) as error:
        db = book_db(book_id)
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
    response, run_id = prepare_run(book_id, request, selected_refs)
    if run_id is None:
        return response

    signal = asyncio.Event()
    key = (book_id, run_id)
    _cancel_signals[key] = signal
    _run_tasks[key] = asyncio.create_task(_run_worker(book_id, run_id, signal), name=f"agent-run-{run_id}")
    return response


def create_layer_run(book_id: str, request: dict) -> dict:
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
        from agent.layers.l4_selection import gate

        paragraph = get_paragraph(book_id, target["chapter_idx"], target["paragraph_index"])
        decision = gate(target["term"], paragraph["text"])
        if decision.action not in ("pass", "corrected"):
            raise HTTPException(422, asdict(decision))
        target = {**target, "term": decision.term, "fingerprint": paragraph["fingerprint"]}
        key = validate_target(book_id, layer, target)
    config = load_config()
    if not config.api_key:
        raise HTTPException(422, "请配置 AGENT_API_KEY")
    request = {**request, "target": target, "note_key": key}
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
    return records.cancel_run_record(book_id, run_id, _cancel_signals.get((book_id, run_id)))


def cancel_layer_run(book_id: str, run_id: str) -> dict:
    signal = _cancel_signals.get((book_id, run_id))
    if signal:
        signal.set()
    return records.serialize_layer_run(book_db(book_id), run_id)


async def cancel_book_runs(book_id: str) -> None:
    records.mark_book_deleting(book_id)
    active = [(key, task) for key, task in list(_run_tasks.items()) if key[0] == book_id]
    for key, _ in active:
        signal = _cancel_signals.get(key)
        if signal:
            signal.set()
    if active:
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)
