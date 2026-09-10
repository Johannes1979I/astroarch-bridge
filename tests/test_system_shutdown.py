"""Refusing to power off while the observatory is still working.

Cutting power to a running Linux is what corrupts the files written most
often -- which here are the configuration files themselves. So the bridge
grew a shutdown endpoint; these tests pin the part that can hurt someone:
it must refuse while a session is live, and it must NOT refuse merely
because Ekos is switched off and nothing can be read.

That second half is the subtle one. "The mount reports it is not parked"
and "I cannot reach the mount at all" look alike in code and mean the
opposite in practice, and a shutdown that blocks on the unreadable case
would be permanently unusable on an idle observatory.
"""
import pytest

from astroarch_bridge.routes import _roles, capture_ekos, system


class _FakeState:
    def __init__(self, props: dict[str, dict] | None = None):
        self._props = props or {}

    async def get_property(self, device: str, name: str):
        return self._props.get(name)

    async def list_devices(self):
        return ["EQMod Mount"]


class _FakePhd2:
    def __init__(self, app_state: str | None = None):
        self.live = {"app_state": app_state} if app_state else {}


class _FakeBridge:
    def __init__(self, *, props=None, app_state=None):
        self.state = _FakeState(props)
        self.phd2 = _FakePhd2(app_state)


def _park_property(parked: bool) -> dict:
    """TELESCOPE_PARK as INDI actually shapes it."""
    return {"elements": [{"name": "PARK", "value": parked},
                         {"name": "UNPARK", "value": not parked}]}


@pytest.fixture
def mount_present(monkeypatch):
    """A resolvable mount. Without this every lookup raises and blocks nothing."""
    async def fake_resolve(state, role, override=None):
        return "EQMod Mount"
    monkeypatch.setattr(_roles, "resolve_device", fake_resolve)


@pytest.fixture
def no_capture(monkeypatch):
    """Ekos answers, and says no job is active (-1)."""
    async def fake(service, path, method, *args, **kw):
        if method.endswith("getActiveJobID"):
            return 0, "-1"
        return 1, ""
    monkeypatch.setattr(capture_ekos, "_dbus_call", fake)


@pytest.mark.anyio
async def test_unparked_mount_blocks(mount_present, no_capture):
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(False)})
    blockers = await system._shutdown_blockers(bridge)
    assert [b["code"] for b in blockers] == ["mount_unparked"]


@pytest.mark.anyio
async def test_parked_mount_does_not_block(mount_present, no_capture):
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(True)})
    assert await system._shutdown_blockers(bridge) == []


@pytest.mark.anyio
async def test_running_capture_blocks(mount_present, monkeypatch):
    async def fake(service, path, method, *args, **kw):
        if method.endswith("getActiveJobID"):
            return 0, "3"
        return 1, ""
    monkeypatch.setattr(capture_ekos, "_dbus_call", fake)
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(True)})
    blockers = await system._shutdown_blockers(bridge)
    assert [b["code"] for b in blockers] == ["capture_running"]
    assert blockers[0]["job_id"] == 3


@pytest.mark.anyio
@pytest.mark.parametrize("state", sorted(system._PHD2_BUSY_STATES))
async def test_active_guiding_blocks(mount_present, no_capture, state):
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(True)},
                         app_state=state)
    blockers = await system._shutdown_blockers(bridge)
    assert [b["code"] for b in blockers] == ["guiding"]


@pytest.mark.anyio
async def test_stopped_guiding_does_not_block(mount_present, no_capture):
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(True)},
                         app_state="Stopped")
    assert await system._shutdown_blockers(bridge) == []


@pytest.mark.anyio
async def test_silent_observatory_does_not_block(monkeypatch):
    """Ekos off: no mount to resolve, no DBus, no PHD2 cache.

    Everything raises or returns nothing, and the answer must still be
    "go ahead" -- an idle observatory is exactly when you shut down.
    """
    async def boom(*a, **kw):
        raise RuntimeError("INDI not running")
    monkeypatch.setattr(_roles, "resolve_device", boom)
    monkeypatch.setattr(capture_ekos, "_dbus_call", boom)

    class _NoPhd2:
        live = None

    class _Bridge:
        state = _FakeState()
        phd2 = _NoPhd2()

    assert await system._shutdown_blockers(_Bridge()) == []


