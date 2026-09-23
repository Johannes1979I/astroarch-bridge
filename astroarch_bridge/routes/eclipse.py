"""Route /api/eclipse: conduttore eclissi (Fase 3).

Riceve il piano calcolato dall'app (blocchi di bracket per feature), arma la
camera e lo esegue via INDI diretto, con:
  - state machine idle → planned → armed → running → done/aborted
  - auto-loop (bias EV) calcolato dal median/vmax dell'ultimo frame, **bounded**
    (±3 stop) e **mai applicato** ai blocchi di sicurezza (Baily/diamante)
  - override "a un tap": ±EV, salta blocco, freeze auto, abort

Il direttore vive nel bridge (non nell'app) così, se il telefono si disconnette
in totalità, il piano armato continua a scattare (spec d).

⚠️ Il fire-loop pilota una camera reale: va collaudato a secco (Luna / Sole
filtrato) prima dell'eclissi. L'auto-loop è conservativo e va tarato sul campo.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import first_element, resolve_device
from .camera import _ensure_upload_local, _resolve_gain

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/eclipse", tags=["eclipse"], dependencies=[Depends(require_token)]
)

# --- Parametri auto-loop (conservativi, da tarare sul campo) ---
_MAX16 = 65535.0
_SAT_LIMIT_FRAC = 0.97   # oltre → sta clippando → riduci
_DARK_MEDIAN_FRAC = 0.12  # sotto → troppo scuro → aumenta
_MAX_BIAS_STOPS = 3.0
_BIAS_STEP = 0.5


class _Abort(Exception):
    """Interruzione richiesta dall'utente."""


class _Conductor:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.phase: str = "idle"  # idle|planned|armed|running|done|aborted
        self.plan: Optional[dict] = None
        self.device: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.block_idx: int = -1
        self.block_label: Optional[str] = None
        self.frames_shot: int = 0
        self.frames_total: int = 0
        self.started_at: float = 0.0
        self.ev_bias_stops: float = 0.0
        self.auto_enabled: bool = True
        self.last_median: Optional[float] = None
        self.last_vmax: Optional[float] = None
        self.pending: dict[str, Any] = {}
        self.logs: list[str] = []
        self.error: Optional[str] = None

    def note(self, msg: str) -> None:
        self.logs.append(f"{time.strftime('%H:%M:%S')} {msg}")
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        log.info("[eclipse] %s", msg)

    def snapshot(self) -> dict:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        return {
            "phase": self.phase,
            "device": self.device,
            "block_index": self.block_idx,
            "block_label": self.block_label,
            "blocks_total": len(self.plan["blocks"]) if self.plan else 0,
            "frames_shot": self.frames_shot,
            "frames_total": self.frames_total,
            "elapsed_sec": round(elapsed, 1),
            "ev_bias_stops": round(self.ev_bias_stops, 2),
            "auto_enabled": self.auto_enabled,
            "last_median": self.last_median,
            "last_vmax": self.last_vmax,
            "error": self.error,
            "logs": self.logs[-30:],
        }


CONDUCTOR = _Conductor()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@router.get("/status")
async def status() -> dict:
    return CONDUCTOR.snapshot()


@router.get("/contacts")
async def contacts(date: str, lat: float, lon: float) -> dict:
    """Contatti C1-C4 + posizione del Sole per il GPS dato (calcolo astropy,
    separazione topocentrica Sole-Luna). Best-effort: da correggere sul campo.
    date = YYYY-MM-DD (UT)."""
    try:
        return await asyncio.to_thread(_compute_contacts, date, float(lat), float(lon))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"calcolo contatti fallito: {e}")


