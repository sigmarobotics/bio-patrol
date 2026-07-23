"""Unit tests for LineSink + StaticResolver line channel."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.line import LineSink


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="⚠️ 101-1 量測失敗",
        body="床位：101-1\n狀況：人員躁動，無穩定讀值\n重試次數：19",
        bed_key="101-1",
        task_id="task-1",
    )


def test_format_plain_text_without_footer():
    sink = LineSink(StaticResolver())
    e = _event()
    rendered = sink._format(e)
    assert rendered.startswith("⚠️ 101-1 量測失敗\n\n")
    assert e.body in rendered
    assert e.event_id[-8:] not in rendered
    assert "<b>" not in rendered  # LINE push is plain text, no HTML


def test_format_title_only_event_renders_single_line_title():
    sink = LineSink(StaticResolver())
    e = AnomalyEvent(severity=Severity.INFO, source=Source.TASK_SUMMARY, title="✅ 巡邏完成")
    rendered = sink._format(e)
    assert rendered == "✅ 巡邏完成"


def test_is_enabled_reads_settings():
    sink = LineSink(StaticResolver())
    with patch("services.notifications.sinks.line.get_runtime_settings",
               return_value={"enable_line": True}):
        assert asyncio.run(sink.is_enabled()) is True
    with patch("services.notifications.sinks.line.get_runtime_settings",
               return_value={"enable_line": False}):
        assert asyncio.run(sink.is_enabled()) is False


def test_resolver_returns_configured_group_ids_for_line_channel():
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={"line_group_ids": ["Cgid1", "", "Uuid2"]}):
        resolved = asyncio.run(StaticResolver().resolve(_event(), channel="line"))
    assert resolved == ["Cgid1", "Uuid2"]  # empties filtered out


def test_resolver_line_channel_defaults_to_empty():
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={}):
        assert asyncio.run(StaticResolver().resolve(_event(), channel="line")) == []


def test_resolver_line_channel_tolerates_malformed_settings():
    """settings.json is merged unvalidated — null/string/int must not raise
    (or worse, iterate a string per character → one push per char)."""
    for bad in (None, "Cgid-as-bare-string", 42):
        with patch("services.notifications.recipients.get_runtime_settings",
                   return_value={"line_group_ids": bad}):
            assert asyncio.run(StaticResolver().resolve(_event(), channel="line")) == []


def test_send_no_targets_makes_no_api_call():
    sink = LineSink(StaticResolver())
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={"line_group_ids": []}), \
         patch("services.notifications.sinks.line.send_line_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        mock_send.assert_not_awaited()


def test_send_one_target_per_group():
    sink = LineSink(StaticResolver())
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={"line_group_ids": ["Cgid1", "Cgid2"]}), \
         patch("services.notifications.sinks.line.send_line_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        targets = [call.kwargs["to"] for call in mock_send.await_args_list]
        assert sorted(targets) == ["Cgid1", "Cgid2"]
        args, _ = mock_send.await_args
        assert "⚠️ 101-1 量測失敗" in args[0]


def test_send_survives_one_target_failure():
    """gather(return_exceptions=True) — one失敗不影響其他目標。"""
    calls = []

    async def flaky(message, to):
        calls.append(to)
        if to == "Cbad":
            raise RuntimeError("boom")

    class Three:
        async def resolve(self, event, channel):
            return ["Cgood1", "Cbad", "Cgood2"]

    sink = LineSink(Three())
    with patch("services.notifications.sinks.line.send_line_message", side_effect=flaky):
        asyncio.run(sink.send(_event()))
    assert sorted(calls) == ["Cbad", "Cgood1", "Cgood2"]
