"""
JSON file I/O utilities for loading and saving configuration files.
"""
import json
import os
import logging
import tempfile

logger = logging.getLogger(__name__)


def load_json(filepath: str, default=None):
    """Load JSON from file, returning default if file doesn't exist or is invalid."""
    if default is None:
        default = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.info(f"JSON file not found: {filepath}, using default")
        return default
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error loading JSON from {filepath}: {e}")
        return default


def save_json(filepath: str, data):
    """Save data as JSON to file, creating directories as needed.

    Written to a temp file in the same directory, fsynced, then os.replace()d
    onto the target, so a power cut mid-write leaves the old file intact
    instead of a truncated one that load_json would read back as defaults.
    """
    dirpath = os.path.dirname(filepath) or "."
    tmp_path = None
    try:
        os.makedirs(dirpath, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=dirpath,
            prefix=f".{os.path.basename(filepath)}.", suffix=".tmp", delete=False,
        ) as f:
            tmp_path = f.name
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; keep the target's mode (or a plain 0644).
        try:
            mode = os.stat(filepath).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, filepath)
        tmp_path = None
        _fsync_dir(dirpath)
        logger.info(f"Saved JSON to {filepath}")
        return True
    except OSError as e:
        logger.error(f"Error saving JSON to {filepath}: {e}")
        return False
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _fsync_dir(dirpath: str) -> None:
    """Persist the rename itself; best effort (not supported everywhere)."""
    try:
        fd = os.open(dirpath, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
