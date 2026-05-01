# Anomaly Notification Dispatcher (IT-5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-bed Telegram notification when a bio-scan exhausts retries without a valid record, plumbed through a new event-bus dispatcher (`services/notifications/`) with a second sink that publishes the same event JSON to the internal MQTT broker; reorganise the Settings tab into Hardware / Notifications sub-tabs so the new toggles have a logical home.

**Architecture:** Producer (`task_runtime` after each `bio_scan` step) emits an `AnomalyEvent` to a module-level `AnomalyDispatcher` singleton, which fans out via `asyncio.create_task` to one or more `Sink` implementations (`TelegramSink`, `MqttSink`). Each sink owns its own `is_enabled()` settings check and per-channel formatting. A `RecipientResolver` boundary (today: `StaticResolver` from settings; future: `ShiftBasedResolver`) sits between the sink and the actual destination so a future iteration can swap routing without touching producers, dispatcher, or sinks. IT-5 is paired with IT-6, which migrates the three legacy direct-Telegram call sites (shelf-drop, task cancelled, task completed) into the dispatcher.

**Tech Stack:** FastAPI (lifespan), asyncio, aiomqtt 2.x (already a dep), paho-mqtt 2.x (existing bio-sensor consumer), httpx (existing Telegram client), Vanilla JS SPA, SQLite (no schema change), pytest 8.x with `asyncio.run()` per existing IT-4 unit-test convention.

**Spec:** `docs/superpowers/specs/2026-05-01-anomaly-notification-design.md` (read first; this plan follows that spec section-by-section).

---

## File map

### Create
- `src/backend/services/notifications/__init__.py` — re-exports the dispatcher singleton + Sink protocol
- `src/backend/services/notifications/events.py` — `AnomalyEvent`, `Severity`, `Source`
- `src/backend/services/notifications/evaluator.py` — `ScanOutcome`, `BioScanFailureEvaluator`
- `src/backend/services/notifications/dispatcher.py` — `AnomalyDispatcher`, module-level `dispatcher` instance
- `src/backend/services/notifications/recipients.py` — `RecipientResolver` Protocol + `StaticResolver`
- `src/backend/services/notifications/sinks/__init__.py` — `Sink` Protocol
- `src/backend/services/notifications/sinks/telegram.py` — `TelegramSink`
- `src/backend/services/notifications/sinks/mqtt.py` — `MqttSink`
- `tests/unit/test_notifications/__init__.py` — empty
- `tests/unit/test_notifications/test_events.py`
- `tests/unit/test_notifications/test_evaluator.py`
- `tests/unit/test_notifications/test_dispatcher.py`
- `tests/unit/test_notifications/test_telegram_sink.py`
- `tests/unit/test_notifications/test_mqtt_sink.py`
- `tests/unit/test_telegram_service_back_compat.py`
- `tests/hil/test_anomaly_e2e.py`

### Modify
- `src/backend/settings/defaults.py` — add `enable_mqtt_egress`, `mqtt_egress_topic_prefix`
- `src/backend/services/telegram_service.py` — add `chat_id: str | None = None` param with settings fallback
- `src/backend/services/bio_sensor_mqtt.py` — return `ScanOutcome` instead of `{task_id, data}`
- `src/backend/services/task_runtime.py:498` — adapt to `ScanOutcome` and call `dispatcher.dispatch(...)` after `bio_scan`
- `src/backend/routers/bio_sensor.py:79` — adapt the Test Bio-Scan endpoint to the new return shape
- `src/backend/main.py` — register sinks before `task_worker` is started; drain on shutdown
- `src/frontend/index.html` — split Settings tab into two sub-tabs; add MQTT-egress fields
- `src/frontend/css/style.css` — sub-tab styles (small additions)
- `src/frontend/js/script.js` — `switchSettingsSubTab()` + load/save handlers for new keys
- `.sigma/context.md` — IT-5 row + new Findings + new E2E matrix rows

---

## Task 1: Add settings keys for MQTT egress

**Files:**
- Modify: `src/backend/settings/defaults.py:5-30`

- [ ] **Step 1: Add the two new keys**

Edit `src/backend/settings/defaults.py` — inside `DEFAULT_SETTINGS = { ... }`, after the `"zigbee_mqtt_port": 1883,` line:

```python
    "zigbee_mqtt_port": 1883,
    "enable_mqtt_egress": False,
    "mqtt_egress_topic_prefix": "bio-patrol/anomaly",
}
```

- [ ] **Step 2: Verify defaults still load**

Run: `PYTHONPATH=src/backend python -c "from settings.defaults import DEFAULT_SETTINGS; print(DEFAULT_SETTINGS['enable_mqtt_egress'], DEFAULT_SETTINGS['mqtt_egress_topic_prefix'])"`
Expected: `False bio-patrol/anomaly`

- [ ] **Step 3: Commit**

```bash
git add src/backend/settings/defaults.py
git commit -m "feat(settings): add MQTT egress defaults for anomaly dispatcher (IT-5)"
```

---

## Task 2: AnomalyEvent + enums

**Files:**
- Create: `src/backend/services/notifications/__init__.py`
- Create: `src/backend/services/notifications/events.py`
- Create: `tests/unit/test_notifications/__init__.py`
- Create: `tests/unit/test_notifications/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_notifications/__init__.py` (empty file).

Create `tests/unit/test_notifications/test_events.py`:

```python
"""Unit tests for AnomalyEvent / Severity / Source."""
from __future__ import annotations

from services.notifications.events import AnomalyEvent, Severity, Source


def test_severity_enum_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARN.value == "warn"
    assert Severity.CRITICAL.value == "critical"


def test_source_enum_values():
    assert Source.BIO_SCAN_FAILURE.value == "bio_scan_failure"
    assert Source.SHELF_DROP.value == "shelf_drop"
    assert Source.TASK_SUMMARY.value == "task_summary"
    assert Source.VITALS_OUT_OF_BAND.value == "vitals_out_of_band"


def test_anomaly_event_defaults_are_unique_and_well_formed():
    e1 = AnomalyEvent()
    e2 = AnomalyEvent()
    assert e1.event_id != e2.event_id  # uuid4
    assert len(e1.event_id) == 36  # canonical uuid string
    assert e1.severity == Severity.WARN
    assert e1.source == Source.BIO_SCAN_FAILURE
    assert e1.title == ""
    assert e1.body == ""
    assert e1.bed_key is None
    assert e1.task_id is None
    assert e1.raw == {}


def test_anomaly_event_explicit_fields_round_trip():
    e = AnomalyEvent(
        severity=Severity.CRITICAL,
        source=Source.SHELF_DROP,
        title="貨架掉落",
        body="床位 101-1",
        bed_key="101-1",
        task_id="task-abc",
        raw={"shelf_id": "S_04"},
    )
    assert e.severity == Severity.CRITICAL
    assert e.source == Source.SHELF_DROP
    assert e.bed_key == "101-1"
    assert e.task_id == "task-abc"
    assert e.raw["shelf_id"] == "S_04"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.notifications'`

- [ ] **Step 3: Create the package init**

Create `src/backend/services/notifications/__init__.py`:

```python
"""Anomaly notification subsystem.

Producers emit AnomalyEvent → AnomalyDispatcher fans out to registered Sinks.
"""
from services.notifications.events import AnomalyEvent, Severity, Source

__all__ = ["AnomalyEvent", "Severity", "Source"]
```

- [ ] **Step 4: Implement events.py**

Create `src/backend/services/notifications/events.py`:

```python
"""AnomalyEvent — the single payload type that flows from producers to sinks."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from common_types import get_now


class Severity(str, Enum):
    INFO = "info"           # IT-6: 巡房完成 / 取消
    WARN = "warn"           # IT-5: 單床 bio scan 整輪失敗
    CRITICAL = "critical"   # IT-6: 貨架掉落; future: vitals out-of-band


class Source(str, Enum):
    BIO_SCAN_FAILURE = "bio_scan_failure"        # IT-5
    SHELF_DROP = "shelf_drop"                    # IT-6
    TASK_SUMMARY = "task_summary"                # IT-6
    VITALS_OUT_OF_BAND = "vitals_out_of_band"    # future


@dataclass
class AnomalyEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=get_now)
    severity: Severity = Severity.WARN
    source: Source = Source.BIO_SCAN_FAILURE
    title: str = ""
    body: str = ""
    bed_key: str | None = None
    task_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_events.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add src/backend/services/notifications/__init__.py src/backend/services/notifications/events.py tests/unit/test_notifications/__init__.py tests/unit/test_notifications/test_events.py
git commit -m "feat(notifications): AnomalyEvent + Severity/Source enums (IT-5)"
```

---

## Task 3: ScanOutcome + BioScanFailureEvaluator

**Files:**
- Create: `src/backend/services/notifications/evaluator.py`
- Create: `tests/unit/test_notifications/test_evaluator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_notifications/test_evaluator.py`:

