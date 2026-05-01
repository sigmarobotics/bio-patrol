"""Anomaly notification subsystem.

Producers emit AnomalyEvent → AnomalyDispatcher fans out to registered Sinks.
"""
from services.notifications.events import AnomalyEvent, Severity, Source

__all__ = ["AnomalyEvent", "Severity", "Source"]
