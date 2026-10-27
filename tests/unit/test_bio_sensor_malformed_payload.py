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
