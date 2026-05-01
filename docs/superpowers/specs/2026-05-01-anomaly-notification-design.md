# Anomaly Notification Dispatcher — IT-5 Design

**Date**: 2026-05-01
**Iteration**: IT-5 (paired with IT-6 migration)
**Authors**: snaken + Claude
**Status**: Draft (awaiting review)

---

## 1. Background

The existing notification path in `services/task_runtime.py` only fires Telegram messages on three task-level events: shelf-drop, task cancelled, task completed. Per-bed bio-scan failures (sensor `status != 4`, no respiration captured, etc.) are silently written to SQLite — nurses have no real-time signal that a bed needs attention.

The customer (BIZ) need is for nurses to see actionable anomaly events promptly, with the recipient and channel set evolving as the deployment matures (Telegram now, MQTT bus next, LINE/NIS/shift-aware routing later). This calls for a producer/consumer split rather than more direct API calls inside `task_runtime`.

## 2. Goals & non-goals

**Goals (IT-5)**
1. Add a per-bed Telegram notification when a bio-scan exhausts all retries without a valid record. Message includes bed identifier, last sensor record, retry count.
2. Stand up a notification dispatcher (`services/notifications/`) that fans an `AnomalyEvent` to one or more sinks. Sinks isolate per-channel concerns and per-channel failure.
3. Add a second sink — `MqttSink` — that publishes the same event JSON to the internal `mqtt-broker` on a hierarchical topic. Toggle-gated, defaults off.
4. Reorganise the Settings tab into two sub-tabs (Hardware / Notifications) so the new toggle has a logical home and the existing 7 panels stop competing for screen space.
5. Leave clean extension points for: (a) future evaluators (e.g., vitals-out-of-band), (b) future sinks (LINE / NIS / Webhook), (c) future shift-aware recipient resolution.

**Non-goals (IT-5)**
- Migrating the three existing direct-Telegram call sites (shelf-drop, task cancelled, task completed). → IT-6.
- Time-based dispatch logic (rate limiting, debouncing, batching).
- Severity-based per-sink filtering.
- Persistence / retry queue for failed sends.
- Implementing the shift-aware recipient lookup. The boundary is shaped, the implementation is a future iteration.
- Implementing the second evaluator rule (`status == 4` but `bpm`/`rpm` out of band). The boundary is shaped, the implementation is a future iteration.

## 3. Architecture

```
                                                ┌─────────────────────┐
   bio_scan (bio_sensor_mqtt.py) ─ScanOutcome─▶ │                     │──▶ TelegramSink ─▶ Telegram Bot API
                                                │ AnomalyDispatcher   │──▶ MqttSink ─────▶ mqtt-broker (internal)
   shelf_drop  (IT-6 hookpoint) ────────────▶   │  (fan-out, async    │──▶ LineSink ─────▶  (future IT)
   task_done   (IT-6 hookpoint) ────────────▶   │   per-sink, with    │──▶ NisSink ──────▶  (future IT)
                                                │   per-sink try/exc) │
   future: vitals_oob (Evaluator) ──────────▶   └─────────────────────┘
                                                          │
                                                  emits AnomalyEvent
                                                  - severity, source
                                                  - bed_key, task_id
                                                  - title / body / raw
                                                  - timestamp, event_id
```

### 3.1 Module layout (`services/notifications/`)

```
services/notifications/
├── __init__.py            # public API: dispatcher singleton
├── events.py              # AnomalyEvent, Severity, Source enums
├── evaluator.py           # BioScanFailureEvaluator (v1 rule)
├── dispatcher.py          # AnomalyDispatcher (fan-out + per-sink isolation)
├── recipients.py          # RecipientResolver protocol + StaticResolver
└── sinks/
    ├── __init__.py        # Sink protocol
    ├── telegram.py        # TelegramSink (wraps existing telegram_service)
    └── mqtt.py            # MqttSink (aiomqtt → internal broker)
```

Rationale: each module has one clear purpose, communicates via narrow interfaces, and is independently testable. Adding a new sink = adding a new file in `sinks/` and one `register()` call. Adding a new evaluator rule = adding a class in `evaluator.py`. Future shift-aware recipient routing = swapping `StaticResolver` for `ShiftBasedResolver` without touching dispatcher or sinks.

