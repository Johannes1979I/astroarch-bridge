"""Route /api/skymap — planetario KStars via DBus.

Espone il SkyMap di KStars (il planetario) all'app mobile come immagine
live + controlli di navigazione, riusando il rendering REALE di KStars
(cataloghi completi, ora corrente, crosshair del telescopio se l'INDI
mount è connesso a KStars).

Metodi KStars DBus usati (org.kde.kstars, path /KStars), verificati su
AstroArch + KStars 3.8.x:
  - exportImage(file, w, h, includeLegend)  → PNG del SkyMap corrente
  - getFocusInformationXML()                → centro RA/Dec, FOV, Alt/Az, oggetto
  - setRaDec(ra_hours, dec_deg)             → centra su coordinate
  - lookTowards("M 51")                     → centra su oggetto per nome
  - setApproxFOV(deg) / zoomIn / zoomOut    → zoom
  - setTracking(bool)                       → blocca/sblocca tracking

NON-INVASIVO: il planetario è una vista separata. Centrare/zoomare il
SkyMap NON muove il telescopio. Il goto vero passa per /api/mount/goto
con conferma esplicita lato app.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response

from ..auth import require_token
from ..dbus_session import with_session_bus

router = APIRouter(prefix="/api/skymap", tags=["skymap"],
                   dependencies=[Depends(require_token)])
_logger = logging.getLogger("astroarch_bridge.skymap")

KSTARS_SERVICE = "org.kde.kstars"
KSTARS_PATH = "/KStars"
_EXPORT_PATH = "/tmp/astroarch_bridge_skymap.png"


async def _kstars_dbus(method: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Esegue qdbus6 verso KStars con il DBUS_SESSION_BUS_ADDRESS giusto
    (la sessione utente grafica). Ritorna (returncode, stdout)."""
    env = with_session_bus()
    proc = await asyncio.create_subprocess_exec(
        "qdbus6", KSTARS_SERVICE, KSTARS_PATH, method, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def _get_focus() -> dict:
    """Legge getFocusInformationXML e lo parsa in dict.
    Ritorna {} se KStars non risponde."""
    rc, out = await _kstars_dbus("org.kde.kstars.getFocusInformationXML", timeout=8.0)
    if rc != 0 or not out.startswith("<?xml"):
        return {}
    try:
        root = ET.fromstring(out)
        def _f(tag):
            el = root.find(tag)
            if el is None or not el.text:
                return None
            try:
                v = float(el.text)
            except ValueError:
                return None
            # KStars in stato degenere (mappa vuota / puntamento indefinito)
            # può restituire nan/inf → NON serializzabili in JSON. Li annulliamo.
            if not math.isfinite(v):
                return None
            return v
        def _s(tag):
            el = root.find(tag)
            return el.text.strip() if el is not None and el.text else None
        return {
            "fov_deg": _f("FOV_Degrees"),
            "ra_deg": _f("RA_JNow_Degrees"),
            "dec_deg": _f("Dec_JNow_Degrees"),
            "ra_hms": _s("RA_JNow_HMS"),
            "dec_dms": _s("Dec_JNow_DMS"),
            "alt_deg": _f("Altitude_Degrees"),
            "az_deg": _f("Azimuth_Degrees"),
            "object": _s("Focused_Object"),
        }
    except Exception as e:
        _logger.warning("getFocus parse error: %s", e)
        return {}


async def _kstars_alive() -> bool:
    rc, _ = await _kstars_dbus("org.kde.kstars.getFocusInformationXML", timeout=5.0)
    return rc == 0


def _kstars_down_error() -> HTTPException:
    return HTTPException(status_code=503,
        detail="KStars non in esecuzione. Avvialo dall'app (Dashboard → "
               "LAUNCH KStars) o dal desktop del Raspberry.")


@router.get("/focus")
async def focus() -> dict:
    """Centro corrente del planetario: RA/Dec, FOV, Alt/Az, oggetto al centro."""
    f = await _get_focus()
    if not f:
        raise _kstars_down_error()
    return f


@router.get("/view")
async def view(width: int = 900, height: int = 600, fov: float = 0.0,
               legend: bool = False, fmt: str = "json"):
    """Esporta il SkyMap corrente di KStars come PNG.

    Query:
      width, height : dimensioni immagine in px (default 900x600)
      fov           : se >0, imposta prima il FOV approssimato in gradi
      legend        : includi la legenda KStars (default no)
      fmt           : "json" (default, PNG in base64 + focus) o "png" (binary)

    Ritorna anche le info di focus (centro RA/Dec + FOV) per il tap-to-goto.
    """
    import base64

    if not await _kstars_alive():
        raise _kstars_down_error()

    # clamp dimensioni (evita richieste assurde)
    width = max(200, min(2000, int(width)))
    height = max(150, min(1500, int(height)))

    if fov and fov > 0:
        await _kstars_dbus("org.kde.kstars.setApproxFOV", f"{fov:g}", timeout=6.0)
        await asyncio.sleep(0.3)

    # Esporta. exportImage è Q_NOREPLY → non ritorna, dobbiamo poi leggere il file.
    try:
        os.unlink(_EXPORT_PATH)
    except Exception:
        pass
    await _kstars_dbus("org.kde.kstars.exportImage", _EXPORT_PATH,
                       str(width), str(height),
                       "true" if legend else "false", timeout=10.0)

    # Attende che il file sia scritto (KStars lo genera in modo asincrono)
    png_bytes = None
    for _ in range(20):  # max ~2s
        try:
            if os.path.exists(_EXPORT_PATH) and os.path.getsize(_EXPORT_PATH) > 0:
                with open(_EXPORT_PATH, "rb") as fh:
                    png_bytes = fh.read()
                break
        except Exception:
            pass
        await asyncio.sleep(0.1)

    if not png_bytes:
        raise HTTPException(status_code=502,
            detail="KStars non ha generato l'immagine SkyMap (exportImage).")

    foc = await _get_focus()

    if fmt.lower() == "png":
        headers = {"Cache-Control": "no-store"}
        if foc.get("ra_deg") is not None:
            headers["X-Center-RA-Deg"] = str(foc["ra_deg"])
            headers["X-Center-Dec-Deg"] = str(foc["dec_deg"])
            headers["X-FOV-Deg"] = str(foc.get("fov_deg", ""))
        return Response(content=png_bytes, media_type="image/png", headers=headers)

    return {
        "width": width, "height": height,
        "png_base64": base64.b64encode(png_bytes).decode("ascii"),
        "focus": foc,
    }


@router.post("/center")
async def center(payload: dict = Body(default={})) -> dict:
    """Centra il planetario.

    Body (una delle forme):
      {"object": "M 51"}                 → lookTowards (per nome)
      {"ra_deg": 202.4, "dec_deg": 47.2} → setRaDec (coordinate in gradi)
      {"ra_hours": 13.5, "dec_deg": 47}  → setRaDec (RA in ore)
      {"alt": 45, "az": 180}             → setAltAz
      {"direction": "zenith"}            → lookTowards direzione speciale
    """
    if not await _kstars_alive():
        raise _kstars_down_error()

    obj = payload.get("object") or payload.get("direction")
    if obj:
        # lookTowards vuole "M 51" con spazio per i Messier
        await _kstars_dbus("org.kde.kstars.lookTowards", str(obj), timeout=8.0)
    elif payload.get("alt") is not None and payload.get("az") is not None:
        await _kstars_dbus("org.kde.kstars.setAltAz",
                           f"{float(payload['alt']):g}", f"{float(payload['az']):g}",
                           timeout=8.0)
    else:
        # coordinate: setRaDec vuole RA in ORE, Dec in gradi
        if payload.get("ra_hours") is not None:
            ra_h = float(payload["ra_hours"])
        elif payload.get("ra_deg") is not None:
            ra_h = float(payload["ra_deg"]) / 15.0
        else:
            raise HTTPException(status_code=400,
                detail="Specifica object | ra_deg+dec_deg | ra_hours+dec_deg | alt+az")
        dec = float(payload["dec_deg"])
        await _kstars_dbus("org.kde.kstars.setRaDec", f"{ra_h:g}", f"{dec:g}",
                           timeout=8.0)
    await asyncio.sleep(0.4)
    return {"ok": True, "focus": await _get_focus()}


@router.post("/zoom")
async def zoom(payload: dict = Body(default={})) -> dict:
    """Zoom del planetario.

    Body:
      {"fov_deg": 5.0}    → setApproxFOV (FOV target in gradi)
      {"dir": "in"}       → zoomIn
      {"dir": "out"}      → zoomOut
      {"default": true}   → defaultZoom
    """
    if not await _kstars_alive():
        raise _kstars_down_error()
    if payload.get("fov_deg") is not None:
        await _kstars_dbus("org.kde.kstars.setApproxFOV",
                           f"{float(payload['fov_deg']):g}", timeout=6.0)
    elif payload.get("default"):
        await _kstars_dbus("org.kde.kstars.defaultZoom", timeout=6.0)
    else:
        d = (payload.get("dir") or "in").lower()
        method = "org.kde.kstars.zoomIn" if d == "in" else "org.kde.kstars.zoomOut"
        await _kstars_dbus(method, timeout=6.0)
    await asyncio.sleep(0.3)
    return {"ok": True, "focus": await _get_focus()}


@router.post("/center_telescope")
async def center_telescope() -> dict:
    """Centra il planetario sulla posizione CORRENTE del telescopio.

    Legge le coordinate del mount dalle property INDI (EQUATORIAL_EOD_COORD)
    e centra lì il SkyMap. Non muove il telescopio — solo la vista.
    """
    if not await _kstars_alive():
        raise _kstars_down_error()
    # KStars, se slaved al telescopio INDI, disegna già il crosshair.
    # Centrare sulla posizione: usiamo lookTowards("telescope") se supportato,
    # altrimenti l'app passa le coord del mount via /center.
    rc, out = await _kstars_dbus("org.kde.kstars.lookTowards", "telescope", timeout=8.0)
    await asyncio.sleep(0.4)
    return {"ok": True, "focus": await _get_focus()}


@router.post("/pan")
async def pan(payload: dict = Body(...)) -> dict:
    """Scorri la mappa trascinando (come il mouse su KStars desktop).

    L'app invia il delta del trascinamento in pixel dell'immagine + le sue
    dimensioni. Il bridge calcola il nuovo centro (proiezione tangente dal
    centro+FOV correnti) e ri-centra il SkyMap con setRaDec.

    Convenzione "mappa che segue il dito": trascinare verso destra porta in
    vista il cielo che era a sinistra (il centro si sposta di conseguenza).

    Body: {"dx": float, "dy": float, "width": int, "height": int}
      dx, dy = spostamento del dito in pixel (B - A) sull'immagine
    """
    if not await _kstars_alive():
        raise _kstars_down_error()
    try:
        dx = float(payload["dx"]); dy = float(payload["dy"])
        w = float(payload["width"]); h = float(payload["height"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Richiede dx, dy, width, height")

    foc = await _get_focus()
    if not foc or foc.get("ra_deg") is None:
        raise _kstars_down_error()
    fov = foc.get("fov_deg") or 10.0
    deg_per_px = fov / w
    dec0 = math.radians(foc["dec_deg"])
    cosd = math.cos(dec0) if abs(math.cos(dec0)) > 1e-3 else 1e-3

    # "Mappa segue il dito": trascino a destra (dx>0) → vedo cielo a sinistra
    # → in equatoriale nord-su sinistra = RA crescente → centro RA aumenta.
    ra_new = (foc["ra_deg"] + (dx * deg_per_px) / cosd) % 360.0
    # trascino in basso (dy>0) → vedo cielo in alto → Dec aumenta
    dec_new = max(-89.9, min(89.9, foc["dec_deg"] + dy * deg_per_px))

    await _kstars_dbus("org.kde.kstars.setTracking", "false", timeout=5.0)
    await _kstars_dbus("org.kde.kstars.setRaDec", f"{ra_new / 15.0:g}", f"{dec_new:g}",
                       timeout=8.0)
    await asyncio.sleep(0.3)
    return {"ok": True, "focus": await _get_focus()}


@router.post("/tap")
async def tap(payload: dict = Body(...)) -> dict:
    """FASE 2: tap-to-goto.

    L'app invia il pixel toccato + dimensioni immagine. Il bridge:
      1. stima le coordinate RA/Dec del punto (proiezione tangente, nord-su,
         dal centro+FOV correnti)
      2. centra lì il SkyMap (setRaDec) e sblocca il tracking
      3. ri-legge getFocus → KStars riporta il centro PRECISO + l'oggetto
         più vicino (Focused_Object), che usiamo come candidato

    Ritorna le coordinate precise + l'oggetto candidato. L'app mostra un
    dialog di conferma e poi chiama /api/mount/goto. NIENTE goto automatico.

    Body: {"x": int, "y": int, "width": int, "height": int}
    """
    if not await _kstars_alive():
        raise _kstars_down_error()
    try:
        x = float(payload["x"]); y = float(payload["y"])
        w = float(payload["width"]); h = float(payload["height"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Richiede x, y, width, height")

    foc = await _get_focus()
    if not foc or foc.get("ra_deg") is None:
        raise _kstars_down_error()

    ra0 = math.radians(foc["ra_deg"])
    dec0 = math.radians(foc["dec_deg"])
    fov = foc.get("fov_deg") or 10.0  # gradi, lato orizzontale del SkyMap

    # Offset angolare del pixel dal centro (proiezione tangente, nord-su).
    # Scala: FOV è circa la dimensione orizzontale visibile.
    deg_per_px = fov / w
    dx_deg = (x - w / 2.0) * deg_per_px   # +verso destra (RA decrescente)
    dy_deg = (y - h / 2.0) * deg_per_px   # +verso il basso (Dec decrescente)

    # In equatoriale nord-su: destra = RA minore, su = Dec maggiore.
    dec_tap = foc["dec_deg"] - dy_deg
    # correzione RA per coseno declinazione
    cosd = math.cos(dec0) if abs(math.cos(dec0)) > 1e-3 else 1e-3
    ra_tap = foc["ra_deg"] - dx_deg / cosd
    ra_tap %= 360.0
    dec_tap = max(-89.9, min(89.9, dec_tap))

    # Centra lì (sblocca tracking così setRaDec "tiene")
    await _kstars_dbus("org.kde.kstars.setTracking", "false", timeout=5.0)
    await _kstars_dbus("org.kde.kstars.setRaDec", f"{ra_tap / 15.0:g}", f"{dec_tap:g}",
                       timeout=8.0)
    await asyncio.sleep(0.5)
    foc2 = await _get_focus()

    return {
        "ok": True,
        "estimate": {"ra_deg": ra_tap, "dec_deg": dec_tap},
        "focus": foc2,            # centro PRECISO dopo recentratura
        "candidate_object": foc2.get("object"),
        "goto_ra_deg": foc2.get("ra_deg", ra_tap),
        "goto_dec_deg": foc2.get("dec_deg", dec_tap),
    }
