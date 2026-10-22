"""IT-25 — 板橋榮家 1F 電子白板：每床結果定案即 publish 一筆 retained QoS1 訊息到
``bio-patrol/board/<site>/bed/<bed>``，廠商 server 以唯讀帳號訂閱。

These tests pin the wire contract (topic, the five payload fields, the five
status values) and the one rule that matters most on site: the publisher never
blocks or breaks a patrol — every failure is a WARN and a False return.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import aiomqtt
import pytest

from services.integration import board_publisher


@pytest.fixture(autouse=True)
def _reset_module_state():
    board_publisher.configure(None)
    board_publisher._warned_no_site = False
    yield
    board_publisher.configure(None)
    board_publisher._warned_no_site = False


@pytest.fixture
def cfg(monkeypatch):
    settings = {
        "whiteboard_enabled": True,
        "whiteboard_site_id": "banqiao-1f",
        "whiteboard_bed_id_map": {},
        "zigbee_mqtt_host": "mqtt-broker",
        "zigbee_mqtt_port": 1883,
    }
    monkeypatch.setattr(board_publisher, "get_runtime_settings", lambda: settings)
    return settings


class _Shared:
    """Stands in for the long-lived ZigbeeMQTT (publish(topic, dict, qos, retain) -> bool)."""

    def __init__(self, result=True, exc=None, delay=0.0):
        self.calls = []
        self.result = result
        self.exc = exc
        self.delay = delay

    async def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def _pub(*args, **kwargs):
    return asyncio.run(board_publisher.publish_bed_status(*args, **kwargs))


# ── classify_scan ──────────────────────────────────────────────────────

@pytest.mark.parametrize("status,is_valid,expected", [
    (4, True, "normal"),
    (None, 1, "normal"),
    (2, False, "restless"),
    ("2", 0, "restless"),
    (0, False, "absent"),
    ("0", None, "absent"),
    ("N/A", False, "unreachable"),
    (None, False, "no_reading"),
    (4, False, "no_reading"),   # status 4 but bpm/rpm 0 → not valid
    (3, 0, "no_reading"),
    ("", False, "no_reading"),
])
def test_classify_scan_five_values(status, is_valid, expected):
    assert board_publisher.classify_scan(status, is_valid) == expected


# ── gates ──────────────────────────────────────────────────────────────

def test_disabled_publishes_nothing(cfg):
    cfg["whiteboard_enabled"] = False
    shared = _Shared()
    board_publisher.configure(shared)
    assert _pub("101-1", "normal", 58, 13) is False
    assert shared.calls == []


def test_disabled_by_default_without_key(monkeypatch):
    monkeypatch.setattr(board_publisher, "get_runtime_settings", lambda: {})
    shared = _Shared()
    board_publisher.configure(shared)
    assert _pub("101-1", "normal", 58, 13) is False
    assert shared.calls == []


def test_demo_never_publishes(cfg):
    shared = _Shared()
    board_publisher.configure(shared)
    assert _pub("101-1", "normal", 58, 13, demo=True) is False
    assert shared.calls == []


def test_empty_site_warns_once(cfg, caplog):
    cfg["whiteboard_site_id"] = "  "
    shared = _Shared()
    board_publisher.configure(shared)
    with caplog.at_level(logging.WARNING, logger=board_publisher.logger.name):
        assert _pub("101-1", "normal", 58, 13) is False
        assert _pub("101-2", "normal", 58, 13) is False
    assert shared.calls == []
    assert len([r for r in caplog.records if "whiteboard_site_id" in r.getMessage()]) == 1


# ── payload / topic ────────────────────────────────────────────────────

def test_payload_has_exactly_five_fields(cfg):
    shared = _Shared()
    board_publisher.configure(shared)
    assert _pub("101-1", "normal", 58, 13) is True
    (topic, payload, qos, retain), = shared.calls
    assert topic == "bio-patrol/board/banqiao-1f/bed/101-1"
    assert qos == 1 and retain is True
    assert set(payload) == {"bed", "time", "bpm", "rpm", "status"}
    assert payload["bed"] == "101-1"
    assert payload["bpm"] == 58 and type(payload["bpm"]) is int
    assert payload["rpm"] == 13 and type(payload["rpm"]) is int
    assert payload["status"] == "normal"
    assert datetime.fromisoformat(payload["time"]).tzinfo is not None
    json.dumps(payload)  # wire-serialisable


def test_explicit_ts_is_used(cfg):
    shared = _Shared()
    board_publisher.configure(shared)
    _pub("101-1", "normal", 58, 13, ts="2026-10-13T22:41:03+08:00")
    assert shared.calls[0][1]["time"] == "2026-10-13T22:41:03+08:00"


def test_restless_keeps_values_float_becomes_int(cfg):
    shared = _Shared()
    board_publisher.configure(shared)
    _pub("101-2", "restless", 74.0, 19.6)
    payload = shared.calls[0][1]
    assert payload["status"] == "restless"
    assert payload["bpm"] == 74 and type(payload["bpm"]) is int
    assert payload["rpm"] == 20 and type(payload["rpm"]) is int


@pytest.mark.parametrize("status", ["absent", "unreachable", "no_reading"])
def test_non_reading_statuses_send_null_vitals(cfg, status):
    shared = _Shared()
    board_publisher.configure(shared)
    _pub("101-3", status, 70, 15)
    payload = shared.calls[0][1]
    assert payload["status"] == status
    assert payload["bpm"] is None and payload["rpm"] is None


def test_bed_id_map_rewrites_topic_and_bed(cfg):
    cfg["whiteboard_bed_id_map"] = {"101-1": "A-01"}
    shared = _Shared()
    board_publisher.configure(shared)
    _pub("101-1", "normal", 58, 13)
    _pub("101-2", "normal", 60, 14)
    assert shared.calls[0][0] == "bio-patrol/board/banqiao-1f/bed/A-01"
    assert shared.calls[0][1]["bed"] == "A-01"
    assert shared.calls[1][0] == "bio-patrol/board/banqiao-1f/bed/101-2"
    assert shared.calls[1][1]["bed"] == "101-2"


# ── never raise, never block ───────────────────────────────────────────

def test_shared_client_not_connected_warns(cfg, caplog):
    board_publisher.configure(_Shared(result=False))
    with caplog.at_level(logging.WARNING, logger=board_publisher.logger.name):
        assert _pub("101-1", "normal", 58, 13) is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_shared_client_exception_is_swallowed(cfg, caplog):
    board_publisher.configure(_Shared(exc=aiomqtt.MqttError("boom")))
    with caplog.at_level(logging.WARNING, logger=board_publisher.logger.name):
        assert _pub("101-1", "normal", 58, 13) is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_shared_client_unexpected_exception_is_swallowed(cfg):
    board_publisher.configure(_Shared(exc=RuntimeError("weird")))
    assert _pub("101-1", "normal", 58, 13) is False


def test_publish_times_out(cfg, monkeypatch, caplog):
    monkeypatch.setattr(board_publisher, "PUBLISH_TIMEOUT_S", 0.05)
    board_publisher.configure(_Shared(delay=5.0))
    with caplog.at_level(logging.WARNING, logger=board_publisher.logger.name):
        assert _pub("101-1", "normal", 58, 13) is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_timeout_default_is_five_seconds():
    assert board_publisher.PUBLISH_TIMEOUT_S == 5.0


class _FakeAioClient:
    """Short-lived aiomqtt.Client stand-in for the zigbee-disabled fallback."""
    instances = []
    fail_with = None

    def __init__(self, hostname, port=1883, **kwargs):
        self.hostname, self.port, self.kwargs = hostname, port, kwargs
        self.published = []
        _FakeAioClient.instances.append(self)

    async def __aenter__(self):
        if _FakeAioClient.fail_with:
            raise _FakeAioClient.fail_with
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload=None, qos=0, retain=False, **kwargs):
        self.published.append((topic, payload, qos, retain))


@pytest.fixture
def fake_aio(monkeypatch):
    _FakeAioClient.instances = []
    _FakeAioClient.fail_with = None
    monkeypatch.setattr(board_publisher.aiomqtt, "Client", _FakeAioClient)
    return _FakeAioClient


def test_fallback_short_lived_client(cfg, fake_aio):
    cfg["zigbee_mqtt_host"] = "broker-x"
    cfg["zigbee_mqtt_port"] = 1999
    assert _pub("101-1", "normal", 58, 13) is True
    (client,) = fake_aio.instances
    assert (client.hostname, client.port) == ("broker-x", 1999)
    (topic, wire, qos, retain), = client.published
    assert topic == "bio-patrol/board/banqiao-1f/bed/101-1"
    assert qos == 1 and retain is True
    assert json.loads(wire) == {
        "bed": "101-1", "time": json.loads(wire)["time"],
        "bpm": 58, "rpm": 13, "status": "normal",
    }


def test_fallback_connect_failure_is_swallowed(cfg, fake_aio, caplog):
    fake_aio.fail_with = aiomqtt.MqttError("refused")
    with caplog.at_level(logging.WARNING, logger=board_publisher.logger.name):
        assert _pub("101-1", "normal", 58, 13) is False
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ── online / LWT ───────────────────────────────────────────────────────

def test_will_none_when_disabled(cfg):
    cfg["whiteboard_enabled"] = False
    assert board_publisher.will() is None


def test_will_none_without_site(cfg):
    cfg["whiteboard_site_id"] = ""
    assert board_publisher.will() is None


def test_will_is_retained_zero(cfg):
    w = board_publisher.will()
    assert isinstance(w, aiomqtt.Will)
    assert w.topic == "bio-patrol/board/banqiao-1f/online"
    assert w.payload == "0"
    assert w.qos == 1 and w.retain is True


class _RawClient:
    def __init__(self, exc=None):
        self.published = []
        self.exc = exc

    async def publish(self, topic, payload=None, qos=0, retain=False, **kwargs):
        self.published.append((topic, payload, qos, retain))
        if self.exc:
            raise self.exc


def test_announce_online_publishes_retained_one(cfg):
    raw = _RawClient()
    asyncio.run(board_publisher.announce_online(raw))
    assert raw.published == [("bio-patrol/board/banqiao-1f/online", "1", 1, True)]


def test_announce_online_swallows_errors(cfg):
    raw = _RawClient(exc=aiomqtt.MqttError("gone"))
    asyncio.run(board_publisher.announce_online(raw))  # must not raise
    assert len(raw.published) == 1


def test_announce_online_skips_when_disabled(cfg):
    cfg["whiteboard_enabled"] = False
    raw = _RawClient()
    asyncio.run(board_publisher.announce_online(raw))
    assert raw.published == []


# ── shipped defaults ───────────────────────────────────────────────────

def test_shipped_defaults_keep_the_board_off(monkeypatch):
    # Every site runs the same image: only an explicit settings.json turns it on.
    from settings.defaults import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["whiteboard_enabled"] is False
    assert DEFAULT_SETTINGS["whiteboard_site_id"] == ""
    assert DEFAULT_SETTINGS["whiteboard_bed_id_map"] == {}
    monkeypatch.setattr(board_publisher, "get_runtime_settings", lambda: dict(DEFAULT_SETTINGS))
    shared = _Shared()
    board_publisher.configure(shared)
    assert _pub("101-1", "normal", 58, 13) is False
    assert board_publisher.will() is None
    assert shared.calls == []
