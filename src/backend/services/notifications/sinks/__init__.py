"""Sink protocol — every concrete notification channel implements this."""
from __future__ import annotations

from typing import Protocol

from services.notifications.events import AnomalyEvent


class Sink(Protocol):
    async def is_enabled(self) -> bool: ...
    async def send(self, event: AnomalyEvent) -> None: ...
