"""Schedule (cron) endpoints. Mutations re-load the live scheduler."""
import logging

from fastapi import APIRouter, HTTPException

from settings.config import SCHEDULE_FILE
from settings.defaults import DEFAULT_SCHEDULE
from utils.json_io import load_json, save_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Schedule"])


async def _reload_scheduler() -> None:
    """Re-read schedule.json and rebuild scheduler jobs. Logs but does not raise."""
    from services.scheduler import notify_schedule_issue, scheduler_service
    try:
        await scheduler_service.reload_from_json()
    except Exception as e:
        # 存檔成功但 job 沒重建 —— 畫面顯示已排好，實際排程狀態不明。
        logger.warning(f"Scheduler reload failed: {e}")
        await notify_schedule_issue(
            "⚠️ 排程重新載入失敗",
            f"排程已存檔，但 scheduler 沒有套用：{e}\n請重啟服務並確認排程仍會執行。",
        )


def _enabled_entries(data: dict) -> dict[str, dict]:
    """id -> entry，只取啟用中的排程（其餘本來就不會產生 job）。
    body 是未驗證的 raw dict，形狀不對就當作沒有排程，別讓通報把存檔弄成 500。"""
    entries = data.get("schedules")
    if not isinstance(entries, list):
        return {}
    return {
        s.get("id"): s
        for s in entries
        if isinstance(s, dict) and s.get("id") and s.get("enabled")
    }


async def _notify_turned_off(before: dict, after: dict) -> None:
    """在 UI 停用／刪除排程是完全靜默的：job 被移掉後 _run_patrol 永遠不會跑，
    到了 23:00 沒有任何訊息——TODO-020 那次整場漏巡就是這個形狀。改成存檔當下
    就推一則，讓誤觸當天就被發現，而不是隔天早上才知道。"""
    from services.scheduler import notify_schedule_issue

    still_on = _enabled_entries(after)
    for schedule_id, entry in _enabled_entries(before).items():
        if schedule_id in still_on:
            continue
        await notify_schedule_issue(
            "🛑 排程巡房已關閉",
            f"原排程時間：{entry.get('time') or '未設定'}\n"
            f"這個時段起不會再自動巡房，若非刻意調整請回設定頁重新啟用。",
            raw={"schedule_id": schedule_id, "time": entry.get("time")},
        )


@router.get("/schedule")
async def get_schedule():
    """Return schedule.json (or defaults if empty/missing)."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    return data or DEFAULT_SCHEDULE


@router.post("/schedule")
async def save_schedule(body: dict):
    """Save schedule.json and reload the scheduler. The page POSTs the whole
    list back, so a disable shows up as an entry no longer enabled in `body` —
    see `_notify_turned_off`."""
    before = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    save_json(SCHEDULE_FILE, body)
    await _reload_scheduler()
    await _notify_turned_off(before, body)
    return {"status": "ok", "data": body}


@router.delete("/schedule/{schedule_id}")
async def delete_schedule_entry(schedule_id: str):
    """Remove a single schedule entry by id and reload the scheduler."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    schedules = data.get("schedules", [])
    remaining = [s for s in schedules if s.get("id") != schedule_id]
    if len(remaining) == len(schedules):
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    data["schedules"] = remaining
    save_json(SCHEDULE_FILE, data)
    await _reload_scheduler()
    await _notify_turned_off({"schedules": schedules}, data)
    return {"status": "ok", "data": data}
