import os
from services.fleet_api import FleetAPI
from services.bio_sensor_mqtt import BioSensorMQTTClient

# Global fleet instance
fleet_instance: FleetAPI = None

def get_fleet() -> FleetAPI:
    """Dependency function to get the fleet instance"""
    global fleet_instance
    if fleet_instance is None:
        fleet_instance = FleetAPI()
    return fleet_instance

# Global bio-sensor client
bio_sensor_client: BioSensorMQTTClient = None

def get_bio_sensor_client() -> BioSensorMQTTClient:
    """Get the global MQTT client instance. Returns None if mqtt_enabled is false."""
    global bio_sensor_client
    if bio_sensor_client is None:
        from settings.config import get_runtime_settings
        cfg = get_runtime_settings()
        if not cfg.get("mqtt_enabled"):
            return None
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        tls_cert = cfg.get("mqtt_tls_cert", "")
        tls_key = cfg.get("mqtt_tls_key", "")
        bio_sensor_client = BioSensorMQTTClient(
            broker=cfg.get("mqtt_broker", "localhost"),
            port=int(cfg.get("mqtt_port", 8883)),
            topic=cfg.get("mqtt_topic", "deviceData-qt/201906078"),
            username=cfg.get("mqtt_username") or None,
            password=cfg.get("mqtt_password") or None,
            tls_cert=os.path.join(project_root, tls_cert) if tls_cert else None,
            tls_key=os.path.join(project_root, tls_key) if tls_key else None,
        )
    return bio_sensor_client
