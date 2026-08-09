"""
common_types.py
集中定義所有專案共用的型別、Enum、工具函式與型別別名，供各模組 import 使用。
"""
from enum import Enum
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from datetime import datetime

# Common Enum
class StepStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAIL = "fail"
    SKIPPED = "skipped"

class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SHELF_DROPPED = "shelf_dropped"


class StepAction(str, Enum):
    """Patrol step actions executed by TaskEngine. The string values are the
    on-the-wire representation kept on TaskStep.action and in stored task JSON.
    """
    SPEAK = "speak"
    MOVE_TO_POSE = "move_to_pose"
    MOVE_TO_LOCATION = "move_to_location"
    DOCK_SHELF = "dock_shelf"
    UNDOCK_SHELF = "undock_shelf"
    MOVE_SHELF = "move_shelf"
    RETURN_SHELF = "return_shelf"
    RESET_SHELF_POSE = "reset_shelf_pose"
    RETURN_HOME = "return_home"
    BIO_SCAN = "bio_scan"
    WAIT = "wait"


# Actions that should not fail the whole task when they error out.
NON_CRITICAL_ACTIONS = frozenset({
    StepAction.BIO_SCAN.value,
    StepAction.WAIT.value,
    StepAction.SPEAK.value,
    StepAction.RETURN_SHELF.value,
    StepAction.RESET_SHELF_POSE.value,
})

# Enhanced Result Models
class StepResult(BaseModel):
    success: bool
    error_code: int = 0
    error_message: str = ""
    data: Optional[Dict[str, Any]] = None
    timestamp: str = ""

# Common Pydantic Model
class TaskStep(BaseModel):
    step_id: str
    action: str
    params: Dict[str, Any]
    status: StepStatus = StepStatus.PENDING
    result: Optional[StepResult] = None
    skip_on_failure: Optional[List[str]] = None  # Step IDs to skip if this step fails

class Task(BaseModel):
    task_id: str = ""
    robot_id: Optional[str] = None
    steps: List[TaskStep]
    status: TaskStatus = TaskStatus.QUEUED
    metadata: Optional[Dict[str, Any]] = None

class Robot(BaseModel):
    robot_id: str

def validate_task_conditional_logic(task: "Task") -> List[str]:
    """
    Validate task conditional logic and return list of validation errors
    """
    errors = []
    step_ids = {step.step_id for step in task.steps}
    
    for step in task.steps:
        if step.skip_on_failure:
            # Check if all skip targets exist
            for skip_target in step.skip_on_failure:
                if skip_target not in step_ids:
                    errors.append(f"Step '{step.step_id}' references non-existent skip target '{skip_target}'")
                
                # Check for self-reference
                if skip_target == step.step_id:
                    errors.append(f"Step '{step.step_id}' cannot skip itself")
    
    return errors

def get_now():
    """Get current time in the configured timezone."""
    from datetime import datetime, timezone, timedelta
    try:
        from settings.config import get_runtime_settings
        import zoneinfo
        tz_name = get_runtime_settings().get("timezone", "Asia/Taipei")
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=8))  # fallback to UTC+8
    return datetime.now(tz)

def generate_task_id() -> str:
    """Timestamp + random suffix. The bare second-resolution timestamp collided
    when two runs started in the same second: the second Task overwrote the
    first in tasks_db, so cancel only reached the survivor while the first
    runtime object kept executing. Consumers match on the 14-digit prefix
    (bio_sensor history LIKE, frontend formatTaskId), so the suffix is safe."""
    import uuid
    return f"{get_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
