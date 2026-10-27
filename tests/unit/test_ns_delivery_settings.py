"""F-53 / BIZ-019: the 送護理站 action ("speak" key) reads its shelf and
location from per-site settings instead of 板榮's hardcoded S05/NS."""
from __future__ import annotations

import asyncio

import pytest

from services import action_registry
from services import task_runtime
from services.task_runtime import tasks_db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    action_registry.reset()
    action_registry.init_default_actions()
    tasks_db.clear()
    submitted = []

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(task_runtime, "submit_task", _submit)
    yield submitted
    tasks_db.clear()
    action_registry.reset()


def _settings(monkeypatch, **cfg):
    monkeypatch.setattr(action_registry, "get_runtime_settings", lambda: cfg)


def _steps(task):
    return {s.step_id: s.params for s in task.steps}


def test_registry_entry_carries_no_site_shelf():
    entry = next(a for a in action_registry.list_actions() if a["key"] == "speak")
    assert entry["label"] == "送感測貨架到護理站＋回家充電"
    assert entry["default_params"] == {}


def test_bare_fire_uses_configured_shelf_and_location(monkeypatch, _env):
    _settings(monkeypatch, ns_delivery_shelf_id="S01", ns_delivery_location_id="L_NS")
    result = asyncio.run(action_registry.fire("speak"))
    assert result["ok"] is True
    (task,) = _env
    steps = _steps(task)
    assert steps["reset_shelf"] == {"shelf_id": "S01"}
    assert steps["deliver"] == {"shelf_id": "S01", "location_id": "L_NS"}


def test_location_falls_back_to_ns(monkeypatch, _env):
    _settings(monkeypatch, ns_delivery_shelf_id="S05")
    assert asyncio.run(action_registry.fire("speak"))["ok"] is True
    assert _steps(_env[0])["deliver"] == {"shelf_id": "S05", "location_id": "NS"}


def test_refuses_without_configured_shelf(monkeypatch, _env):
    _settings(monkeypatch, ns_delivery_shelf_id="", ns_delivery_location_id="NS")
    result = asyncio.run(action_registry.fire("speak"))
    assert result == {"ok": False,
                      "error": "未設定送護理站貨架（Settings ns_delivery_shelf_id）"}
    assert _env == []
    assert len(tasks_db) == 0


def test_explicit_params_win_over_settings(monkeypatch, _env):
    _settings(monkeypatch, ns_delivery_shelf_id="S01", ns_delivery_location_id="NS")
    result = asyncio.run(action_registry.fire(
        "speak", {"shelf_id": "S09", "location_id": "L2"}))
    assert result["ok"] is True
    assert _steps(_env[0])["deliver"] == {"shelf_id": "S09", "location_id": "L2"}
