"""KStars must never be started as a side effect of reading Ekos' state.

The kstars package installs a D-Bus activation file, so any qdbus6 call to
org.kde.kstars made while KStars is closed launches it. The app polls the
Ekos state every 3 s: before this guard, opening the app started KStars,
and closing KStars got it reopened within seconds (reported 18/11/2026).

The tests use a fake /proc and a trap in place of create_subprocess_exec:
with KStars off, not a single qdbus6 may be spawned.
"""
import asyncio

import pytest

from astroarch_bridge import dbus_session, ekos_dbus
from astroarch_bridge.routes import align, capture_ekos, guide, skymap, system


def _proc(tmp_path, entries):
    """Fake /proc: {pid: (comm, state)}."""
    for pid, (comm, state) in entries.items():
        d = tmp_path / pid
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "stat").write_text(f"{pid} ({comm}) {state} 1 1 1 0")
        (d / "environ").write_bytes(b"")
    return str(tmp_path)


class _FakeProc:
    def __init__(self, out: str, rc: int = 0):
        self._out = out
        self.returncode = rc

    async def communicate(self):
        return self._out.encode(), b""

    def kill(self):
        pass


@pytest.fixture(autouse=True)
def _clean():
    dbus_session.reset_cache()
    ekos_dbus._cache_value, ekos_dbus._cache_ts = None, 0.0
    yield
    dbus_session.reset_cache()


@pytest.fixture
def trap(monkeypatch):
    """Fails the test if any subprocess is spawned."""
    spawned = []

    async def boom(*args, **kw):
        spawned.append(args)
        raise AssertionError(f"processo lanciato con KStars spento: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    return spawned


@pytest.fixture
def kstars_off(tmp_path, monkeypatch):
    monkeypatch.setattr(dbus_session, "_PROC", _proc(tmp_path, {"10": ("plasmashell", "S")}))


def _bus(monkeypatch, name_has_owner: str, answer: str = "2"):
    """KStars process present; NameHasOwner answers [name_has_owner]."""
    seen = []

    async def fake(*args, **kw):
        seen.append(args)
        if "org.freedesktop.DBus.NameHasOwner" in args:
            return _FakeProc(name_has_owner)
        return _FakeProc(answer)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return seen


# --- riconoscimento del processo -------------------------------------------

@pytest.mark.parametrize("entries,expected", [
    ({"1": ("plasmashell", "S")}, False),
    ({"1": ("kstars", "S")}, True),
    ({"1": ("kstars.bin", "R")}, True),
    ({"1": ("kstars", "Z")}, False),          # zombie: non risponde a nessuno
    ({"1": ("kstarsfoo", "S")}, False),       # nome esatto, non prefisso
])
def test_process_detection(tmp_path, monkeypatch, entries, expected):
    monkeypatch.setattr(dbus_session, "_PROC", _proc(tmp_path, entries))
    assert dbus_session.kstars_process_running() is expected


# --- con KStars spento nessun qdbus6, da nessuna delle sei funzioni ----------

@pytest.mark.anyio
async def test_capture_dbus_call_does_not_spawn(kstars_off, trap):
    rc, out = await capture_ekos._dbus_call(
        "org.kde.kstars", "/KStars/Ekos", "org.kde.kstars.Ekos.ekosStatus")
    assert (rc, out) == (-1, dbus_session.KSTARS_NOT_RUNNING)
    assert trap == []


@pytest.mark.anyio
async def test_guide_qdbus_does_not_spawn(kstars_off, trap):
    rc, _ = await guide._qdbus_call("org.kde.kstars", "/KStars/Ekos/Guide", "x")
    assert rc == -1 and trap == []


@pytest.mark.anyio
async def test_ekos_dbus_does_not_spawn_and_does_not_cache_none(kstars_off, trap):
    assert await ekos_dbus.guide_camera() is None
    # «non chiedibile adesso» non e' «nessuna camera»: niente cache da 30 s
    assert ekos_dbus._cache_ts == 0.0
    assert trap == []


@pytest.mark.anyio
async def test_skymap_does_not_spawn(kstars_off, trap):
    rc, _ = await skymap._kstars_dbus("getFocusInformationXML")
    assert rc == -1 and trap == []


@pytest.mark.anyio
async def test_align_literal_does_not_spawn(kstars_off, trap):
    assert await align._dbus_call_literal("/KStars/Ekos/Align", "x") == ""
    assert trap == []


@pytest.mark.anyio
async def test_ekos_state_answers_inactive_without_dbus(kstars_off, trap):
    class _State:
        async def list_devices(self):
            return []

    class _Bridge:
        state = _State()

    out = await system.ekos_state(bridge=_Bridge())
    assert out["active"] == "inactive"
    assert out["kstars_running"] is False
    assert trap == []


# --- con KStars presente ----------------------------------------------------

@pytest.mark.anyio
async def test_name_not_yet_on_the_bus_blocks(tmp_path, monkeypatch):
    # Subito dopo l'avvio: il processo c'e', il nome non ancora. Una chiamata
    # adesso farebbe partire un secondo KStars.
    monkeypatch.setattr(dbus_session, "_PROC", _proc(tmp_path, {"5": ("kstars", "S")}))
    seen = _bus(monkeypatch, "false")
    rc, _ = await capture_ekos._dbus_call("org.kde.kstars", "/KStars/Ekos", "x")
    assert rc == -1
    assert all("org.freedesktop.DBus.NameHasOwner" in a for a in seen), seen


@pytest.mark.anyio
async def test_kstars_on_the_bus_lets_the_call_through(tmp_path, monkeypatch):
    monkeypatch.setattr(dbus_session, "_PROC", _proc(tmp_path, {"5": ("kstars", "S")}))
    seen = _bus(monkeypatch, "true", answer="2")
    rc, out = await capture_ekos._dbus_call(
        "org.kde.kstars", "/KStars/Ekos", "org.kde.kstars.Ekos.ekosStatus")
    assert (rc, out) == (0, "2")
    assert any("org.kde.kstars.Ekos.ekosStatus" in a for a in seen)


@pytest.mark.anyio
async def test_a_remembered_yes_is_ignored_once_kstars_is_gone(tmp_path, monkeypatch):
    # Il difetto da non reintrodurre: dopo la chiusura di KStars, un «si'»
    # rimasto in memoria lascerebbe passare la chiamata che lo riavvia.
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(dbus_session, "_PROC", _proc(root, {"5": ("kstars", "S")}))
    _bus(monkeypatch, "true")
    assert await dbus_session.kstars_on_bus() is True
    (root / "5" / "comm").write_text("bash\n")          # KStars chiuso
    assert await dbus_session.kstars_on_bus() is False


@pytest.mark.anyio
async def test_calls_to_other_services_are_not_blocked(kstars_off, monkeypatch):
    seen = _bus(monkeypatch, "false", answer="ok")
    rc, out = await capture_ekos._dbus_call("org.freedesktop.Notifications", "/", "x")
    assert (rc, out) == (0, "ok") and len(seen) == 1
