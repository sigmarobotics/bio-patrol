"""A malformed MQTT payload must be dropped, not raised.

paho 2.x re-raises callback exceptions by default; one bad payload used to
kill the network thread while `connected` stayed True, so no reconnect nudge
ever ran and every patrol was refused as "sensor silent".
"""
import types

import pytest

from tests.unit.test_bio_sensor_scan_manual_default import _make_client


@pytest.mark.parametrize("payload", [b"", b"\xff\xfe", b"not json", b"[1]"])
def test_malformed_payload_is_dropped(tmp_path, monkeypatch, payload):
    client = _make_client(tmp_path, monkeypatch)
    msg = types.SimpleNamespace(topic="data-test/wisleep/X", payload=payload)

    client._on_message(client.client, None, msg)

    assert client._msg_seq == 0
    assert client.latest_data is None
    assert client.last_message_at is None


def test_paho_callback_exceptions_are_suppressed(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)

    assert client.client.suppress_exceptions is True


def test_ingest_error_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    """A schema change in the Wisleep payload must not reach paho (which would
    log one bare line per message) — log it here with a traceback."""
    import logging
    from types import SimpleNamespace

    client = _make_client(tmp_path, monkeypatch)

    def boom(payload):
        raise KeyError("records")

    client._ingest = boom
    with caplog.at_level(logging.WARNING, logger="services.bio_sensor_mqtt"):
        client._on_message(client.client, None, SimpleNamespace(topic="t", payload=b'{"x": 1}'))
        client._on_message(client.client, None, SimpleNamespace(topic="t", payload=b'{"x": 1}'))
    msgs = [r for r in caplog.records if "Failed to ingest" in r.getMessage()]
    assert len(msgs) == 2
    assert msgs[0].exc_info is not None      # first one carries the traceback
    assert msgs[1].exc_info is None          # throttled to a one-liner