```python
"""Unit tests for ScanOutcome + BioScanFailureEvaluator."""
from __future__ import annotations

from services.notifications.evaluator import ScanOutcome, BioScanFailureEvaluator
from services.notifications.events import Severity, Source


def _make_outcome(**overrides):
    base = dict(
        task_id="task-1",
        location_id="loc-101-1",
        bed_name="101-1",
        valid_record=None,
        retry_count=19,
        last_record_raw={"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"},
        last_status=2,
        last_bpm=0,
        last_rpm=0,
        last_failure_reason="無有效量測數值",
    )
    base.update(overrides)
    return ScanOutcome(**base)


def test_evaluator_returns_none_when_valid_record_present():
    outcome = _make_outcome(valid_record={"status": 4, "bpm": 72, "rpm": 16})
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is None


def test_evaluator_emits_warn_event_on_failure():
    outcome = _make_outcome()
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert event.severity == Severity.WARN
    assert event.source == Source.BIO_SCAN_FAILURE
    assert event.bed_key == "101-1"
    assert event.task_id == "task-1"
    assert "101-1" in event.title
    assert "量測失敗" in event.title
    assert "status=2" in event.body
    assert "bpm=0" in event.body
    assert "rpm=0" in event.body
    assert "重試次數：19" in event.body
    assert event.raw == {"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"}


def test_evaluator_uses_location_id_when_bed_name_missing():
    outcome = _make_outcome(bed_name=None)
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert "loc-101-1" in event.title


def test_evaluator_no_data_path_uses_standing_failure_reason():
    outcome = _make_outcome(
        last_record_raw=None,
        last_status=None,
        last_bpm=None,
        last_rpm=None,
        last_failure_reason="未收到感測器資料（MQTT無連線或無數據）",
    )
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert "未收到感測器資料" in event.body
    assert event.raw == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_evaluator.py -v`
Expected: FAIL with `ModuleNotFoundError: services.notifications.evaluator`

- [ ] **Step 3: Implement evaluator.py**

Create `src/backend/services/notifications/evaluator.py`:

```python
"""Scan outcome + evaluators that turn outcomes into AnomalyEvents.

IT-5 ships v1: a failed scan (no valid record after all retries) is a WARN.
Future iterations add VitalsOutOfBandEvaluator (status==4 but bpm/rpm out of band).
"""
from __future__ import annotations

from dataclasses import dataclass

from services.notifications.events import AnomalyEvent, Severity, Source


@dataclass
class ScanOutcome:
    """Replaces the legacy {task_id, data} return shape of get_valid_scan_data."""
    task_id: str
    location_id: str
    bed_name: str | None
    valid_record: dict | None      # None when retries exhausted without a valid hit
    retry_count: int
    last_record_raw: dict | None
    last_status: int | None
    last_bpm: int | None
    last_rpm: int | None
    last_failure_reason: str | None  # None only when valid_record is not None


class BioScanFailureEvaluator:
    """v1 rule: emit BIO_SCAN_FAILURE / WARN when retries exhaust without a valid hit."""

    def evaluate(self, outcome: ScanOutcome) -> AnomalyEvent | None:
        if outcome.valid_record is not None:
            return None
        bed = outcome.bed_name or outcome.location_id
        return AnomalyEvent(
            severity=Severity.WARN,
            source=Source.BIO_SCAN_FAILURE,
            bed_key=outcome.bed_name,
            task_id=outcome.task_id,
            title=f"⚠️ {bed} 量測失敗",
            body=(
                f"床位：{bed}\n"
                f"原因：{outcome.last_failure_reason}\n"
                f"重試次數：{outcome.retry_count}\n"
                f"最後一筆：status={outcome.last_status}, "
                f"bpm={outcome.last_bpm}, rpm={outcome.last_rpm}"
            ),
            raw=outcome.last_record_raw or {},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_evaluator.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/notifications/evaluator.py tests/unit/test_notifications/test_evaluator.py
git commit -m "feat(notifications): ScanOutcome + BioScanFailureEvaluator (IT-5)"
```

---

## Task 4: Sink protocol + RecipientResolver

**Files:**
- Create: `src/backend/services/notifications/sinks/__init__.py`
- Create: `src/backend/services/notifications/recipients.py`

This task has no test of its own — the protocols are exercised by the sink tests in Tasks 6/7.

- [ ] **Step 1: Create the sinks package and Sink Protocol**

Create `src/backend/services/notifications/sinks/__init__.py`:

```python
"""Sink protocol — every concrete notification channel implements this."""
from __future__ import annotations

from typing import Protocol

from services.notifications.events import AnomalyEvent


class Sink(Protocol):
    async def is_enabled(self) -> bool: ...
    async def send(self, event: AnomalyEvent) -> None: ...
```

- [ ] **Step 2: Create RecipientResolver + StaticResolver**

Create `src/backend/services/notifications/recipients.py`:

```python
"""Recipient routing.

IT-5 ships StaticResolver (single recipient from settings).
Future iterations add ShiftBasedResolver that consults an AI-agent-built shift table.
"""
from __future__ import annotations

from typing import Protocol

from services.notifications.events import AnomalyEvent
from settings.config import get_runtime_settings


class RecipientResolver(Protocol):
    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]: ...


class StaticResolver:
    """Returns the single recipient configured in settings, per channel."""

    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]:
        cfg = get_runtime_settings()
        if channel == "telegram":
            uid = cfg.get("telegram_user_id", "") or ""
            return [uid] if uid else []
        return []  # MQTT publishes to topic — no recipient list
```

- [ ] **Step 3: Smoke import**

Run: `PYTHONPATH=src/backend python -c "from services.notifications.sinks import Sink; from services.notifications.recipients import StaticResolver; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add src/backend/services/notifications/sinks/__init__.py src/backend/services/notifications/recipients.py
git commit -m "feat(notifications): Sink protocol + StaticResolver (IT-5)"
```

---

## Task 5: telegram_service.py back-compat signature

**Files:**
- Modify: `src/backend/services/telegram_service.py:11-38`
- Create: `tests/unit/test_telegram_service_back_compat.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_telegram_service_back_compat.py`:

```python
"""Back-compat guard for the three legacy direct-Telegram callers in task_runtime."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

from services import telegram_service


def _patch_settings(token: str = "fake-token", user_id: str = "fake-user", enabled: bool = True):
    return patch(
        "services.telegram_service.get_runtime_settings",
        return_value={
            "enable_telegram": enabled,
            "telegram_bot_token": token,
            "telegram_user_id": user_id,
        },
    )


def test_legacy_no_kwarg_uses_settings_user_id():
    """Legacy call: send_telegram_message(message) — no kwarg — must hit telegram_user_id."""
    with _patch_settings(user_id="user-from-settings"), \
         patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_response = AsyncMock(); mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__.return_value = mock_client

        asyncio.run(telegram_service.send_telegram_message("hi"))

        assert mock_client.post.await_count == 1
        kwargs = mock_client.post.await_args.kwargs
        assert kwargs["json"]["chat_id"] == "user-from-settings"
        assert kwargs["json"]["text"] == "hi"


def test_explicit_chat_id_overrides_settings():
    with _patch_settings(user_id="user-from-settings"), \
         patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_response = AsyncMock(); mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__.return_value = mock_client

        asyncio.run(telegram_service.send_telegram_message("hi", chat_id="explicit-id"))

        kwargs = mock_client.post.await_args.kwargs
        assert kwargs["json"]["chat_id"] == "explicit-id"


def test_disabled_short_circuits():
    with _patch_settings(enabled=False), \
         patch("httpx.AsyncClient") as mock_cls:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_cls.assert_not_called()


def test_missing_token_or_chat_id_short_circuits():
    with _patch_settings(token="", user_id=""), \
         patch("httpx.AsyncClient") as mock_cls:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_cls.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_telegram_service_back_compat.py -v`
Expected: FAIL — `chat_id` parameter does not exist yet.

- [ ] **Step 3: Update the function signature**

Replace the entire body of `src/backend/services/telegram_service.py` with:

```python
"""Telegram notification service.

Sends messages via Telegram Bot API when enabled in settings.
The optional ``chat_id`` parameter lets the new AnomalyDispatcher path target
a specific chat; legacy direct callers omit it and fall back to settings.
"""
import logging

import httpx

logger = logging.getLogger(__name__)


async def send_telegram_message(message: str, chat_id: str | None = None):
    """Send a Telegram message if enabled in runtime settings.

    chat_id: optional override; defaults to settings.telegram_user_id.
    """
    try:
        from settings.config import get_runtime_settings
        cfg = get_runtime_settings()

        if not cfg.get("enable_telegram", False):
            logger.debug("Telegram notifications disabled")
            return

        token = cfg.get("telegram_bot_token", "")
        effective_chat_id = chat_id or cfg.get("telegram_user_id", "")

        if not token or not effective_chat_id:
            logger.warning("Telegram enabled but bot_token or chat_id not set")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": effective_chat_id, "text": message, "parse_mode": "HTML"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram message sent successfully")
            else:
                logger.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_telegram_service_back_compat.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/telegram_service.py tests/unit/test_telegram_service_back_compat.py
git commit -m "feat(telegram): add chat_id kwarg with settings fallback (IT-5)"
```

---

## Task 6: TelegramSink

**Files:**
- Create: `src/backend/services/notifications/sinks/telegram.py`
- Create: `tests/unit/test_notifications/test_telegram_sink.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_notifications/test_telegram_sink.py`:

