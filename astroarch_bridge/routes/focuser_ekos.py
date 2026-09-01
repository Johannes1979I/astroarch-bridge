"""Route /api/focuser/ekos_* — integrazione con Ekos.Focus DBus.

A differenza dell'autofocus iterativo lato bridge (in focuser.py:
_run_autofocus), qui deleghiamo TUTTO a Ekos.Focus, identico a quello
che fa il pulsante "Auto Focus" nella Focus tab di Ekos sul desktop.
Vantaggi:
  • algoritmo Ekos (Linear/Iterative/Polynomial/Hyperbola/Linear1Pass)
  • backlash compensation, walking, refining già gestiti
  • parametri configurabili nella UI di Ekos persistono

Il bridge:
  • intercetta via dbus-monitor il signal `newHFR(hfr, position,
    inAutofocus, train)` emesso da Ekos ad ogni esposizione AF →
    V-curve live esposta via REST
  • intercetta anche `newStatus` e `newLog` per stato + log live
  • fornisce endpoint per state/start/abort/setParams/curve

Nessuna nuova dipendenza Python: dbus-monitor è installato di default
con dbus su qualsiasi distro Linux con sessione D-Bus utente.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..dbus_session import with_session_bus

router = APIRouter(prefix="/api/focuser", tags=["focuser"],
                    dependencies=[Depends(require_token)])

logger = logging.getLogger("astroarch_bridge.ekos_focus")

# In-memory state della V-curve corrente per il run AF attivo
_state: dict = {
    "samples": [],          # list of {position, hfr, in_autofocus, train, ts}
    "log_tail": [],         # ultime righe da Ekos.Focus.newLog
    "last_status": None,    # int da newStatus
    "monitor_running": False,
}
_lock = asyncio.Lock()
_monitor_task: asyncio.Task | None = None

# v0.3.13: nome del train attivo, risolto una volta e cachato.
# CRITICO: in Ekos 3.8 il modulo Focus è multi-train e i metodi DBus
# (status/camera/focuser/start...) prendono il nome del train. Chiamarli
# con train VUOTO ("") fa creare a Ekos una NUOVA tab Focus ad ogni
# chiamata → con il polling ogni 2s dell'app si aprivano decine di tab
# "MoonLite". Passando SEMPRE il nome reale del train (es. "Principale")
# tutte le chiamate colpiscono la STESSA tab.
_cached_train: str | None = None


def _resolve_focus_train(train: str = "") -> str:
    """Ritorna il nome del train da usare per le chiamate Ekos.Focus.
    Se il caller passa un train esplicito lo usa; altrimenti risolve il
    train attivo dal userdb di KStars (CaptureTrainID → nome) e lo cacha.
    Non ritorna mai stringa vuota se può evitarlo."""
    global _cached_train
    if train:
        return train
    if _cached_train:
        return _cached_train
    try:
        from .capture_ekos import _read_active_train_name
        name = _read_active_train_name()
        if name:
            _cached_train = name
            return name
    except Exception:
        pass
    return ""


def _focus_status_label(s: int | None) -> str:
    """Enum Ekos FocusState (KStars 3.8.x):
       0=Idle, 1=Complete, 2=Failed, 3=Aborted, 4=Waiting,
       5=Progress, 6=FrameAdjusted, 7=Framing, 8=Changing"""
    return {
        0: "idle", 1: "complete", 2: "failed", 3: "aborted",
        4: "waiting", 5: "progress", 6: "frame_adjusted",
        7: "framing", 8: "changing",
    }.get(s, "unknown")


async def _ensure_monitor() -> None:
    """Lancia (una sola volta) dbus-monitor in background e parsea i
    signal Ekos.Focus per popolare _state. Idempotente: si auto-riavvia
    se il process è morto."""
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return
    _monitor_task = asyncio.create_task(_run_monitor())


async def _run_monitor() -> None:
    """Long-running: legge stdout di dbus-monitor e aggiorna _state.
    Restart automatico in caso di crash."""
    while True:
        try:
            env = with_session_bus()
            proc = await asyncio.create_subprocess_exec(
                "dbus-monitor", "--session",
                "type='signal',interface='org.kde.kstars.Ekos.Focus'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            logger.info("ekos focus monitor started pid=%s", proc.pid)
            async with _lock:
                _state["monitor_running"] = True
            await _parse_stream(proc)
        except Exception as e:
            logger.warning("ekos focus monitor crashed: %s", e)
        async with _lock:
            _state["monitor_running"] = False
        await asyncio.sleep(2.0)


_SIG_RE = re.compile(r"member=(\w+)")
_VAL_RE = re.compile(
    r"^\s+(?:double|int32|int16|uint16|uint32|boolean|string)\s+(.+)$")


async def _parse_stream(proc) -> None:
    """Parse output dbus-monitor. Riconosce newHFR/newStatus/newLog."""
    current_signal: str | None = None
    pending_args: list = []

    async def flush():
        nonlocal current_signal, pending_args
        if current_signal is None:
            return
        try:
            if current_signal == "newHFR" and len(pending_args) >= 4:
                hfr = float(pending_args[0])
                position = int(pending_args[1])
                in_af = pending_args[2].lower() == "true"
                train = pending_args[3].strip('"')
                async with _lock:
                    _state["samples"].append({
                        "position": position,
                        "hfr": hfr,
                        "in_autofocus": in_af,
                        "train": train,
                        "ts": time.time(),
                    })
                    if len(_state["samples"]) > 500:
                        _state["samples"] = _state["samples"][-500:]
                logger.info("newHFR pos=%d hfr=%.3f af=%s train=%s",
                            position, hfr, in_af, train)
            elif current_signal == "newStatus" and len(pending_args) >= 1:
                try:
                    st = int(pending_args[0])
                    async with _lock:
                        _state["last_status"] = st
                    logger.info("newStatus = %d (%s)", st,
                                _focus_status_label(st))
                except ValueError:
                    pass
            elif current_signal == "newLog" and len(pending_args) >= 1:
                line = pending_args[0].strip('"')
                async with _lock:
                    _state["log_tail"].append(line)
                    if len(_state["log_tail"]) > 100:
                        _state["log_tail"] = _state["log_tail"][-100:]
        finally:
            current_signal = None
            pending_args = []

    while True:
        line_b = await proc.stdout.readline()
        if not line_b:
            break
        line = line_b.decode("utf-8", "replace").rstrip()
        if line.startswith("signal "):
            await flush()
            m = _SIG_RE.search(line)
            if m:
                current_signal = m.group(1)
                pending_args = []
        elif current_signal is not None:
            m = _VAL_RE.match(line)
            if m:
                pending_args.append(m.group(1))
    await flush()


@router.get("/ekos_state")
async def ekos_state(train: str = "") -> dict:
    """Stato corrente del modulo Ekos Focus: device, status, canAF.
    Avvia idempotentemente il listener dei signal per la V-curve.
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    focus_path = "/KStars/Ekos/Focus"

    # v0.3.13: risolvi SEMPRE il nome train reale (mai vuoto) per non far
    # creare a Ekos una nuova tab Focus ad ogni poll.
    train = _resolve_focus_train(train)

    await _ensure_monitor()

    async def _g(method, *args):
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, focus_path,
                                    f"org.kde.kstars.Ekos.Focus.{method}",
                                    *args)
        return val if rc == 0 else None

    out: dict = {
        "camera": await _g("camera", train),
        "focuser": await _g("focuser", train),
        "filter": await _g("filter", train),
        "filter_wheel": await _g("filterWheel", train),
    }
    raw = await _g("canAutoFocus", train)
    out["can_autofocus"] = (raw == "true") if raw else False

    # status() ritorna un type "(i)" struct: qdbus6 senza --literal non lo
    # visualizza ("I don't know how to display..."). Usiamo --literal e
    # estraiamo l'int dalla forma "[Argument: (i) <int>]".
    proc = await asyncio.create_subprocess_exec(
        "qdbus6", "--literal",
        EKOS_DBUS_SERVICE, focus_path,
        "org.kde.kstars.Ekos.Focus.status", train,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=with_session_bus(),
    )
    try:
        sout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        raw = sout.decode("utf-8", "replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        raw = "timeout"
    out["status_raw"] = raw
    m = re.search(r"-?\d+", raw or "")
    out["status_int"] = int(m.group(0)) if m else None
    out["status_label"] = _focus_status_label(out["status_int"])
    async with _lock:
        out["monitor_running"] = _state["monitor_running"]
    return out


@router.post("/ekos_start")
async def ekos_start(payload: dict = Body(default={})) -> dict:
    """Avvia autofocus Ekos = pulsante "Auto Focus" della Focus tab.
    Imposta opzionalmente i parametri prima (idempotente).

    Body params (tutti opzionali):
      train: str (default "")
      box_size: int
      step_size: int
      max_travel: int
      tolerance: float
      binning: [x, y]
      filter: str
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    focus_path = "/KStars/Ekos/Focus"
    # v0.3.13: risolvi SEMPRE il train reale (mai vuoto) → una sola tab Focus
    train = _resolve_focus_train(payload.get("train", ""))

    # Reset curva per nuovo run
    async with _lock:
        _state["samples"] = []
        _state["log_tail"] = []
        _state["last_status"] = None

    await _ensure_monitor()

    # setAutoFocusParameters se TUTTI e 4 i parametri sono dati
    if all(payload.get(k) is not None for k in
           ("box_size", "step_size", "max_travel", "tolerance")):
        logger.info(
            "setAutoFocusParameters(box=%s step=%s travel=%s tol=%s)",
            payload["box_size"], payload["step_size"],
            payload["max_travel"], payload["tolerance"])
        await _dbus_call(EKOS_DBUS_SERVICE, focus_path,
            "org.kde.kstars.Ekos.Focus.setAutoFocusParameters",
            train,
            str(int(payload["box_size"])),
            str(int(payload["step_size"])),
            str(int(payload["max_travel"])),
            str(float(payload["tolerance"])))
    if payload.get("binning"):
        bx, by = payload["binning"][0], payload["binning"][1]
        await _dbus_call(EKOS_DBUS_SERVICE, focus_path,
            "org.kde.kstars.Ekos.Focus.setBinning",
            str(int(bx)), str(int(by)), train)
    if payload.get("filter"):
        await _dbus_call(EKOS_DBUS_SERVICE, focus_path,
            "org.kde.kstars.Ekos.Focus.setFilter",
            str(payload["filter"]), train)

    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, focus_path,
        "org.kde.kstars.Ekos.Focus.start", train)
    logger.info("Ekos.Focus.start train=%r -> rc=%d raw=%s",
                train, rc, raw)
    if rc != 0 or raw.lower() == "false":
        raise HTTPException(status_code=500,
            detail=f"Ekos.Focus.start failed: {raw}")
    return {"ok": True, "started": True}


@router.post("/ekos_abort")
async def ekos_abort(payload: dict = Body(default={})) -> dict:
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    # v0.3.13: train reale (mai vuoto)
    train = _resolve_focus_train(payload.get("train", ""))
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Focus",
        "org.kde.kstars.Ekos.Focus.abort", train)
    return {"ok": rc == 0, "raw": raw}


@router.get("/ekos_curve")
async def ekos_curve() -> dict:
    """V-curve corrente: campioni HFR/position + log tail + status.
    Polled dall'app ogni ~1s durante l'autofocus."""
    async with _lock:
        return {
            "samples": list(_state["samples"]),
            "log_tail": list(_state["log_tail"][-30:]),
            "last_status": _state["last_status"],
            "last_status_label": _focus_status_label(
                _state["last_status"]),
            "monitor_running": _state["monitor_running"],
        }


@router.post("/ekos_curve_reset")
async def ekos_curve_reset() -> dict:
    async with _lock:
        _state["samples"] = []
        _state["log_tail"] = []
        _state["last_status"] = None
    return {"ok": True}
