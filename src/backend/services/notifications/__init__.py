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
