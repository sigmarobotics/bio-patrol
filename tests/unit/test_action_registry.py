"""Unit tests for the action_registry — pure logic, no robot."""
from __future__ import annotations

import asyncio

import pytest

from services import action_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    action_registry.reset()
    yield
    action_registry.reset()


def test_register_and_list():
    async def _h(_p):
        return {"ok": True}

    action_registry.register("foo", "Foo", _h)
    listed = action_registry.list_actions()
    assert listed == [{"key": "foo", "label": "Foo", "default_params": {}}]
    assert action_registry.is_registered("foo")
    assert not action_registry.is_registered("missing")


def test_fire_unknown_action_returns_error():
    result = asyncio.run(action_registry.fire("nope"))
    assert result["ok"] is False
    assert "unknown action" in result["error"]


def test_fire_calls_handler_and_returns_dict():
    captured = {}

    async def _h(params):
        captured["params"] = params
        return {"ok": True, "echo": params.get("text")}

    action_registry.register("speak", "Speak", _h, default_params={"text": "hi"})
    result = asyncio.run(action_registry.fire("speak"))
    assert result == {"ok": True, "echo": "hi"}
    assert captured["params"] == {"text": "hi"}


def test_fire_caller_params_override_defaults():
    async def _h(params):
        return {"ok": True, "params": params}

    action_registry.register("speak", "Speak", _h, default_params={"text": "hi", "lang": "zh"})
    result = asyncio.run(action_registry.fire("speak", {"text": "你好"}))
    assert result["params"] == {"text": "你好", "lang": "zh"}


def test_fire_swallows_handler_exceptions():
    async def _h(_p):
        raise RuntimeError("boom")

    action_registry.register("explode", "Explode", _h)
    result = asyncio.run(action_registry.fire("explode"))
    assert result["ok"] is False
    assert "boom" in result["error"]


def test_handler_returning_non_dict_is_wrapped():
    async def _h(_p):
        return "scalar"

    action_registry.register("s", "S", _h)
    result = asyncio.run(action_registry.fire("s"))
    assert result == {"ok": True, "data": "scalar"}


def test_init_default_actions_registers_six():
    action_registry.init_default_actions()
    keys = {a["key"] for a in action_registry.list_actions()}
    assert keys == {"demo_run", "shelf_resume", "patrol_start",
                     "patrol_cancel", "return_home", "speak"}