### 3.2 Event type

```python
# services/notifications/events.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
import uuid
from common_types import get_now

class Severity(str, Enum):
    INFO = "info"           # IT-6: 巡房完成 / 取消
    WARN = "warn"           # IT-5: bio scan failure (per bed)
    CRITICAL = "critical"   # IT-6: shelf drop;  future: vitals out-of-band

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

`raw` carries the full sensor record (or last-attempted record on failure) for downstream MQTT consumers; Telegram only renders `title` + `body`.

### 3.3 Evaluator (v1 rule + future hook)

```python
# services/notifications/evaluator.py
@dataclass
class ScanOutcome:
    """Returned by bio_sensor_mqtt.get_valid_scan_data() — replaces today's {task_id, data}."""
    task_id: str
    location_id: str
    bed_name: str | None
    valid_record: dict | None          # None when retries exhausted without a valid hit
    retry_count: int
    last_record_raw: dict | None       # last attempted record (None if no MQTT data ever)
    last_status: int | None
    last_bpm: int | None
    last_rpm: int | None
    last_failure_reason: str            # human-readable Chinese reason

class BioScanFailureEvaluator:
    """v1: emit BIO_SCAN_FAILURE / WARN when scan retries exhaust without a valid hit."""

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

Future evaluator (out of scope but the shape is fixed):

```python
class VitalsOutOfBandEvaluator:
    """Future: status==4 but bpm/rpm outside configured safe band → CRITICAL."""
    def evaluate(self, outcome: ScanOutcome) -> AnomalyEvent | None: ...
```

`task_runtime` will eventually run a chain `[BioScanFailureEvaluator, VitalsOutOfBandEvaluator]`; in IT-5 it runs only the first.

### 3.4 Dispatcher

```python
# services/notifications/dispatcher.py
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

    async def _safe_send(self, sink: Sink, event: AnomalyEvent) -> None:
        try:
            if not await sink.is_enabled():
                return
            await sink.send(event)
        except Exception:
            logger.exception("Sink %s failed", sink.__class__.__name__)

dispatcher = AnomalyDispatcher()  # module-level singleton
```

- Fire-and-forget per sink — `task_runtime` does not await the network round-trip.
- Pending-task set keeps a hard reference so the GC does not cancel an in-flight Telegram POST that the patrol loop has already moved past.
- Per-sink try/except — one channel down does not affect any other channel or the patrol loop.
- `is_enabled()` is checked at dispatch time (reads runtime settings) so a UI toggle takes effect on the next event without restart.

### 3.5 Sink protocol

```python
# services/notifications/sinks/__init__.py
class Sink(Protocol):
    async def is_enabled(self) -> bool: ...
    async def send(self, event: AnomalyEvent) -> None: ...
```

### 3.6 TelegramSink

```python
# services/notifications/sinks/telegram.py
class TelegramSink:
    def __init__(self, resolver: RecipientResolver):
        self._resolver = resolver

    async def is_enabled(self) -> bool:
        return get_runtime_settings().get("enable_telegram", False)

    async def send(self, event: AnomalyEvent) -> None:
        # IT-5: resolver returns [telegram_user_id] from settings (StaticResolver)
        # Future:  resolver returns chat_ids of nurses on shift at event.timestamp
        chat_ids = await self._resolver.resolve(event, channel="telegram")
        message = self._format(event)
        for chat_id in chat_ids:
            await send_telegram_message(message, chat_id=chat_id)

    def _format(self, event: AnomalyEvent) -> str:
        # event_id last-8 footer lets a recipient cross-reference MQTT logs
        return f"<b>{event.title}</b>\n\n{event.body}\n\n<code>{event.event_id[-8:]}</code>"
```

`telegram_service.send_telegram_message` gains an optional `chat_id` parameter that **defaults to None**; inside the function the effective recipient is `chat_id or cfg.get("telegram_user_id", "")`. The three legacy direct callers (`task_runtime.py:231, 419, 421`) keep calling without a kwarg and continue to land in the same chat — back-compat preserved by construction.

### 3.7 MqttSink