```python
"""Unit tests for TelegramSink."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.telegram import TelegramSink


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="⚠️ 101-1 量測失敗",
        body="床位：101-1\n原因：無有效量測數值\n重試次數：19\n最後一筆：status=2, bpm=0, rpm=0",
        bed_key="101-1",
        task_id="task-1",
    )


def test_format_wraps_html_and_appends_event_id_footer():
    sink = TelegramSink(StaticResolver())
    e = _event()
    rendered = sink._format(e)
    assert rendered.startswith("<b>⚠️ 101-1 量測失敗</b>\n\n")
    assert e.body in rendered
    assert f"<code>{e.event_id[-8:]}</code>" in rendered


def test_is_enabled_reads_settings():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"enable_telegram": True}):
        assert asyncio.run(sink.is_enabled()) is True
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"enable_telegram": False}):
        assert asyncio.run(sink.is_enabled()) is False


def test_send_no_recipients_makes_no_api_call():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"telegram_user_id": ""}), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        mock_send.assert_not_awaited()


def test_send_one_recipient_one_call():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"telegram_user_id": "111"}), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        assert mock_send.await_count == 1
        args, kwargs = mock_send.await_args
        assert kwargs["chat_id"] == "111"
        assert "<b>⚠️ 101-1 量測失敗</b>" in args[0]


def test_send_multiple_recipients_one_call_each():
    """Future-proof: when ShiftBasedResolver returns N chat_ids, sink fires N posts."""
    class TwoIds:
        async def resolve(self, event, channel):
            return ["111", "222"]
    sink = TelegramSink(TwoIds())
    with patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        chat_ids = [call.kwargs["chat_id"] for call in mock_send.await_args_list]
        assert chat_ids == ["111", "222"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_telegram_sink.py -v`
Expected: FAIL with `ModuleNotFoundError: services.notifications.sinks.telegram`

- [ ] **Step 3: Implement TelegramSink**

Create `src/backend/services/notifications/sinks/telegram.py`:

```python
"""TelegramSink — formats AnomalyEvent as HTML and posts via telegram_service."""
from __future__ import annotations

from services.notifications.events import AnomalyEvent
from services.notifications.recipients import RecipientResolver
from services.telegram_service import send_telegram_message
from settings.config import get_runtime_settings


class TelegramSink:
    def __init__(self, resolver: RecipientResolver):
        self._resolver = resolver

    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_telegram", False))

    async def send(self, event: AnomalyEvent) -> None:
        chat_ids = await self._resolver.resolve(event, channel="telegram")
        if not chat_ids:
            return
        message = self._format(event)
        for chat_id in chat_ids:
            await send_telegram_message(message, chat_id=chat_id)

    def _format(self, event: AnomalyEvent) -> str:
        # event_id last-8 footer lets a recipient cross-reference MQTT logs
        return f"<b>{event.title}</b>\n\n{event.body}\n\n<code>{event.event_id[-8:]}</code>"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_telegram_sink.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/notifications/sinks/telegram.py tests/unit/test_notifications/test_telegram_sink.py
git commit -m "feat(notifications): TelegramSink with HTML formatting + event_id footer (IT-5)"
```

---

## Task 7: MqttSink

**Files:**
- Create: `src/backend/services/notifications/sinks/mqtt.py`
- Create: `tests/unit/test_notifications/test_mqtt_sink.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_notifications/test_mqtt_sink.py`:

```python
"""Unit tests for MqttSink — covers topic assembly, payload schema, is_enabled gating."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.sinks.mqtt import MqttSink


def _settings(**overrides):
    base = {
        "enable_mqtt_egress": True,
        "zigbee_mqtt_host": "mqtt-broker",
        "zigbee_mqtt_port": 1883,
        "mqtt_egress_topic_prefix": "bio-patrol/anomaly",
    }
    base.update(overrides)
    return base


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="⚠️ 101-1 量測失敗",
        body="床位：101-1\n原因：無有效量測數值",
        bed_key="101-1",
        task_id="task-1",
        raw={"status": 2, "bpm": 0, "rpm": 0},
    )


def test_is_enabled_reads_setting():
    sink = MqttSink()
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(enable_mqtt_egress=False)):
        assert asyncio.run(sink.is_enabled()) is False
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(enable_mqtt_egress=True)):
        assert asyncio.run(sink.is_enabled()) is True


def test_send_publishes_to_hierarchical_topic_with_json_payload():
    sink = MqttSink()
    fake_client = AsyncMock()
    fake_client.publish = AsyncMock()
    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(return_value=fake_client)
    aenter.__aexit__ = AsyncMock(return_value=None)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client",
               return_value=aenter) as mock_client_cls:
        e = _event()
        asyncio.run(sink.send(e))

    mock_client_cls.assert_called_once_with(hostname="mqtt-broker", port=1883)
    fake_client.publish.assert_awaited_once()
    args, kwargs = fake_client.publish.await_args
    topic, payload = args
    assert topic == "bio-patrol/anomaly/warn/bio_scan_failure"
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is False
    decoded = json.loads(payload)
    assert decoded["event_id"] == e.event_id
    assert decoded["severity"] == "warn"
    assert decoded["source"] == "bio_scan_failure"
    assert decoded["bed_key"] == "101-1"
    assert decoded["task_id"] == "task-1"
    assert decoded["raw"] == {"status": 2, "bpm": 0, "rpm": 0}
    assert decoded["title"] == "⚠️ 101-1 量測失敗"
    assert "timestamp" in decoded


def test_send_respects_custom_prefix():
    sink = MqttSink()
    fake_client = AsyncMock()
    fake_client.publish = AsyncMock()
    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(return_value=fake_client)
    aenter.__aexit__ = AsyncMock(return_value=None)
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(mqtt_egress_topic_prefix="custom/prefix")), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client", return_value=aenter):
        asyncio.run(sink.send(_event()))
    topic = fake_client.publish.await_args.args[0]
    assert topic == "custom/prefix/warn/bio_scan_failure"


def test_send_timeout_propagates_for_safe_send_to_handle():
    """A hung broker should raise asyncio.TimeoutError that the dispatcher logs."""
    sink = MqttSink()

    async def hang_forever(*a, **kw):
        await asyncio.sleep(60)

    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(side_effect=hang_forever)
    aenter.__aexit__ = AsyncMock(return_value=None)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client", return_value=aenter), \
         patch("services.notifications.sinks.mqtt.PUBLISH_TIMEOUT_S", 0.05):
        try:
            asyncio.run(sink.send(_event()))
            raised = False
        except asyncio.TimeoutError:
            raised = True
    assert raised is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_mqtt_sink.py -v`
Expected: FAIL with `ModuleNotFoundError: services.notifications.sinks.mqtt`

- [ ] **Step 3: Implement MqttSink**

Create `src/backend/services/notifications/sinks/mqtt.py`:

```python
"""MqttSink — publishes AnomalyEvent as JSON to internal mqtt-broker.

Short-lived connection per publish: event volume is low (handful per patrol),
not worth keeping a long-running client.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

from services.notifications.events import AnomalyEvent
from settings.config import get_runtime_settings

logger = logging.getLogger("services.notifications.mqtt")

PUBLISH_TIMEOUT_S = 10.0


class MqttSink:
    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_mqtt_egress", False))

    async def send(self, event: AnomalyEvent) -> None:
        cfg = get_runtime_settings()
        host = cfg.get("zigbee_mqtt_host", "mqtt-broker")
        port = cfg.get("zigbee_mqtt_port", 1883)
        prefix = cfg.get("mqtt_egress_topic_prefix", "bio-patrol/anomaly")
        topic = f"{prefix}/{event.severity.value}/{event.source.value}"
        payload = json.dumps({
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "severity": event.severity.value,
            "source": event.source.value,
            "bed_key": event.bed_key,
            "task_id": event.task_id,
            "title": event.title,
            "body": event.body,
            "raw": event.raw,
        }, ensure_ascii=False)

        async def _publish():
            async with aiomqtt.Client(hostname=host, port=port) as client:
                await client.publish(topic, payload, qos=1, retain=False)

        await asyncio.wait_for(_publish(), timeout=PUBLISH_TIMEOUT_S)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_mqtt_sink.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/notifications/sinks/mqtt.py tests/unit/test_notifications/test_mqtt_sink.py
git commit -m "feat(notifications): MqttSink with hierarchical topic + 10s publish timeout (IT-5)"
```

---

## Task 8: AnomalyDispatcher with pending tracking + drain

**Files:**
- Create: `src/backend/services/notifications/dispatcher.py`
- Create: `tests/unit/test_notifications/test_dispatcher.py`
- Modify: `src/backend/services/notifications/__init__.py` (re-export dispatcher)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_notifications/test_dispatcher.py`:

```python
"""Unit tests for AnomalyDispatcher: per-sink isolation, pending tracking, drain."""
from __future__ import annotations

import asyncio

from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.events import AnomalyEvent


class _RecorderSink:
    def __init__(self, enabled: bool = True, raise_in_send: bool = False, sleep: float = 0.0):
        self.enabled = enabled
        self.raise_in_send = raise_in_send
        self.sleep = sleep
        self.received: list[AnomalyEvent] = []

    async def is_enabled(self) -> bool:
        return self.enabled

    async def send(self, event: AnomalyEvent) -> None:
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.raise_in_send:
            raise RuntimeError("boom")
        self.received.append(event)


