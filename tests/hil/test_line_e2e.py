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

from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.evaluator import ScanOutcome, BioScanFailureEvaluator
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


def _make_failure_event():
    outcome = ScanOutcome(
        task_id="hil-line-test",
        location_id="hil-101-1",
        bed_name="HIL-101-1",
        valid_record=None,
        retry_count=19,
        last_record_raw={"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"},
        last_failure_reason="無有效量測數值",
    )
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    return event


@pytest.mark.hil
@pytest.mark.slow
def test_dispatcher_pushes_to_line(line_settings):
    """Real LINE push to the configured target. send_line_message must return True
    (LINE API 200) — visual confirmation in the group is the human-side check.
    """
    from services import line_service

    sent: list[bool] = []
    orig = line_service.send_line_message

    async def _tracking(message, to):
        ok = await orig(message, to=to)
        sent.append(ok)
        return ok

    async def _run():
        import services.notifications.sinks.line as line_sink_mod
        original = line_sink_mod.send_line_message
        line_sink_mod.send_line_message = _tracking
        try:
            d = AnomalyDispatcher()
            d.register(LineSink(StaticResolver()))
            await d.dispatch(_make_failure_event())
            await d.drain(timeout=15.0)
        finally:
            line_sink_mod.send_line_message = original

    asyncio.run(_run())
    assert sent == [True], f"LINE push failed: {sent}"
