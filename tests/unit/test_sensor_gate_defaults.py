"""IT-23: the sensor liveness gate keys ship in DEFAULT_SETTINGS so the
Settings UI shows them and the .get() fallbacks in patrol/scheduler/
task_runtime agree with what the user sees."""
from settings.defaults import DEFAULT_SETTINGS


def test_sensor_gate_defaults():
    assert DEFAULT_SETTINGS["sensor_precheck_wait_seconds"] == 30
    assert DEFAULT_SETTINGS["sensor_precheck_retry_count"] == 3
    assert DEFAULT_SETTINGS["sensor_precheck_retry_minutes"] == 10
    assert DEFAULT_SETTINGS["sensor_silent_abort_beds"] == 3
