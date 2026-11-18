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


# ---------------------------------------------------------------------------
# Guardia contro l'avvio di KStars per «attivazione automatica» D-Bus.
#
# Il pacchetto kstars installa /usr/share/dbus-1/services/org.kde.kstars.service
# (Exec=/usr/bin/kstars). Con quel file, QUALUNQUE chiamata di metodo a
# org.kde.kstars fatta con qdbus6 mentre KStars e' spento fa partire KStars:
# qdbus6 non ha un'opzione per non avviare il servizio. Il bridge interroga
# lo stato di Ekos ogni 3-4 secondi per conto dell'app, quindi bastava aprire
# l'app perche' KStars si aprisse da solo, e si riapriva appena lo si chiudeva
# (segnalato dall'utente il 18/11/2026, impronta: KStars nel cgroup
# app-dbus-... un secondo dopo la prima GET /api/system/ekos_state).
#
# Regola: si parla con KStars solo se KStars c'e' gia'. Lo si avvia SOLO su
# richiesta esplicita (POST /api/system/launch_kstars, o il pulsante master che
# ora lo lancia per esteso prima di avviare Ekos).
# ---------------------------------------------------------------------------

KSTARS_SERVICE = "org.kde.kstars"
#: Testo restituito dalle funzioni qdbus6 al posto dell'output quando la
#: chiamata e' stata evitata: i chiamanti trattano gia' rc != 0 come «non
#: leggibile», quindi a valle non cambia nulla.
KSTARS_NOT_RUNNING = "KStars non in esecuzione"
_KSTARS_COMMS = (b"kstars", b"kstars.bin")

# Solo il «si'» del controllo sul bus resta in memoria, e per poco: un si'
# vecchio, subito dopo la chiusura di KStars, farebbe partire proprio la
# chiamata che lo riavvia. Il controllo sui processi invece si rifa' sempre
# (lettura di /proc, nessun sottoprocesso).
_OWNER_TTL = 1.0
_owner_ok_until = 0.0


def kstars_process_running() -> bool:
    """True se c'e' un processo KStars vivo (kstars o kstars.bin, non zombie).

    Nessun sottoprocesso: con KStars spento, il caso frequente, la guardia
    costa una scansione di /proc."""
    try:
        pids = [p for p in os.listdir(_PROC) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(f"{_PROC}/{pid}/comm", "rb") as f:
                if f.read().strip() not in _KSTARS_COMMS:
                    continue
            with open(f"{_PROC}/{pid}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        # "1234 (kstars) S ...": lo stato segue l'ultima ')'
        state = stat.rsplit(b")", 1)[-1].strip()[:1]
        if state not in (b"Z", b"X"):
            return True
    return False


async def kstars_on_bus() -> bool:
    """True se KStars gira E ha gia' registrato il suo nome sul bus.

    Il secondo controllo copre i secondi dopo l'avvio, quando il processo
    esiste ma il nome non e' ancora preso: una chiamata in quel momento
    farebbe partire un secondo KStars. NameHasOwner e' un metodo del demone
    del bus, che non attiva mai nulla."""
    global _owner_ok_until
    if not kstars_process_running():
        _owner_ok_until = 0.0
        return False
    now = time.monotonic()
    if now < _owner_ok_until:
        return True
    import asyncio
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "qdbus6", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus.NameHasOwner", KSTARS_SERVICE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=with_session_bus(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        ok = proc.returncode == 0 and out.decode("utf-8", "replace").strip() == "true"
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        # Nel dubbio «spento»: meglio non leggere lo stato che avviarlo.
        if proc is not None and proc.returncode is None:
            proc.kill()
        ok = False
    _owner_ok_until = (time.monotonic() + _OWNER_TTL) if ok else 0.0
    return ok


async def kstars_call_allowed(args) -> bool:
    """False se la chiamata qdbus6 con questi [args] e' diretta a KStars e
    KStars non e' sul bus: in quel caso la chiamata lo avvierebbe."""
    if KSTARS_SERVICE not in args:
        return True
    return await kstars_on_bus()


def forget_kstars_owner() -> None:
    """Da chiamare quando KStars viene avviato o chiuso dal bridge."""
    global _owner_ok_until
    _owner_ok_until = 0.0


def reset_cache() -> None:
    """Forget the memoised address and owner check (used by the tests)."""
    global _cache
    _cache = (0.0, None)
    forget_kstars_owner()
