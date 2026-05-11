"""Lifespan-scoped task registries + shutdown helper.

Lives in its own module so `main.py` and `routers/settings.py` can both
import without a circular dependency: main.py imports routers/settings.py
at startup; settings.py needs the retry-task registry to cancel an
in-flight retry on IP change.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Keyed by robot_id; one entry per robot.
_register_retry_tasks: dict[str, asyncio.Task] = {}
_worker_tasks: dict[str, asyncio.Task] = {}


async def _register_retry_loop(
    fleet,
    robot_id: str,
    ip: str,
    name: str,
    *,
    initial_delay: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Background retry: register the robot with exp backoff up to 60s.

    Sleeps BEFORE each attempt; first sleep uses ``initial_delay``. The
    sleep callable is injectable so unit tests can drive deterministic
    timings without monkey-patching the global ``asyncio.sleep``.
    """
    delay = max(initial_delay, 1.0)
    while True:
        await sleep(delay)
        try:
            result = await fleet.register_robot(robot_id, ip, name)
            if result.get("ok"):
                logger.info("Robot '%s' registered after retry at %s", robot_id, ip)
                return
            logger.warning("Robot '%s' retry register failed: %s", robot_id, result.get("error"))
        except Exception as exc:
            logger.exception("Robot '%s' retry register raised: %s", robot_id, exc)
        delay = min(delay * 2, 60.0)


async def cancel_register_retry(robot_id: str) -> None:
    """Pop and await the in-flight retry task for ``robot_id``, if any.

    Used by both ``FleetAPI.unregister_robot`` and the settings router's
    IP-change path: a retry that succeeds against a stale IP must not be
    allowed to reinstate a slot the caller just removed.
    """
    task = _register_retry_tasks.pop(robot_id, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def shutdown_robot_tasks(fleet) -> None:
    """Cancel-and-await retries → workers → unregister, in that order.

    Order matters: a retry task that wins between cancellation passes
    could leave a half-built slot, so retries must be drained BEFORE
    the worker is cancelled and the slot is unregistered.
    """
    # Yield once so any just-created task gets to run its coroutine prelude
    # before we cancel it — otherwise cancel() on a not-yet-started task
    # transitions it straight to CANCELLED without ever raising
    # CancelledError inside the coroutine, skipping cleanup.
    await asyncio.sleep(0)

    for t in list(_register_retry_tasks.values()):
        t.cancel()
    if _register_retry_tasks:
        await asyncio.gather(*_register_retry_tasks.values(), return_exceptions=True)
    _register_retry_tasks.clear()

    for t in list(_worker_tasks.values()):
        t.cancel()
    if _worker_tasks:
        await asyncio.gather(*_worker_tasks.values(), return_exceptions=True)
    _worker_tasks.clear()

    # Final step — unregister every still-registered robot. Iterate a
    # snapshot of keys because unregister mutates fleet._robots.
    for robot_id in list(getattr(fleet, "_robots", {}).keys()):
        try:
            await fleet.unregister_robot(robot_id)
        except Exception:
            logger.exception("unregister_robot %s during shutdown", robot_id)
