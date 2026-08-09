"""AnomalyEvent — the single payload type that flows from producers to sinks."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from common_types import get_now


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Source(str, Enum):
    BIO_SCAN_FAILURE = "bio_scan_failure"
    SHELF_DROP = "shelf_drop"
    TASK_SUMMARY = "task_summary"
    VITALS_OUT_OF_BAND = "vitals_out_of_band"
    ROBOT_OFFLINE = "robot_offline"
    ROBOT_RECOVERED = "robot_recovered"
    SCHEDULE_NOT_RUN = "schedule_not_run"


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
