"""HIL fixtures — real bio-sensor MQTT broker."""
from __future__ import annotations

import tempfile
import time

import pytest

CONNECT_TIMEOUT_S = 10.0


@pytest.fixture(scope="session")
def bio_sensor_client(mqtt_settings):
    """Real BioSensorMQTTClient connected to the configured WiSleep broker.

    Uses a throwaway SQLite DB so HIL tests never touch data/sensor_data.db.
    Skips the test session if the broker doesn't accept the connection within
    CONNECT_TIMEOUT_S — it's a hardware test, not a paho config test.
    """
    from services.bio_sensor_mqtt import BioSensorMQTTClient

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    client = BioSensorMQTTClient(
        broker=mqtt_settings["mqtt_broker"],
        port=mqtt_settings["mqtt_port"],
        topic=mqtt_settings["mqtt_topic"],
        db_path=db_path,
    )
    try:
        client.start()
    except Exception as exc:
        pytest.skip(f"MQTT broker unreachable: {exc}")

    deadline = time.time() + CONNECT_TIMEOUT_S
    while time.time() < deadline and not client.connected:
        time.sleep(0.2)
    if not client.connected:
        client.stop()
        pytest.skip(
            f"MQTT broker {mqtt_settings['mqtt_broker']}:{mqtt_settings['mqtt_port']} "
            f"did not connect within {CONNECT_TIMEOUT_S}s"
        )

    yield client
    client.stop()
