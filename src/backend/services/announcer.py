"""Robot voice announcements played by an on-robot player (IT-24).

Skeleton — the deploy channel and player wiring land in the IT-24 impl.
"""

SOUND_SENSOR_OFFLINE = "sensor_pc_offline_zh"
SOUND_SENSOR_SILENT = "sensor_silent_zh"
SOUND_BATTERY_LOW = "battery_low_zh"


async def announce(name: str, *, pre_delay_s: float = 0.0, wait: bool = False) -> bool:
    """Play bundled clip ``name`` on the robot at volume 10, then restore the
    robot's own volume. Never raises; returns False when nothing was played."""
    return False
