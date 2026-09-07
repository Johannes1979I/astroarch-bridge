"""Find the DBus session bus KStars is actually talking on.

The bridge drives Ekos by running `qdbus6` and `dbus-monitor`, and both
need the address of the right session bus. Until now that address was
assumed: `unix:path=/run/user/<uid>/bus`, the bus of the user's systemd
manager. The assumption holds as long as KStars is started inside that
session — by the bridge itself, for instance — but it breaks in a
scenario that is anything but rare on AstroArch: the remote desktop.

xrdp opens a separate X session (`DISPLAY=:10`) whose startup script
creates a session bus of its own, typically `unix:path=/tmp/dbus-XXXXXX`.
KStars, PHD2 and indiserver launched from there register their services
on that bus. The bridge, running as a user service, keeps querying
`/run/user/<uid>/bus`, where `org.kde.kstars` does not exist:

    Service 'org.kde.kstars' does not exist.

and with it goes everything that runs through Ekos — alignment, focus,
sequences, the internal guider — while the user is looking at a perfectly
healthy KStars. Since tablet-over-RDP is one of the normal ways to drive
AstroArch, it is worth asking the one that knows for certain: the running
KStars process.

The technique is the same one `routes/system.py::_user_graphical_env`
already uses for DISPLAY and XAUTHORITY, that is, reading
`/proc/<pid>/environ`. Here, though, the process inspected is not just
any desktop process but the very peer of the calls, so the answer is
exact rather than heuristic.

When KStars is not running, or its environment cannot be read, the
address falls back to the one used before: behaviour stays as it was.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

_PROC = "/proc"
_TARGET = "kstars"
_TARGET_BYTES = _TARGET.encode("ascii")
_VAR = "DBUS_SESSION_BUS_ADDRESS"

# The address is needed on every DBus call, and scanning /proc for each of
# them would be wasteful while the app polls. The cache is deliberately
# very short: KStars can be closed and reopened in another session, and a
# stale address is worse than no cache at all.
_CACHE_TTL = 5.0
_cache: tuple[float, str | None] = (0.0, None)


def default_bus_address() -> str:
    """The historically assumed address: the user manager's bus."""
    return f"unix:path=/run/user/{os.getuid()}/bus"


def _read_environ(pid: str) -> dict[str, str]:
    try:
        with open(f"{_PROC}/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        # Another user's process is not readable (PermissionError is an
        # OSError): not an error, just a candidate to skip.
        return {}
    env: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry or b"=" not in entry:
            continue
        k, _, v = entry.partition(b"=")
        env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def kstars_bus_address() -> str | None:
    """The bus address KStars runs on, if KStars is running at all."""
    try:
        pids = [p for p in os.listdir(_PROC) if p.isdigit()]
    except OSError:
        # No /proc: we are not on Linux (during development on macOS, say).
        return None
    for pid in pids:
        try:
            # In binario: `comm` e' quello che il processo ha scritto in
            # /proc/self/comm, non per forza UTF-8, e in modalita' testo un
            # solo processo con un nome strano farebbe saltare la lettura
            # dell'indirizzo per tutti gli altri.
            with open(f"{_PROC}/{pid}/comm", "rb") as f:
                if f.read().strip() != _TARGET_BYTES:
                    continue
        except OSError:
            continue
        addr = _read_environ(pid).get(_VAR)
        if addr:
            return addr
    return None


def session_bus_address() -> str:
    """The bus to talk to KStars on, falling back to the assumption."""
    global _cache
    now = time.monotonic()
    stamp, cached = _cache
    if cached is not None and now - stamp < _CACHE_TTL:
        return cached
    addr = kstars_bus_address()
    if addr is None:
        addr = os.environ.get(_VAR) or default_bus_address()
    elif addr != os.environ.get(_VAR):
        # INFO, non DEBUG: e' esattamente il caso che il manutentore non
        # puo' vedere in nessun altro modo, e succede una volta ogni TTL.
        log.info("KStars sta su un altro bus di sessione, uso il suo: %s", addr)
    _cache = (now, addr)
    return addr


def with_session_bus(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with the right `DBUS_SESSION_BUS_ADDRESS`.

    It overwrites the variable rather than honouring an existing one: if
    KStars sits on another bus, the inherited value is of no use.
    """
    out = dict(env) if env is not None else os.environ.copy()
    out[_VAR] = session_bus_address()
    return out


def reset_cache() -> None:
    """Forget the memoised address (used by the tests)."""
    global _cache
    _cache = (0.0, None)
