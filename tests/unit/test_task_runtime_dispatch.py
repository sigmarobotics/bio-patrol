"""Unit tests for TaskEngine action dispatch table.

Verifies the StepAction → handler mapping covers every enum member and that
unknown actions yield a structured failure StepResult instead of raising.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import StepAction, TaskStep
from services import task_runtime


def _make_engine() -> task_runtime.TaskEngine:
    fleet = MagicMock()
    return task_runtime.TaskEngine(fleet, "kachaka")


def test_dispatch_table_covers_every_step_action():
    engine = _make_engine()
    handler_keys = set(engine._action_handlers.keys())
    expected = {a.value for a in StepAction}
    assert handler_keys == expected


def test_unknown_action_returns_failure_step_result():
    engine = _make_engine()
    engine.current_task_id = "t-1"
    step = TaskStep(step_id="s-1", action="not_a_real_action", params={})

    result = asyncio.run(engine._execute_step(step))

    assert result.success is False
    assert result.error_code == -1
    assert "Unknown action" in result.error_message
    assert result.data == {"action": "not_a_real_action"}


def test_value_error_in_handler_is_caught():
    engine = _make_engine()
    engine.fleet.return_home = AsyncMock(side_effect=ValueError("Robot kachaka not found"))
    step = TaskStep(step_id="s-1", action=StepAction.RETURN_HOME.value, params={})

    result = asyncio.run(engine._execute_step(step))

    assert result.success is False
    assert "not found" in result.error_message
    assert result.data["action"] == StepAction.RETURN_HOME.value


def test_unexpected_exception_in_handler_is_caught():
    engine = _make_engine()
    engine.fleet.return_home = AsyncMock(side_effect=RuntimeError("network down"))
    step = TaskStep(step_id="s-1", action=StepAction.RETURN_HOME.value, params={})

    result = asyncio.run(engine._execute_step(step))

    assert result.success is False
    assert "Unexpected error" in result.error_message
    assert "network down" in result.error_message


def test_speak_handler_calls_fleet_speak():
    engine = _make_engine()
    engine.fleet.speak = AsyncMock(return_value={"ok": True})
    step = TaskStep(step_id="s-1", action=StepAction.SPEAK.value, params={"speak_text": "hi"})

    result = asyncio.run(engine._execute_step(step))

    engine.fleet.speak.assert_awaited_once_with("kachaka", "hi")
    assert result.success is True
    assert result.data == {"speak_text": "hi"}


def test_reset_shelf_pose_handler_calls_fleet_with_shelf_id():
    engine = _make_engine()
    engine.fleet.reset_shelf_pose = AsyncMock(return_value={"ok": True})
    step = TaskStep(
        step_id="reset_shelf",
        action=StepAction.RESET_SHELF_POSE.value,
        params={"shelf_id": "S_04"},
    )

    result = asyncio.run(engine._execute_step(step))

    engine.fleet.reset_shelf_pose.assert_awaited_once_with("kachaka", "S_04")
    assert result.success is True
    assert result.data == {"shelf_id": "S_04"}


def test_wait_handler_sleeps_and_succeeds():
    engine = _make_engine()
    step = TaskStep(step_id="s-1", action=StepAction.WAIT.value, params={"seconds": "0.01"})

    result = asyncio.run(engine._execute_step(step))

    assert result.success is True
    assert result.data == {"seconds": 0.01}
