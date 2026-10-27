"""An exception escaping run_task must not kill the robot's task worker.

Before the fix the worker coroutine died, the crashed task stayed
IN_PROGRESS and every later patrol was refused as "already running" until
the container restarted (audit 2026-10-27).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from common_types import Task, TaskStatus
from services import task_runtime

ROBOT = "kachaka"


def test_crashing_task_fails_and_worker_runs_the_next(monkeypatch):
    ran = []

    async def run_task(task):
        ran.append(task.task_id)
        task.status = TaskStatus.IN_PROGRESS
        task_runtime.current_tasks[ROBOT] = task.task_id
        if task.task_id == "boom":
            raise RuntimeError("boom in run_task")
        task.status = TaskStatus.DONE
        task_runtime.current_tasks.pop(ROBOT, None)  # real run_task's finally
        return task

    engine = MagicMock()
    engine.run_task = run_task

    async def go():
        queue = asyncio.Queue()
        monkeypatch.setitem(task_runtime.task_queues, ROBOT, queue)
        monkeypatch.setitem(task_runtime.engines, ROBOT, engine)
        monkeypatch.setattr(task_runtime, "tasks_db", {})
        monkeypatch.setattr(task_runtime, "current_tasks", {})
        crash = Task(task_id="boom", robot_id=ROBOT, steps=[])
        nxt = Task(task_id="next", robot_id=ROBOT, steps=[])
        await queue.put(crash)
        await queue.put(nxt)
        worker = asyncio.create_task(task_runtime.task_worker(ROBOT))
        await asyncio.wait_for(queue.join(), timeout=2)
        alive = not worker.done()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        return alive, crash, nxt

    alive, crash, nxt = asyncio.run(go())

    assert alive
    assert ran == ["boom", "next"]
    assert crash.status == TaskStatus.FAILED
    assert "boom in run_task" in crash.metadata["error"]
    assert task_runtime.tasks_db["boom"].status == TaskStatus.FAILED
    assert nxt.status == TaskStatus.DONE
    assert task_runtime.tasks_db["next"].status == TaskStatus.DONE
    assert ROBOT not in task_runtime.current_tasks


def test_crash_dispatches_a_ward_notification(monkeypatch):
    """A crash before run_task's own try block skips its summary; the worker
    must still tell the ward instead of only logging (code-review #54)."""
    sent = []

    async def run_task(task):
        raise RuntimeError("boom before try")

    async def dispatch(event):
        sent.append(event)

    engine = MagicMock()
    engine.run_task = run_task
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", dispatch)

    async def go():
        queue = asyncio.Queue()
        monkeypatch.setitem(task_runtime.task_queues, ROBOT, queue)
        monkeypatch.setitem(task_runtime.engines, ROBOT, engine)
        monkeypatch.setattr(task_runtime, "tasks_db", {})
        monkeypatch.setattr(task_runtime, "current_tasks", {})
        await queue.put(Task(task_id="boom", robot_id=ROBOT, steps=[]))
        worker = asyncio.create_task(task_runtime.task_worker(ROBOT))
        await asyncio.wait_for(queue.join(), timeout=2)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert len(sent) == 1
    assert sent[0].task_id == "boom"
    assert "RuntimeError" in sent[0].body