@router.post("/plan")
async def set_plan(
    payload: dict = Body(...),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Registra il piano. blocks = [{label, exposures[], shots, priority, with_filter}]."""
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise HTTPException(status_code=400, detail="blocks vuoto o mancante")
    norm: list[dict] = []
    total = 0
    for b in blocks:
        expos = [float(x) for x in (b.get("exposures") or []) if float(x) > 0]
        if not expos:
            continue
        shots = max(1, int(b.get("shots", 1)))
        blk = {
            "label": str(b.get("label", "?")),
            "exposures": expos,
            "shots": shots,
            "priority": int(b.get("priority", 9)),
            "with_filter": bool(b.get("with_filter", False)),
        }
        total += len(expos) * shots
        norm.append(blk)
    if not norm:
        raise HTTPException(status_code=400, detail="nessun blocco valido")

    CONDUCTOR.reset()
    CONDUCTOR.plan = {
        "blocks": norm,
        "gain": payload.get("gain"),
        "offset": payload.get("offset"),
        "overhead_sec": float(payload.get("overhead_sec", 1.5)),
        "totality_sec": payload.get("totality_sec"),
    }
    CONDUCTOR.device = payload.get("device")
    CONDUCTOR.frames_total = total
    CONDUCTOR.phase = "planned"
    CONDUCTOR.note(f"Piano registrato: {len(norm)} blocchi, {total} frame")
    return {"ok": True, "blocks": len(norm), "frames_total": total}


@router.post("/arm")
async def arm(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Prepara la camera: UPLOAD_MODE=BOTH+dir, BLOB attivo, frame LIGHT, gain/offset."""
    if CONDUCTOR.plan is None:
        raise HTTPException(status_code=409, detail="nessun piano: chiama /plan")
    dev = await resolve_device(bridge.state, "camera", CONDUCTOR.device)
    CONDUCTOR.device = dev

    # Upload BOTH + cartella dedicata (così il bridge riceve il BLOB per l'auto-loop
    # E i FITS sono salvati su disco).
    from ..config import get_settings
    settings = get_settings()
    target_dir = str(settings.images_dir / "Eclipse")
    await _ensure_upload_local(bridge, dev, target_dir, "ECL_XXX")
    try:
        await bridge.indi.enable_blob(dev, "Also")
    except Exception as e:  # noqa: BLE001
        CONDUCTOR.note(f"enable_blob warning: {e}")

    # Frame LIGHT
    try:
        await bridge.indi.send_switch(dev, "CCD_FRAME_TYPE", {
            "FRAME_LIGHT": True, "FRAME_DARK": False,
            "FRAME_FLAT": False, "FRAME_BIAS": False,
        })
    except Exception:  # noqa: BLE001
        pass

    # Gain/Offset dal piano (uno per tutto il piano, come dall'app).
    await _apply_gain_offset(bridge, dev,
                             CONDUCTOR.plan.get("gain"), CONDUCTOR.plan.get("offset"))

    CONDUCTOR.phase = "armed"
    CONDUCTOR.note(f"Armato su {dev}")
    _, gain_prop, gain_elt = await _resolve_gain(bridge, dev)
    return {"ok": True, "device": dev, "upload_dir": target_dir,
            "gain_property": gain_prop, "gain_element": gain_elt}


@router.post("/start")
async def start(bridge: Bridge = Depends(get_bridge)) -> dict:
    if CONDUCTOR.plan is None:
        raise HTTPException(status_code=409, detail="nessun piano")
    if CONDUCTOR.phase == "running":
        raise HTTPException(status_code=409, detail="già in esecuzione")
    if CONDUCTOR.phase not in ("armed", "planned", "done", "aborted"):
        raise HTTPException(status_code=409, detail=f"stato non valido: {CONDUCTOR.phase}")
    if CONDUCTOR.phase != "armed":
        await arm(bridge)  # auto-arm se non fatto

    CONDUCTOR.pending = {}
    CONDUCTOR.error = None
    CONDUCTOR.frames_shot = 0
    CONDUCTOR.block_idx = -1
    CONDUCTOR.ev_bias_stops = 0.0
    CONDUCTOR.started_at = time.monotonic()
    CONDUCTOR.phase = "running"
    CONDUCTOR.task = asyncio.ensure_future(_run(bridge))
    CONDUCTOR.note("Avvio conduttore")
    return {"ok": True}


@router.post("/override")
async def override(payload: dict = Body(...)) -> dict:
    """Override a un tap: {ev: ±0.5, skip: true, freeze: true|false, abort: true}."""
    if "abort" in payload and payload["abort"]:
        CONDUCTOR.pending["abort"] = True
        CONDUCTOR.note("Override: ABORT")
    if "skip" in payload and payload["skip"]:
        CONDUCTOR.pending["skip"] = True
        CONDUCTOR.note("Override: salta blocco")
    if "freeze" in payload:
        CONDUCTOR.auto_enabled = not bool(payload["freeze"])
        CONDUCTOR.note(f"Override: auto-loop {'OFF' if not CONDUCTOR.auto_enabled else 'ON'}")
    if "ev" in payload:
        delta = float(payload["ev"])
        CONDUCTOR.ev_bias_stops = _clamp(
            CONDUCTOR.ev_bias_stops + delta, -_MAX_BIAS_STOPS, _MAX_BIAS_STOPS)
        CONDUCTOR.note(f"Override: EV {'+' if delta >= 0 else ''}{delta} → bias {CONDUCTOR.ev_bias_stops:+.1f}")
    return {"ok": True, "ev_bias_stops": CONDUCTOR.ev_bias_stops,
            "auto_enabled": CONDUCTOR.auto_enabled}


@router.post("/stop")
async def stop(bridge: Bridge = Depends(get_bridge)) -> dict:
    CONDUCTOR.pending["abort"] = True
    if CONDUCTOR.device:
        try:
            await bridge.indi.send_switch(CONDUCTOR.device, "CCD_ABORT_EXPOSURE", {"ABORT": True})
        except Exception:  # noqa: BLE001
            pass
    t = CONDUCTOR.task
    if t and not t.done():
        t.cancel()
    CONDUCTOR.phase = "aborted"
    CONDUCTOR.note("Stop richiesto")
    return {"ok": True}


# ---------------------------------------------------------------- interni

def _compute_contacts(date: str, lat: float, lon: float) -> dict:
    """Calcola i contatti dell'eclissi via astropy (separazione topocentrica
    Sole-Luna) per l'osservatore a (lat, lon). Ritorna orari UT + Sole a max."""
    import numpy as np
    from astropy.time import Time
    from astropy.coordinates import EarthLocation, AltAz, get_body, get_sun
    import astropy.units as u

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=0 * u.m)

    def sep_radii(times):
        frame = AltAz(obstime=times, location=loc)
        sun = get_sun(times).transform_to(frame)
        moon = get_body("moon", times).transform_to(frame)
        sep = sun.separation(moon).deg
        sun_r = (959.63 / sun.distance.to(u.au).value) / 3600.0  # deg
        moon_r = np.degrees(np.arctan(1737.4 / moon.distance.to(u.km).value))
        return sep, sun_r, moon_r, sun

    def hhmmss(t) -> str:
        return t.utc.iso[11:19] + " UT"

    # Scansione grezza sul giorno (1 min) per trovare max + C1/C4.
    t0 = Time(f"{date}T00:00:00", scale="utc")
    n = 24 * 60
    tc = t0 + np.arange(n) * u.min
    sep, sun_r, moon_r, _ = sep_radii(tc)
    ext = sun_r + moon_r          # contatto esterno (C1/C4)
    partial = sep <= ext
    if not bool(partial.any()):
        return {"visible": False,
                "note": "Eclissi non visibile da questa posizione."}
    i1 = int(np.argmax(partial))
    i4 = int(n - 1 - np.argmax(partial[::-1]))
    imax = int(np.argmin(sep))

    # Scansione fine attorno al massimo (±20 min a 1 s) per max + C2/C3.
    fn = 40 * 60 + 1
    tf = tc[imax] + np.linspace(-1200, 1200, fn) * u.s
    fsep, fsun_r, fmoon_r, fsun = sep_radii(tf)
    finte = np.abs(fmoon_r - fsun_r)   # contatto interno (C2/C3)
    jmax = int(np.argmin(fsep))
    ftotal = fsep <= finte
    c2 = c3 = None
    tot_sec = None
    if bool(ftotal.any()):
        j2 = int(np.argmax(ftotal))
        j3 = int(fn - 1 - np.argmax(ftotal[::-1]))
        c2, c3 = tf[j2], tf[j3]
        tot_sec = int(round((c3 - c2).to(u.s).value))

    sun_max = fsun[jmax]
    return {
        "visible": True,
        "type": "total" if c2 is not None else "partial",
        "c1": hhmmss(tc[i1]),
        "c2": hhmmss(c2) if c2 is not None else None,
        "max": hhmmss(tf[jmax]),
        "c3": hhmmss(c3) if c3 is not None else None,
        "c4": hhmmss(tc[i4]),
        "totality_sec": tot_sec,
        "sun": {
            "alt": round(float(sun_max.alt.deg), 2),
            "az": round(float(sun_max.az.deg), 2),
        },
        "note": "Calcolo astropy (topocentrico). Verifica/correggi sul campo.",
    }


