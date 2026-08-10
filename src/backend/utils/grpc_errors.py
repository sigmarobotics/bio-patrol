"""Recognise "the robot is unreachable" among robot-call failures.

A read that failed is not a measurement. kachaka_core's ``@with_retry``
turns an exhausted gRPC retry into ``{"ok": False, "error": "UNAVAILABLE: ..."}``
and the raw SDK raises ``grpc.RpcError`` carrying the same words, so both the
task runtime and the patrol router need one place to ask: was this a
connection failure, or a real answer from a robot that is actually there?
"""
from __future__ import annotations

# grpc.StatusCode names that mean "no usable connection right now".
_CONNECTION_CODES = {"UNAVAILABLE", "DEADLINE_EXCEEDED"}

_CONNECTION_MARKERS = (
    "unavailable",
    "deadline_exceeded",
    "deadline exceeded",
    "no route to host",
    "failed to connect",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "socket closed",
    "timeout",
    "timed out",
)


def is_connection_error(err: object) -> bool:
    """True when ``err`` (exception, error string, or None) is connection-class."""
    code = getattr(err, "code", None)
    if callable(code):
        try:
            if getattr(code(), "name", "") in _CONNECTION_CODES:
                return True
        except Exception:
            pass
    text = str(err or "").lower()
    return any(marker in text for marker in _CONNECTION_MARKERS)
