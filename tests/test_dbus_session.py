"""Choosing the DBus bus to talk to KStars on.

The tests build a fake `/proc` instead of depending on a live KStars, so
they also run on a development machine that is not Linux.
"""
import os

import pytest

from astroarch_bridge import dbus_session


@pytest.fixture(autouse=True)
def _clean_cache():
    dbus_session.reset_cache()
    yield
    dbus_session.reset_cache()


def _fake_proc(tmp_path, entries: dict[str, tuple[str, dict[str, str]]]):
    """Build a /proc-like tree: {pid: (comm, environ)}."""
    for pid, (comm, env) in entries.items():
        d = tmp_path / pid
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        raw = b"".join(f"{k}={v}".encode() + b"\x00" for k, v in env.items())
        (d / "environ").write_bytes(raw)
    return str(tmp_path)


def test_uses_the_bus_kstars_is_actually_on(tmp_path, monkeypatch):
    # The remote desktop case: KStars is on a private session bus, not on
    # the user manager's one.
    proc = _fake_proc(tmp_path, {
        "100": ("plasmashell", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}),
        "200": ("kstars", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-abc123"}),
    })
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    assert dbus_session.session_bus_address() == "unix:path=/tmp/dbus-abc123"


def test_falls_back_when_kstars_is_not_running(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, {"100": ("plasmashell", {})})
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert dbus_session.session_bus_address() == dbus_session.default_bus_address()


def test_falls_back_to_the_inherited_address_if_there_is_one(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, {})
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    assert dbus_session.session_bus_address() == "unix:path=/run/user/1000/bus"


def test_no_proc_filesystem_is_not_an_error(monkeypatch):
    # There is no /proc on macOS: the module must stay quiet and fall back.
    monkeypatch.setattr(dbus_session, "_PROC", "/proc-that-does-not-exist")
    assert dbus_session.kstars_bus_address() is None


def test_with_session_bus_overrides_the_inherited_value(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, {
        "200": ("kstars", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-abc123"}),
    })
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    env = dbus_session.with_session_bus({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                                         "PATH": "/usr/bin"})
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/dbus-abc123"
    # The rest of the environment passes through untouched.
    assert env["PATH"] == "/usr/bin"


def test_with_session_bus_starts_from_the_process_environment(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, {})
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    monkeypatch.setenv("ASTROARCH_MARKER", "present")
    env = dbus_session.with_session_bus()
    assert env["ASTROARCH_MARKER"] == "present"
    assert "DBUS_SESSION_BUS_ADDRESS" in env
    assert os.environ.get("ASTROARCH_MARKER") == "present"


def test_the_address_is_cached_briefly(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path, {
        "200": ("kstars", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-abc123"}),
    })
    monkeypatch.setattr(dbus_session, "_PROC", proc)
    calls = {"n": 0}
    real = dbus_session.kstars_bus_address

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(dbus_session, "kstars_bus_address", counting)
    dbus_session.session_bus_address()
    dbus_session.session_bus_address()
    assert calls["n"] == 1, "the second call must come from the cache"
