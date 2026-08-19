"""Action registry for Zigbee button bindings.

Each registered action is an async coroutine that performs a bio-patrol
operation. Buttons in `button_bindings` are bound to one action key; pressing a
paired button looks up its IEEE → action_key and calls the registered handler.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("services.action_registry")

ActionHandler = Callable[[dict], Awaitable[dict]]


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    handler: ActionHandler
    default_params: dict


_ACTIONS: dict[str, Action] = {}


def register(key: str, label: str, handler: ActionHandler,
             default_params: dict | None = None) -> None:
    if key in _ACTIONS:
        logger.warning("Action %s already registered, overwriting", key)
    _ACTIONS[key] = Action(
        key=key, label=label, handler=handler, default_params=default_params or {}
    )


def reset() -> None:
    """Clear all registered actions (test helper)."""
    _ACTIONS.clear()


def list_actions() -> list[dict]:
    return [
        {"key": a.key, "label": a.label, "default_params": dict(a.default_params)}
        for a in _ACTIONS.values()
    ]


def is_registered(key: str) -> bool:
    return key in _ACTIONS


async def fire(key: str, params: dict | None = None) -> dict:
    action = _ACTIONS.get(key)
    if action is None:
        return {"ok": False, "error": f"unknown action: {key}"}
    merged = {**action.default_params, **(params or {})}
    try:
        result = await action.handler(merged)
        return result if isinstance(result, dict) else {"ok": True, "data": result}
    except Exception as e:
        logger.exception("Action %s raised", key)
        return {"ok": False, "error": str(e)}


async def _action_demo_run(_params: dict) -> dict:
    from routers.patrol import PatrolStartRequest, start_patrol
    return await start_patrol(PatrolStartRequest(mode="demo"))


async def _action_shelf_resume(_params: dict) -> dict:
    from routers.patrol import resume_latest_shelf_drop
    return await resume_latest_shelf_drop()


async def _action_patrol_start(_params: dict) -> dict:
    from routers.patrol import PatrolStartRequest, start_patrol
    return await start_patrol(PatrolStartRequest(mode="patrol"))


async def _action_patrol_cancel(_params: dict) -> dict:
    from services.task_runtime import current_tasks
    running_id = current_tasks.get("kachaka")
    if not running_id:
        return {"ok": False, "error": "No running task"}
    from routers.tasks import cancel_task
    task = await cancel_task(running_id)
    return {
        "ok": True,
        "task_id": running_id,
        "status": task.status.value if task and task.status else None,
    }


async def _action_return_home(_params: dict) -> dict:
    from dependencies import get_fleet
    fleet = get_fleet()
    try:
        result = await fleet.return_home("kachaka")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return result if isinstance(result, dict) else {"ok": True, "data": str(result)}


async def _action_shelf_to_ns(params: dict) -> dict:
    """Deliver a sensor shelf to the nursing station, then send the robot home.

    Replaces the old TTS test action; the registry key stays "speak" so an
    existing button binding keeps firing without re-pairing.
    """
    shelf_id = str(params.get("shelf_id") or "S05")
    location_id = str(params.get("location_id") or "NS")
    from common_types import (
        StepAction, StepStatus, Task, TaskStatus, TaskStep, generate_task_id,
    )
    from services.task_runtime import submit_task, tasks_db
    # Same one-robot/one-shelf dedup as start_patrol: any live patrol-family
    # task means this press is a duplicate or a conflict — refuse, don't queue.
    for existing in tasks_db.values():
        if existing.status not in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
            continue
        if (existing.metadata or {}).get("mode"):
            return {"ok": False, "error": f"task {existing.task_id} already running"}
    steps = [
        TaskStep(step_id="reset_shelf", action=StepAction.RESET_SHELF_POSE.value,
                 params={"shelf_id": shelf_id}, status=StepStatus.PENDING),
        TaskStep(step_id="deliver", action=StepAction.MOVE_SHELF.value,
                 params={"shelf_id": shelf_id, "location_id": location_id},
                 status=StepStatus.PENDING),
        TaskStep(step_id="go_home", action=StepAction.RETURN_HOME.value,
                 params={}, status=StepStatus.PENDING),
    ]
    task = Task(task_id=generate_task_id(), robot_id="kachaka", steps=steps,
                status=TaskStatus.QUEUED, metadata={"mode": "delivery"})
    tasks_db[task.task_id] = task
    await submit_task(task)
    return {"ok": True, "task_id": task.task_id}


DEFAULT_ACTIONS: list[tuple[str, str, ActionHandler, dict]] = [
    ("demo_run",      "Demo Run",                  _action_demo_run,      {}),
    ("shelf_resume",  "Resume after shelf drop",   _action_shelf_resume,  {}),
    ("patrol_start",  "Start patrol",              _action_patrol_start,  {}),
    ("patrol_cancel", "Cancel running patrol",     _action_patrol_cancel, {}),
    ("return_home",   "Return home",               _action_return_home,   {}),
    ("speak",         "送 S_01 到護理站＋回家充電", _action_shelf_to_ns,
     {"shelf_id": "S05", "location_id": "NS"}),
]


def init_default_actions() -> None:
    """Register all bio-patrol actions. Idempotent."""
    for key, label, handler, params in DEFAULT_ACTIONS:
        register(key, label, handler, params)
