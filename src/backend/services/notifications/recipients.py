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
    """Returns the single recipient configured in settings, per channel."""

    async def resolve(self, event: AnomalyEvent, channel: str) -> list[str]:
        if channel != "telegram":
            return []  # MQTT publishes to topic — no recipient list
        uid = get_runtime_settings().get("telegram_user_id", "") or ""
        return [uid] if uid else []
