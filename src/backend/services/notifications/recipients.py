"""Recipient routing.

The Protocol shapes a forward boundary for shift-aware routing — see the
notification dispatcher spec.
"""
from __future__ import annotations

from typing import Protocol

from services.notifications.events import AnomalyEvent
from settings.config import get_runtime_settings


class RecipientResolver(Protocol):
    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]: ...


class StaticResolver:
    """Returns the recipients configured in settings, per channel."""

    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]:
        cfg = get_runtime_settings()
        if channel == "telegram":
            uid = cfg.get("telegram_user_id", "") or ""
            return [uid] if uid else []
        if channel == "line":
            return [gid for gid in cfg.get("line_group_ids", []) if gid]
        return []  # MQTT publishes to topic — no recipient list
