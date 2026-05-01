"""
Default settings for the Bio Patrol system.
"""

DEFAULT_SETTINGS = {
    "robot_ip": "192.168.204.37:26400",
    "mqtt_broker": "demo.wisleep-eck.org",
    "mqtt_port": 1883,
    "mqtt_topic": "data-test-level2/demo/wisleep-eck/org/201906559",
    "mqtt_shelf_id": "",
    "mqtt_enabled": False,
    "bio_scan_wait_time": 10,
    "bio_scan_retry_count": 19,
    "bio_scan_initial_wait": 120,
    "bio_scan_valid_status": 4,
    "robot_max_retries": 3,
    "robot_retry_base_delay": 2.0,
    "robot_retry_max_delay": 10.0,
    "enable_telegram": False,
    "telegram_bot_token": "",
    "telegram_user_id": "",
    "gemini_api_key": "",
    "active_map": "",
    "shelf_id": "S_04",
    "demo_preset": "",
    "timezone": "Asia/Taipei",
    "zigbee_enabled": True,
    "zigbee_mqtt_host": "mqtt-broker",
    "zigbee_mqtt_port": 1883,
    "enable_mqtt_egress": False,
    "mqtt_egress_topic_prefix": "bio-patrol/anomaly",
}

DEFAULT_BEDS = {
    "room_count": 14,
    "room_start": 101,
    "bed_numbers": [1],
    "beds": {},
}

DEFAULT_PATROL = {
    "beds_order": [],
}

DEFAULT_SCHEDULE = {
    "schedules": [],
}