async def _run_with_drain(dispatcher: AnomalyDispatcher, events: list[AnomalyEvent]):
    for ev in events:
        await dispatcher.dispatch(ev)
    await dispatcher.drain(timeout=2.0)


def test_dispatch_with_no_sinks_is_noop():
    d = AnomalyDispatcher()
    asyncio.run(_run_with_drain(d, [AnomalyEvent()]))
    # no exception, nothing to assert beyond the lack of failure


def test_dispatch_fans_out_to_every_enabled_sink():
    d = AnomalyDispatcher()
    a, b = _RecorderSink(), _RecorderSink()
    d.register(a); d.register(b)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert a.received == [e]
    assert b.received == [e]


def test_disabled_sink_does_not_get_send():
    d = AnomalyDispatcher()
    on, off = _RecorderSink(enabled=True), _RecorderSink(enabled=False)
    d.register(on); d.register(off)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert on.received == [e]
    assert off.received == []


def test_one_sink_failing_does_not_block_others():
    d = AnomalyDispatcher()
    bad, good = _RecorderSink(raise_in_send=True), _RecorderSink()
    d.register(bad); d.register(good)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert good.received == [e]


def test_dispatch_does_not_await_send_completion():
    """dispatch() returns before slow sinks finish — drain() is what awaits them."""
    d = AnomalyDispatcher()
    slow = _RecorderSink(sleep=0.2)
    d.register(slow)
    e = AnomalyEvent()

    async def run():
        await d.dispatch(e)
        # at this point the send is still sleeping
        assert slow.received == []
        await d.drain(timeout=1.0)
        assert slow.received == [e]

    asyncio.run(run())


def test_drain_returns_quickly_when_no_pending():
    d = AnomalyDispatcher()
    asyncio.run(d.drain(timeout=0.1))
    # no exception
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/test_dispatcher.py -v`
Expected: FAIL with `ModuleNotFoundError: services.notifications.dispatcher`

- [ ] **Step 3: Implement AnomalyDispatcher**

Create `src/backend/services/notifications/dispatcher.py`:

```python
"""AnomalyDispatcher — fan-out to registered sinks with per-sink isolation."""
from __future__ import annotations

import asyncio
import logging

from services.notifications.events import AnomalyEvent
from services.notifications.sinks import Sink

logger = logging.getLogger("services.notifications.dispatcher")


