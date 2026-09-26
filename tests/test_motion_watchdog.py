"""The manual-slew dead-man switch.

A manual slew starts with one INDI command and stops only with a second one.
If the second is lost (Tailscale drops, phone sleeps, Bluetooth controller
dies) the mount keeps turning. These tests pin the three things that matter:
the mount stops by itself when the client goes quiet, it does NOT stop while
the client keeps confirming, and old clients that never send ttl_ms keep the
old behaviour.
"""
import asyncio

import pytest

from astroarch_bridge import motion_watchdog
from astroarch_bridge.motion_watchdog import MotionWatchdog, clamp_ttl
from astroarch_bridge.routes import _roles, mount


class _FakeIndi:
    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []

    async def send_switch(self, device, name, values):
        self.sent.append((device, name, dict(values)))


class _FakeBridge:
    def __init__(self):
        self.state = None
        self.indi = _FakeIndi()


@pytest.fixture
def bridge(monkeypatch):
    async def fake_resolve(state, role, override=None):
        return "EQMod Mount"
    monkeypatch.setattr(mount, "resolve_device", fake_resolve)
    monkeypatch.setattr(_roles, "resolve_device", fake_resolve)
    monkeypatch.setattr(mount, "_watchdog", MotionWatchdog())
    monkeypatch.setattr(motion_watchdog, "TTL_MIN_S", 0.05)
    return _FakeBridge()


def _moving(sent_item) -> bool:
    return any(sent_item[2].values())


def test_clamp_ttl():
    assert clamp_ttl(1000) == 1.0
    assert clamp_ttl(10) == motion_watchdog.TTL_MIN_S
    assert clamp_ttl(60_000) == motion_watchdog.TTL_MAX_S


async def test_stops_by_itself_when_client_goes_quiet(bridge):
    await mount.slew({"direction": "N", "active": True, "ttl_ms": 50}, bridge)
    assert len(bridge.indi.sent) == 1 and _moving(bridge.indi.sent[0])
    await asyncio.sleep(0.15)
    assert len(bridge.indi.sent) == 2
    dev, prop, values = bridge.indi.sent[1]
    assert prop == "TELESCOPE_MOTION_NS" and not any(values.values())


async def test_keeps_moving_while_client_confirms(bridge):
    for _ in range(6):
        await mount.slew({"direction": "E", "active": True, "ttl_ms": 80}, bridge)
        await asyncio.sleep(0.03)
    # Una sola partenza verso INDI, le ripetizioni spostano solo la scadenza.
    assert len(bridge.indi.sent) == 1
    assert bridge.indi.sent[0][1] == "TELESCOPE_MOTION_WE"
    await mount.slew({"direction": "E", "active": False, "ttl_ms": 80}, bridge)
    await asyncio.sleep(0.15)
    # Stop esplicito e nessun secondo stop dal watchdog disarmato.
    assert len(bridge.indi.sent) == 2 and not _moving(bridge.indi.sent[1])


async def test_confirmation_after_auto_stop_restarts(bridge):
    await mount.slew({"direction": "S", "active": True, "ttl_ms": 50}, bridge)
    await asyncio.sleep(0.12)  # un buco di rete: il bridge ferma
    await mount.slew({"direction": "S", "active": True, "ttl_ms": 50}, bridge)
    assert [_moving(s) for s in bridge.indi.sent] == [True, False, True]
    await mount.slew({"direction": "S", "active": False}, bridge)


async def test_direction_change_on_same_axis_is_sent(bridge):
    await mount.slew({"direction": "N", "active": True, "ttl_ms": 500}, bridge)
    await mount.slew({"direction": "S", "active": True, "ttl_ms": 500}, bridge)
    assert bridge.indi.sent[-1][2] == {"MOTION_NORTH": False, "MOTION_SOUTH": True}
    await mount.slew({"direction": "S", "active": False}, bridge)


async def test_axes_are_independent(bridge):
    await mount.slew({"direction": "N", "active": True, "ttl_ms": 60}, bridge)
    await mount.slew({"direction": "W", "active": True, "ttl_ms": 500}, bridge)
    await asyncio.sleep(0.15)
    stops = [s for s in bridge.indi.sent if not _moving(s)]
    assert [s[1] for s in stops] == ["TELESCOPE_MOTION_NS"]
    await mount.slew({"direction": "W", "active": False}, bridge)


async def test_old_clients_without_ttl_are_never_stopped(bridge):
    await mount.slew({"direction": "W", "active": True}, bridge)
    await asyncio.sleep(0.15)
    assert len(bridge.indi.sent) == 1 and _moving(bridge.indi.sent[0])
    assert mount._watchdog.armed("EQMod Mount", "WE") is None
