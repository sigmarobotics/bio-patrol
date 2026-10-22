"""IT-25 — the shared ZigbeeMQTT connection carries the whiteboard online LWT
and announces "online" right after each (re)connect."""
from __future__ import annotations

import asyncio

import aiomqtt

from services import zigbee_mqtt as zm


def _fake_client_cls(zig_holder, created):
    class _Client:
        def __init__(self, hostname, port=1883, **kwargs):
            self.hostname, self.port, self.kwargs = hostname, port, kwargs
            self.subscribed = []
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def subscribe(self, topic, *a, **k):
            self.subscribed.append(topic)

        @property
        def messages(self):
            async def _gen():
                # End the loop after one connect: no messages, stop requested.
                zig_holder[0]._stop.set()
                return
                yield  # pragma: no cover
            return _gen()

    return _Client


def _run_once(monkeypatch, **kwargs):
    created = []
    holder = [None]
    monkeypatch.setattr(zm.aiomqtt, "Client", _fake_client_cls(holder, created))
    zig = zm.ZigbeeMQTT(host="mqtt-broker", port=1883, **kwargs)
    holder[0] = zig
    asyncio.run(asyncio.wait_for(zig._run(), timeout=2))
    return zig, created


def test_default_has_no_will_and_no_hook(monkeypatch):
    _, created = _run_once(monkeypatch)
    (client,) = created
    assert client.kwargs.get("will") is None
    assert client.subscribed == [zm.ZigbeeMQTT.SUB_TOPIC]


def test_will_is_passed_and_on_connect_runs_after_subscribe(monkeypatch):
    will = aiomqtt.Will("bio-patrol/board/banqiao-1f/online", "0", qos=1, retain=True)
    seen = []

    async def on_connect(client):
        seen.append((client, list(client.subscribed)))

    _, created = _run_once(monkeypatch, will=will, on_connect=on_connect)
    (client,) = created
    assert client.kwargs["will"] is will
    assert seen == [(client, [zm.ZigbeeMQTT.SUB_TOPIC])]


def test_on_connect_error_does_not_break_the_loop(monkeypatch):
    async def on_connect(client):
        raise RuntimeError("hook broke")

    zig, created = _run_once(monkeypatch, on_connect=on_connect)
    # The loop went on to consume messages (which set stop) after the hook raised.
    assert len(created) == 1
    assert zig._stop.is_set()


class _LiveClient:
    def __init__(self, exc=None, delay=0.0):
        self.calls = []
        self.exc = exc
        self.delay = delay

    async def publish(self, topic, payload=None, qos=0, retain=False, **kw):
        self.calls.append((topic, payload, qos, retain))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc


def _stop_live(will, client, connected=True):
    zig = zm.ZigbeeMQTT(will=will)
    zig._client = client
    zig._connected = connected
    asyncio.run(zig.stop())
    return zig


def test_graceful_stop_publishes_the_will_payload():
    # A clean DISCONNECT makes the broker discard the LWT, so stop() sends it itself.
    will = aiomqtt.Will("bio-patrol/board/banqiao-1f/online", "0", qos=1, retain=True)
    client = _LiveClient()
    _stop_live(will, client)
    assert client.calls == [("bio-patrol/board/banqiao-1f/online", "0", 1, True)]


def test_stop_without_will_or_connection_publishes_nothing():
    client = _LiveClient()
    _stop_live(None, client)
    assert client.calls == []
    will = aiomqtt.Will("bio-patrol/board/banqiao-1f/online", "0", qos=1, retain=True)
    _stop_live(will, client, connected=False)
    assert client.calls == []


def test_stop_survives_a_failing_or_hanging_offline_publish(monkeypatch, caplog):
    monkeypatch.setattr(zm, "STOP_PUBLISH_TIMEOUT_S", 0.05)
    will = aiomqtt.Will("bio-patrol/board/banqiao-1f/online", "0", qos=1, retain=True)
    zig = _stop_live(will, _LiveClient(exc=aiomqtt.MqttError("gone")))
    assert zig.connected is False
    zig = _stop_live(will, _LiveClient(delay=1.0))
    assert zig.connected is False
    assert "offline" in caplog.text
