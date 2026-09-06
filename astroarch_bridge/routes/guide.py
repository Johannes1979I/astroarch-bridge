"""Route /api/guide: PHD2."""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ..phd2.client import Phd2RpcError

router = APIRouter(prefix="/api/guide", tags=["guide"], dependencies=[Depends(require_token)])
_logger = logging.getLogger("astroarch_bridge.guide")

# --- Guider INTERNO di Ekos (v0.3.19) -----------------------------------
# Oltre a PHD2, l'app può pilotare il guider interno di Ekos via DBus
# (org.kde.kstars.Ekos.Guide). Serve per: (a) utenti che guidano con il
# guider interno invece di PHD2; (b) abilitare in futuro l'AI guiding/GPG,
# che vivono nel guider interno. Tutto ADDITIVO: non tocca il flusso PHD2.
_EKOS_SERVICE = "org.kde.kstars"
_EKOS_GUIDE_PATH = "/KStars/Ekos/Guide"
_EKOS_GUIDE_IFACE = "org.kde.kstars.Ekos.Guide"
# Mappa best-effort Ekos::GuideState (indice enum) → etichetta. Da rifinire
# sul Pi: qdbus può ritornare il nome o il numero, gestiamo entrambi.
_EKOS_GUIDE_STATES = {
    0: "IDLE", 1: "ABORTED", 2: "CONNECTED", 3: "DISCONNECTED",
    4: "CAPTURE", 5: "LOOPING", 6: "STAR_SELECT", 7: "CALIBRATING",
    8: "CALIBRATION_ERROR", 9: "CALIBRATION_SUCCESS", 10: "GUIDING",
    11: "MANUAL_DITHERING", 12: "DITHERING", 13: "DITHERING_SETTLE",
    14: "DITHERING_ERROR", 15: "DITHERING_SUCCESS", 16: "SUSPENDED",
}


def _phd2_http_error(op: str, e: BaseException) -> HTTPException:
    """Mappa eccezioni PHD2 a HTTPException con status code coerenti.
    Risolve il problema "Internal Server Error" quando PHD2 va in timeout
    o è in stato strano: invece di lasciar propagare l'eccezione (che
    diventa 500), ritorniamo 504/503/422 con un detail leggibile."""
    if isinstance(e, asyncio.TimeoutError):
        _logger.warning("PHD2 timeout on %s", op)
        return HTTPException(status_code=504,
            detail=f"PHD2 timeout su {op}. PHD2 è in stato bloccato? "
                   f"Verifica sul desktop che il server sia avviato e "
                   f"che nessun dialog modale stia bloccando.")
    if isinstance(e, Phd2RpcError):
        _logger.warning("PHD2 RPC error on %s: %s", op, e)
        return HTTPException(status_code=422, detail=f"PHD2: {e}")
    if isinstance(e, ConnectionError):
        _logger.warning("PHD2 not reachable on %s: %s", op, e)
        return HTTPException(status_code=503,
            detail=f"PHD2 non raggiungibile. Avvia PHD2 e abilita il server.")
    _logger.exception("PHD2 unexpected error on %s", op)
    return HTTPException(status_code=500,
        detail=f"Errore inatteso su {op}: {type(e).__name__}: {e}")


@router.get("/status")
async def status(bridge: Bridge = Depends(get_bridge)) -> dict:
    return {
        "connection": bridge.phd2.state,
        "live": dict(bridge.phd2.live),
    }


