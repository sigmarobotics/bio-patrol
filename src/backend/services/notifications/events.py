"""AnomalyEvent — the single payload type that flows from producers to sinks."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from common_types import get_now


class Severity(str, Enum):
    INFO = "info"           # IT-6: 巡房完成 / 取消
    WARN = "warn"           # IT-5: 單床 bio scan 整輪失敗
    CRITICAL = "critical"   # IT-6: 貨架掉落; future: vitals out-of-band


class Source(str, Enum):
    BIO_SCAN_FAILURE = "bio_scan_failure"        # IT-5
    SHELF_DROP = "shelf_drop"                    # IT-6
    TASK_SUMMARY = "task_summary"                # IT-6
    VITALS_OUT_OF_BAND = "vitals_out_of_band"    # future


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
