import os


def get_settings_dir():
    """Get the path to the data/config directory (JSON configs).
    From src/backend/settings/config.py → up 4 levels to project root."""
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(base_path, "data", "config")


# --- JSON config file paths ---
SETTINGS_FILE = os.path.join(get_settings_dir(), "settings.json")
BEDS_FILE = os.path.join(get_settings_dir(), "beds.json")
PATROL_FILE = os.path.join(get_settings_dir(), "patrol.json")
SCHEDULE_FILE = os.path.join(get_settings_dir(), "schedule.json")
PATROL_PRESETS_DIR = os.path.join(get_settings_dir(), "patrol_presets")
MAPS_DIR = os.path.join(os.path.dirname(get_settings_dir()), "maps")


def get_runtime_settings() -> dict:
    """Load runtime settings merged with defaults. Called per-request for fresh values."""
    from settings.defaults import DEFAULT_SETTINGS
    from utils.json_io import load_json
    saved = load_json(SETTINGS_FILE, {})
    merged = {**DEFAULT_SETTINGS, **saved}
    return merged


def update_settings(**kwargs) -> dict:
    """Read settings.json, merge kwargs in-place, write back. Returns the saved dict."""
    from utils.json_io import load_json, save_json
    current = load_json(SETTINGS_FILE, {})
    current.update(kwargs)
    save_json(SETTINGS_FILE, current)
    return current


def get_port() -> int:
    """Get the server port from environment or default."""
    return int(os.environ.get("PORT", "8000"))
