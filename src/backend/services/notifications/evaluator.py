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


def vitals_out_of_band(bpm, rpm, cfg: dict) -> bool:
    """True when heart or respiration rate lies strictly outside its band —
    a reading sitting exactly on a threshold is still normal."""
    hr_low, hr_high = cfg.get("vitals_hr_low", 50), cfg.get("vitals_hr_high", 120)
    rr_low, rr_high = cfg.get("vitals_rr_low", 10), cfg.get("vitals_rr_high", 30)
    return not (hr_low <= bpm <= hr_high) or not (rr_low <= rpm <= rr_high)


class VitalsOutOfBandEvaluator:
    """Emits VITALS_OUT_OF_BAND / WARN when a VALID reading crosses the
    configured heart/respiration thresholds (IT-21 FEAT-025). Failed scans
    are BioScanFailureEvaluator's business."""

    def evaluate(self, outcome: ScanOutcome, cfg: dict) -> AnomalyEvent | None:
        rec = outcome.valid_record
        if rec is None:
            return None
        bpm, rpm = rec.get("bpm") or 0, rec.get("rpm") or 0
        if not vitals_out_of_band(bpm, rpm, cfg):
            return None
        bed = outcome.bed_name or outcome.location_id
        return AnomalyEvent(
            severity=Severity.WARN,
            source=Source.VITALS_OUT_OF_BAND,
            bed_key=outcome.bed_name,
            task_id=outcome.task_id,
            title=f"⚠️ {bed} 心跳呼吸異常",
            body=(
                f"床位：{bed}\n"
                f"心跳：{bpm} 次／分（正常範圍 "
                f"{cfg.get('vitals_hr_low', 50)}–{cfg.get('vitals_hr_high', 120)}）\n"
                f"呼吸：{rpm} 次／分（正常範圍 "
                f"{cfg.get('vitals_rr_low', 10)}–{cfg.get('vitals_rr_high', 30)}）"
            ),
            raw={"bpm": bpm, "rpm": rpm, "status": rec.get("status")},
        )