@pytest.mark.anyio
async def test_all_three_blockers_are_reported_together(mount_present, monkeypatch):
    """The user gets the whole list, not just the first thing that fails."""
    async def fake(service, path, method, *args, **kw):
        if method.endswith("getActiveJobID"):
            return 0, "0"
        return 1, ""
    monkeypatch.setattr(capture_ekos, "_dbus_call", fake)
    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(False)},
                         app_state="Guiding")
    codes = [b["code"] for b in await system._shutdown_blockers(bridge)]
    assert codes == ["mount_unparked", "capture_running", "guiding"]


@pytest.mark.anyio
async def test_endpoint_refuses_with_409_and_lists_reasons(mount_present, no_capture):
    from fastapi import BackgroundTasks, HTTPException

    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(False)})
    with pytest.raises(HTTPException) as excinfo:
        await system.shutdown(background=BackgroundTasks(), payload={},
                              bridge=bridge)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["blockers"][0]["code"] == "mount_unparked"


@pytest.mark.anyio
async def test_force_overrides_and_still_reports_what_was_overridden(
        mount_present, no_capture):
    """force=true must not hide the risk it just walked past."""
    from fastapi import BackgroundTasks

    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(False)})
    background = BackgroundTasks()
    out = await system.shutdown(background=background,
                                payload={"force": True}, bridge=bridge)
    assert out["ok"] is True
    assert out["forced"] is True
    assert [b["code"] for b in out["blockers"]] == ["mount_unparked"]
    assert len(background.tasks) == 1


@pytest.mark.anyio
async def test_bad_mode_is_rejected(mount_present, no_capture):
    from fastapi import BackgroundTasks, HTTPException

    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(True)})
    with pytest.raises(HTTPException) as excinfo:
        await system.shutdown(background=BackgroundTasks(),
                              payload={"mode": "halt"}, bridge=bridge)
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_reboot_endpoint_reuses_the_same_guard(mount_present, no_capture):
    from fastapi import BackgroundTasks, HTTPException

    bridge = _FakeBridge(props={"TELESCOPE_PARK": _park_property(False)})
    with pytest.raises(HTTPException) as excinfo:
        await system.reboot(background=BackgroundTasks(), payload={},
                            bridge=bridge)
    assert excinfo.value.status_code == 409

    out = await system.reboot(background=BackgroundTasks(),
                              payload={"force": True}, bridge=bridge)
    assert out["mode"] == "reboot"


# ---------------------------------------------------------------------------
# Attraverso FastAPI, non chiamando la funzione a mano: e' l'unico modo di
# provare che la rotta e' registrata, che il token la protegge e che il body
# arriva davvero come dict. _graceful_close_and_power e' sostituito, altrimenti
# la suite spegnerebbe il computer di chi la lancia.
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from astroarch_bridge.app import create_app

    fired: list[str] = []

    async def fake_power(mode: str) -> None:
        fired.append(mode)

    monkeypatch.setattr(system, "_graceful_close_and_power", fake_power)

    async def no_blockers(bridge):
        return []
    monkeypatch.setattr(system, "_shutdown_blockers", no_blockers)

    c = TestClient(create_app())
    c.fired = fired
    return c


def test_route_is_registered_and_needs_the_token(client):
    assert client.post("/api/system/shutdown", json={}).status_code == 401


def test_shutdown_returns_immediately_and_schedules_the_poweroff(client):
    r = client.post("/api/system/shutdown", json={}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["mode"] == "poweroff"
    assert client.fired == ["poweroff"]


def test_reboot_route_asks_for_a_reboot(client):
    r = client.post("/api/system/reboot", json={}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["mode"] == "reboot"
    assert client.fired == ["reboot"]


def test_shutdown_check_is_a_plain_read(client):
    r = client.get("/api/system/shutdown_check", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"safe": True, "blockers": []}
    assert client.fired == []
