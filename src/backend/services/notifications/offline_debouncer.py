"""OfflineDebouncer — per-robot state machine for connection-loss notifications.

State-machine ordering invariants (review-driven):
1. ``_offline_emitted = True`` is set BEFORE ``await self._dispatch(evt)``
   so a reconnect that arrives while the dispatch is awaiting still sees
   the flag and correctly schedules RECOVERED.
2. ``_emit_recovered`` takes the disconnect ``started_at`` as an arg, so a
   re-disconnect that lands during recovery does not corrupt the duration.
3. ``note_connected`` clears ``_offline_emitted`` unconditionally — a
   stuck-True flag must never block a future debounce.
4. ``shutdown`` sets ``_closed = True`` BEFORE awaiting cancellation, so
   a ``note_*`` racing with shutdown becomes a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from services.notifications.events import AnomalyEvent, Severity, Source

logger = logging.getLogger(__name__)


class OfflineDebouncer:
    def __init__(
        self,
        *,
        robot_id: str,
        debounce_seconds_provider: Callable[[], float],
        is_patrol_running: Callable[[], bool],
        emit: Callable[[AnomalyEvent], None] | Callable[[AnomalyEvent], Awaitable[None]],
        get_serial: Callable[[], str],
    ) -> None:
        self._robot_id = robot_id
        self._debounce_seconds = debounce_seconds_provider
        self._is_patrol_running = is_patrol_running
        self._emit = emit
        self._get_serial = get_serial

        self._timer_task: Optional[asyncio.Task] = None
        self._recovery_task: Optional[asyncio.Task] = None
        self._offline_emitted: bool = False
        self._offline_severity: Severity = Severity.INFO
        self._disconnect_started_at: Optional[float] = None
        self._closed: bool = False

    # Public test hook — replaces direct private-attr poke in HIL tests.
    def set_debounce_provider(self, provider: Callable[[], float]) -> None:
        self._debounce_seconds = provider

    def is_offline_pending(self) -> bool:
        """True iff a debounce timer is running but has not yet fired."""
        return self._timer_task is not None and not self._timer_task.done()

    def note_disconnected(self) -> None:
        if self._closed:
            return
        if self._timer_task is not None and not self._timer_task.done():
            return
        # If we already emitted OFFLINE for an in-progress outage, do not
        # start another timer — note_connected will clear the flag and a
        # subsequent disconnect will then re-arm.
        if self._offline_emitted:
            return
        self._disconnect_started_at = time.monotonic()
        self._timer_task = asyncio.create_task(self._emit_after_debounce())

    def note_connected(self) -> None:
        if self._closed:
            return
        # 1. Cancel any pending debounce timer. Cancellation of a timer
        #    that is already past _emit_after_debounce's sleep does NOT
        #    cancel the dispatch — the dispatch is shielded.
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None
        # 2. Capture original state before mutating.
        started_at = self._disconnect_started_at
        was_emitted = self._offline_emitted
        prior_sev = self._offline_severity
        # 3. Reset state UNCONDITIONALLY — never leave a stuck flag.
        self._offline_emitted = False
        self._disconnect_started_at = None
        # 4. Only emit RECOVERED if the matching OFFLINE was previously sent.
        #    Mirror the OFFLINE severity so operators subscribed to
        #    CRITICAL-only channels still see the recovery.
        if was_emitted and started_at is not None:
            self._recovery_task = asyncio.create_task(
                self._emit_recovered(started_at, prior_sev)
            )

    async def shutdown(self) -> None:
        # Set _closed FIRST so any racing note_* becomes a no-op.
        self._closed = True
        for t in (self._timer_task, self._recovery_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._timer_task = None
        self._recovery_task = None

    async def _emit_after_debounce(self) -> None:
        try:
            # Provider is invoked once per disconnect, before sleeping.
            # An in-flight timer keeps its captured value; settings live-edits
            # only apply to subsequent disconnect transitions.
            await asyncio.sleep(self._debounce_seconds())
        except asyncio.CancelledError:
            return
        if self._closed:
            return
        in_patrol = False
        try:
            in_patrol = bool(self._is_patrol_running())
        except Exception:
            logger.exception("is_patrol_running raised — defaulting to idle")
        sev = Severity.CRITICAL if in_patrol else Severity.INFO
        started = self._disconnect_started_at or time.monotonic()
        evt = AnomalyEvent(
            severity=sev,
            source=Source.ROBOT_OFFLINE,
            title="🚨 巡房中機器人離線" if in_patrol else "🤖 機器人離線",
            body=self._format_offline_body(in_patrol),
            raw={
                "robot_id": self._robot_id,
                "serial": self._get_serial() or "",
                "in_patrol": in_patrol,
                "duration_sec": round(time.monotonic() - started, 1),
            },
        )
        # Set flag + severity BEFORE dispatching so a racing note_connected
        # sees it as emitted and pairs an INFO/CRITICAL recovery.
        self._offline_emitted = True
        self._offline_severity = sev
        # Shield: a note_connected cancelling _timer_task while we await
        # _dispatch must NOT cancel the in-flight Telegram/MQTT POST. The
        # outer await raises CancelledError; the inner _dispatch continues.
        try:
            await asyncio.shield(self._dispatch(evt))
        except asyncio.CancelledError:
            # Outer cancelled (note_connected); inner dispatch continues.
            return

    async def _emit_recovered(self, started_at: float, prior_severity: Severity) -> None:
        if self._closed:
            return
        total = round(time.monotonic() - started_at, 1)
        mins, secs = int(total // 60), int(total % 60)
        duration = f"{mins} 分 {secs} 秒" if mins else f"{secs} 秒"
        evt = AnomalyEvent(
            severity=prior_severity,
            source=Source.ROBOT_RECOVERED,
            title="✅ 機器人已恢復連線",
            body=f"機器人 {self._get_serial() or self._robot_id}\n離線約 {duration}",
            raw={
                "robot_id": self._robot_id,
                "serial": self._get_serial() or "",
                "total_offline_sec": total,
            },
        )
        await self._dispatch(evt)

    async def _dispatch(self, evt: AnomalyEvent) -> None:
        try:
            result = self._emit(evt)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("OfflineDebouncer emit failed for %s", evt.source)

    def _format_offline_body(self, in_patrol: bool) -> str:
        head = f"機器人 {self._get_serial() or self._robot_id}"
        if in_patrol:
            return head + "\n巡房進行中，任務可能中斷"
        return head