```python
# services/notifications/sinks/mqtt.py
class MqttSink:
    """Short-lived connection per publish. Volume is low (handful per patrol)."""

    async def is_enabled(self) -> bool:
        return get_runtime_settings().get("enable_mqtt_egress", False)

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
        async with aiomqtt.Client(hostname=host, port=port) as client:
            await client.publish(topic, payload, qos=1, retain=False)
```

**Topic structure** (hierarchical): `bio-patrol/anomaly/{severity}/{source}`
- Example: `bio-patrol/anomaly/warn/bio_scan_failure`
- Lets future consumers subscribe with wildcards:
  - `bio-patrol/anomaly/critical/+` — critical only
  - `bio-patrol/anomaly/+/bio_scan_failure` — bio scan only
  - `bio-patrol/anomaly/#` — everything

QoS=1, retain=false. Telegram is the durable second copy; MQTT is for live downstream systems.

**Topic contract**: prefix is configurable (`mqtt_egress_topic_prefix`); the `{severity}/{source}` suffix structure is **fixed** and the enum vocabulary is treated as a public contract — adding a value is allowed (subscribers fail open), removing/renaming is a breaking change. Subscribers should prefer `<prefix>/+/+` or `<prefix>/#` over hard-coding individual severity values.

**Per-publish timeout**: the `aiomqtt.Client` block is wrapped in `asyncio.wait_for(..., timeout=10.0)` so a hung broker cannot stall the per-sink task indefinitely. On timeout the exception bubbles to `_safe_send` and is logged, identical to any other sink failure.

**Settings race during dispatch**: `host` / `port` / `prefix` are read at the top of `send()` and used for that one publish. If the operator saves new settings while a publish is in flight, that publish completes against the old config and the next event uses the new config. Documented as benign — at most one event sees stale config.

Reusing `zigbee_mqtt_host` / `zigbee_mqtt_port` instead of adding new keys: the deployment has one internal `mqtt-broker` service; two settings would inevitably drift. Split if and when egress targets a different broker.

### 3.8 RecipientResolver (boundary only in IT-5)

```python
# services/notifications/recipients.py
class RecipientResolver(Protocol):
    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]: ...

class StaticResolver:
    """IT-5: returns the single recipient configured in settings, per channel."""
    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]:
        cfg = get_runtime_settings()
        if channel == "telegram":
            uid = cfg.get("telegram_user_id", "")
            return [uid] if uid else []
        return []  # MQTT publishes to topic, no recipient list needed
```

Future `ShiftBasedResolver` consults the AI-agent-built shift table without dispatcher/sink changes.

## 4. Integration points (where existing code is touched)

### 4.1 `services/bio_sensor_mqtt.py`

Replace the current return value of `get_valid_scan_data` from `{"task_id", "data"}` to a `ScanOutcome`. Existing callers in `task_runtime.py:498` and `routers/bio_sensor.py:79` (the Test Bio-Scan endpoint) both adapt: `outcome.valid_record` substitutes for today's `result["data"]`.

**Tracking implementation** — current code does not retain a "last record" across retries. Add three local variables initialised before the retry loop:

```python
last_record_processed: dict | None = None    # last record from the most-recent retry that produced data
final_retry_count: int = 0
```

Inside `for data in self.latest_data['records']:` (line ~152), after the `_save_scan_data` call, set `last_record_processed = data`. Update `final_retry_count = retry_count` at the bottom of each retry loop. After the loop ends (or on early return for valid hit), construct `ScanOutcome` from these locals.

**`last_failure_reason` merge rule** — exactly one of:
1. `last_record_processed["details"]` if any retry produced data and no valid record was found (typically `"無有效量測數值"`).
2. The standing string `"未收到感測器資料（MQTT無連線或無數據）"` if `has_any_data` was False at the end of all retries.
3. `None` when `valid_record is not None` (no failure to report).

This makes the evaluator's `body` deterministic for any given scan timeline.

### 4.2 `services/task_runtime.py`

In the bio_scan step handler, after the scan call:

```python
outcome = await self.bio_sensor.get_valid_scan_data(...)
event = bio_scan_evaluator.evaluate(outcome)
if event:
    await dispatcher.dispatch(event)
# existing DB-write / step.status logic uses outcome.valid_record
```

The three direct `send_telegram_message` calls (shelf-drop, task cancelled, task completed) are **left alone in IT-5**. IT-6 migrates them.

### 4.3 `services/telegram_service.py`