@router.post("/start")
async def start(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        result = await bridge.phd2.start_guiding(
            settle_pixels=float(payload.get("settle_pixels", 1.5)),
            settle_time=float(payload.get("settle_time", 10.0)),
            settle_timeout=float(payload.get("settle_timeout", 60.0)),
        )
    except Exception as e:
        raise _phd2_http_error("start_guiding", e)
    return {"ok": True, "result": result}


@router.post("/stop")
async def stop(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        await bridge.phd2.stop_capture()
    except Exception as e:
        raise _phd2_http_error("stop", e)
    return {"ok": True}


@router.post("/dither")
async def dither(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Dither PHD2.

    Se `wait=true` (default ora), dopo l'ACK della RPC blocca finché PHD2 emette
    SettleDone (settling torna False) o scade `settle_timeout`. Cosi' il client
    (app) sa che il settling e' davvero finito e puo' scattare il frame dopo
    senza trail. Parametri settle presi dal payload (l'app passa i valori
    configurati in Ekos/PHD2: amount 5px, settle_time 30s, ecc.).
    """
    import asyncio
    import time as _time
    amount = float(payload.get("amount", 3.0))
    ra_only = bool(payload.get("ra_only", False))
    settle_pixels = float(payload.get("settle_pixels", 1.5))
    settle_time = float(payload.get("settle_time", 10.0))
    settle_timeout = float(payload.get("settle_timeout", 60.0))
    wait = bool(payload.get("wait", True))
    try:
        result = await bridge.phd2.dither(
            amount=amount,
            ra_only=ra_only,
            settle_pixels=settle_pixels,
            settle_time=settle_time,
            settle_timeout=settle_timeout,
        )
    except Exception as e:
        raise _phd2_http_error("dither", e)

    settled = None
    if wait:
        # Aspetta Settling=True (max 5s) poi Settling=False (max settle_timeout+5)
        t0 = _time.monotonic()
        while _time.monotonic() - t0 < 5.0:
            if bridge.phd2.live.get("settling") is True:
                break
            await asyncio.sleep(0.2)
        t1 = _time.monotonic()
        max_wait = settle_timeout + 5.0
        settled = True
        while _time.monotonic() - t1 < max_wait:
            if bridge.phd2.live.get("settling") is not True:
                break
            await asyncio.sleep(0.3)
        else:
            settled = False  # timeout
    return {"ok": True, "result": result, "settled": settled}


@router.post("/connect_equipment")
async def connect_equipment(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Connette TUTTE le periferiche del profilo PHD2 attivo (RPC set_connected).

    Body opzionale: {"connected": true|false, "profile_id": <int>}.
    Se `profile_id` e' fornito, prima disconnette, seleziona il profilo, poi
    connette (PHD2 richiede equipment disconnesso per cambiare profilo).
    """
    connected = bool(payload.get("connected", True))
    profile_id = payload.get("profile_id")
    try:
        if profile_id is not None:
            try:
                await bridge.phd2.set_connected(False)
            except Exception:
                pass
            await bridge.phd2.set_profile(int(profile_id))
        result = await bridge.phd2.set_connected(connected)
    except Exception as e:
        raise _phd2_http_error("connect_equipment", e)
    return {"ok": True, "connected": connected, "result": result}


@router.post("/loop")
async def loop_(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        await bridge.phd2.loop()
    except Exception as e:
        raise _phd2_http_error("loop", e)
    return {"ok": True}


@router.post("/clear_calibration")
async def clear_calibration(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    which = payload.get("which", "Both")
    try:
        await bridge.phd2.clear_calibration(which)
    except Exception as e:
        raise _phd2_http_error("clear_calibration", e)
    return {"ok": True}


@router.post("/pause")
async def pause(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        await bridge.phd2.set_paused(bool(payload.get("paused", True)),
                                    full=bool(payload.get("full", False)))
    except Exception as e:
        raise _phd2_http_error("pause", e)
    return {"ok": True}


@router.post("/find_star")
async def find_star(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        r = await bridge.phd2.call("find_star", timeout=30.0)
    except Exception as e:
        raise _phd2_http_error("find_star", e)
    return {"ok": True, "result": r}


@router.post("/calibrate")
async def calibrate(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Avvia calibration completa: clear cal → loop una volta per essere
    sicuri che ci sia un frame fresco → guide(recalibrate=True).

    Bug fix v0.2.25: prima si chiamava direttamente `guide` senza alcun
    timeout esteso, e PHD2 a volte impiegava >10s a rispondere
    all'acknowledgment se la sua stato interno era in transizione
    (Looping/Stopped/Selected). Risultato: asyncio.TimeoutError →
    Internal Server Error 500 visibile in app.
    Adesso:
      - log esplicito di ogni step
      - timeout esteso a 30s (l'acknowledgment di "guide" deve essere
        comunque rapido ma diamo margine)
      - errori mappati a 504/422/503 con detail leggibili
    """
    _logger.info("calibrate: clearing calibration (Both)")
    try:
        await bridge.phd2.call("clear_calibration", "Both", timeout=10.0)
    except Exception as e:
        raise _phd2_http_error("calibrate (clear_calibration)", e)

    # Piccola pausa: PHD2 ha bisogno di un attimo per processare il clear
    # prima di poter accettare un nuovo guide command.
    await asyncio.sleep(0.5)

    _logger.info("calibrate: triggering guide(recalibrate=True)")
    try:
        await bridge.phd2.call("guide", {
            "settle": {"pixels": 1.5, "time": 10.0, "timeout": 60.0},
            "recalibrate": True,
        }, timeout=30.0)
    except Exception as e:
        raise _phd2_http_error("calibrate (guide)", e)
    return {"ok": True}


@router.get("/profile")
async def profile(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Restituisce info su profilo guide attivo (camera, mount, scope)."""
    try:
        eq = await bridge.phd2.call("get_current_equipment", timeout=5.0)
    except Exception:
        eq = None
    info: dict = {"equipment": eq or {}}
    try:
        info["pixel_scale"] = await bridge.phd2.call("get_pixel_scale", timeout=3.0)
    except Exception:
        pass
    try:
        info["calibrated"] = await bridge.phd2.call("get_calibrated", timeout=3.0)
    except Exception:
        pass
    try:
        info["app_state"] = await bridge.phd2.call("get_app_state", timeout=3.0)
    except Exception:
        pass
    return info


@router.get("/star_image")
async def star_image(
    fmt: str = "json", size: int = 0,
    bridge: Bridge = Depends(get_bridge),
):
    """Ritorna l'immagine del riquadro intorno alla stella di guida di PHD2.

    PHD2 espone `get_star_image` via JSON-RPC che ritorna:
      {
        "frame": int,                  # numero frame
        "width": int, "height": int,   # dimensioni del crop in pixel
        "star_pos": [x, y],            # posizione stella nel crop
        "pixels": "<base64-rawdata>"   # array di uint16 little-endian
      }
    Lo riconvertiamo in PNG 8-bit stretchato (auto-stretch in stile PI)
    così la app può mostrarlo direttamente con <Image.memory>.

    Query:
      fmt:  "json" (default, ritorna anche PNG in base64) o "png" (binary)
      size: opzionale, suggerimento dimensione (ignorato da PHD2 di solito)
    """
    import base64
    import io
    import struct
    import numpy as np
    from fastapi.responses import Response
    from ..images.processor import _percentile_stretch

    params: list = []
    if size > 0:
        params = [size]
    try:
        res = await bridge.phd2.call("get_star_image", params, timeout=5.0)
    except Phd2RpcError as e:
        # PHD2 ritorna errore se non c'è stella selezionata o se è in modalità
        # incompatibile (es. looping ma senza star)
        raise HTTPException(status_code=409, detail=f"PHD2: {e}")
    except asyncio.TimeoutError:
        # Comune: get_star_image durante settling/transitorio. Trattiamo
        # come 409 (transitoriamente non disponibile) così l'UI mostra
        # "no star selected" invece di spammare errori.
        raise HTTPException(status_code=409,
            detail="PHD2 non ha risposto in tempo (probabilmente nessuna stella selezionata)")
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"PHD2 not reachable: {e}")
    except Exception as e:
        _logger.exception("get_star_image unexpected")
        raise HTTPException(status_code=500,
            detail=f"PHD2 get_star_image unexpected: {type(e).__name__}: {e}")

    if not isinstance(res, dict) or "pixels" not in res:
        raise HTTPException(status_code=502,
                            detail=f"PHD2 get_star_image bad payload: {res}")

    w = int(res.get("width", 0))
    h = int(res.get("height", 0))
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=502, detail="PHD2: invalid image size")

    # pixels è base64 di un array di uint16 (PHD2 convention)
    try:
        raw = base64.b64decode(res["pixels"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PHD2 b64 decode: {e}")

    if len(raw) != w * h * 2:
        raise HTTPException(status_code=502,
                            detail=f"PHD2: pixel buffer len {len(raw)} != {w*h*2}")

    arr = np.frombuffer(raw, dtype="<u2").reshape((h, w)).astype(np.float64)

    # Stretch con lo stesso algoritmo che usiamo per i frame Ekos.
    stretched = _percentile_stretch(arr)

    # Crea PNG via PIL
    try:
        from PIL import Image
        img = Image.fromarray(stretched, mode="L").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        png_bytes = buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PNG encode: {e}")

    star_pos = res.get("star_pos") or [w / 2.0, h / 2.0]
    payload = {
        "frame": res.get("frame"),
        "width": w,
        "height": h,
        "star_x": float(star_pos[0]) if len(star_pos) > 0 else None,
        "star_y": float(star_pos[1]) if len(star_pos) > 1 else None,
    }

    if fmt.lower() == "png":
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Star-X": str(payload["star_x"] or ""),
                                 "X-Star-Y": str(payload["star_y"] or ""),
                                 "X-Width": str(w),
                                 "X-Height": str(h),
                                 "X-Frame": str(payload["frame"] or "")})
    payload["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
    return payload


# ============================================================================
# v0.3.19: GUIDER INTERNO di Ekos (via DBus qdbus6). Additivo — il flusso
# PHD2 sopra resta invariato. Endpoint sotto il prefisso /api/guide/ekos_*.
# ============================================================================

async def _qdbus_call(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    """qdbus6 → (returncode, stdout). Copia locale isolata (non dipende da
    altri route module) per pilotare org.kde.kstars.Ekos.Guide."""
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    proc = await asyncio.create_subprocess_exec(
        "qdbus6", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, stdout.decode("utf-8", "replace").strip()


async def _guide_dbus(method: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    return await _qdbus_call(_EKOS_SERVICE, _EKOS_GUIDE_PATH,
                             f"{_EKOS_GUIDE_IFACE}.{method}", *args, timeout=timeout)

# INDI DBus: per prendere il frame della camera di guida quando si usa il
# guider INTERNO (PHD2 non gira). org.kde.kstars.INDI.getBLOBFile ritorna il
# path del FITS dell'ultimo BLOB ricevuto dal device.
_INDI_PATH = "/KStars/INDI"
_INDI_IFACE = "org.kde.kstars.INDI"


async def _indi_dbus(method: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    return await _qdbus_call(_EKOS_SERVICE, _INDI_PATH,
                             f"{_INDI_IFACE}.{method}", *args, timeout=timeout)


def _read_guider_type() -> "int | None":
    """GuiderType da kstarsrc [Guide]: 0=internal, 1=PHD2, 2=LinGuider.
    None se assente. Read-only, non modifica nulla."""
    from pathlib import Path
    cfg = Path.home() / ".config/kstarsrc"
    if not cfg.exists():
        return None
    try:
        in_guide = False
        for line in cfg.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t.startswith("[") and t.endswith("]"):
                in_guide = (t == "[Guide]")
                continue
            if in_guide and t.startswith("GuiderType="):
                try:
                    return int(t.split("=", 1)[1])
                except ValueError:
                    return None
    except Exception as e:
        _logger.warning("cannot read GuiderType: %s", e)
    # KStars omits GuiderType when it equals the default (0=internal): absent key => internal
    return 0


def _parse_float_list(raw: str) -> "list[float]":
    out: list[float] = []
    for tok in raw.replace(",", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


@router.get("/backend")
async def guide_backend() -> dict:
    """Quale guider usa Ekos: 'internal' | 'phd2' | 'linguider' | None.
    L'app lo legge per adattare la UI (pannello PHD2 vs guider interno)."""
    gt = _read_guider_type()
    name = {0: "internal", 1: "phd2", 2: "linguider"}.get(gt)
    return {"backend": name, "guider_type": gt}


@router.get("/ekos_status")
async def guide_ekos_status() -> dict:
    """Stato del guider INTERNO di Ekos via DBus: stato + RMS RA/DEC (arcsec)."""
    out: dict = {"state_raw": None, "state": None,
                 "rms_ra": None, "rms_dec": None, "rms_total": None,
                 "delta_ra": None, "delta_dec": None,
                 "camera": None, "guider": None, "exposure": None, "log": []}
    rc, val = await _guide_dbus("status", timeout=6.0)
    if rc == 0 and val.strip():
        v = val.strip()
        out["state_raw"] = v
        out["state"] = _EKOS_GUIDE_STATES.get(int(v), v) if v.lstrip("-").isdigit() else v
    rc, val = await _guide_dbus("axisSigma", timeout=6.0)
    if rc == 0:
        nums = _parse_float_list(val)
        if len(nums) >= 2:
            out["rms_ra"], out["rms_dec"] = nums[0], nums[1]
            out["rms_total"] = round((nums[0] ** 2 + nums[1] ** 2) ** 0.5, 3)
    rc, val = await _guide_dbus("axisDelta", timeout=6.0)
    if rc == 0:
        nums = _parse_float_list(val)
        if len(nums) >= 2:
            out["delta_ra"], out["delta_dec"] = nums[0], nums[1]
    # Info aggiuntive per la UI: camera, guider, esposizione, ultime righe di log
    rc, val = await _guide_dbus("camera", timeout=4.0)
    out["camera"] = val.strip() if rc == 0 and val.strip() else None
    rc, val = await _guide_dbus("guider", timeout=4.0)
    out["guider"] = val.strip() if rc == 0 and val.strip() else None
    rc, val = await _guide_dbus("exposure", timeout=4.0)
    try:
        out["exposure"] = float(val.strip()) if rc == 0 and val.strip() else None
    except ValueError:
        out["exposure"] = None
    rc, val = await _guide_dbus("logText", timeout=4.0)
    if rc == 0 and val.strip():
        lines = [ln for ln in val.splitlines() if ln.strip()]
        out["log"] = lines[-8:]
    else:
        out["log"] = []
    return out


@router.post("/ekos_start")
async def guide_ekos_start() -> dict:
    """Avvia l'autoguiding col guider interno di Ekos (guide())."""
    rc, out = await _guide_dbus("guide", timeout=15.0)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"Ekos.Guide.guide fallito: {out}")
    return {"ok": True, "raw": out}


@router.post("/ekos_stop")
async def guide_ekos_stop() -> dict:
    """Ferma calibrazione/guiding/dithering (abort())."""
    rc, out = await _guide_dbus("abort", timeout=10.0)
    return {"ok": rc == 0, "raw": out}


@router.post("/ekos_calibrate")
async def guide_ekos_calibrate() -> dict:
    """Ricalibra il guider interno: clearCalibration() → calibrate()."""
    await _guide_dbus("clearCalibration", timeout=10.0)
    await asyncio.sleep(0.4)
    rc, out = await _guide_dbus("calibrate", timeout=15.0)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"Ekos.Guide.calibrate fallito: {out}")
    return {"ok": True, "raw": out}


@router.post("/ekos_dither")
async def guide_ekos_dither() -> dict:
    """Dither immediato in direzione casuale (dither())."""
    rc, out = await _guide_dbus("dither", timeout=10.0)
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"Ekos.Guide.dither fallito: {out}")
    return {"ok": True, "raw": out}


@router.post("/ekos_loop")
async def guide_ekos_loop() -> dict:
    """Loop continuo dei frame di guida (loop()), utile per framing/star select."""
    rc, out = await _guide_dbus("loop", timeout=10.0)
    return {"ok": rc == 0, "raw": out}


@router.get("/ekos_full_frame")
async def guide_ekos_full_frame(fmt: str = "json", max_dim: int = 1024,
                                timeout: float = 20.0) -> dict:
    """Frame LIVE della camera di guida quando si usa il GUIDER INTERNO.

    Parita' con /full_frame (PHD2) ma senza PHD2: apriamo un client INDI
    dedicato su :7624 e ci iscriviamo allo stream BLOB della camera di guida
    (la modalita' BLOB e' per-client, quindi Ekos continua a guidare
    indisturbato). Il FITS ricevuto viene stretchato (STF stile PI) e servito
    come PNG.

    Richiede che la camera stia esponendo (LOOP o guiding attivo): altrimenti
    non c'e' nessun frame da ricevere e rispondiamo 409.
    """
    import base64
    import io
    import numpy as np
    from fastapi.responses import Response
    from ..images.processor import _percentile_stretch, _read_fits_bytes
    from ..indi_blob import grab_blob

    # 1. nome della camera di guida (dal modulo Guide di Ekos)
    rc, cam = await _guide_dbus("camera", timeout=6.0)
    cam = (cam or "").strip()
    if rc != 0 or not cam:
        raise HTTPException(status_code=409,
            detail="Camera di guida non disponibile. Avvia Ekos e imposta il "
                   "guider interno con una camera di guida.")

    # 2. prossimo frame dallo stream BLOB INDI
    try:
        raw = await grab_blob(cam, timeout=max(3.0, min(timeout, 60.0)))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=409,
            detail=f"Nessun frame da '{cam}' entro il timeout. Avvia il LOOP "
                   "o la guida e riprova.")
    except ConnectionError as e:
        raise HTTPException(status_code=503,
            detail=f"Server INDI non raggiungibile: {e}")
    except Exception as e:
        _logger.exception("grab_blob fallito")
        raise HTTPException(status_code=500,
            detail=f"Errore lettura frame INDI: {type(e).__name__}: {e}")

    # 3. FITS (bytes) -> stretch -> PNG
    try:
        data, _hdr = _read_fits_bytes(raw)
        data = np.asarray(data, dtype=np.float64)
    except Exception as e:
        raise HTTPException(status_code=502,
            detail=f"Frame ricevuto ma non leggibile come FITS: {e}")

    if data.ndim == 3:  # eventuale RGB: prendi la luminanza del primo piano
        data = data[0]
    if data.ndim != 2:
        raise HTTPException(status_code=502,
            detail=f"FITS shape inattesa: {data.shape} (atteso 2D)")

    h, w = data.shape
    stretched = _percentile_stretch(data)
    from PIL import Image
    img = Image.fromarray(stretched, mode="L")
    if max_dim > 0 and (w > max_dim or h > max_dim):
        scale = max_dim / max(w, h)
        w2, h2 = int(w * scale), int(h * scale)
        img = img.resize((w2, h2), Image.LANCZOS)
        w, h = w2, h2
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    png_bytes = buf.getvalue()

    if fmt.lower() == "png":
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Width": str(w), "X-Height": str(h)})
    return {"width": w, "height": h, "camera": cam,
            "png_base64": base64.b64encode(png_bytes).decode("ascii")}


# v0.3.3: endpoint full-frame.
# PHD2 JSON-RPC NON espone direttamente un'API per il frame intero della
# camera di guida (`get_star_image` torna solo il crop ~100×100 intorno
# alla stella). L'unico modo per recuperare il frame completo è chiamare
# `save_image` che salva un FITS sul filesystem del Pi, poi lo leggiamo
# noi, lo stretchiamo con lo stesso STF di PI e ritorniamo PNG.
# Risultato: identico a quello che l'utente vede nella finestra principale
# di PHD2 sul desktop.
@router.get("/full_frame")
async def full_frame(
    fmt: str = "json", max_dim: int = 1024,
    bridge: Bridge = Depends(get_bridge),
):
    """Frame completo della camera di guida via PHD2 save_image.

    Query:
      fmt:     "json" (default, ritorna PNG in base64) o "png" (binary stream)
      max_dim: downscale alla dimensione massima richiesta (default 1024 px)
               per ridurre traffico sulla rete Tailscale. 0 = no resize.

    Flusso interno:
      1. RPC `save_image` su PHD2 → ritorna {"filename": "/path/to.fits"}
      2. Leggiamo il FITS con astropy
      3. Auto-stretch (PixInsight STF) + downscale a max_dim
      4. PNG encode + cleanup del FITS temporaneo
    """
    import base64
    import io
    import os
    import numpy as np
    from fastapi.responses import Response
    from ..images.processor import _percentile_stretch

    try:
        res = await bridge.phd2.call("save_image", timeout=8.0)
    except Phd2RpcError as e:
        # Tipico: PHD2 non sta ancora loopando o non c'è una camera attiva
        raise HTTPException(status_code=409, detail=f"PHD2: {e}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504,
            detail="PHD2 timeout su save_image (camera attiva?)")
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"PHD2 not reachable: {e}")
    except Exception as e:
        _logger.exception("save_image unexpected")
        raise HTTPException(status_code=500,
            detail=f"PHD2 save_image unexpected: {type(e).__name__}: {e}")

    if not isinstance(res, dict) or "filename" not in res:
        raise HTTPException(status_code=502,
                            detail=f"PHD2 save_image bad payload: {res}")
    fits_path = res["filename"]

    # Legge il FITS prodotto da PHD2
    try:
        from astropy.io import fits  # type: ignore
        with fits.open(fits_path, memmap=False) as hdul:
            data = np.asarray(hdul[0].data, dtype=np.float64)
    except FileNotFoundError:
        raise HTTPException(status_code=502,
            detail=f"PHD2 ha salvato {fits_path} ma il bridge non lo trova "
                   "(il bridge gira su un host diverso da PHD2?)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FITS read error: {e}")
    finally:
        # Cleanup: cancelliamo il FITS subito, è solo un buffer di trasferimento.
        # Se fallisce non è grave (PHD2 sovrascrive comunque al prossimo save).
        try:
            os.unlink(fits_path)
        except Exception:
            pass

    if data.ndim != 2:
        raise HTTPException(status_code=502,
            detail=f"FITS shape inattesa: {data.shape} (atteso 2D)")

    h, w = data.shape
    # Downscale opzionale per ridurre traffico via Tailscale
    if max_dim > 0 and (w > max_dim or h > max_dim):
        from PIL import Image  # local import to avoid forcing PIL global
        stretched = _percentile_stretch(data)
        img = Image.fromarray(stretched, mode="L")
        scale = max_dim / max(w, h)
        new_w = int(w * scale); new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        w, h = new_w, new_h
    else:
        stretched = _percentile_stretch(data)
        from PIL import Image
        img = Image.fromarray(stretched, mode="L")

    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    png_bytes = buf.getvalue()

    if fmt.lower() == "png":
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Width": str(w),
                                 "X-Height": str(h)})
    return {
        "width": w,
        "height": h,
        "png_base64": base64.b64encode(png_bytes).decode("ascii"),
    }
