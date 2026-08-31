"""External notifications: parsing, UDP listener, history, REST route.

Mistakes prevented:
- A malformed or non-UTF-8 datagram bringing the listener down
- A verbose sender growing the in-memory history without bound
- A notification lost by a reconnecting client (it must be in the snapshot)
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
from fastapi.testclient import TestClient

from astroarch_bridge import config
from astroarch_bridge.notify.listener import (
    MAX_MESSAGE,
    UdpNotifyListener,
    parse_datagram,
)
from astroarch_bridge.state import StateManager


# --- parse_datagram -------------------------------------------------------

def test_parse_plain_text():
    """astro_monitor sends plain text: it must work without JSON."""
    n = parse_datagram(b"KStars has stopped.", peer="127.0.0.1")
    assert n["message"] == "KStars has stopped."
    assert n["level"] == "info"
    assert n["title"] == ""
    # With no source field it falls back to the sender's address.
    assert n["source"] == "127.0.0.1"


def test_parse_json():
    payload = json.dumps({
        "title": "KStars", "message": "process gone",
        "level": "error", "source": "astro_monitor",
    }).encode()
    n = parse_datagram(payload)
    assert n["title"] == "KStars"
    assert n["message"] == "process gone"
    assert n["level"] == "error"
    assert n["source"] == "astro_monitor"


def test_parse_unknown_level_falls_back_to_info():
    n = parse_datagram(json.dumps({"message": "x", "level": "critical"}).encode())
    assert n["level"] == "info"


def test_parse_broken_json_is_treated_as_text():
    """A payload starting with '{' but not valid JSON stays readable."""
    n = parse_datagram(b'{not really json')
    assert n["message"] == "{not really json"


def test_parse_rejects_empty_and_meaningless():
    assert parse_datagram(b"") is None
    assert parse_datagram(b"   ") is None
    # Valid JSON, but with nothing to show
    assert parse_datagram(b'{"level":"info"}') is None


def test_parse_survives_non_utf8():
    n = parse_datagram(b"\xff\xfe bad bytes")
    assert n is not None and n["message"]


def test_parse_truncates_long_message():
    n = parse_datagram(b"A" * 50_000)
    assert len(n["message"]) == MAX_MESSAGE


# --- UDP listener ---------------------------------------------------------

async def test_udp_listener_delivers_to_callback():
    received: list[dict] = []
    done = asyncio.Event()

    async def _on_notification(payload: dict) -> None:
        received.append(payload)
        done.set()

    listener = UdpNotifyListener("127.0.0.1", 0, _on_notification)
    await listener.start()
    try:
        assert listener.running
        port = listener.local_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"KStars has stopped.", ("127.0.0.1", port))
            await asyncio.wait_for(done.wait(), timeout=5.0)
        finally:
            sock.close()
    finally:
        await listener.stop()

    assert len(received) == 1
    assert received[0]["message"] == "KStars has stopped."
    assert not listener.running


async def test_udp_listener_survives_garbage():
    """A useless datagram must not stop the good ones from arriving."""
    good = asyncio.Event()

    async def _on_notification(payload: dict) -> None:
        if payload["message"] == "real one":
            good.set()

    listener = UdpNotifyListener("127.0.0.1", 0, _on_notification)
    await listener.start()
    try:
        port = listener.local_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"", ("127.0.0.1", port))
            sock.sendto(b"\x00\x01\x02", ("127.0.0.1", port))
            sock.sendto(b"real one", ("127.0.0.1", port))
            await asyncio.wait_for(good.wait(), timeout=5.0)
        finally:
            sock.close()
    finally:
        await listener.stop()
    assert listener.running is False


async def test_bind_failure_does_not_raise():
    """Port taken: the bridge must start anyway, just without notifications."""
    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", 0))
    port = busy.getsockname()[1]
    try:
        listener = UdpNotifyListener("127.0.0.1", port, lambda p: asyncio.sleep(0))
        await listener.start()  # must not raise
        assert not listener.running
    finally:
        busy.close()


# --- StateManager ---------------------------------------------------------

async def test_notification_is_broadcast_and_stored():
    state = StateManager()
    events: list[dict] = []

    async def _listener(ev: dict) -> None:
        events.append(ev)

    state.add_listener(_listener)
    await state.handle_notification({"message": "unsafe weather",
                                     "level": "warning"})

    assert events and events[0]["type"] == "notification"
    assert events[0]["message"] == "unsafe weather"
    assert events[0]["level"] == "warning"
    assert events[0]["ts"] > 0

    snap = await state.snapshot()
    assert len(snap["notifications"]) == 1


async def test_notification_history_is_capped():
    state = StateManager()
    for i in range(60):
        await state.handle_notification({"message": f"n{i}"})
    items = await state.recent_notifications(limit=50)
    assert len(items) == 50
    # The most recent ones are the ones kept.
    assert items[-1]["message"] == "n59"


# --- REST route -----------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTROARCH_TOKEN", "test-token")
    monkeypatch.setenv("ASTROARCH_IMAGES_DIR", str(tmp_path / "images"))
    config.reset_settings_cache()
    from astroarch_bridge.app import create_app
    c = TestClient(create_app())
    yield c
    c.close()
    config.reset_settings_cache()


AUTH = {"Authorization": "Bearer test-token"}


def test_rest_push_and_recent(client):
    r = client.post("/api/notify", headers=AUTH,
                    json={"message": "sequenza completata", "level": "info",
                          "source": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["notification"]["message"] == "sequenza completata"

    r = client.get("/api/notify/recent", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["source"] == "test"


def test_rest_push_requires_content(client):
    r = client.post("/api/notify", headers=AUTH, json={"level": "error"})
    assert r.status_code == 400


def test_rest_push_requires_token(client):
    r = client.post("/api/notify", json={"message": "x"})
    assert r.status_code == 401


def test_rest_clear_empties_the_history(client):
    client.post("/api/notify", headers=AUTH, json={"message": "star lost"})
    client.post("/api/notify", headers=AUTH, json={"message": "unsafe weather"})

    r = client.delete("/api/notify", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 2

    # Cleared at the source, so a client reconnecting finds nothing either.
    assert client.get("/api/notify/recent", headers=AUTH).json()["count"] == 0


def test_rest_clear_requires_token(client):
    assert client.delete("/api/notify").status_code == 401