Signature becomes:

```python
async def send_telegram_message(message: str, chat_id: str | None = None):
    cfg = get_runtime_settings()
    if not cfg.get("enable_telegram", False):
        return
    token = cfg.get("telegram_bot_token", "")
    effective_chat_id = chat_id or cfg.get("telegram_user_id", "")
    if not token or not effective_chat_id:
        logger.warning("Telegram enabled but bot_token or chat_id not set")
        return
    # ...rest unchanged, using effective_chat_id in payload...
```

The three legacy callers in `task_runtime.py:231, 419, 421` continue to call `send_telegram_message(message)` and resolve to the configured `telegram_user_id` — back-compat preserved by construction.

### 4.4 `main.py` lifespan

**Startup ordering** — sinks must register **before** any `task_worker` task is created at line 121, otherwise an early-firing patrol could call `dispatcher.dispatch()` against an empty sink list.

```python
# Insert near the top of lifespan(), before the per-robot register/task_worker block:
from services.notifications.dispatcher import dispatcher
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.telegram import TelegramSink
from services.notifications.sinks.mqtt import MqttSink

resolver = StaticResolver()
dispatcher.register(TelegramSink(resolver))
dispatcher.register(MqttSink())
```

**Shutdown drain** — on lifespan teardown, drain in-flight notifications with a 3-second cap so a container restart does not silently kill a Telegram POST mid-flight:

```python
# In the shutdown half of lifespan, before unregister_robot calls:
if dispatcher._pending:
    try:
        await asyncio.wait_for(
            asyncio.gather(*dispatcher._pending, return_exceptions=True),
            timeout=3.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Notification dispatcher drain timed out; %d tasks abandoned", len(dispatcher._pending))
```

A `drain()` helper method on `AnomalyDispatcher` is acceptable to avoid touching the private `_pending` attribute from main.py.

### 4.5 `settings/defaults.py`

Add:
```python
"enable_mqtt_egress": False,
"mqtt_egress_topic_prefix": "bio-patrol/anomaly",
```

## 5. Frontend changes

### 5.1 Settings tab → two sub-tabs

Inside `<div id="view-settings">`, replace the flat layout with a sub-tab navigation. Same DOM panels, regrouped.

```
Settings  ┌─────────────┬───────────────┐
          │  硬體設定    │  通報設定      │
          ├─────────────┴───────────────┤
          │                              │
          │  [active sub-tab content]    │
          │                              │
          └──────────────────────────────┘
```

**硬體設定 (Hardware)** sub-tab — existing panels:
- Robot Connection (Robot IP, Shelf ID, Reconnect)
- MQTT Configuration (bio-sensor / WiSleep MQTT)
- Bio-Sensor Timing (scan timing + Test Bio-Scan)
- Robot Retry Settings
- General (Timezone)
- Map Management
- Zigbee Buttons (existing IT-4 panel)

**通報設定 (Notifications)** sub-tab — moved + new:
- Telegram Notifications (moved from current right column)
- MQTT 異常事件外送 (NEW)
  - Checkbox: 啟用 MQTT egress
  - Input: Topic 前綴 (`bio-patrol/anomaly`)
- AI Integration / Gemini API Key (moved per user request — anticipates future shift-aware recipient resolver consumption)

**UX risk noted, accepted for IT-5**: Gemini API Key has no consumer in the codebase today. Placing it under "通報設定" pre-commits the panel layout to the resolver-uses-Gemini design before the resolver lands. If the future resolver lands on a different backend, the panel may want to move to a new "進階" sub-tab. Revisit at the iteration that ships the shift-aware resolver.

The Save Settings button stays at the bottom of `view-settings`, applying to whichever sub-tab is active.

### 5.2 Implementation

- HTML: wrap each panel in a `data-settings-subtab="hardware|notifications"` container; sub-tab nav buttons toggle a CSS class `active-subtab` on the matching container.
- JS: small `switchSettingsSubTab(name)` in `js/script.js` mirroring the existing `switchTab` pattern.
- CSS: extend existing `.tab-btn` styling for sub-tab buttons, slightly smaller; reuse `.glass-panel` styles unchanged.

No new build step (the project is a vanilla-JS SPA).

## 6. Testing

### 6.1 Unit tests (`tests/unit/test_notifications/`)

