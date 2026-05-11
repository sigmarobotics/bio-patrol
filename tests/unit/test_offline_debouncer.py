import asyncio
import pytest

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.offline_debouncer import OfflineDebouncer


@pytest.mark.asyncio
async def test_blip_under_debounce_emits_nothing():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.2,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.05)
    deb.note_connected()
    await asyncio.sleep(0.3)
    assert emitted == []
    await deb.shutdown()


@pytest.mark.asyncio
async def test_long_idle_disconnect_emits_info_then_recovered():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.1,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.25)
    assert len(emitted) == 1
    assert emitted[0].source == Source.ROBOT_OFFLINE
    assert emitted[0].severity == Severity.INFO

    deb.note_connected()
    await asyncio.sleep(0.05)
    assert len(emitted) == 2
    assert emitted[1].source == Source.ROBOT_RECOVERED
    assert emitted[1].severity == Severity.INFO
    await deb.shutdown()


@pytest.mark.asyncio
async def test_long_disconnect_during_patrol_emits_critical():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.1,
        is_patrol_running=lambda: True,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.25)
    assert len(emitted) == 1
    assert emitted[0].severity == Severity.CRITICAL
    assert emitted[0].source == Source.ROBOT_OFFLINE
    await deb.shutdown()


@pytest.mark.asyncio
async def test_recovery_without_prior_emit_is_silent():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 60.0,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_connected()  # initial state — should be no-op
    await asyncio.sleep(0.05)
    assert emitted == []
    await deb.shutdown()


@pytest.mark.asyncio
async def test_disconnect_then_disconnect_is_idempotent():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.1,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    deb.note_disconnected()
    await asyncio.sleep(0.25)
    assert len(emitted) == 1
    await deb.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_timer():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 60.0,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await deb.shutdown()
    await asyncio.sleep(0.1)
    assert emitted == []


@pytest.mark.asyncio
async def test_emit_records_total_offline_seconds_on_recovery():
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.1,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.25)
    deb.note_connected()
    await asyncio.sleep(0.05)
    recovered = emitted[1]
    assert "total_offline_sec" in recovered.raw
    assert recovered.raw["total_offline_sec"] >= 0.2
    await deb.shutdown()


@pytest.mark.asyncio
async def test_setting_change_takes_effect_on_next_event():
    """Reading debounce via provider lets settings updates propagate."""
    debounce = [0.05]
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: debounce[0],
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    debounce[0] = 60.0
    deb.note_disconnected()
    await asyncio.sleep(0.2)
    assert emitted == []  # used new (long) value
    await deb.shutdown()


@pytest.mark.asyncio
async def test_recovery_fires_when_disconnect_emitted_before_dispatch_returned():
    """CORNER-032: reconnect arrives while OFFLINE is awaiting dispatch.

    The flag must be set BEFORE the await, so note_connected sees it as True
    even though _emit_after_debounce has not yet returned.
    """
    emitted: list[AnomalyEvent] = []

    async def slow_emit(evt: AnomalyEvent) -> None:
        await asyncio.sleep(0.2)
        emitted.append(evt)

    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.05,
        is_patrol_running=lambda: False,
        emit=slow_emit,
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.1)            # emit_after_debounce now in slow_emit
    deb.note_connected()                # racing reconnect
    await asyncio.sleep(0.5)
    assert any(e.source == Source.ROBOT_OFFLINE for e in emitted)
    assert any(e.source == Source.ROBOT_RECOVERED for e in emitted)
    await deb.shutdown()


@pytest.mark.asyncio
async def test_disconnect_after_recovery_starts_fresh_debounce():
    """CORNER-033: outage A → emit → reconnect → outage B re-arms cleanly."""
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.1,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    deb.note_disconnected()
    await asyncio.sleep(0.25)
    assert len(emitted) == 1            # OFFLINE A
    deb.note_connected()
    await asyncio.sleep(0.05)
    assert len(emitted) == 2            # RECOVERED A
    deb.note_disconnected()             # outage B
    await asyncio.sleep(0.25)
    assert len(emitted) == 3            # OFFLINE B
    assert emitted[2].source == Source.ROBOT_OFFLINE
    await deb.shutdown()


@pytest.mark.asyncio
async def test_shutdown_makes_subsequent_notes_noop():
    """Closed debouncer must ignore note_disconnected and note_connected."""
    emitted: list[AnomalyEvent] = []
    deb = OfflineDebouncer(
        robot_id="kachaka",
        debounce_seconds_provider=lambda: 0.05,
        is_patrol_running=lambda: False,
        emit=lambda evt: emitted.append(evt),
        get_serial=lambda: "BKP",
    )
    await deb.shutdown()
    deb.note_disconnected()
    await asyncio.sleep(0.15)
    deb.note_connected()
    await asyncio.sleep(0.05)
    assert emitted == []
