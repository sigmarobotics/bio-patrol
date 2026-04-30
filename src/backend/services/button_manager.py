"""Glue layer between zigbee_mqtt events, the action_registry, and the
button_bindings table.

Two responsibilities:
  1. Pairing — when a `pair` request arms a target action_key, the next
     `device_joined` event within the timeout binds that IEEE to the target.
  2. Button presses — `button_action` events with action="single" look up the
     IEEE, fire the registered action, and update fire stats. Other action
     types (double / long) are ignored.
"""
from __future__ import annotations

import asyncio
import logging
import time

from common_types import get_now
from services import action_registry, button_db
from services.zigbee_mqtt import ZigbeeMQTT

logger = logging.getLogger("services.button_manager")

SUPPORTED_TRIGGER = "single"
# z2m occasionally emits duplicate `single` events for one physical press;
# 0.3 s catches those without eating deliberate rapid presses.
DEBOUNCE_SECONDS = 0.3
PAIR_WINDOW_SECONDS = 120
# When a bound device announces leave, hold permit_join open this long so the
# next press auto-rejoins without nurse intervention. SNZB-01P firmware sleeps
# at end of every press session — without this window the device is orphaned.
AUTO_REJOIN_WINDOW_SECONDS = 254  # z2m max — single-byte field


class ButtonManager:
    def __init__(self, zigbee: ZigbeeMQTT, db_path: str | None = None):
        self.zigbee = zigbee
        self._db_path = db_path
        self._pair_target: str | None = None
        self._pair_expires_at: float = 0.0
        self._pair_lock = asyncio.Lock()
        self._last_fire_at: dict[str, float] = {}

    async def arm_pair(self, action_key: str,
                       time_s: int = PAIR_WINDOW_SECONDS) -> dict:
        if not action_registry.is_registered(action_key):
            return {"ok": False, "error": f"unknown action: {action_key}"}
        async with self._pair_lock:
            self._pair_target = action_key
            self._pair_expires_at = time.monotonic() + time_s
        ok = await self.zigbee.permit_join(True, time_s=time_s)
        if not ok:
            async with self._pair_lock:
                self._pair_target = None
                self._pair_expires_at = 0.0
            return {"ok": False, "error": "MQTT broker unavailable"}
        logger.info("Pairing armed for %s (%ds)", action_key, time_s)
        return {"ok": True, "action_key": action_key, "timeout": time_s}

    async def cancel_pair(self) -> dict:
        async with self._pair_lock:
            target = self._pair_target
            self._pair_target = None
            self._pair_expires_at = 0.0
        await self.zigbee.permit_join(False, time_s=0)
        return {"ok": True, "cancelled_target": target}

    async def forget_device(self, ieee_addr: str) -> bool:
        return await self.zigbee.remove_device(ieee_addr)

    @property
    def pairing_target(self) -> str | None:
        if self._pair_target is None:
            return None
        if time.monotonic() >= self._pair_expires_at:
            return None
        return self._pair_target

    @property
    def pair_remaining_seconds(self) -> int | None:
        if self._pair_target is None:
            return None
        remaining = self._pair_expires_at - time.monotonic()
        if remaining <= 0:
            return None
        return int(remaining)

    async def handle_event(self, event: dict) -> None:
        evt = event.get("type")
        if evt in ("device_joined", "device_announce"):
            await self._on_device_event(event)
        elif evt == "device_leave":
            await self._on_device_leave(event)
        elif evt == "button_action":
            await self._on_button_action(event)

    async def _on_device_event(self, event: dict) -> None:
        ieee = event.get("ieee_addr")
        if not ieee:
            return

        # Already-bound devices that left and rejoined: silent re-admit. No
        # pair_target needed; nurse never has to re-pair after a sleep cycle.
        existing = button_db.get_binding_by_ieee(ieee, self._db_path)
        if existing and existing.get("action_key"):
            button_db.update_status(
                ieee, event.get("battery"), get_now().isoformat(), self._db_path
            )
            logger.info("Bound device %s rejoined → %s", ieee, existing["action_key"])
            return

        async with self._pair_lock:
            target = self._pair_target
            expired = time.monotonic() >= self._pair_expires_at
            if target is None or expired:
                logger.debug("Device %s seen but no active pairing target", ieee)
                return
            self._pair_target = None
            self._pair_expires_at = 0.0
        button_db.bind_action(target, ieee, event.get("friendly_name"), self._db_path)
        await self.zigbee.permit_join(False, time_s=0)
        logger.info("Paired %s → action %s", ieee, target)

    async def _on_device_leave(self, event: dict) -> None:
        ieee = event.get("ieee_addr")
        if not ieee:
            return
        binding = button_db.get_binding_by_ieee(ieee, self._db_path)
        if not (binding and binding.get("action_key")):
            return
        ok = await self.zigbee.permit_join(True, time_s=AUTO_REJOIN_WINDOW_SECONDS)
        logger.info(
            "Bound device %s left network → permit_join open %ds for auto-rejoin (publish ok=%s)",
            ieee, AUTO_REJOIN_WINDOW_SECONDS, ok,
        )

    async def _on_button_action(self, event: dict) -> None:
        ieee = event.get("ieee_addr")
        action_str = event.get("action")
        if not ieee:
            return

        # Look up binding before writing status — an unbound device's status
        # is irrelevant and burns a write per stray press.
        binding = button_db.get_binding_by_ieee(ieee, self._db_path)
        if binding is None:
            logger.warning("Press from unbound device %s, ignoring", ieee)
            return
        button_db.update_status(
            ieee, event.get("battery"), get_now().isoformat(), self._db_path
        )
        if action_str != SUPPORTED_TRIGGER:
            return

        now = time.monotonic()
        last = self._last_fire_at.get(ieee, 0.0)
        if now - last < DEBOUNCE_SECONDS:
            logger.debug("Debounced repeat press from %s", ieee)
            return
        self._last_fire_at[ieee] = now

        action_key = binding.get("action_key")
        if not action_key or not action_registry.is_registered(action_key):
            logger.warning("Bound action %s no longer registered", action_key)
            return

        logger.info("Firing action %s (button %s)", action_key, ieee)
        result = await action_registry.fire(action_key)
        button_db.record_fire(action_key, self._db_path)
        logger.info("Action %s result ok=%s", action_key, result.get("ok"))