| File | Covers |
|------|--------|
| `test_evaluator.py` | v1 rule: valid_record present → returns None. valid_record None → returns AnomalyEvent with correct severity / source / bed_key / body fields including last_status, last_bpm, last_rpm. |
| `test_dispatcher.py` | One sink raising does not block others (mock two sinks, one raises, second still receives event). `is_enabled()=False` → `send()` not called. No sinks registered → safe no-op. |
| `test_telegram_sink.py` | `_format` produces `<b>{title}</b>\n\n{body}\n\n<code>{event_id_last8}</code>` — exact HTML wrapping verified. `is_enabled` returns the value of `enable_telegram` from settings. Resolver returning `[]` → no API call. Resolver returning two chat_ids → two `send_telegram_message` invocations with each chat_id. |
| `test_telegram_service_back_compat.py` | `send_telegram_message(message)` — no kwarg — uses `telegram_user_id` from settings as the recipient. Guards the three legacy callers. |
| `test_mqtt_sink.py` | Topic assembly: `bio-patrol/anomaly/warn/bio_scan_failure` for the v1 path. Payload is valid JSON, parses back to a dict containing `event_id`, `severity`, `source`, `raw`. Configurable prefix respected. |

### 6.2 HIL test (`tests/hil/test_anomaly_e2e.py`)

Real Telegram + real internal mosquitto. Marked `@pytest.mark.hil`.

