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
import math
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
# Calibrazione per-fase: prima di sparare i keeper, alcuni scatti di prova per
# trovare il tempo giusto (così non si brucia l'intera fase con pose sbagliate).
_MAX_PROBE = 4               # scatti di calibrazione massimi per fase
_TARGET_MEDIAN_FRAC = 0.28   # mediana "buona" a cui puntare in calibrazione


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
        self.base_dir: Optional[str] = None  # <images_dir>/Eclissi (cartella madre)
        self.cool: bool = False
        self.cool_temp: float = -10.0
        self.last_temp: Optional[float] = None  # temperatura camera letta
        self.frames_shot: int = 0
        self.calib_shot: int = 0  # scatti di calibrazione (non keeper)
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
            "cooling": self.cool,
            "cool_temp": self.cool_temp,
            "last_temp": self.last_temp,
            "calib_shot": self.calib_shot,
            "error": self.error,
            "logs": self.logs[-30:],
        }


CONDUCTOR = _Conductor()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _phase_folder(feature_key: str, label: str = "") -> str:
    """Sottocartella (dentro 'Eclissi/') in cui salvare gli scatti di una fase.
    Le corone (interna/media/esterna) confluiscono tutte in 'corona', come da
    richiesta utente ("tutti gli scatti della corona vanno nella cartella corona").
    Fallback sull'etichetta se la feature non è nota."""
    k = (feature_key or "").lower().replace("_", "-")
    text = f"{k} {label.lower()}"
    if "corona" in text or k == "totality":
        return "corona"
    if "baily" in text or "diamond" in text or "diamante" in text or "perle" in text:
        return "baily"
    if "chromo" in text or "cromosfera" in text:
        return "cromosfera"
    if "promin" in text or "protuberanz" in text:
        return "protuberanze"
    if "partial" in text or "parzial" in text:
        return "parziale"
    return "varie"


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


