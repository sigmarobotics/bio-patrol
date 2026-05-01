"""
JSON file I/O utilities for loading and saving configuration files.
"""
import json
import os
import logging

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
    """Save data as JSON to file, creating directories as needed."""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved JSON to {filepath}")
        return True
    except IOError as e:
        logger.error(f"Error saving JSON to {filepath}: {e}")
        return False
