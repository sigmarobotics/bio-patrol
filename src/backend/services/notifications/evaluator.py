"""Scan outcome + evaluators that turn outcomes into AnomalyEvents."""
from __future__ import annotations

from dataclasses import dataclass

from services.notifications.events import AnomalyEvent, Severity, Source


@dataclass
class ScanOutcome:
    """Result of one bio-scan window. Replaces the legacy {task_id, data} return."""
    task_id: str
    location_id: str
    bed_name: str | None
    valid_record: dict | None      # None when retries exhausted without a valid hit
    retry_count: int
    last_record_raw: dict | None
    last_failure_reason: str | None  # None only when valid_record is not None


# Wisleep sensor status values, in operator terms (status 4 = valid reading)
_STATUS_TEXT = {
    0: "偵測不到人",
    2: "人員躁動，無穩定讀值",
}


class BioScanFailureEvaluator:
    """Emits BIO_SCAN_FAILURE / WARN when retries exhaust without a valid hit."""

    def evaluate(self, outcome: ScanOutcome) -> AnomalyEvent | None:
        if outcome.valid_record is not None:
            return None
        bed = outcome.bed_name or outcome.location_id
        last = outcome.last_record_raw or {}
        status_text = _STATUS_TEXT.get(last.get("status"))
        return AnomalyEvent(
            severity=Severity.WARN,
            source=Source.BIO_SCAN_FAILURE,
            bed_key=outcome.bed_name,
            task_id=outcome.task_id,
            title=f"⚠️ {bed} 量測失敗",
            body=(
                f"床位：{bed}\n"
                f"狀況：{status_text or outcome.last_failure_reason}\n"
                f"重試次數：{outcome.retry_count}"
            ),
            raw=outcome.last_record_raw or {},
        )