- **Telegram** path: build a `ScanOutcome` with `valid_record=None`, call `dispatcher.dispatch(evaluator.evaluate(outcome))`, assert via Telegram `getUpdates` API (or a poll on the bot's recent messages) that the formatted message landed within 30 s.
- **MQTT** path: subscribe to `bio-patrol/anomaly/#` on the internal broker, dispatch the same event, assert message arrives within 5 s with the expected topic and a JSON-parseable payload.

Credentials sourced from `tests/hil/.env.test` (gitignored). Spec keeps no token strings inline.

### 6.3 Existing E2E matrix updates

`.sigma/context.md` rows changed:

| ID | Description | After IT-5 |
|----|-------------|-------------|
| FEAT-002 | Anomaly alerting via Telegram | 🟡 partial — `bio_scan_failure` source covered; shelf-drop/summary still legacy direct path until IT-6 |
| (new) FEAT-008 | Anomaly notification dispatcher (Telegram + MQTT, bio_scan_failure source) | 🟡 covered by HIL test once it runs |
| (new) CORNER-010 | Sink failure does not block dispatch loop or `task_runtime` | covered by `test_dispatcher.py` |
| (new) CORNER-011 | mqtt-broker unavailable → MqttSink fails, TelegramSink unaffected | covered by HIL with broker temporarily stopped |
| (new) CORNER-012 | Dispatcher with zero sinks registered | covered by `test_dispatcher.py` |
| (new) CORNER-013 | `enable_telegram=True` but `telegram_bot_token=""` | covered by `test_telegram_sink.py` (existing telegram_service guards this) |
| (new) CORNER-014 | Same bed double-fired (out of scope; documented assumption: no dedup, downstream responsible) | spec note only |

## 7. CORNER cases (failure modes addressed by design)

| ID | Scenario | Handling |
|----|----------|----------|
| CORNER-010 | TelegramSink hangs 60 s | Dispatcher uses `asyncio.create_task` per sink; main flow does not wait. Per-sink try/except logs the error. |
| CORNER-011 | mqtt-broker container down | aiomqtt `connect` raises immediately → caught in `_safe_send` → logged. Telegram path unaffected because each sink runs in its own task. |
| CORNER-012 | No sinks registered | Loop is zero-iteration; method returns. |
| CORNER-013 | Telegram credentials half-configured | `send_telegram_message` already short-circuits and warns. No new code needed. |
| CORNER-014 | Same bed scan fires twice in one task (e.g., manual retry) | IT-5 does not de-duplicate. Each event has a unique `event_id`; downstream MQTT consumers can de-dup if they care. Telegram users get two messages by design (visible signal). |
| CORNER-015 | mqtt-broker reachable but slow (>10 s publish) | `MqttSink.send` wraps the `aiomqtt.Client` block in `asyncio.wait_for(timeout=10.0)`; on timeout `_safe_send` logs and the next event is unaffected. |
| CORNER-016 | Telegram bot rate-limit (HTTP 429) | Out of scope for IT-5. `send_telegram_message` logs the non-200 and returns; no retry, no backoff. Realistic if 14 beds × failure fires inside one patrol — accepted as a known limit; revisit if it becomes a real-world pain point. |
| CORNER-017 | FastAPI shutdown mid-publish | Lifespan teardown drains `dispatcher._pending` with a 3 s cap (§4.4). Beyond the cap, in-flight publishes are abandoned and counted in a warning log; explicitly traded for shutdown latency. |

## 8. Things not done (YAGNI, by explicit decision)

- Rate limiting / debouncing — not needed at IT-5 volume; user explicitly asked for fan-out only.
- Severity-based per-sink filtering — every enabled sink receives every event.
- Persistence / retry queue — fire-and-forget; failures are logged.
- Migrating the three legacy direct-Telegram call sites — IT-6 owns this strictly. **Rejected option**: parallel-dispatch (legacy direct call + dispatcher in IT-5) was floated to test multi-source paths earlier; declined to keep IT-5 PR focused on a single producer path.
- Implementing `VitalsOutOfBandEvaluator` — future iteration; the boundary is shaped.
- Implementing `ShiftBasedResolver` — future iteration backed by an AI agent that reads facility-specific shift tables; the boundary is shaped.
- Telegram 429 retry/backoff — see CORNER-016, accepted limit.

## 9. Build sequence

1. Add `services/notifications/` package with `events.py`, `evaluator.py`, `dispatcher.py` (incl. `drain()` helper), `recipients.py`, `sinks/`. Unit-tested in isolation.
2. Update `services/telegram_service.py` signature per §4.3 (`chat_id` kwarg with settings fallback). Add legacy back-compat unit test.
3. Extend `bio_sensor_mqtt.py:get_valid_scan_data` to return `ScanOutcome` per §4.1 tracking implementation. Adapt **both** callers: `task_runtime.py:498` and `routers/bio_sensor.py:79`.
4. Wire `TelegramSink` + `MqttSink` registration in `main.py` lifespan **before** the `task_worker` block; add shutdown drain on the teardown half. Add settings keys to `defaults.py`.
5. Hook `evaluator.evaluate` + `dispatcher.dispatch` into `task_runtime.py` bio_scan handler.
6. Frontend: split Settings tab into two sub-tabs; move panels per §5.1; add MQTT-egress fields.
7. HIL test against real Telegram (credentials in `tests/hil/.env.test`) and real internal broker.
8. Update `.sigma/context.md` Iterations + Findings + E2E matrix.

## 10. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Telegram API rate-limit / abuse during HIL repeats | HIL test runs once per CI invocation; the bot is dedicated to this project. Tester can rotate token after spec review (see security note below). |
| Internal `mqtt-broker` not running in dev → MqttSink errors | Each sink fully isolated; TelegramSink unaffected. Logs stay actionable. |
| `ScanOutcome` change breaks tests in `tests/hil/test_bio_sensor_mqtt.py` | The IT-1 tests cover MQTT subscribe/receive only, not the return shape. Risk low; will be re-run as part of IT-5 verification. The Test Bio-Scan endpoint at `routers/bio_sensor.py:79` is also adapted in §4.1 — manual click-test required. |
| Concurrent dispatch correctness | `AnomalyDispatcher` is a module-level singleton, `_pending` is unprotected. asyncio is single-threaded, so concurrent calls from multiple async tasks interleave at await points only — `set.add` / `add_done_callback` are safe. Documented assumption; revisit only if FastAPI moves to multi-process workers (currently `--workers 1`). |
| Frontend sub-tab reorg confuses operators familiar with current single-page settings | The default sub-tab is "硬體設定" (the existing layout); "通報設定" is additive. No persisted state changes. |

## 11. Security note (credentials handling)

- Telegram bot token and chat_id are runtime credentials. They go in `data/config/settings.json` (already gitignored) for production, and in `tests/hil/.env.test` (must be added to `.gitignore`) for HIL tests.
- This spec contains no credential strings.
- After IT-5 ships, the bot token in use during development should be rotated (BotFather `/revoke`) since it appeared in the design conversation transcript.
