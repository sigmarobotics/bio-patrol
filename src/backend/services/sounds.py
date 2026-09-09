"""Pre-recorded WAV clips shipped with the app (assets/sounds/).

The robot's Sound API stores a clip under a name; we key each upload by
content hash so a re-recorded WAV uploads as a new sound instead of silently
playing the old one.
"""
import hashlib
import wave
from pathlib import Path

SOUND_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"


def sound_path(name: str) -> Path:
    """Path of the bundled WAV for ``name``."""
    return SOUND_DIR / f"{name}.wav"


def wav_seconds(path) -> float:
    """Duration of a WAV file in seconds."""
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def sound_key(name: str, data: bytes) -> str:
    """On-robot sound name: content-addressed so an edited clip re-uploads."""
    return f"{name}-{hashlib.sha256(data).hexdigest()[:8]}"
