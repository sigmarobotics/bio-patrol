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