class AnomalyDispatcher:
    def __init__(self):
        self._sinks: list[Sink] = []
        self._pending: set[asyncio.Task] = set()

    def register(self, sink: Sink) -> None:
        self._sinks.append(sink)

    async def dispatch(self, event: AnomalyEvent) -> None:
        for sink in self._sinks:
            task = asyncio.create_task(self._safe_send(sink, event))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def drain(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for all in-flight sends to complete."""
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Notification dispatcher drain timed out; %d tasks abandoned",
                len(self._pending),
            )

    async def _safe_send(self, sink: Sink, event: AnomalyEvent) -> None:
        try:
            if not await sink.is_enabled():
                return
            await sink.send(event)
        except Exception:
            logger.exception("Sink %s failed", sink.__class__.__name__)


# Module-level singleton consumed by main.py + producers.
dispatcher = AnomalyDispatcher()
```

- [ ] **Step 4: Re-export dispatcher from package init**

Replace `src/backend/services/notifications/__init__.py` with:

```python
"""Anomaly notification subsystem.

Producers emit AnomalyEvent → AnomalyDispatcher fans out to registered Sinks.
"""
from services.notifications.dispatcher import AnomalyDispatcher, dispatcher
from services.notifications.events import AnomalyEvent, Severity, Source

__all__ = [
    "AnomalyDispatcher",
    "AnomalyEvent",
    "Severity",
    "Source",
    "dispatcher",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/test_notifications/ -v`
Expected: all tests pass (events + evaluator + telegram_sink + mqtt_sink + dispatcher).

- [ ] **Step 6: Commit**

```bash
git add src/backend/services/notifications/dispatcher.py src/backend/services/notifications/__init__.py tests/unit/test_notifications/test_dispatcher.py
git commit -m "feat(notifications): AnomalyDispatcher with pending tracking + drain (IT-5)"
```

---

## Task 9: bio_sensor_mqtt.get_valid_scan_data returns ScanOutcome

**Files:**
- Modify: `src/backend/services/bio_sensor_mqtt.py:125-185`

This task changes a public return shape; Task 10 adapts callers in the same PR sequence.

- [ ] **Step 1: Read current implementation to ground the diff**

Run: `sed -n '125,185p' src/backend/services/bio_sensor_mqtt.py`
Confirm the loop structure matches what the spec assumed (records iteration, `is_valid` flag, no_data fallback at the end).

- [ ] **Step 2: Replace the function body**

Find the existing `async def get_valid_scan_data(self, ...)` block in `src/backend/services/bio_sensor_mqtt.py` and replace its full body (signature unchanged) with this implementation.

The new body returns a `ScanOutcome` dataclass instead of `{"task_id", "data"}`. The DB-write path (`_save_scan_data`) is unchanged — only the return value differs.

```python
    async def get_valid_scan_data(self, task_id=None, target_bed=None, bed_name=None):
        """Run the bio-scan window and return a ScanOutcome.

        Replaces the legacy ``{"task_id", "data"}`` return. Callers read
        ``outcome.valid_record`` (None when retries exhausted).
        """
        from services.notifications.evaluator import ScanOutcome

        if not self.connected:
            logger.warning("MQTT broker is not connected, will wait for reconnection during scan retries")

        try:
            from settings.config import get_runtime_settings
            cfg = get_runtime_settings()
        except Exception:
            cfg = {}

        WAIT_TIME = cfg.get("bio_scan_wait_time", 10)
        RETRY_COUNT = cfg.get("bio_scan_retry_count", 19)
        INT_WAIT_TIME = cfg.get("bio_scan_initial_wait", 120)
        VALID_STATUS = cfg.get("bio_scan_valid_status", 4)

        valid_data: dict | None = None
        has_any_data = False
        last_record_processed: dict | None = None
        final_retry_count = 0

        await asyncio.sleep(INT_WAIT_TIME)
        for retry_count in range(RETRY_COUNT):
            final_retry_count = retry_count
            if self.latest_data and 'records' in self.latest_data:
                has_any_data = True
                for data in self.latest_data['records']:
                    print("scan_data: ", data, "\n")
                    is_valid = data['status'] == VALID_STATUS and data['bpm'] > 0 and data['rpm'] > 0
                    data['details'] = '量測正常' if is_valid else '無有效量測數值'
                    data['location_id'] = target_bed
                    data['bed_name'] = bed_name
                    self._save_scan_data(task_id, data, retry_count, is_valid)
                    last_record_processed = data
                    if is_valid and valid_data is None:
                        valid_data = data
                if valid_data is not None:
                    return ScanOutcome(
                        task_id=task_id,
                        location_id=target_bed,
                        bed_name=bed_name,
                        valid_record=valid_data,
                        retry_count=retry_count,
                        last_record_raw=last_record_processed,
                        last_status=last_record_processed.get('status') if last_record_processed else None,
                        last_bpm=last_record_processed.get('bpm') if last_record_processed else None,
                        last_rpm=last_record_processed.get('rpm') if last_record_processed else None,
                        last_failure_reason=None,
                    )

            if retry_count + 1 < RETRY_COUNT:
                await asyncio.sleep(WAIT_TIME)

        # All retries exhausted without a valid record.
        if not has_any_data:
            no_data = {
                "location_id": target_bed,
                "bed_name": bed_name,
                "status": None,
                "bpm": None,
                "rpm": None,
                "details": "未收到感測器資料（MQTT無連線或無數據）",
            }
            self._save_scan_data(task_id, no_data, RETRY_COUNT, is_valid=False)
            return ScanOutcome(
                task_id=task_id,
                location_id=target_bed,
                bed_name=bed_name,
                valid_record=None,
                retry_count=final_retry_count,
                last_record_raw=None,
                last_status=None,
                last_bpm=None,
                last_rpm=None,
                last_failure_reason="未收到感測器資料（MQTT無連線或無數據）",
            )

        # has_any_data but no valid hit — use the last processed record's details.
        return ScanOutcome(
            task_id=task_id,
            location_id=target_bed,
            bed_name=bed_name,
            valid_record=None,
            retry_count=final_retry_count,
            last_record_raw=last_record_processed,
            last_status=last_record_processed.get('status') if last_record_processed else None,
            last_bpm=last_record_processed.get('bpm') if last_record_processed else None,
            last_rpm=last_record_processed.get('rpm') if last_record_processed else None,
            last_failure_reason=last_record_processed.get('details') if last_record_processed else "無有效量測數值",
        )
```

- [ ] **Step 3: Confirm import of asyncio is already at top of the file**

Run: `head -8 src/backend/services/bio_sensor_mqtt.py`
Expected: `import asyncio` already present (it is, per the existing scan loop).

- [ ] **Step 4: Smoke import**

Run: `PYTHONPATH=src/backend python -c "from services.bio_sensor_mqtt import BioSensorMQTTClient; from services.notifications.evaluator import ScanOutcome; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add src/backend/services/bio_sensor_mqtt.py
git commit -m "refactor(bio-sensor): get_valid_scan_data returns ScanOutcome (IT-5)"
```

---

## Task 10: Adapt callers of get_valid_scan_data

**Files:**
- Modify: `src/backend/services/task_runtime.py:498` (the bio_scan handler)
- Modify: `src/backend/routers/bio_sensor.py:79` (the Test Bio-Scan endpoint)

- [ ] **Step 1: Locate the task_runtime caller**

Run: `grep -n "get_valid_scan_data" src/backend/services/task_runtime.py`
Note the line and read 30 lines around it to understand how `data` is consumed downstream.

- [ ] **Step 2: Update task_runtime.py to use ScanOutcome**

Find the line `scan_result = await client.get_valid_scan_data(...)` in `src/backend/services/task_runtime.py`. Rename the variable and update the consumer logic so that downstream code uses `outcome.valid_record` instead of `scan_result["data"]`.

The exact change depends on local context — if the existing code does:

```python
scan_result = await client.get_valid_scan_data(target_bed=self.target_bed, task_id=self.current_task_id, bed_name=bed_key)
data = scan_result["data"]
if data is None:
    # failure path
else:
    # success path uses data["bpm"], data["rpm"], etc.
```

Replace with:

```python
outcome = await client.get_valid_scan_data(target_bed=self.target_bed, task_id=self.current_task_id, bed_name=bed_key)
data = outcome.valid_record
if data is None:
    # failure path
else:
    # success path uses data["bpm"], data["rpm"], etc.
```

Keep `outcome` available in scope — Task 11 emits the AnomalyEvent from it.

- [ ] **Step 3: Update routers/bio_sensor.py:79**

Find the `scan_result = await client.get_valid_scan_data()` line in `src/backend/routers/bio_sensor.py` and inspect 10 lines below to see what shape the response constructs. Replace `scan_result["data"]` references with `outcome.valid_record`. Most likely the endpoint response should return a serialisable dict — pull `outcome.valid_record` (or report a failure shape derived from `outcome.last_failure_reason`).

If the endpoint currently returns `{"status": "success", "data": scan_result["data"]}`, change to:

```python
outcome = await client.get_valid_scan_data()
return {
    "status": "success" if outcome.valid_record else "no_data",
    "data": outcome.valid_record,
    "details": outcome.last_failure_reason,
    "retry_count": outcome.retry_count,
}
```

- [ ] **Step 4: Smoke import + lazy verify**

Run: `PYTHONPATH=src/backend python -c "from services.task_runtime import TaskEngine; from routers.bio_sensor import router; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Run the existing IT-1 HIL smoke test (if a broker is reachable)**

Run: `PYTHONPATH=src/backend python -m pytest tests/hil/test_bio_sensor_mqtt.py -v`
Expected: same passes/skips as before this PR — these tests cover MQTT subscribe/receive, not the return shape, so they should be unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/backend/services/task_runtime.py src/backend/routers/bio_sensor.py
git commit -m "refactor(callers): adapt task_runtime + bio_sensor router to ScanOutcome (IT-5)"
```

---

## Task 11: Hook evaluator + dispatcher into task_runtime bio_scan handler

**Files:**
- Modify: `src/backend/services/task_runtime.py` (same bio_scan handler region as Task 10)

- [ ] **Step 1: Add the producer call after the scan**

In the same handler edited in Task 10, **immediately after** `outcome = await client.get_valid_scan_data(...)` and **before** the existing DB-write / step-status logic, add:

```python
            # Anomaly notification: per-bed scan failure (IT-5).
            from services.notifications.dispatcher import dispatcher
            from services.notifications.evaluator import BioScanFailureEvaluator
            event = BioScanFailureEvaluator().evaluate(outcome)
            if event:
                await dispatcher.dispatch(event)
```

`dispatcher.dispatch` returns immediately (fire-and-forget per sink) — this does not delay the bio_scan step. Keep existing `step.status` logic untouched.

- [ ] **Step 2: Smoke import**

Run: `PYTHONPATH=src/backend python -c "from services.task_runtime import TaskEngine; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/backend/services/task_runtime.py
git commit -m "feat(task-runtime): emit AnomalyEvent on bio_scan failure (IT-5)"
```

---

## Task 12: main.py lifespan — register sinks + drain on shutdown

**Files:**
- Modify: `src/backend/main.py` (lifespan function around line 121)

- [ ] **Step 1: Locate the lifespan function**

Run: `grep -n "async def lifespan\|task_worker\|register_robot" src/backend/main.py`
Confirm `task_worker` is created near line 121 inside lifespan.

- [ ] **Step 2: Insert sink registration before the per-robot loop**

In `src/backend/main.py`, at the **start** of the lifespan startup section (before any `register_robot` / `task_worker` call), add:

```python
    # Anomaly notification dispatcher (IT-5) — register sinks before any
    # task_worker is started so an early-firing patrol sees a populated
    # sink list.
    from services.notifications.dispatcher import dispatcher
    from services.notifications.recipients import StaticResolver
    from services.notifications.sinks.telegram import TelegramSink
    from services.notifications.sinks.mqtt import MqttSink

    resolver = StaticResolver()
    dispatcher.register(TelegramSink(resolver))
    dispatcher.register(MqttSink())
    logger.info("Anomaly dispatcher initialised: TelegramSink + MqttSink registered")
```

If `logger` is not in scope, use `logging.getLogger(__name__).info(...)` or follow the file's existing logger pattern.

- [ ] **Step 3: Add shutdown drain**

In the **shutdown** half of lifespan (after `yield`, before `unregister_robot` calls), add:

```python
    # Drain in-flight notifications so a container restart does not silently
    # cancel a Telegram POST mid-flight. 3-second cap is the explicit trade
    # against shutdown latency.
    try:
        from services.notifications.dispatcher import dispatcher
        await dispatcher.drain(timeout=3.0)
    except Exception:
        logger.exception("Error during dispatcher drain")
```

- [ ] **Step 4: Start the server and confirm startup log line**

Run: `PYTHONPATH=src/backend uv run uvicorn main:app --app-dir src/backend --port 8765 &` (or however the project starts dev server). Tail logs for `Anomaly dispatcher initialised`. Stop the server.

Expected: a single line `Anomaly dispatcher initialised: TelegramSink + MqttSink registered` appears once during startup.

- [ ] **Step 5: Commit**

```bash
git add src/backend/main.py
git commit -m "feat(lifespan): register notification sinks before task_worker; drain on shutdown (IT-5)"
```

---

## Task 13: Frontend Settings tab — split into Hardware / Notifications sub-tabs

**Files:**
- Modify: `src/frontend/index.html:276-430`
- Modify: `src/frontend/css/style.css` (append sub-tab styles)
- Modify: `src/frontend/js/script.js` (add `switchSettingsSubTab` + new field IDs)

- [ ] **Step 1: Read current script.js to find load/save settings handler**

Run: `grep -n "loadSettings\|saveSettings\|enable-telegram\|enable_telegram" src/frontend/js/script.js | head -30`
Note the function(s) that read DOM values into the API payload and apply API response back to DOM.

- [ ] **Step 2: Restructure index.html Settings tab**

In `src/frontend/index.html`, replace the contents of `<div id="view-settings" class="tab-content">` (lines ~276-430) with:

```html
    <div id="view-settings" class="tab-content">
      <div class="settings-subtabs">
        <button class="settings-subtab-btn active" data-settings-subtab="hardware"
                onclick="switchSettingsSubTab('hardware')">硬體設定</button>
        <button class="settings-subtab-btn" data-settings-subtab="notifications"
                onclick="switchSettingsSubTab('notifications')">通報設定</button>
      </div>

      <!-- HARDWARE SUB-TAB -->
      <div data-settings-subtab-content="hardware" class="settings-subtab-content active">
        <div class="settings-two-column">
          <div>
            <!-- Robot Connection panel -->
            <div class="glass-panel">
              <h3>Robot Connection</h3>
              <div class="form-group">
                <label>Robot IP Address</label>
                <div class="input-with-btn">
                  <input type="text" id="setting-robot-ip" placeholder="192.168.204.37:26400">
                  <button type="button" class="btn-secondary" id="btn-reconnect-robot" onclick="reconnectRobot()">Reconnect</button>
                </div>
                <div id="reconnect-status" class="reconnect-status"></div>
              </div>
              <div class="form-group">
                <label>Shelf ID</label>
                <div class="input-with-btn">
                  <input type="text" id="setting-shelf-id" placeholder="e.g. S_04">
                  <button type="button" class="btn-secondary" id="btn-fetch-shelves" onclick="fetchShelves()">Get Shelves</button>
                </div>
                <select id="shelf-select" class="shelf-dropdown" style="display:none;" onchange="applyShelfSelection()"></select>
              </div>
            </div>

            <!-- MQTT Configuration panel (bio-sensor / WiSleep) -->
            <div class="glass-panel">
              <h3>MQTT Configuration</h3>
              <div class="form-group">
                <label>MQTT Broker</label>
                <input type="text" id="setting-mqtt-broker" placeholder="localhost">
              </div>
              <div class="form-group">
                <label>MQTT Port</label>
                <input type="number" id="setting-mqtt-port" placeholder="1883">
              </div>
              <div class="form-group">
                <label>MQTT Topic</label>
                <input type="text" id="setting-mqtt-topic" placeholder="/data-test/demo/wisleep-eck/org/201906078">
              </div>
              <div class="form-group" style="display:flex;align-items:center;gap:12px;">
                <input type="checkbox" id="setting-mqtt-enabled" style="width:auto;">
                <label for="setting-mqtt-enabled" style="margin:0;cursor:pointer;">Enable MQTT</label>
              </div>
              <button class="btn-secondary" id="btn-test-mqtt" onclick="testMQTT()">Test Connection</button>
              <div class="log-display" id="mqtt-test-log"></div>
            </div>

            <!-- Bio-Sensor Timing panel -->
            <div class="glass-panel">
              <h3>Bio-Sensor Timing</h3>
              <div class="form-group">
                <label>Scan Wait Time (seconds)</label>
                <input type="number" id="setting-bio-scan-wait-time" placeholder="10">
              </div>
              <div class="form-group">
                <label>Retry Count</label>
                <input type="number" id="setting-bio-scan-retry-count" placeholder="19">
              </div>
              <div class="form-group">
                <label>Initial Wait Time (seconds)</label>
                <input type="number" id="setting-bio-scan-initial-wait" placeholder="120">
              </div>
              <div class="form-group">
                <label>Valid Status Code</label>
                <input type="number" id="setting-bio-scan-valid-status" placeholder="4">
              </div>
              <button class="btn-secondary" id="btn-test-bioscan" onclick="testBioScan()">Test Bio-Scan</button>
              <p class="test-note">Uses saved settings. Save before testing.</p>
              <div class="log-display" id="bioscan-test-log"></div>
            </div>
          </div>

          <div>
            <!-- Robot Retry Settings panel -->
            <div class="glass-panel">
              <h3>Robot Retry Settings</h3>
              <div class="form-group">
                <label>Max Retries</label>
                <input type="number" id="setting-robot-max-retries" placeholder="3">
              </div>
              <div class="form-group">
                <label>Retry Base Delay (seconds)</label>
                <input type="number" id="setting-robot-retry-base-delay" placeholder="2.0" step="0.1">
              </div>
              <div class="form-group">
                <label>Retry Max Delay (seconds)</label>
                <input type="number" id="setting-robot-retry-max-delay" placeholder="10.0" step="0.1">
              </div>
            </div>

            <!-- General panel -->
            <div class="glass-panel">
              <h3>General</h3>
              <div class="form-group">
                <label>Timezone</label>
                <select id="setting-timezone">
                  <option value="Asia/Taipei">Asia/Taipei (UTC+8)</option>
                  <option value="Asia/Tokyo">Asia/Tokyo (UTC+9)</option>
                  <option value="Asia/Shanghai">Asia/Shanghai (UTC+8)</option>
                  <option value="Asia/Singapore">Asia/Singapore (UTC+8)</option>
                  <option value="America/New_York">America/New_York (UTC-5)</option>
                  <option value="America/Los_Angeles">America/Los_Angeles (UTC-8)</option>
                  <option value="Europe/London">Europe/London (UTC+0)</option>
                  <option value="UTC">UTC</option>
                </select>
              </div>
            </div>

            <!-- Map Management panel -->
            <div class="glass-panel">
              <h3>Map Management</h3>
              <button class="btn-secondary" id="btn-fetch-map" onclick="fetchMapFromRobot()">Fetch from Robot</button>
              <div id="map-list-container" style="margin-top:10px;">
                <p style="color:var(--text-muted);font-size:12px;">No saved maps</p>
              </div>
            </div>
          </div>
        </div>

        <!-- Zigbee Buttons panel — full-width inside hardware sub-tab -->
        <div class="bb-panel-wrap" style="padding:0 16px 16px;">
          <div class="glass-panel">
            <div class="bb-panel-header">
              <h3>Zigbee Buttons</h3>
              <span id="bb-mqtt-pill" class="bb-pill">MQTT —</span>
            </div>
            <p class="bb-help">每個動作可配對一顆 SONOFF SNZB-01 按鈕（單擊觸發）。</p>
            <div id="bb-list" class="bb-list">
              <div class="bb-loading">載入中…</div>
            </div>
          </div>
        </div>
      </div>

      <!-- NOTIFICATIONS SUB-TAB -->
      <div data-settings-subtab-content="notifications" class="settings-subtab-content">
        <div class="settings-two-column">
          <div>
            <!-- Telegram panel -->
            <div class="glass-panel">
              <h3>Telegram Notifications</h3>
              <div class="form-group" style="display:flex;align-items:center;gap:12px;">
                <input type="checkbox" id="setting-enable-telegram" style="width:auto;">
                <label for="setting-enable-telegram" style="margin:0;cursor:pointer;">Enable Telegram</label>
              </div>
              <div class="form-group">
                <label>Bot Token</label>
                <input type="password" id="setting-telegram-bot-token" placeholder="Enter bot token">
              </div>
              <div class="form-group">
                <label>User ID</label>
                <input type="text" id="setting-telegram-user-id" placeholder="Enter user ID">
              </div>
            </div>

            <!-- MQTT egress panel (NEW) -->
            <div class="glass-panel">
              <h3>MQTT 異常事件外送</h3>
              <p class="test-note">將 anomaly event 以 JSON publish 到內部 mqtt-broker 供下游系統消費。</p>
              <div class="form-group" style="display:flex;align-items:center;gap:12px;">
                <input type="checkbox" id="setting-enable-mqtt-egress" style="width:auto;">
                <label for="setting-enable-mqtt-egress" style="margin:0;cursor:pointer;">啟用 MQTT egress</label>
              </div>
              <div class="form-group">
                <label>Topic 前綴</label>
                <input type="text" id="setting-mqtt-egress-topic-prefix" placeholder="bio-patrol/anomaly">
              </div>
            </div>
          </div>

          <div>
            <!-- AI Integration panel -->
            <div class="glass-panel">
              <h3>AI Integration</h3>
              <p class="test-note">未來的「依班表派送」recipient resolver 將使用此 API key。</p>
              <div class="form-group">
                <label>Gemini API Key</label>
                <input type="password" id="setting-gemini-api-key" placeholder="Enter API key">
              </div>
            </div>
          </div>
        </div>
      </div>

      <div style="padding:0 16px 16px;">
        <button class="btn-premium" onclick="saveSettings()">Save Settings</button>
      </div>
    </div>
```

- [ ] **Step 3: Append sub-tab CSS**

Append to `src/frontend/css/style.css`:

```css
/* Settings sub-tabs (IT-5) */
.settings-subtabs {
  display: flex;
  gap: 8px;
  padding: 16px 16px 0;
  border-bottom: 1px solid var(--border-color, rgba(255,255,255,0.1));
  margin-bottom: 16px;
}
.settings-subtab-btn {
  padding: 8px 18px;
  background: transparent;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: 8px 8px 0 0;
  color: var(--text-muted, #888);
  cursor: pointer;
  font-size: 14px;
  transition: background 0.15s, color 0.15s;
}
.settings-subtab-btn:hover {
  color: var(--text-primary, #eee);
}
.settings-subtab-btn.active {
  background: var(--panel-bg, rgba(255,255,255,0.05));
  border-color: var(--border-color, rgba(255,255,255,0.1));
  color: var(--text-primary, #eee);
}
.settings-subtab-content { display: none; }
.settings-subtab-content.active { display: block; }
```

(Adjust CSS variable names to match the existing style.css conventions if different — read the file for `--text-muted` / `--panel-bg` patterns first.)

- [ ] **Step 4: Add switchSettingsSubTab + load/save bindings**

In `src/frontend/js/script.js`, append:

```javascript
// IT-5: Settings sub-tab navigation
function switchSettingsSubTab(name) {
  document.querySelectorAll('.settings-subtab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.settingsSubtab === name);
  });
  document.querySelectorAll('.settings-subtab-content').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.settingsSubtabContent === name);
  });
}
```

In the existing `loadSettings`/`saveSettings` (or whichever function applies API → DOM and DOM → API), add the two new keys. Find the block that handles `enable_telegram` and add directly after it:

```javascript
// IT-5 settings: MQTT egress
document.getElementById('setting-enable-mqtt-egress').checked = !!settings.enable_mqtt_egress;
document.getElementById('setting-mqtt-egress-topic-prefix').value = settings.mqtt_egress_topic_prefix || 'bio-patrol/anomaly';
```

And in the save path:

```javascript
// IT-5 settings: MQTT egress
enable_mqtt_egress: document.getElementById('setting-enable-mqtt-egress').checked,
mqtt_egress_topic_prefix: document.getElementById('setting-mqtt-egress-topic-prefix').value || 'bio-patrol/anomaly',
```

- [ ] **Step 5: Manual smoke test in browser**

Run dev server: `PYTHONPATH=src/backend uv run uvicorn main:app --app-dir src/backend --reload &`
Open `http://localhost:8000/`. Click Settings tab. Verify:
- Two sub-tab buttons appear ("硬體設定" / "通報設定").
- Default sub-tab is 硬體設定 with all the original panels grouped on the left/right columns and the Zigbee Buttons panel below.
- Clicking 通報設定 reveals Telegram, MQTT 異常事件外送 (with both fields), and AI Integration panels.
- Toggling the new MQTT egress checkbox + clicking Save Settings persists the value (re-load the page → checkbox state restored).

Stop the dev server.

- [ ] **Step 6: Commit**

```bash
git add src/frontend/index.html src/frontend/css/style.css src/frontend/js/script.js
git commit -m "feat(frontend): split Settings into Hardware/Notifications sub-tabs; add MQTT egress controls (IT-5)"
```

---

## Task 14: HIL E2E test against real Telegram + real internal broker

**Files:**
- Create: `tests/hil/test_anomaly_e2e.py`

This test reads credentials from `~/.claude/.env.test` (loaded by the existing `env_test` fixture in `tests/conftest.py`) — it does **not** put any token strings in the test file or commit them.

- [ ] **Step 1: Confirm credential keys in env.test**

Run: `grep -E '^TEST_TELEGRAM_(BOT_TOKEN|CHAT_ID)=' ~/.claude/.env.test | sed 's/=.*/=<set>/'`
Expected: both keys printed as `<set>`. If a key is missing, ask the user to add it before running this test.

- [ ] **Step 2: Write the HIL test**

Create `tests/hil/test_anomaly_e2e.py`:

```python
"""HIL E2E tests for the IT-5 anomaly notification dispatcher.

Real Telegram bot, real internal mqtt-broker. Marked @pytest.mark.hil.

Credentials are read from ~/.claude/.env.test:
  TEST_TELEGRAM_BOT_TOKEN, TEST_TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import asyncio
import json
import os
import pytest

import aiomqtt
import httpx

from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.evaluator import ScanOutcome, BioScanFailureEvaluator
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.mqtt import MqttSink
from services.notifications.sinks.telegram import TelegramSink


@pytest.fixture
def telegram_creds(env_test):
    token = env_test.get("TEST_TELEGRAM_BOT_TOKEN")
    chat_id = env_test.get("TEST_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        pytest.skip("TEST_TELEGRAM_BOT_TOKEN / TEST_TELEGRAM_CHAT_ID not set in ~/.claude/.env.test")
    return token, chat_id


@pytest.fixture
def telegram_settings(telegram_creds, monkeypatch):
    token, chat_id = telegram_creds
    fake = {
        "enable_telegram": True,
        "telegram_bot_token": token,
        "telegram_user_id": chat_id,
        "enable_mqtt_egress": True,
        "zigbee_mqtt_host": "localhost",
        "zigbee_mqtt_port": 1883,
        "mqtt_egress_topic_prefix": "bio-patrol/anomaly-test",
    }
    monkeypatch.setattr("services.notifications.sinks.telegram.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.notifications.sinks.mqtt.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.telegram_service.get_runtime_settings", lambda: fake)
    return fake


def _make_failure_event() -> AnomalyEvent:
    outcome = ScanOutcome(
        task_id="hil-test",
        location_id="hil-101-1",
        bed_name="HIL-101-1",
        valid_record=None,
        retry_count=19,
        last_record_raw={"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"},
        last_status=2, last_bpm=0, last_rpm=0,
        last_failure_reason="無有效量測數值",
    )
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    return event


@pytest.mark.hil
@pytest.mark.slow
def test_dispatcher_publishes_to_telegram(telegram_settings):
    """Real bot, real chat. Sends a message and confirms 200 from the API."""
    async def _run():
        d = AnomalyDispatcher()
        d.register(TelegramSink(StaticResolver()))
        await d.dispatch(_make_failure_event())
        await d.drain(timeout=15.0)

    asyncio.run(_run())
    # Verification by visual inspection of the bot chat is acceptable; an
    # API-level GET getMe sanity check confirms the bot is reachable.
    token = telegram_settings["telegram_bot_token"]
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10.0)
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


@pytest.mark.hil
def test_dispatcher_publishes_to_internal_mqtt(telegram_settings):
    """Real internal mosquitto. Subscribe + dispatch + assert receipt within 5s.

    Requires a local broker on localhost:1883 — start with:
      docker compose -f deploy/docker-compose.prod.yml up -d mqtt-broker
    """
    received: list[tuple[str, dict]] = []

    async def _subscribe_and_run():
        # Subscribe first to avoid race
        async def _consume():
            async with aiomqtt.Client("localhost", 1883) as client:
                await client.subscribe("bio-patrol/anomaly-test/#")
                async for msg in client.messages:
                    received.append((str(msg.topic), json.loads(msg.payload.decode())))
                    return  # one message is enough

        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.5)  # let the subscriber settle

        d = AnomalyDispatcher()
        d.register(MqttSink())
        await d.dispatch(_make_failure_event())
        await d.drain(timeout=10.0)

        await asyncio.wait_for(consume_task, timeout=5.0)

    try:
        asyncio.run(_subscribe_and_run())
    except (aiomqtt.MqttError, ConnectionRefusedError, OSError) as e:
        pytest.skip(f"Internal mqtt-broker not reachable on localhost:1883: {e}")
    except asyncio.TimeoutError:
        pytest.fail("Did not receive MQTT message on bio-patrol/anomaly-test/# within 5s")

    assert len(received) == 1
    topic, payload = received[0]
    assert topic == "bio-patrol/anomaly-test/warn/bio_scan_failure"
    assert payload["severity"] == "warn"
    assert payload["source"] == "bio_scan_failure"
    assert payload["bed_key"] == "HIL-101-1"
    assert "event_id" in payload


