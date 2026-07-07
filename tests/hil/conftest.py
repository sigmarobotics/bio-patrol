"""HIL fixtures — real bio-sensor MQTT broker + shared anomaly-event factory."""
from __future__ import annotations

import tempfile
import time

import pytest

CONNECT_TIMEOUT_S = 10.0


@pytest.fixture
def make_failure_event():
    """Canonical bio-scan-failure AnomalyEvent shared by the sink E2E tests."""
    from services.notifications.evaluator import ScanOutcome, BioScanFailureEvaluator

    def _make(task_id: str = "hil-test"):
        outcome = ScanOutcome(
            task_id=task_id,
            location_id="hil-101-1",
            bed_name="HIL-101-1",
            valid_record=None,
            retry_count=19,
            last_record_raw={"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"},
            last_failure_reason="無有效量測數值",
        )
        event = BioScanFailureEvaluator().evaluate(outcome)
        assert event is not None
        return event

    return _make


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
        username=mqtt_settings.get("mqtt_username") or None,
        password=mqtt_settings.get("mqtt_password") or None,
        tls_cert=mqtt_settings.get("mqtt_tls_cert") or None,
        tls_key=mqtt_settings.get("mqtt_tls_key") or None,
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
