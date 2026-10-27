"""
JSON file I/O utilities for loading and saving configuration files.
"""
import glob
import json
import os
import logging
import tempfile

logger = logging.getLogger(__name__)

# Read once at import, before worker threads exist: os.umask() is process-wide
# and toggling it later would race files being created on other threads.
_UMASK = os.umask(0)
os.umask(_UMASK)


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
    return _atomic_write(filepath, lambda f: json.dump(data, f, indent=2, ensure_ascii=False), "w", "JSON")


def save_bytes(filepath: str, data: bytes) -> bool:
    """Atomic counterpart of save_json for binary files (map PNGs)."""
    return _atomic_write(filepath, lambda f: f.write(data), "wb", "bytes")


def _atomic_write(filepath: str, writer, mode: str, what: str) -> bool:
    # Follow a symlinked target so the replace lands on the real file.
    filepath = os.path.realpath(filepath)
    dirpath = os.path.dirname(filepath) or "."
    base = os.path.basename(filepath)
    tmp_path = None
    try:
        os.makedirs(dirpath, exist_ok=True)
        # A crash between tempfile creation and replace leaves orphans behind;
        # sweep this file's own stale temps before writing a new one.
        for stale in glob.glob(os.path.join(dirpath, f".{base}.*.tmp")):
            try:
                os.remove(stale)
            except OSError:
                pass
        kwargs = {"encoding": "utf-8"} if "b" not in mode else {}
        with tempfile.NamedTemporaryFile(
            mode, dir=dirpath, prefix=f".{base}.", suffix=".tmp", delete=False, **kwargs,
        ) as f:
            tmp_path = f.name
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600 owned by us; keep the target's mode and owner
        # (bind-mounted site files belong to the Pi user, the container runs
        # as root), or for a new file the mode open() would have given it.
        try:
            st = os.stat(filepath)
            os.chmod(tmp_path, st.st_mode & 0o777)
            try:
                os.chown(tmp_path, st.st_uid, st.st_gid)
            except OSError:
                pass  # not permitted when running unprivileged; owner is us anyway
        except FileNotFoundError:
            os.chmod(tmp_path, 0o666 & ~_UMASK)
        os.replace(tmp_path, filepath)
        tmp_path = None
        _fsync_dir(dirpath)
        logger.info(f"Saved {what} to {filepath}")
        return True
    except OSError as e:
        logger.error(f"Error saving {what} to {filepath}: {e}")
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