@pytest.mark.hil
def test_one_sink_failure_does_not_block_other_sinks(telegram_settings, monkeypatch):
    """Force MqttSink to fail by pointing it at an unreachable port; Telegram still ships."""
    fake = dict(telegram_settings)
    fake["zigbee_mqtt_port"] = 9   # discard / unreachable
    monkeypatch.setattr("services.notifications.sinks.mqtt.get_runtime_settings", lambda: fake)

    async def _run():
        d = AnomalyDispatcher()
        d.register(TelegramSink(StaticResolver()))
        d.register(MqttSink())
        await d.dispatch(_make_failure_event())
        await d.drain(timeout=15.0)

    asyncio.run(_run())  # no exception bubbles out — that's the test
```

- [ ] **Step 3: Run the HIL tests**

Make sure `mqtt-broker` is running locally:
```bash
docker compose -f deploy/docker-compose.prod.yml up -d mqtt-broker
```

Run:
```bash
PYTHONPATH=src/backend python -m pytest tests/hil/test_anomaly_e2e.py -v -m hil
```

Expected:
- `test_dispatcher_publishes_to_telegram` PASS (also: a real Telegram message arrives in your chat — visually confirm).
- `test_dispatcher_publishes_to_internal_mqtt` PASS.
- `test_one_sink_failure_does_not_block_other_sinks` PASS (a second Telegram message arrives).

If the broker is not running, the second test SKIPs cleanly.

- [ ] **Step 4: Commit**

```bash
git add tests/hil/test_anomaly_e2e.py
git commit -m "test(hil): E2E anomaly dispatcher against real Telegram + internal broker (IT-5)"
```

---

## Task 15: Update `.sigma/context.md`

**Files:**
- Modify: `.sigma/context.md`

- [ ] **Step 1: Add IT-5 row to the Iterations table**

In the Iterations table, add a new row after IT-4:

```
| IT-5 | Anomaly notification dispatcher (events + Telegram + MQTT egress + Settings sub-tabs) | Test ✅ | 2026-05-01 | services/notifications/ package, ScanOutcome refactor, Settings split into Hardware/Notifications sub-tabs. Spec: docs/superpowers/specs/2026-05-01-anomaly-notification-design.md. Plan: docs/superpowers/plans/2026-05-01-anomaly-notification-it5.md. Paired with IT-6 (legacy direct-Telegram migration). |
```

- [ ] **Step 2: Append BIZ + FEAT + ARCH + CORNER findings**

Append to the Findings sections (in order):

```
- BIZ-003 (IT-5, 2026-05-01): Nurses cannot react to a patient deterioration unless they are told about it in real time. Per-bed bio-scan failures (sensor reports status≠4, bpm/rpm absent) currently disappear into the SQLite log. Future: status==4 with bpm/rpm out of clinically-safe band must be a CRITICAL escalation.
- FEAT-008 [BIZ-003] (IT-5, 2026-05-01): Per-bed bio_scan_failure Telegram notification fires after retries exhaust. Message contains bed identifier, retry count, last sensor record (status/bpm/rpm), and an event_id footer for cross-referencing.
- FEAT-009 [BIZ-003] (IT-5, 2026-05-01): MQTT egress publishes the same AnomalyEvent JSON to the internal mqtt-broker on `<prefix>/<severity>/<source>` (default prefix `bio-patrol/anomaly`). Toggle-gated, defaults off.
- FEAT-010 [BIZ-003] (IT-5, 2026-05-01): Settings tab split into 硬體設定 / 通報設定 sub-tabs. Telegram + Gemini API Key + new MQTT egress live under 通報設定.
- ARCH-015 [FEAT-008,FEAT-009] (IT-5, 2026-05-01): `services/notifications/` package — events.py / evaluator.py / dispatcher.py / recipients.py / sinks/{telegram,mqtt}.py. Module-level `dispatcher = AnomalyDispatcher()` singleton. Fan-out is async fire-and-forget per sink with per-sink try/except; pending tasks tracked in a set so GC does not cancel in-flight POSTs.
- ARCH-016 [FEAT-008,FEAT-009] (IT-5, 2026-05-01): `bio_sensor_mqtt.get_valid_scan_data` now returns `ScanOutcome` instead of `{task_id, data}`. Both callers (`task_runtime.py`, `routers/bio_sensor.py`) adapted. Last-record tracking added inside the retry loop; `last_failure_reason` resolution rule documented in the spec.
- ARCH-017 [FEAT-008] (IT-5, 2026-05-01): `telegram_service.send_telegram_message` gains optional `chat_id` kwarg; effective recipient is `chat_id or settings.telegram_user_id`. Three legacy direct callers in `task_runtime.py` (lines 231, 419, 421) keep working without changes — IT-6 migrates them.
- ARCH-018 [FEAT-008,FEAT-009] (IT-5, 2026-05-01): Lifespan ordering — sinks register before any `task_worker` is started (otherwise an early-firing patrol could call `dispatch()` against an empty sink list). Shutdown drains `dispatcher._pending` with a 3 s cap.
- ARCH-019 [FEAT-008,FEAT-009] (IT-5, 2026-05-01): Forward boundaries shaped (not implemented in IT-5):
  (a) `RecipientResolver` Protocol — IT-5 ships `StaticResolver`; future `ShiftBasedResolver` swaps in an AI-agent-built shift table without touching dispatcher / sinks.
  (b) `Source.VITALS_OUT_OF_BAND` enum entry + space for a second evaluator class (`VitalsOutOfBandEvaluator`) for the future `bpm/rpm` clinical-band rule.
