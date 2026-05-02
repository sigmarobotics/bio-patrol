"""Shared fixtures for unit tests under tests/unit/."""
import pytest


@pytest.fixture(autouse=True)
def _reset_lifespan_state():
    """Clear the per-process lifespan registries before/after each test.

    Several tests insert directly into ``lifespan_state._register_retry_tasks``
    or ``_worker_tasks``; without this fixture, leftover entries from one test
    can break the next.
    """
    try:
        import lifespan_state
    except ImportError:
        # Module not yet on the path — nothing to clean.
        yield
        return
    lifespan_state._register_retry_tasks.clear()
    lifespan_state._worker_tasks.clear()
    yield
    lifespan_state._register_retry_tasks.clear()
    lifespan_state._worker_tasks.clear()
