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


@pytest.fixture(autouse=True)
def _isolated_demo_db(monkeypatch, tmp_path):
    """IT-21: any code path that writes demo data (a demo patrol start back-fills
    the synthetic history) must hit a temp file, never data/demo_data.db."""
    from services import demo_data
    monkeypatch.setattr(demo_data, "DB_PATH", str(tmp_path / "demo_data.db"))


@pytest.fixture(autouse=True)
def _no_robot_announce(monkeypatch):
    """IT-24: a refusal now schedules a robot announcement. Unit tests must
    never reach a real robot's JupyterLab, so the sync deploy layer fails fast."""
    from services import announcer
    monkeypatch.setattr(announcer, "_deploy", lambda *a, **k: False, raising=False)


@pytest.fixture
def announced(monkeypatch):
    """IT-24: record announcer.announce calls as (name, pre_delay_s, wait)
    instead of deploying anything. Hooks call ``announcer.announce`` through
    the module attribute, so patching it here catches every call site."""
    from services import announcer
    calls = []

    async def fake(name, *, pre_delay_s=0.0, wait=False):
        calls.append((name, pre_delay_s, wait))
        return True

    monkeypatch.setattr(announcer, "announce", fake)
    return calls
