"""TODO-018: task ids must not collide within the same second.

The old second-resolution id meant two submits in one second produced the same
id — the second Task overwrote the first in tasks_db and cancel only reached
the survivor, leaving the first run executing untracked.
"""
import re

from common_types import generate_task_id


def test_consecutive_ids_are_unique():
    ids = [generate_task_id() for _ in range(50)]
    assert len(set(ids)) == len(ids)


def test_id_keeps_14_digit_timestamp_prefix():
    """bio_sensor history filters with `task_id LIKE '<prefix>%'` and the
    frontend parses the leading 14 digits — both must keep working."""
    task_id = generate_task_id()
    assert re.match(r"^\d{14}-[0-9a-f]+$", task_id), task_id
    assert task_id.startswith(task_id[:14])