async def _apply_gain_offset(bridge: Bridge, dev: str,
                             gain: Any, offset: Any) -> None:
    if gain is not None:
        _, prop, elt = await _resolve_gain(bridge, dev)
        try:
            if prop and elt:
                await bridge.indi.send_number(dev, prop, {elt: float(gain)})
            else:
                await bridge.indi.send_number(dev, "CCD_GAIN", {"GAIN": float(gain)})
        except Exception as e:  # noqa: BLE001
            CONDUCTOR.note(f"gain warning: {e}")
    if offset is not None:
        try:
            await bridge.indi.send_number(dev, "CCD_OFFSET", {"OFFSET": float(offset)})
        except Exception:  # noqa: BLE001
            pass


async def _wait_exposure(bridge: Bridge, dev: str, exposure: float,
                         timeout: float) -> bool:
    """Attende il completamento dell'esposizione (stato CCD_EXPOSURE != Busy)."""
    start = time.monotonic()
    seen_busy = False
    while time.monotonic() - start < timeout:
        p = await bridge.state.get_property(dev, "CCD_EXPOSURE")
        st = (p or {}).get("state")
        val = first_element(p or {}, "CCD_EXPOSURE_VALUE", 0.0) or 0.0
        if st == "Busy" or (val and val > 0.05):
            seen_busy = True
        elif (time.monotonic() - start) >= exposure * 0.7:
            # non più busy e trascorso ~il tempo di posa → fatto
            return True
        if seen_busy and st in ("Ok", "Idle") and (val is None or val <= 0.05):
            return True
        await asyncio.sleep(0.2)
    return False