@router.post("/point_sun")
async def point_sun_route(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Punta subito la montatura sul Sole (posizione attuale) + tracking solare.
    Azione esplicita (usata dalla spunta dell'app / test)."""
    try:
        mdev, ra, dec = await _point_sun(bridge)
        return {"ok": True, "mount": mdev, "ra_hours": round(ra, 4), "dec_deg": round(dec, 3)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"punta Sole fallito: {e}")


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
            "feature": str(b.get("feature", "")),
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
        "point_sun": bool(payload.get("point_sun", False)),
    }
    CONDUCTOR.device = payload.get("device")
    CONDUCTOR.frames_total = total
    CONDUCTOR.phase = "planned"
    CONDUCTOR.note(f"Piano registrato: {len(norm)} blocchi, {total} frame")
    return {"ok": True, "blocks": len(norm), "frames_total": total}


@router.post("/arm")
async def arm(payload: dict = Body(default={}), bridge: Bridge = Depends(get_bridge)) -> dict:
    """Prepara: (opz.) punta il Sole, (opz.) raffredda la camera, UPLOAD_MODE=BOTH,
    cartella madre 'Eclissi/', BLOB, frame LIGHT, gain/offset.
    `point_sun` (bool) → autopuntamento del Sole; `cool` (bool) + `cool_temp` (°C,
    default -10) → raffreddamento camera. Tutte scelte dell'utente."""
    return await _do_arm(
        bridge,
        point_sun=payload.get("point_sun"),
        cool=payload.get("cool"),
        cool_temp=payload.get("cool_temp"),
    )


async def _do_arm(bridge: Bridge, point_sun: bool | None = None,
                  cool: bool | None = None, cool_temp: float | None = None) -> dict:
    if CONDUCTOR.plan is None:
        raise HTTPException(status_code=409, detail="nessun piano: chiama /plan")

    # Autopuntamento del Sole — SOLO se richiesto (spunta nell'app). Di notte
    # (test Luna) resta spento, così non si slew verso il Sole per errore.
    do_point = bool(point_sun) if point_sun is not None \
        else bool((CONDUCTOR.plan or {}).get("point_sun"))
    if do_point:
        try:
            mdev, ra, dec = await _point_sun(bridge)
            CONDUCTOR.note(
                f"Puntato il Sole: {mdev} RA={ra:.3f}h Dec={dec:.2f}° + tracking solare")
        except Exception as e:  # noqa: BLE001
            CONDUCTOR.note(f"punta Sole warning: {e}")

    dev = await resolve_device(bridge.state, "camera", CONDUCTOR.device)
    CONDUCTOR.device = dev

    # Upload BOTH + cartella madre "Eclissi/" DENTRO la cartella scelta da Ekos
    # (settings.images_dir). Il bridge riceve il BLOB per l'auto-loop E i FITS
    # sono salvati su disco. Gli scatti di ogni fase finiranno nelle sottocartelle
    # Eclissi/<fase>/ impostate blocco per blocco in _run().
    from ..config import get_settings
    settings = get_settings()
    base_dir = str(settings.images_dir / "Eclissi")
    CONDUCTOR.base_dir = base_dir
    await _ensure_upload_local(bridge, dev, base_dir, "ECL_XXX")
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

    # Raffreddamento camera — SOLO se richiesto (spunta nell'app). Default -10°C.
    # Impostiamo il target: la camera scende da sola; non blocchiamo l'arm in
    # attesa (in eclissi non c'è tempo). L'utente arma in anticipo.
    do_cool = bool(cool) if cool is not None \
        else bool((CONDUCTOR.plan or {}).get("cool")) or CONDUCTOR.cool
    if do_cool:
        t = float(cool_temp) if cool_temp is not None \
            else float((CONDUCTOR.plan or {}).get("cool_temp") or CONDUCTOR.cool_temp)
        CONDUCTOR.cool = True
        CONDUCTOR.cool_temp = t
        try:
            await bridge.indi.send_switch(dev, "CCD_COOLER",
                                          {"COOLER_ON": True, "COOLER_OFF": False})
            await bridge.indi.send_number(dev, "CCD_TEMPERATURE",
                                          {"CCD_TEMPERATURE_VALUE": t})
            CONDUCTOR.note(f"Raffreddamento ON, target {t:.0f}°C")
        except Exception as e:  # noqa: BLE001
            CONDUCTOR.note(f"raffreddamento warning: {e}")

    CONDUCTOR.phase = "armed"
    CONDUCTOR.note(f"Armato su {dev}")
    _, gain_prop, gain_elt = await _resolve_gain(bridge, dev)
    return {"ok": True, "device": dev, "upload_dir": base_dir,
            "cooling": CONDUCTOR.cool, "cool_temp": CONDUCTOR.cool_temp,
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
        await _do_arm(bridge)  # auto-arm se non fatto (usa point_sun del piano)

    CONDUCTOR.pending = {}
    CONDUCTOR.error = None
    CONDUCTOR.frames_shot = 0
    CONDUCTOR.calib_shot = 0
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
    # Copertura massima al culmine: magnitudine (frazione del diametro) e % area.
    d_min = float(fsep[jmax])
    rs = float(fsun_r[jmax])
    rm = float(fmoon_r[jmax])
    magnitude = max(0.0, (rs + rm - d_min) / (2.0 * rs))
    coverage = _obscuration(d_min, rs, rm)
    return {
        "visible": True,
        "type": "total" if c2 is not None else "partial",
        "magnitude": round(magnitude, 3),
        "coverage_pct": round(coverage * 100.0, 1),
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


def _obscuration(d: float, r_sun: float, r_moon: float) -> float:
    """Frazione dell'area del disco solare oscurata (0-1), date le distanze
    angolari (separazione d e raggi apparenti r_sun/r_moon, stesse unità)."""
    import math
    if d >= r_sun + r_moon:
        return 0.0
    if d <= abs(r_moon - r_sun):
        return 1.0 if r_moon >= r_sun else (r_moon / r_sun) ** 2
    d2, rs2, rm2 = d * d, r_sun * r_sun, r_moon * r_moon
    a_sun = math.acos((d2 + rs2 - rm2) / (2 * d * r_sun))
    a_moon = math.acos((d2 + rm2 - rs2) / (2 * d * r_moon))
    area = rs2 * (a_sun - math.sin(2 * a_sun) / 2) + \
        rm2 * (a_moon - math.sin(2 * a_moon) / 2)
    return max(0.0, min(1.0, area / (math.pi * rs2)))


def _sun_radec_now() -> tuple[float, float]:
    """RA (ore) / Dec (gradi) apparenti del Sole ADESSO, equinozio di data (JNow)."""
    from astropy.time import Time
    from astropy.coordinates import get_sun, FK5
    t = Time.now()
    s = get_sun(t).transform_to(FK5(equinox=t))
    return float(s.ra.hour), float(s.dec.deg)


async def _point_sun(bridge: Bridge):
    """Slew della montatura sul Sole (posizione attuale) + inseguimento solare."""
    mdev = await resolve_device(bridge.state, "mount", None)
    ra, dec = await asyncio.to_thread(_sun_radec_now)
    await bridge.indi.send_switch(
        mdev, "ON_COORD_SET", {"SLEW": False, "TRACK": True, "SYNC": False})
    await bridge.indi.send_number(mdev, "EQUATORIAL_EOD_COORD", {"RA": ra, "DEC": dec})
    try:
        await bridge.indi.send_switch(mdev, "TELESCOPE_TRACK_MODE", {
            "TRACK_SIDEREAL": False, "TRACK_LUNAR": False,
            "TRACK_SOLAR": True, "TRACK_CUSTOM": False,
        })
    except Exception:  # noqa: BLE001
        pass
    return mdev, ra, dec


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


async def _wait_for_temperature(bridge: Bridge, dev: str, target: float,
                                tol: float = 1.0, timeout: float = 300.0) -> bool:
    """Attende che la camera raggiunga la temperatura target (±tol) PRIMA di
    scattare. Senza questo, gli scatti raffreddati non hanno senso (dark/segnale
    incoerenti). Ritorna True se raggiunta, False su timeout/abort. Best-effort:
    se il driver non espone CCD_TEMPERATURE, prosegue senza bloccare."""
    t0 = time.monotonic()
    CONDUCTOR.note(f"❄️ Attendo raffreddamento a {target:.0f}°C (±{tol:.0f})…")
    seen = False
    while time.monotonic() - t0 < timeout:
        if CONDUCTOR.pending.get("abort"):
            return False
        p = await bridge.state.get_property(dev, "CCD_TEMPERATURE")
        cur = first_element(p or {}, "CCD_TEMPERATURE_VALUE", None)
        if cur is not None:
            seen = True
            CONDUCTOR.last_temp = float(cur)
            if abs(float(cur) - target) <= tol:
                CONDUCTOR.note(f"❄️ Temperatura raggiunta: {float(cur):.1f}°C")
                return True
        elif not seen and time.monotonic() - t0 > 12.0:
            # Il driver non pubblica la temperatura: non blocchiamo all'infinito.
            CONDUCTOR.note("Raffreddamento: driver senza CCD_TEMPERATURE, proseguo")
            return False
        await asyncio.sleep(3.0)
    CONDUCTOR.note(
        f"⚠️ Timeout raffreddamento ({CONDUCTOR.last_temp}°C, target {target:.0f}°C): proseguo")
    return False


def _expo_verdict(median, vmax) -> str:
    """Giudizio sull'esposizione dell'ultimo frame: 'clip' (satura),
    'dark' (troppo scuro), 'ok' (dentro banda)."""
    if vmax is not None and vmax / _MAX16 >= _SAT_LIMIT_FRAC:
        return "clip"
    if median is not None and median / _MAX16 <= _DARK_MEDIAN_FRAC:
        return "dark"
    return "ok"


async def _shoot_one(bridge: Bridge, dev: str, eff: float,
                     overhead: float) -> tuple[Optional[float], Optional[float]]:
    """Fa UNO scatto e ritorna (median, vmax) dell'ultimo frame (per l'analisi)."""
    eff = _clamp(eff, 1.0 / 8000.0, 30.0)
    await bridge.indi.send_number(dev, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": eff})
    ok = await _wait_exposure(bridge, dev, eff, eff + overhead + 8.0)
    if not ok:
        CONDUCTOR.note(f"⚠️ timeout su posa {eff:.4f}s")
    snap = await bridge.state.snapshot()
    lf = snap.get("last_frame") or {}
    m, v = lf.get("median"), lf.get("vmax")
    CONDUCTOR.last_median, CONDUCTOR.last_vmax = m, v
    return m, v


async def _calibrate_phase(bridge: Bridge, dev: str, blk: dict, base: Optional[str],
                           folder: str, overhead: float) -> float:
    """Scatti di CALIBRAZIONE per trovare il tempo giusto della fase PRIMA di
    sparare i keeper — così non si brucia l'intera fase con pose sbagliate
    (es. 20 scatti di corona tutti neri). Ritorna il bias EV (in stop) da
    applicare al bracket. I frame di prova vanno in Eclissi/<fase>/_calib/,
    NON tra gli scatti buoni."""
    exps = blk["exposures"]
    nominal = exps[len(exps) // 2]  # esposizione rappresentativa del bracket
    bias = CONDUCTOR.ev_bias_stops  # parti dalla stima corrente (fasi precedenti)
    if base:
        try:
            await _ensure_upload_local(
                bridge, dev, f"{base}/{folder}/_calib", f"calib_{folder}_XXX")
        except Exception:  # noqa: BLE001
            pass
    CONDUCTOR.phase = "calibrating"
    CONDUCTOR.note(f"🎯 Calibrazione {folder}: cerco il tempo giusto…")
    for attempt in range(_MAX_PROBE):
        if CONDUCTOR.pending.get("abort"):
            raise _Abort()
        eff = _clamp(nominal * (2.0 ** bias), 1.0 / 8000.0, 30.0)
        m, v = await _shoot_one(bridge, dev, eff, overhead)
        CONDUCTOR.calib_shot += 1
        verdict = _expo_verdict(m, v)
        CONDUCTOR.note(
            f"  prova #{attempt + 1}: {eff:.4f}s → {verdict} (median={m}, vmax={v})")
        if verdict == "ok":
            CONDUCTOR.note(f"✅ {folder}: tempo giusto ≈ {eff:.4f}s (bias {bias:+.1f})")
            break
        if verdict == "clip":
            bias = _clamp(bias - 1.0, -_MAX_BIAS_STOPS, _MAX_BIAS_STOPS)  # satura → −1 stop
        else:  # dark: stima gli stop necessari per portare la mediana in banda
            need = _BIAS_STEP
            if m and m > 0:
                need = _clamp(math.log2((_TARGET_MEDIAN_FRAC * _MAX16) / max(m, 1.0)),
                              _BIAS_STEP, 2.0)
            bias = _clamp(bias + need, -_MAX_BIAS_STOPS, _MAX_BIAS_STOPS)
    else:
        CONDUCTOR.note(f"⚠️ {folder}: calibrazione non perfetta, uso bias {bias:+.1f}")
    CONDUCTOR.ev_bias_stops = bias
    return bias


async def _fire_keepers(bridge: Bridge, dev: str, blk: dict, mult: float,
                        overhead: float) -> None:
    """Spara gli scatti BUONI della fase al tempo corretto (×mult), salvati nella
    cartella della fase. Rispetta abort/skip."""
    for expo in blk["exposures"]:
        for _ in range(blk["shots"]):
            if CONDUCTOR.pending.get("abort"):
                raise _Abort()
            if CONDUCTOR.pending.pop("skip", False):
                return
            await _shoot_one(bridge, dev, expo * mult, overhead)
            CONDUCTOR.frames_shot += 1


async def _run(bridge: Bridge) -> None:
    dev = CONDUCTOR.device
    plan = CONDUCTOR.plan
    if not dev or not plan:
        CONDUCTOR.phase = "aborted"
        return
    overhead = float(plan.get("overhead_sec", 1.5))
    try:
        # Raffreddamento: la T deve raggiungere il target PRIMA di scattare.
        if CONDUCTOR.cool:
            CONDUCTOR.phase = "cooling"
            await _wait_for_temperature(bridge, dev, CONDUCTOR.cool_temp)
            if CONDUCTOR.pending.get("abort"):
                raise _Abort()
            CONDUCTOR.phase = "running"
        for i, blk in enumerate(plan["blocks"]):
            if CONDUCTOR.pending.get("abort"):
                raise _Abort()
            CONDUCTOR.block_idx = i
            CONDUCTOR.block_label = blk["label"]
            if CONDUCTOR.pending.pop("skip", False):
                CONDUCTOR.note(f"Salto blocco: {blk['label']}")
                continue

            is_safety = blk.get("priority", 9) <= 1  # Baily/diamante: tempo-critici
            base = CONDUCTOR.base_dir
            # Sottocartella della fase: <images_dir>/Eclissi/<fase>/ — raggruppa
            # perfettamente per fase (corona, baily, cromosfera, protuberanze, ...).
            folder = _phase_folder(blk.get("feature", ""), blk.get("label", ""))

            if is_safety:
                # Baily/diamante: NIENTE calibrazione (durano pochi secondi, pose
                # pre-calcolate e blindate). Si spara subito nella cartella fase.
                CONDUCTOR.phase = "running"
                if base:
                    try:
                        await _ensure_upload_local(
                            bridge, dev, f"{base}/{folder}", f"{folder}_XXX")
                    except Exception as e:  # noqa: BLE001
                        CONDUCTOR.note(f"cartella fase warning: {e}")
                CONDUCTOR.note(f"Blocco {i + 1}: {blk['label']} (sicurezza, ×1.00)")
                await _fire_keepers(bridge, dev, blk, 1.0, overhead)
                continue

            # 1) CALIBRAZIONE: trova il tempo giusto della fase (prove in _calib/),
            #    così se il tempo pianificato è sbagliato non si perde l'intera fase.
            CONDUCTOR.note(f"Blocco {i + 1}: {blk['label']}")
            bias = await _calibrate_phase(bridge, dev, blk, base, folder, overhead)
            if CONDUCTOR.pending.get("abort"):
                raise _Abort()
            # 2) KEEPER: spara la sequenza al tempo corretto, salvando SOLO i buoni.
            CONDUCTOR.phase = "running"
            if base:
                try:
                    await _ensure_upload_local(
                        bridge, dev, f"{base}/{folder}", f"{folder}_XXX")
                    CONDUCTOR.note(f"→ Eclissi/{folder}/ (×{2.0 ** bias:.2f})")
                except Exception as e:  # noqa: BLE001
                    CONDUCTOR.note(f"cartella fase warning: {e}")
            await _fire_keepers(bridge, dev, blk, 2.0 ** bias, overhead)

        CONDUCTOR.phase = "done"
        CONDUCTOR.note(f"Completato: {CONDUCTOR.frames_shot} frame")
    except (_Abort, asyncio.CancelledError):
        CONDUCTOR.phase = "aborted"
        CONDUCTOR.note("Conduttore interrotto")
    except Exception as e:  # noqa: BLE001
        CONDUCTOR.error = str(e)
        CONDUCTOR.phase = "aborted"
        CONDUCTOR.note(f"Errore: {e}")
