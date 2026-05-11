"""Pin the fix for: bio-sensor MQTT broker unreachable at startup leaves a
dead singleton that never recovers (sensor.log 2026-05-04 / 2026-05-11).

Before the fix, BioSensorMQTTClient.start() called paho's blocking connect()
and re-raised on failure — loop_start() was never reached, so paho's
background reconnect logic never ran. Settings "test" used a separate
throwaway client, masking the rot.

After the fix, start() uses connect_async() + loop_start() with
reconnect_delay_set, so paho's loop owns retries from the first tick.
"""
from __future__ import annotations

import tempfile
import time

import pytest

from services.bio_sensor_mqtt import BioSensorMQTTClient


def test_start_does_not_raise_when_broker_unreachable():
    """Pin: start() must not raise on a dead broker — paho's loop owns retry."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    # 127.0.0.1:19999 is reliably refused (nothing listens). Using a non-
    # routable address would test DNS/timeout instead, which is slower.
    client = BioSensorMQTTClient(
        broker="127.0.0.1", port=19999, topic="/x", db_path=db_path,
    )
    try:
        client.start()  # must not raise
        time.sleep(0.5)  # give paho one tick to attempt + fail
        assert client.connected is False
    finally:
        client.stop()


@pytest.mark.hil
def test_real_broker_eventually_connects(mqtt_settings):
    """Pin: against the real broker, start() reaches `connected=True` within
    a few seconds — proves connect_async + loop_start actually subscribe.
    Skips if the WiSleep broker is unreachable from the test machine."""
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
        deadline = time.time() + 10.0
        while time.time() < deadline and not client.connected:
            time.sleep(0.2)
        if not client.connected:
            pytest.skip("WiSleep broker not reachable from this host")
        assert client.connected is True
    finally:
        client.stop()