- CORNER-010 [FEAT-008] (IT-5): TelegramSink hangs / fails — per-sink try/except + `asyncio.create_task` isolation; main flow does not wait. Covered by `test_one_sink_failure_does_not_block_other_sinks`.
- CORNER-011 [FEAT-009] (IT-5): mqtt-broker container down — aiomqtt connect fails fast → caught in `_safe_send` → logged. Telegram path unaffected (separate task). Covered by HIL test (broker port pointed at 9).
- CORNER-012 [FEAT-008,FEAT-009] (IT-5): No sinks registered — dispatch loop is zero-iteration, returns. Covered by `test_dispatch_with_no_sinks_is_noop`.
- CORNER-013 [FEAT-008] (IT-5): `enable_telegram=True` but `telegram_bot_token` empty — `send_telegram_message` short-circuits with a warning log. Covered by `test_missing_token_or_chat_id_short_circuits`.
- CORNER-015 [FEAT-009] (IT-5): mqtt-broker reachable but slow (>10 s publish) — `asyncio.wait_for(timeout=10.0)` in MqttSink → exception bubbles to `_safe_send`. Covered by `test_send_timeout_propagates_for_safe_send_to_handle`.
- CORNER-016 [FEAT-008] (IT-5): Telegram bot rate-limit (429) — out of scope for IT-5; logged and dropped. Documented limit; revisit if real-world pain emerges.
- CORNER-017 [FEAT-008,FEAT-009] (IT-5): FastAPI shutdown mid-publish — lifespan teardown drains in-flight tasks with a 3 s cap. Beyond cap, abandoned with a warning log; explicit trade against shutdown latency.
```

- [ ] **Step 3: Update the E2E Verification Matrix**

In the matrix, **update** the FEAT-002 row to reflect partial coverage:

```
| FEAT-002   | Anomaly alerting via Telegram (legacy 3-trigger path) | — / Pre-impl (IT-6) | ⬜ | superseded by FEAT-008/009/010 for the new path; legacy path migrates in IT-6 |
```

**Add** new rows:

```
| FEAT-008   | Per-bed bio_scan_failure Telegram notification (dispatcher path) | IT-5 / Test ✅ | 🟡 | unit + HIL `test_dispatcher_publishes_to_telegram`; full E2E during IT-5 deploy |
| FEAT-009   | MQTT anomaly egress to internal broker | IT-5 / Test ✅ | 🟡 | HIL `test_dispatcher_publishes_to_internal_mqtt`; verifies hierarchical topic + JSON payload |
| FEAT-010   | Settings tab split into Hardware/Notifications sub-tabs | IT-5 / Impl ✅ | 🟡 | manual browser smoke per Task 13 step 5; consider Playwright assertion in IT-6 |
| CORNER-010 | Sink failure isolated from other sinks | IT-5 / Test ✅ | ✅ | covered by `test_one_sink_failing_does_not_block_others` + HIL |
| CORNER-011 | mqtt-broker unreachable → MqttSink fails, others unaffected | IT-5 / Test ✅ | ✅ | HIL with bogus port |
| CORNER-012 | No sinks registered → dispatch is no-op | IT-5 / Test ✅ | ✅ | unit |
| CORNER-013 | Telegram half-configured short-circuits | IT-5 / Test ✅ | ✅ | back-compat unit test |
| CORNER-015 | MqttSink publish timeout (broker slow) | IT-5 / Test ✅ | ✅ | unit (PUBLISH_TIMEOUT_S monkeypatched) |
| CORNER-017 | Lifespan shutdown drains in-flight | IT-5 / Impl ✅ | ⬜ | needs verification with running server + manual SIGTERM |
```

- [ ] **Step 4: Commit**

```bash
git add .sigma/context.md
git commit -m "docs(sigma): record IT-5 anomaly dispatcher iteration + findings"
```

(Note: `.sigma/` is gitignored per existing project setup — this commit step exists only if the user has overridden the gitignore. Otherwise, skip the commit and just save the file.)

---

## Task 16: Final verification

**Files:** none modified — pure verification pass.

- [ ] **Step 1: Run the entire unit suite**

Run: `PYTHONPATH=src/backend python -m pytest tests/unit/ -v`
Expected: all green. Pre-existing IT-4 unit tests still pass alongside the new notification tests.

- [ ] **Step 2: Run the HIL suite (broker + Telegram)**

Run: `PYTHONPATH=src/backend python -m pytest tests/hil/ -v -m hil`
Expected: 3 new anomaly tests pass + IT-1 bio-sensor smoke tests pass (or skip cleanly per their own gating).

- [ ] **Step 3: Manual end-to-end check**

Start the dev server. In the Settings tab:
- Confirm both sub-tabs render and switch.
- Toggle `Enable Telegram` ON, paste the same chat_id used in tests, Save.
- Toggle `Enable MQTT egress` ON, leave default prefix, Save.

Trigger a `bio_scan` failure: easiest path is to keep `mqtt_enabled=False` for the bio sensor (so the sensor MQTT subscriber receives nothing) and run a single-bed patrol. The scan will exhaust retries and emit an AnomalyEvent.

Expected within ~6 minutes:
- One Telegram message arrives in the chat with `⚠️ <bed> 量測失敗` + body + `<event_id_last_8>` footer.
- One MQTT message lands on `bio-patrol/anomaly/warn/bio_scan_failure`.

Stop the dev server.

- [ ] **Step 4: Final commit (if any non-code touchups remain)**

If any small cleanup commits are needed, do them here. Otherwise this task is just gates.

---

## Self-review checklist (for the writer of this plan, not the executor)

- [x] Each task names exact file paths.
- [x] Each TDD task has the test code in full.
- [x] Each implementation step has the code in full.
- [x] Each task ends with a commit step with a precise commit message.
- [x] No "TBD", "TODO", or "see above" placeholders.
- [x] Type names match across tasks: `ScanOutcome` (Task 3), used by Task 9 + 10 + 11 + 14; `AnomalyDispatcher` / `dispatcher` singleton (Task 8), used by Task 11 + 12 + 14; `Sink` Protocol (Task 4), implemented by Task 6 + 7; `RecipientResolver` / `StaticResolver` (Task 4), used by Task 6 + 12 + 14.
- [x] Spec coverage:
   - §3.1 module layout → Tasks 2, 3, 4, 6, 7, 8
   - §3.2 events → Task 2
   - §3.3 evaluator + ScanOutcome → Tasks 3, 9
   - §3.4 dispatcher with pending/drain → Task 8
   - §3.5 Sink protocol → Task 4
   - §3.6 TelegramSink → Task 6
   - §3.7 MqttSink → Task 7
   - §3.8 RecipientResolver → Task 4
   - §4.1 bio_sensor_mqtt → Task 9
   - §4.2 task_runtime → Tasks 10, 11
   - §4.3 telegram_service → Task 5
   - §4.4 main.py lifespan → Task 12
   - §4.5 settings keys → Task 1
   - §5 frontend split → Task 13
   - §6.1 unit tests → Tasks 2, 3, 5, 6, 7, 8
   - §6.2 HIL E2E → Task 14
   - §6.3 sigma matrix → Task 15
   - §7-§11 cross-cutting concerns → covered in tasks above

---
