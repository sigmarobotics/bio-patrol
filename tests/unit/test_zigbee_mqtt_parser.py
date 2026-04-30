"""Unit tests for zigbee_mqtt.parse_zigbee_message — pure decoding, no network."""
from __future__ import annotations

import json

from services.zigbee_mqtt import parse_zigbee_message


def test_parse_device_joined_event():
    payload = json.dumps({
        "type": "device_joined",
        "data": {"ieee_address": "0x00158d0001abcd01", "friendly_name": "0x00158d0001abcd01"},
    })
    out = parse_zigbee_message("zigbee2mqtt/bridge/event", payload)
    assert out == {
        "type": "device_joined",
        "ieee_addr": "0x00158d0001abcd01",
        "friendly_name": "0x00158d0001abcd01",
    }


def test_parse_device_announce_event():
    payload = json.dumps({
        "type": "device_announce",
        "data": {"ieee_address": "0x00158d0001abcd02", "friendly_name": "demo_btn"},
    })
    out = parse_zigbee_message("zigbee2mqtt/bridge/event", payload)
    assert out["type"] == "device_announce"
    assert out["ieee_addr"] == "0x00158d0001abcd02"
    assert out["friendly_name"] == "demo_btn"


def test_parse_button_action_single():
    payload = json.dumps({
        "action": "single",
        "battery": 89,
        "linkquality": 220,
    })
    out = parse_zigbee_message("zigbee2mqtt/0x00158d0001abcd03", payload)
    assert out == {
        "type": "button_action",
        "ieee_addr": "0x00158d0001abcd03",
        "action": "single",
        "battery": 89,
        "linkquality": 220,
    }


def test_parse_other_bridge_topics_ignored():
    out = parse_zigbee_message("zigbee2mqtt/bridge/state", '{"state": "online"}')
    assert out is None


def test_parse_device_leave_event():
    payload = json.dumps({"type": "device_leave", "data": {"ieee_address": "0x00158d0001abcd99"}})
    out = parse_zigbee_message("zigbee2mqtt/bridge/event", payload)
    assert out == {"type": "device_leave", "ieee_addr": "0x00158d0001abcd99"}


def test_parse_unknown_bridge_event_type():
    payload = json.dumps({"type": "ota_update_finished", "data": {"ieee_address": "0x123"}})
    out = parse_zigbee_message("zigbee2mqtt/bridge/event", payload)
    assert out is None


def test_parse_device_message_without_action():
    out = parse_zigbee_message("zigbee2mqtt/0xabc", '{"battery": 90}')
    assert out is None


def test_parse_non_zigbee2mqtt_topic_ignored():
    out = parse_zigbee_message("homeassistant/sensor/x", '{"action": "single"}')
    assert out is None


def test_parse_invalid_json_returns_none():
    out = parse_zigbee_message("zigbee2mqtt/0xabc", "not json")
    assert out is None


def test_parse_non_dict_payload_returns_none():
    out = parse_zigbee_message("zigbee2mqtt/0xabc", "[1, 2, 3]")
    assert out is None


def test_device_joined_without_ieee_returns_none():
    payload = json.dumps({"type": "device_joined", "data": {}})
    assert parse_zigbee_message("zigbee2mqtt/bridge/event", payload) is None
