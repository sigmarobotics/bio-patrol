"""HIL-1: Bio-sensor MQTT smoke tests against the real WiSleep broker."""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.hil

MESSAGE_TIMEOUT_S = 30.0


def test_connect_and_subscribe(bio_sensor_client, mqtt_settings):
    """HIL-1.1: Client connects to the broker and subscribes to the topic."""
    assert bio_sensor_client.connected is True
    assert bio_sensor_client.topic == mqtt_settings["mqtt_topic"]


def test_receives_message(bio_sensor_client):
    """HIL-1.2: Broker delivers at least one message within MESSAGE_TIMEOUT_S."""
    deadline = time.time() + MESSAGE_TIMEOUT_S
    while time.time() < deadline and bio_sensor_client.latest_data is None:
        time.sleep(0.5)
    assert bio_sensor_client.latest_data is not None, (
        f"No MQTT message received within {MESSAGE_TIMEOUT_S}s — broker silent or wrong topic"
    )


@pytest.mark.slow
def test_payload_schema(bio_sensor_client):
    """HIL-1.3: Each record in latest_data has the keys get_valid_scan_data depends on."""
    deadline = time.time() + MESSAGE_TIMEOUT_S
    while time.time() < deadline and bio_sensor_client.latest_data is None:
        time.sleep(0.5)
    if bio_sensor_client.latest_data is None:
        pytest.skip("No MQTT data received in time")

    payload = bio_sensor_client.latest_data
    assert "records" in payload, f"payload missing 'records': {payload!r}"
    assert isinstance(payload["records"], list) and payload["records"], (
        f"'records' empty or not a list: {payload!r}"
    )
    for record in payload["records"]:
        for key in ("status", "bpm", "rpm"):
            assert key in record, f"record missing '{key}': {record!r}"
