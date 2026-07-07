"""HIL E2E tests for the IT-12 LINE notification sink.

Real LINE Messaging API push. Marked @pytest.mark.hil.

Credentials are read from ~/.claude/.env.test:
  TEST_LINE_CHANNEL_ACCESS_TOKEN, TEST_LINE_TARGET_ID
TEST_LINE_TARGET_ID is a groupId/userId captured by the xinyin7f webhook
service (invite the bot @850pdvwr to a group, then GET /groups).
"""
from __future__ import annotations

import asyncio

import pytest

from services import line_service
from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.line import LineSink


@pytest.fixture
def line_creds(env_test):
    token = env_test.get("TEST_LINE_CHANNEL_ACCESS_TOKEN")
    target = env_test.get("TEST_LINE_TARGET_ID")
    if not token or not target:
        pytest.skip("TEST_LINE_CHANNEL_ACCESS_TOKEN / TEST_LINE_TARGET_ID not set in ~/.claude/.env.test")
    return token, target


@pytest.fixture
def line_settings(line_creds, monkeypatch):
    """Patch every get_runtime_settings consumer in the LINE notification path."""
    token, target = line_creds
    fake = {
        "enable_line": True,
        "line_channel_access_token": token,
        "line_group_ids": [target],
    }
    monkeypatch.setattr("services.notifications.sinks.line.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.notifications.recipients.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.line_service.get_runtime_settings", lambda: fake)
    return fake


@pytest.mark.hil
@pytest.mark.slow
def test_dispatcher_pushes_to_line(line_settings, make_failure_event, monkeypatch):
    """Real LINE push to the configured target. send_line_message must return True
    (LINE API 200) — visual confirmation in the group is the human-side check.
    """
    sent: list[bool] = []
    orig = line_service.send_line_message

    async def _tracking(message, to):
        ok = await orig(message, to=to)
        sent.append(ok)
        return ok

    monkeypatch.setattr("services.notifications.sinks.line.send_line_message", _tracking)

    async def _run():
        d = AnomalyDispatcher()
        d.register(LineSink(StaticResolver()))
        await d.dispatch(make_failure_event("hil-line-test"))
        await d.drain(timeout=15.0)

    asyncio.run(_run())
    assert sent == [True], f"LINE push failed: {sent}"
