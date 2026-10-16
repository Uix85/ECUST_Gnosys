"""Run control flow: queue a run, loop over paragraphs, and handle its outcome."""

import asyncio

from agent.book import book_db
from agent.config import load_config
from agent.engine import AgentCancelled
from agent import run_service


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
        started = run_service.start_run(db, run_id)
        if not started:
            return
        run, tasks = started

        for task in tasks:
            if cancelled.is_set():
                break
            run_service.mark_task_running(db, task["id"])
            try:
                async with _parallel:
                    await run_service.ParagraphRun(
                        book_id, db, run, task, cancelled, config
                    ).run()
            except AgentCancelled as error:
                run_service.preserve_candidate(db, run, task["id"], error, canceled=True)
                raise
            except Exception as error:
                run_service.preserve_candidate(db, run, task["id"], error, canceled=False)

        run_service.finish_run(db, run_id, cancelled.is_set())
    except AgentCancelled:
        try:
            run_service.cancel_remaining_tasks(db or book_db(book_id), run_id)
        except Exception:
            pass
    except Exception as error:
        try:
            run_service.fail_run(db or book_db(book_id), run_id, error)
        except Exception:
            pass
    finally:
        _run_tasks.pop((book_id, run_id), None)
        _cancel_signals.pop((book_id, run_id), None)


def create_run(book_id: str, request: dict, selected_refs: list[dict] | None = None) -> dict:
    response, run_id = run_service.prepare_run(book_id, request, selected_refs)
    if run_id is None:
        return response

    signal = asyncio.Event()
    _cancel_signals[(book_id, run_id)] = signal
    task = asyncio.create_task(_run_worker(book_id, run_id, signal), name=f"agent-run-{run_id}")
    _run_tasks[(book_id, run_id)] = task
    return response


def cancel_run(book_id: str, run_id: str) -> dict:
    return run_service.cancel_run_record(
        book_id, run_id, _cancel_signals.get((book_id, run_id))
    )


async def cancel_book_runs(book_id: str) -> None:
    run_service.mark_book_deleting(book_id)
    active = [(key, task) for key, task in list(_run_tasks.items()) if key[0] == book_id]
    for key, _ in active:
        signal = _cancel_signals.get(key)
        if signal:
            signal.set()
    if active:
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)