def _read_last_stats() -> None:
    # Nota: usiamo direttamente lo state manager (stesso processo) via snapshot
    # asincrono nel loop; qui è un placeholder, il valore viene aggiornato in _run.
    pass


def _auto_adjust() -> None:
    """Aggiusta il bias EV per i blocchi successivi in base all'ultimo frame.
    Conservativo: riduce se clippa, aumenta se molto scuro. Bounded ±3 stop."""
    if not CONDUCTOR.auto_enabled:
        return
    vmax = CONDUCTOR.last_vmax
    median = CONDUCTOR.last_median
    old = CONDUCTOR.ev_bias_stops
    if vmax is not None and vmax / _MAX16 >= _SAT_LIMIT_FRAC:
        CONDUCTOR.ev_bias_stops = _clamp(old - _BIAS_STEP, -_MAX_BIAS_STOPS, _MAX_BIAS_STOPS)
    elif median is not None and median / _MAX16 <= _DARK_MEDIAN_FRAC:
        CONDUCTOR.ev_bias_stops = _clamp(old + _BIAS_STEP, -_MAX_BIAS_STOPS, _MAX_BIAS_STOPS)
    if CONDUCTOR.ev_bias_stops != old:
        CONDUCTOR.note(
            f"Auto-loop: bias {old:+.1f} → {CONDUCTOR.ev_bias_stops:+.1f} "
            f"(median={median}, vmax={vmax})")


async def _run(bridge: Bridge) -> None:
    dev = CONDUCTOR.device
    plan = CONDUCTOR.plan
    if not dev or not plan:
        CONDUCTOR.phase = "aborted"
        return
    overhead = float(plan.get("overhead_sec", 1.5))
    try:
        for i, blk in enumerate(plan["blocks"]):
            if CONDUCTOR.pending.get("abort"):
                raise _Abort()
            CONDUCTOR.block_idx = i
            CONDUCTOR.block_label = blk["label"]
            if CONDUCTOR.pending.pop("skip", False):
                CONDUCTOR.note(f"Salto blocco: {blk['label']}")
                continue

            is_safety = blk.get("priority", 9) <= 1  # Baily/diamante
            mult = 1.0 if is_safety else (2.0 ** CONDUCTOR.ev_bias_stops)
            CONDUCTOR.note(f"Blocco {i + 1}: {blk['label']} (×{mult:.2f})")

            for expo in blk["exposures"]:
                for _ in range(blk["shots"]):
                    if CONDUCTOR.pending.get("abort"):
                        raise _Abort()
                    if CONDUCTOR.pending.pop("skip", False):
                        break
                    eff = _clamp(expo * mult, 1.0 / 8000.0, 30.0)
                    await bridge.indi.send_number(
                        dev, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": eff})
                    ok = await _wait_exposure(bridge, dev, eff, eff + overhead + 8.0)
                    CONDUCTOR.frames_shot += 1
                    if not ok:
                        CONDUCTOR.note(f"⚠️ timeout su posa {eff:.4f}s")
                    # stat ultimo frame (per auto-loop)
                    snap = await bridge.state.snapshot()
                    lf = snap.get("last_frame") or {}
                    CONDUCTOR.last_median = lf.get("median")
                    CONDUCTOR.last_vmax = lf.get("vmax")

            if not is_safety:
                _auto_adjust()

        CONDUCTOR.phase = "done"
        CONDUCTOR.note(f"Completato: {CONDUCTOR.frames_shot} frame")
    except (_Abort, asyncio.CancelledError):
        CONDUCTOR.phase = "aborted"
        CONDUCTOR.note("Conduttore interrotto")
    except Exception as e:  # noqa: BLE001
        CONDUCTOR.error = str(e)
        CONDUCTOR.phase = "aborted"
        CONDUCTOR.note(f"Errore: {e}")
