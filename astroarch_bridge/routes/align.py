"""Route /api/align — plate solving + polar alignment.

Si appoggia al driver INDI Astrometry (se attivo nel profilo Ekos) usando
le sue property TELESCOPE_*, MOUNT_TYPE, ASTROMETRY_RESULTS.
Per polar align fa riferimento a CAP_PARK + Ekos DBus (futuro).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import resolve_device

router = APIRouter(prefix="/api/align", tags=["align"], dependencies=[Depends(require_token)])


@router.get("/status")
async def status(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Cerca driver Astrometry / StellarSolver / OnlineAstrometry tra i device."""
    devices = await bridge.state.list_devices()
    solver_device = None
    for d in devices:
        # Convenzione: driver con "Astrometry" nel nome
        if "astrometry" in d.lower() or "solver" in d.lower():
            solver_device = d
            break
    last_solve = None
    if solver_device:
        p = await bridge.state.get_property(solver_device, "ASTROMETRY_RESULTS")
        if p:
            last_solve = {
                "state": p["state"],
                "elements": [
                    {"name": e["name"], "value": e.get("value")}
                    for e in p.get("elements", [])
                ],
            }
    return {
        "solver_device": solver_device,
        "available": solver_device is not None,
        "last_solve": last_solve,
    }


@router.post("/solve_last_frame")
async def solve_last_frame(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Trigger del solve sull'ultimo frame catturato dalla camera primaria.

    Richiede driver Astrometry attivo. Imposta SOLVER_RESULTS=Idle e ASTROMETRY_SOLVE=ON.
    """
    devices = await bridge.state.list_devices()
    solver = None
    for d in devices:
        if "astrometry" in d.lower():
            solver = d
            break
    if solver is None:
        raise HTTPException(status_code=503, detail="No astrometry driver loaded")
    # Avvia solve
    try:
        await bridge.indi.send_switch(solver, "ASTROMETRY_SOLVER",
                                      {"ASTROMETRY_SOLVER_ENABLE": True,
                                       "ASTROMETRY_SOLVER_DISABLE": False})
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "solver": solver}


# In-memory state degli ongoing solve runs
_SOLVE_RUNS: dict[str, dict] = {}


async def _resolve_primary_camera(bridge: Bridge, override: str | None) -> str:
    """Risolve la camera primaria. Se override esiste, lo usa (verificando che sia
    connesso). Altrimenti se c'è una sola camera la usa. Se ce ne sono più,
    chiama /api/system/camera_roles e usa primary (PHD2/heuristic).
    """
    cameras = await bridge.state.find_devices_by_role("CCD_EXPOSURE")
    if not cameras:
        raise HTTPException(status_code=503, detail="no camera connected")
    if override:
        if override in cameras:
            return override
        raise HTTPException(status_code=404, detail=f"camera {override!r} not connected")
    if len(cameras) == 1:
        return cameras[0]
    # Determina primary via heuristic naming (stessa logica di /system/camera_roles)
    guide_keywords = ("guide", "guider", "asi120", "asi174", "asi178",
                      "asi290", "asi585", "qhy5")
    for c in cameras:
        cl = c.lower()
        is_guide = any(k in cl for k in guide_keywords)
        if not is_guide:
            return c
    # Tutte sembrano guide → primo
    return cameras[0]


@router.post("/solve")
async def solve(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Plate solve usando solve-field (astrometry.net) sull'ultimo FITS catturato
    o su un path specificato.

    Body:
      path: str opzionale (relative a images_dir o assoluto)
      hint_ra: float opzionale (gradi)
      hint_dec: float opzionale (gradi)
      hint_radius: float opzionale (gradi, default 5)
      scale_low: float opzionale (arcsec/px)
      scale_high: float opzionale (arcsec/px)

    Ritorna run_id; lo stato si polla con GET /api/align/solve/{run_id}.
    """
    import asyncio
    import time
    from pathlib import Path

    # Determina path FITS
    fits_path = payload.get("path")
    if not fits_path:
        snap = await bridge.state.snapshot()
        last = snap.get("last_frame", {}) or {}
        fits_path = last.get("path")
    if not fits_path:
        raise HTTPException(status_code=400, detail="No FITS path (capture a frame first)")
    p = Path(fits_path)
    if not p.is_absolute():
        from ..config import get_settings
        s = get_settings()
        p = s.images_dir / p
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"FITS not found: {p}")

    run_id = f"sv_{int(time.time() * 1000)}"
    _SOLVE_RUNS[run_id] = {
        "id": run_id,
        "path": str(p),
        "status": "running",
        "result": None,
        "error": None,
        "stdout_tail": "",
    }
    asyncio.create_task(_run_solve(run_id, p, payload))
    return {"ok": True, "run_id": run_id}


@router.get("/solve/{run_id}")
async def solve_status(run_id: str) -> dict:
    run = _SOLVE_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


async def _run_solve(run_id: str, fits_path, payload: dict) -> None:
    import asyncio
    import shutil
    import tempfile
    from pathlib import Path
    run = _SOLVE_RUNS[run_id]

    # Index dir: prefer Kstars dir
    index_dirs = [
        Path.home() / ".local/share/kstars/astrometry",
        Path("/usr/share/astrometry/data"),
    ]
    index_dir = next((d for d in index_dirs if d.exists()), None)
    if index_dir is None:
        run["status"] = "failed"
        run["error"] = "No astrometry index dir found"
        return

    # Build solve-field command
    args = [
        "solve-field", "--no-plots", "--no-verify", "--overwrite",
        "--cpulimit", "30",
        "--no-tweak",  # faster
        "-D", tempfile.gettempdir(),
        # Mostra index dir all'engine
    ]
    # Hints
    if payload.get("hint_ra") is not None:
        args += ["--ra", str(payload["hint_ra"])]
    if payload.get("hint_dec") is not None:
        args += ["--dec", str(payload["hint_dec"])]
    if payload.get("hint_radius") is not None:
        args += ["--radius", str(payload["hint_radius"])]
    if payload.get("scale_low") is not None:
        args += ["--scale-low", str(payload["scale_low"]),
                 "--scale-units", "arcsecperpix"]
    if payload.get("scale_high") is not None:
        args += ["--scale-high", str(payload["scale_high"]),
                 "--scale-units", "arcsecperpix"]
    args.append(str(fits_path))

    # Crea config che punta agli index files
    cfg_text = f"""inparallel
cpulimit 30
add_path {index_dir}
autoindex
"""
    with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as cfg:
        cfg.write(cfg_text)
        cfg_path = cfg.name
    args.insert(1, "--config")
    args.insert(2, cfg_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout = b""
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            run["status"] = "failed"
            run["error"] = "solve-field timeout"
            run["stdout_tail"] = stdout.decode("utf-8", "replace")[-2000:]
            return

        run["stdout_tail"] = stdout.decode("utf-8", "replace")[-2000:]
        if proc.returncode != 0:
            run["status"] = "failed"
            run["error"] = f"solve-field exit code {proc.returncode}"
            return

        # Parse risultato dal file .solved e .wcs
        base = Path(tempfile.gettempdir()) / fits_path.stem
        wcs_path = base.with_suffix(".wcs")
        solved_path = base.with_suffix(".solved")
        if not solved_path.exists() or not wcs_path.exists():
            run["status"] = "failed"
            run["error"] = "solve-field did not produce .solved/.wcs"
            return

        # Leggi WCS via astropy
        from astropy.io import fits as afits
        from astropy.wcs import WCS as AWCS
        try:
            with afits.open(wcs_path) as hdul:
                w = AWCS(hdul[0].header)
            # Centro
            naxis1 = hdul[0].header.get("IMAGEW", 0)
            naxis2 = hdul[0].header.get("IMAGEH", 0)
            ra_c, dec_c = w.wcs_pix2world(naxis1 / 2.0, naxis2 / 2.0, 1)
            # Scale (arcsec/px) approssimato dalla diagonale
            ra_a, dec_a = w.wcs_pix2world(0, 0, 1)
            ra_b, dec_b = w.wcs_pix2world(1, 0, 1)
            from math import cos, radians, sqrt
            dra = (ra_b - ra_a) * cos(radians(float(dec_a))) * 3600.0
            ddec = (dec_b - dec_a) * 3600.0
            scale = sqrt(dra * dra + ddec * ddec)
            run["result"] = {
                "ra_deg": float(ra_c),
                "dec_deg": float(dec_c),
                "ra_hours": float(ra_c) / 15.0,
                "scale_arcsec_px": float(scale),
                "image_w": int(naxis1),
                "image_h": int(naxis2),
            }
            run["status"] = "done"
        except Exception as e:
            run["status"] = "failed"
            run["error"] = f"WCS parse: {e}"
        finally:
            # Cleanup
            for ext in [".wcs", ".solved", ".axy", ".match", ".rdls",
                        ".corr", ".new", "-indx.xyls"]:
                try:
                    p = base.with_suffix(ext) if ext.startswith(".") else \
                        Path(str(base) + ext)
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
    except FileNotFoundError:
        run["status"] = "failed"
        run["error"] = "solve-field not installed"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
    finally:
        try:
            import os
            os.unlink(cfg_path)
        except Exception:
            pass


@router.post("/solve/{run_id}/sync_mount")
async def solve_sync_mount(
    run_id: str,
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Sincronizza la mount sul risultato del solve."""
    run = _SOLVE_RUNS.get(run_id)
    if not run or run["status"] != "done" or not run["result"]:
        raise HTTPException(status_code=400, detail="solve non completato")
    r = run["result"]
    mount_devs = await bridge.state.find_devices_by_role("EQUATORIAL_EOD_COORD")
    if not mount_devs:
        raise HTTPException(status_code=503, detail="no mount connected")
    dev = mount_devs[0]
    await bridge.indi.send_switch(dev, "ON_COORD_SET",
                                   {"SLEW": False, "TRACK": False, "SYNC": True})
    await bridge.indi.send_number(dev, "EQUATORIAL_EOD_COORD",
                                   {"RA": r["ra_hours"], "DEC": r["dec_deg"]})
    return {"ok": True, "synced_to": r}


def _parse_dbus_array(raw: str) -> list[float]:
    """Estrae array di doppi da output qdbus6 --literal '[Argument: ad {a, b, c}]'."""
    import re
    m = re.search(r'\{([^}]+)\}', raw)
    if not m:
        # fallback: spazi/newlines
        out = []
        for tok in raw.replace("\n", " ").split():
            try: out.append(float(tok))
            except ValueError: pass
        return out
    return [float(x.strip()) for x in m.group(1).split(",") if x.strip()]


async def _dbus_get_property(path: str, prop: str) -> str:
    """Legge una property via Properties.Get."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, path, prop)
    return raw if rc == 0 else ""


async def _dbus_call_literal(path: str, method: str, *args: str) -> str:
    """Chiamata DBus con --literal output (per array/varianti)."""
    import os, asyncio
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    proc = await asyncio.create_subprocess_exec(
        "qdbus6", "--literal",
        "org.kde.kstars", path, method, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", "replace").strip()


@router.get("/ekos_full_status")
async def ekos_full_status(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Status completo del modulo Ekos Align — clone Ekos.

    Ritorna tutte le info che la GUI Align di Ekos mostra, lette via DBus.
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    align_path = "/KStars/Ekos/Align"
    out: dict = {}

    # Status enum
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                "org.kde.kstars.Ekos.Align.status")
    status_int = int(raw) if rc == 0 and raw.lstrip("-").isdigit() else None
    status_label = {
        0: "idle", 1: "complete", 2: "failed", 3: "aborted",
        4: "progress", 5: "syncing", 6: "slewing",
        7: "suspended", 8: "paused", 9: "refresh",
    }.get(status_int, "unknown")
    out["status_int"] = status_int
    out["status"] = status_label

    # Properties testuali
    out["camera"] = (await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                       "org.kde.kstars.Ekos.Align.camera"))[1]
    out["filter"] = (await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                       "org.kde.kstars.Ekos.Align.filter"))[1]
    out["filterWheel"] = (await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                            "org.kde.kstars.Ekos.Align.filterWheel"))[1]
    out["opticalTrain"] = (await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                             "org.kde.kstars.Ekos.Align.opticalTrain"))[1]
    out["solverArguments"] = (await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                                "org.kde.kstars.Ekos.Align.solverArguments"))[1]

    # Log text (lista righe — manualmente split)
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                "org.kde.kstars.Ekos.Align.logText")
    out["log"] = raw.split("\n")[:30] if rc == 0 else []

    # FOV [w_arcmin, h_arcmin, pixel_scale_arcsec_per_px]
    raw = await _dbus_call_literal(align_path, "fov")
    fov_vals = _parse_dbus_array(raw)
    if len(fov_vals) >= 3:
        out["fov"] = {
            "w_arcmin": fov_vals[0],
            "h_arcmin": fov_vals[1],
            "pixel_scale_arcsec_px": fov_vals[2],
        }
    else:
        out["fov"] = None

    # telescopeInfo [focal_length_mm, aperture_mm, ratio]
    raw = await _dbus_call_literal(align_path,
                                    "org.kde.kstars.Ekos.Align.telescopeInfo")
    tel = _parse_dbus_array(raw)
    if len(tel) >= 2:
        focal = tel[0]
        aperture = tel[1]
        out["telescope"] = {
            "focal_length_mm": focal,
            "aperture_mm": aperture,
            "f_ratio": (focal / aperture) if aperture else None,
        }
    else:
        out["telescope"] = None

    # cameraInfo [width, height, pixel_w_um, pixel_h_um]
    raw = await _dbus_call_literal(align_path,
                                    "org.kde.kstars.Ekos.Align.cameraInfo")
    cam = _parse_dbus_array(raw)
    if len(cam) >= 4:
        out["camera_info"] = {
            "width_px": int(cam[0]),
            "height_px": int(cam[1]),
            "pixel_w_um": cam[2],
            "pixel_h_um": cam[3],
        }
    else:
        out["camera_info"] = None

    # Solution result [orientation_deg, ra_deg, dec_deg]
    raw = await _dbus_call_literal(align_path,
                                    "org.kde.kstars.Ekos.Align.getSolutionResult")
    sol = _parse_dbus_array(raw)
    if len(sol) >= 3 and sol[1] > -1e5:
        out["solution"] = {
            "orientation_deg": sol[0],
            "ra_deg": sol[1],
            "dec_deg": sol[2],
            "ra_hours": sol[1] / 15.0,
        }
    else:
        out["solution"] = None

    # Target coords [ra_hours, dec_deg]
    raw = await _dbus_call_literal(align_path,
                                    "org.kde.kstars.Ekos.Align.getTargetCoords")
    tgt = _parse_dbus_array(raw)
    if len(tgt) >= 2:
        out["target"] = {"ra_hours": tgt[0], "dec_deg": tgt[1]}
    else:
        out["target"] = None

    # Mount coords (da INDI direttamente)
    mount_devs = await bridge.state.find_devices_by_role("EQUATORIAL_EOD_COORD")
    if mount_devs:
        mp = await bridge.state.get_property(mount_devs[0], "EQUATORIAL_EOD_COORD")
        if mp:
            ra_h = dec_d = None
            for e in mp.get("elements", []):
                if e["name"] == "RA": ra_h = e.get("value")
                if e["name"] == "DEC": dec_d = e.get("value")
            out["mount_coords"] = {
                "device": mount_devs[0],
                "ra_hours": ra_h,
                "dec_deg": dec_d,
                "state": mp.get("state"),
            }

    return out


@router.get("/ekos_align_status")
async def ekos_align_status() -> dict:
    """Stato live del modulo Ekos Align via DBus.
    Ritorna lo status enum + (se completo) il risultato del solver.

    Status Ekos Align (int):
      0=Idle, 1=Complete, 2=Failed, 3=Aborted, 4=Progress,
      5=Syncing, 6=Slewing, 7=Suspended, 8=Paused, 9=Refresh
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Align",
                                "org.kde.kstars.Ekos.Align.status")
    status_int = int(raw) if rc == 0 and raw.lstrip("-").isdigit() else None
    status_label = {
        0: "idle", 1: "complete", 2: "failed", 3: "aborted",
        4: "progress", 5: "syncing", 6: "slewing",
        7: "suspended", 8: "paused", 9: "refresh",
    }.get(status_int, "unknown")

    # Se complete, prova a leggere il risultato
    result = None
    if status_int == 1:
        rc2, raw2 = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Align",
                                       "org.kde.kstars.Ekos.Align.getSolutionResult")
        if rc2 == 0:
            # qdbus6 ritorna array di doppi separati da newline o spazi
            try:
                vals = []
                for token in raw2.replace("\n", " ").split():
                    try:
                        vals.append(float(token))
                    except ValueError:
                        pass
                # Convenzione Ekos: [orientation, ra_deg, dec_deg, pixscale, fov_w, fov_h, ...]
                if len(vals) >= 4:
                    result = {
                        "orientation_deg": vals[0],
                        "ra_deg": vals[1],
                        "dec_deg": vals[2],
                        "ra_hours": vals[1] / 15.0,
                        "scale_arcsec_px": vals[3],
                    }
                    if len(vals) >= 6:
                        result["fov_w_arcmin"] = vals[4]
                        result["fov_h_arcmin"] = vals[5]
            except Exception:
                pass

    # Coordinate target attualmente impostate
    target = None
    rc3, raw3 = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Align",
                                  "org.kde.kstars.Ekos.Align.getTargetCoords")
    if rc3 == 0:
        try:
            tv = [float(t) for t in raw3.replace("\n", " ").split() if t]
            if len(tv) >= 2:
                target = {"ra_hours": tv[0], "dec_deg": tv[1]}
        except Exception:
            pass

    return {
        "ekos_status_int": status_int,
        "ekos_status": status_label,
        "result": result,
        "target_coords": target,
    }


# Ekos::AlignState values in which the module is already working: on
# their own they explain the refusal. The other values (0 idle, 1 complete,
# 2 failed, 3 aborted) add nothing to the message.
#
# The numbering is verified on KStars 3.8.3: sampling `status` during a
# real capture-and-solve reads 4 for the whole run and 1 the moment the
# solution lands. Do not trust the "intuitive" order: complete comes
# before progress.
_ALIGN_BUSY_STATES = {
    4: "capturing or solving", 5: "syncing",
    6: "slewing", 7: "rotating", 8: "suspended",
}


async def _capture_and_solve_refusal(raw: str) -> tuple[int, str]:
    """Why Ekos refused `captureAndSolve`, in readable words.

    `captureAndSolve` answers nothing but `false` and, for several of its
    preconditions, does not even append a line to the module log. Whoever
    is standing at the telescope is left with "captureAndSolve failed:
    false", which names neither what is missing nor where to fix it — in
    the dark, in the field, the worst possible moment to have to guess.

    The information to answer is already there, exposed by Ekos on the same
    interface: the three checks below cover the common refusals, and the
    last line of the module log is quoted whenever it holds anything. These
    are all reads: no user setting is touched.
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    align_path = "/KStars/Ekos/Align"
    hints: list[str] = []

    # First of all: is there anybody to talk to? These two DBus errors are
    # not a refusal from Ekos, they are the absence of Ekos, and querying
    # its properties right after would only produce more errors.
    if "does not exist" in raw and "org.kde.kstars" in raw:
        return 503, ("KStars is not running, or it runs in a graphical "
                     "session other than the bridge's.")
    if "UnknownObject" in raw or "No such object path" in raw:
        return 503, ("The Ekos Align module does not exist yet: Ekos is not "
                     "started, or its INDI services never came up.")

    # 1. Focal length and aperture of the optical train. Ekos reports them
    #    as -1 when the active train points at optics that are no longer in
    #    its database: without those there is no image scale, and the solve
    #    cannot start.
    tele = _parse_dbus_array(
        await _dbus_call_literal(align_path, "org.kde.kstars.Ekos.Align.telescopeInfo"))
    if len(tele) >= 2 and (tele[0] <= 0 or tele[1] <= 0):
        hints.append(
            "the active optical train has no focal length and aperture "
            f"(Ekos reports focal={tele[0]:g}, aperture={tele[1]:g}). Open "
            "the optical trains editor in Ekos and assign an existing scope")

    # 2. Camera of the Align module.
    rc_cam, cam = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                   "org.kde.kstars.Ekos.Align.camera")
    if rc_cam == 0 and not cam.strip():
        hints.append("the Align module has no camera selected")

    # 3. The module was already busy.
    rc_st, st = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                 "org.kde.kstars.Ekos.Align.status")
    if rc_st == 0 and st.strip().lstrip("-").isdigit():
        busy = _ALIGN_BUSY_STATES.get(int(st.strip()))
        if busy:
            hints.append(f"the Align module is already {busy}")

    # 4. The last log line of the module says the rest, when there is one.
    #    Ekos returns that log newest first, so the line that matters is the
    #    first one.
    rc_log, log = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                   "org.kde.kstars.Ekos.Align.logText")
    if rc_log == 0:
        lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
        if lines:
            hints.append(f"last message from Ekos: '{lines[0]}'")

    if not hints:
        return 500, (f"Ekos refused to start capture and solve "
                     f"(captureAndSolve answered '{raw}') without "
                     f"giving a reason. Check the Align module in Ekos.")
    return 422, "Ekos refused to start capture and solve: " + "; ".join(hints) + "."


@router.post("/ekos_capture_and_solve")
async def ekos_capture_and_solve(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Trigger Ekos Align captureAndSolve via DBus.

    Body:
      bin_index: int opzionale (0=1×1, 1=2×2, 2=3×3, 3=4×4)
      target_ra_hours, target_dec_deg: opzionali (setTargetCoords prima)
      solver_action: int opzionale (Ekos enum: 0=Sync, 1=Slew, 2=Nothing)
      exposure_sec: float opzionale (applicato via INDI alla camera primaria)
      gain: float opzionale (applicato via INDI: CCD_GAIN.GAIN o CCD_CONTROLS.Gain)
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    from .camera import _resolve_gain
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.align")
    align_path = "/KStars/Ekos/Align"

    # BUG FIX v0.2.23: prima di questa release il backend chiamava
    #   bridge.indi.send_number(cam, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": ...})
    # PRIMA di Ekos.Align.captureAndSolve(). Era SBAGLIATO: in INDI
    # settare CCD_EXPOSURE_VALUE non "imposta" la posa, AVVIA L'ESPOSIZIONE.
    # Risultato: l'esposizione partiva fuori controllo Ekos, Ekos vedeva
    # la camera occupata, scriveva nel log "Impossibile acquisire se
    # l'esposizione della fotocamera è in corso, nuovo tentativo tra 10
    # secondi…", riprovava, e il sequencing si rompeva — questo è
    # plausibilmente il motivo per cui "il solving da Ekos funziona ma
    # dalla app no" (l'utente lo dice dal 12 maggio).
    # Soluzione: NON tocchiamo più la camera. captureAndSolve usa le
    # impostazioni configurate nella UI di Ekos Align (esposizione, gain,
    # binning, train). Il binning ha un setter dedicato e quello lo
    # teniamo. Esposizione e gain restano gestiti SOLO da Ekos GUI per
    # ora — finché non troviamo un metodo DBus pulito per impostarli
    # senza side-effect.
    exposure = payload.get("exposure_sec")
    gain = payload.get("gain")
    if exposure is not None or gain is not None:
        _logger.info("ekos_capture_and_solve: ignoring exposure/gain from app "
                     "(would start a rogue INDI exposure conflicting with Ekos). "
                     "Set them in Ekos Align UI. Got exposure=%s gain=%s",
                     exposure, gain)

    # Setup binning — è un combo box in Ekos, no side effects
    bin_index = payload.get("bin_index")
    if bin_index is not None:
        _logger.info("ekos_capture_and_solve: setBinningIndex(%d)", int(bin_index))
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setBinningIndex",
                          str(int(bin_index)))

    # Setup target. NB: l'app normalmente NON manda questi, lascia che
    # Ekos usi il target già impostato (da KStars centering, scheduler, o
    # dialog "Aggiorna target = mount" della Plate Solve tab).
    if payload.get("target_ra_hours") is not None and payload.get("target_dec_deg") is not None:
        tra = float(payload["target_ra_hours"])
        tdc = float(payload["target_dec_deg"])
        _logger.info("ekos_capture_and_solve: setTargetCoords(ra=%.4fh, dec=%.4f°)",
                     tra, tdc)
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setTargetCoords",
                          str(tra), str(tdc))

    # Verifica e logga lo stato PRE-CAPTURE: target effettivo in Ekos
    # + posizione attuale mount. Diagnostica preziosa per quando l'utente
    # vede comportamenti strani della centratura.
    try:
        raw = await _dbus_call_literal(align_path,
            "org.kde.kstars.Ekos.Align.getTargetCoords")
        tgt = _parse_dbus_array(raw)
        if len(tgt) >= 2:
            _logger.info("ekos_capture_and_solve: PRE-CHECK Ekos target = "
                         "ra=%.4fh dec=%.4f°", tgt[0], tgt[1])
        mount_devs = await bridge.state.find_devices_by_role("EQUATORIAL_EOD_COORD")
        if mount_devs:
            mp = await bridge.state.get_property(mount_devs[0], "EQUATORIAL_EOD_COORD")
            if mp:
                m_ra = m_dec = None
                for e in mp.get("elements", []):
                    if e["name"] == "RA": m_ra = e.get("value")
                    if e["name"] == "DEC": m_dec = e.get("value")
                _logger.info("ekos_capture_and_solve: PRE-CHECK Mount = "
                             "ra=%.4fh dec=%.4f° state=%s",
                             m_ra or 0, m_dec or 0, mp.get("state"))
                if len(tgt) >= 2 and m_ra is not None and m_dec is not None:
                    # Distanza grossolana (gradi) tra target e mount
                    import math as _m
                    d_ra = (tgt[0] - m_ra) * 15.0 * _m.cos(_m.radians(m_dec))
                    d_dec = tgt[1] - m_dec
                    dist = _m.sqrt(d_ra * d_ra + d_dec * d_dec)
                    _logger.info("ekos_capture_and_solve: PRE-CHECK "
                                 "target↔mount distance = %.2f°", dist)
                    if dist > 30.0:
                        _logger.warning(
                            "ekos_capture_and_solve: ⚠ target is %.1f° "
                            "from mount — 'Slew to target' will move the "
                            "telescope FAR from the current sky field",
                            dist)
    except Exception as _e:
        _logger.warning("ekos_capture_and_solve: pre-check failed: %s", _e)

    # Solver action: Ekos AlignSolverAction enum: 0=Sync, 1=Slew, 2=Nothing.
    #
    # IMPORTANTE: setSolverAction è dichiarato Q_NOREPLY in DBus — qdbus6 lo
    # invia "fire and forget" e ritorna PRIMA che Ekos processi il messaggio.
    # captureAndSolve invece è sincrono (bool reply). Se le due chiamate
    # partono da subprocess qdbus6 separati c'è un race: a volte
    # captureAndSolve viene processato PRIMA che m_CurrentGotoMode sia stato
    # aggiornato → il solve completa con l'azione VECCHIA.
    # Mitigazioni:
    #   1) chiamiamo setSolverAction DUE volte con piccola pausa, per saturare
    #      la coda eventi di Ekos
    #   2) inseriamo una pausa esplicita di 250ms prima di captureAndSolve
    #   3) logghiamo esplicitamente il valore inviato per troubleshooting
    import asyncio as _asyncio
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.align")
    solver_action = payload.get("solver_action")
    if solver_action is not None:
        sa = int(solver_action)
        if sa not in (0, 1, 2):
            raise HTTPException(status_code=400,
                                detail=f"solver_action must be 0/1/2, got {sa}")
        _logger.info("ekos_capture_and_solve: setSolverAction(%d) [%s]",
                     sa, {0: "Sync", 1: "Slew", 2: "Nothing"}[sa])
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setSolverAction", str(sa))
        await _asyncio.sleep(0.15)
        # Seconda chiamata per sicurezza (idempotente, costa nulla).
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setSolverAction", str(sa))
        await _asyncio.sleep(0.25)

    # Trigger capture & solve
    _logger.info("ekos_capture_and_solve: triggering captureAndSolve")
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                "org.kde.kstars.Ekos.Align.captureAndSolve")
    if rc != 0 or raw.lower() == "false":
        status_code, detail = await _capture_and_solve_refusal(raw)
        _logger.warning("ekos_capture_and_solve refused: %s", detail)
        raise HTTPException(status_code=status_code, detail=detail)
    return {"ok": True, "started": True,
            "solver_action_sent": solver_action}


@router.post("/ekos_align_abort")
async def ekos_align_abort() -> dict:
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Align",
                                "org.kde.kstars.Ekos.Align.abort")
    return {"ok": rc == 0, "raw": raw}


@router.post("/ekos_align_set")
async def ekos_align_set(payload: dict = Body(default={})) -> dict:
    """Imposta parametri Ekos Align.

    Body (tutti opzionali):
      bin_index: int (0=1×1, 1=2×2, 2=3×3, 3=4×4)
      solver_action: int (Ekos enum: 0=Sync, 1=Slew, 2=Nothing)
      solver_mode: int (0=StellarSolver, 1=Remote)
      target_ra_hours, target_dec_deg
      target_position_angle: deg
      solver_arguments: str
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    align_path = "/KStars/Ekos/Align"
    applied = []
    if payload.get("bin_index") is not None:
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setBinningIndex",
                          str(int(payload["bin_index"])))
        applied.append("bin_index")
    if payload.get("solver_action") is not None:
        # Doppia chiamata + delay: Q_NOREPLY è async, vedi nota in
        # ekos_capture_and_solve. Garantisce che m_CurrentGotoMode sia aggiornato
        # prima che chiunque legga lo stato successivo.
        import asyncio as _asyncio
        sa = int(payload["solver_action"])
        if sa not in (0, 1, 2):
            raise HTTPException(status_code=400,
                                detail=f"solver_action must be 0/1/2, got {sa}")
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setSolverAction", str(sa))
        await _asyncio.sleep(0.10)
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setSolverAction", str(sa))
        applied.append("solver_action")
    if payload.get("solver_mode") is not None:
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setSolverMode",
                          str(int(payload["solver_mode"])))
        applied.append("solver_mode")
    if payload.get("target_ra_hours") is not None and payload.get("target_dec_deg") is not None:
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setTargetCoords",
                          str(float(payload["target_ra_hours"])),
                          str(float(payload["target_dec_deg"])))
        applied.append("target_coords")
    if payload.get("target_position_angle") is not None:
        await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                          "org.kde.kstars.Ekos.Align.setTargetPositionAngle",
                          str(float(payload["target_position_angle"])))
        applied.append("target_pa")
    if payload.get("solver_arguments") is not None:
        # Property write via Set
        rc, _ = await _dbus_call(EKOS_DBUS_SERVICE, align_path,
                                  "org.freedesktop.DBus.Properties.Set",
                                  "org.kde.kstars.Ekos.Align",
                                  "solverArguments",
                                  str(payload["solver_arguments"]))
        if rc == 0:
            applied.append("solver_arguments")
    return {"ok": True, "applied": applied}


@router.post("/ekos_load_and_slew")
async def ekos_load_and_slew(payload: dict = Body(...)) -> dict:
    """Carica un file FITS e fa slew sulla soluzione.
    Body: {file_url: "/home/astronaut/Pictures/.../target.fits"}
    """
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    file_url = payload.get("file_url")
    if not file_url:
        raise HTTPException(status_code=400, detail="file_url required")
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos/Align",
                                "org.kde.kstars.Ekos.Align.loadAndSlew",
                                file_url)
    if rc != 0 or raw.lower() == "false":
        raise HTTPException(status_code=500, detail=f"loadAndSlew failed: {raw}")
    return {"ok": True}


@router.post("/capture_and_solve")
async def capture_and_solve(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Workflow combinato: cattura un frame breve dalla camera primaria
    e poi lo risolve con solve-field. Ritorna il run_id del solve.

    Body:
      exposure_sec: float (default 5)
      camera_device: str opzionale (se assente: auto via PHD2/heuristic)
      bin_x, bin_y: int opzionali (default 2 per solving rapido)
      gain: float opzionale (se non passato, lascia gain corrente)
    """
    import asyncio
    cam = await _resolve_primary_camera(bridge, payload.get("camera_device"))
    exposure = float(payload.get("exposure_sec", 5.0))
    bin_x = int(payload.get("bin_x", 2)) if payload.get("bin_x") is not None else 2
    bin_y = int(payload.get("bin_y", bin_x)) if payload.get("bin_y") is not None else bin_x
    gain = payload.get("gain")  # None = non toccare

    # Setup upload BOTH per essere sicuri
    from .camera import _ensure_upload_local, _resolve_gain
    await _ensure_upload_local(bridge, cam,
                               upload_prefix="solve_XXX")
    # Setup format FITS
    try:
        await bridge.indi.send_switch(cam, "CCD_TRANSFER_FORMAT", {
            "FORMAT_FITS": True, "FORMAT_NATIVE": False, "FORMAT_XISF": False,
        })
    except Exception:
        pass

    # Setup binning
    try:
        await bridge.indi.send_number(cam, "CCD_BINNING",
                                       {"HOR_BIN": bin_x, "VER_BIN": bin_y})
    except Exception:
        pass

    # Setup gain (auto-detect property: CCD_GAIN.GAIN o CCD_CONTROLS.Gain)
    if gain is not None:
        try:
            _, prop_name, elt_name = await _resolve_gain(bridge, cam)
            if prop_name and elt_name:
                await bridge.indi.send_number(cam, prop_name,
                                               {elt_name: float(gain)})
            else:
                # Fallback: prova CCD_GAIN.GAIN
                await bridge.indi.send_number(cam, "CCD_GAIN",
                                               {"GAIN": float(gain)})
        except Exception:
            pass

    # Scatta
    await bridge.indi.send_number(cam, "CCD_EXPOSURE",
                                   {"CCD_EXPOSURE_VALUE": exposure})
    # Aspetta fine
    deadline = asyncio.get_event_loop().time() + exposure + 30
    while asyncio.get_event_loop().time() < deadline:
        p = await bridge.state.get_property(cam, "CCD_EXPOSURE")
        st = p.get("state") if p else None
        if st in ("Ok", "Idle"):
            break
        if st == "Alert":
            raise HTTPException(status_code=500, detail="exposure alert")
        await asyncio.sleep(0.5)

    # Aspetta il watcher (file deve apparire). Polling fino a 15s con fallback
    # su scan filesystem se il watcher non aggiorna.
    fits_path = None
    pre_snap = await bridge.state.snapshot()
    pre_path = (pre_snap.get("last_frame", {}) or {}).get("path")
    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        snap = await bridge.state.snapshot()
        last = snap.get("last_frame", {}) or {}
        candidate = last.get("path")
        if candidate and candidate != pre_path:
            fits_path = candidate
            break

    # Fallback: scan directory per file più recente di pre_snap
    if not fits_path:
        from pathlib import Path
        from ..config import get_settings as _gs
        base = _gs().images_dir
        recent = []
        for ext in (".fit", ".fits", ".fz"):
            for p in base.rglob(f"*{ext}"):
                try:
                    recent.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        if recent:
            recent.sort(key=lambda t: t[0], reverse=True)
            fits_path = str(recent[0][1])

    if not fits_path:
        raise HTTPException(status_code=500,
                            detail="frame non trovato. Verifica UPLOAD_MODE=BOTH e UPLOAD_DIR")

    # Hint dal mount corrente
    mount_devs = await bridge.state.find_devices_by_role("EQUATORIAL_EOD_COORD")
    hint_ra_deg = None
    hint_dec_deg = None
    if mount_devs:
        coord = await bridge.state.get_property(mount_devs[0], "EQUATORIAL_EOD_COORD")
        if coord:
            for e in coord.get("elements", []):
                if e["name"] == "RA" and e.get("value") is not None:
                    hint_ra_deg = float(e["value"]) * 15.0
                if e["name"] == "DEC" and e.get("value") is not None:
                    hint_dec_deg = float(e["value"])

    # Lancia solve
    return await solve(payload={
        "path": fits_path,
        "hint_ra": hint_ra_deg,
        "hint_dec": hint_dec_deg,
        "hint_radius": 5.0,
    }, bridge=bridge)


# ============================================================================
# POLAR ALIGNMENT ROUTINE
# ============================================================================

_POLAR_RUNS: dict[str, dict] = {}


@router.post("/polar_align/run")
async def polar_align_run(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Avvia routine polar alignment drift-based 3-step.

    L'utente:
    1. Punta verso meridiano + equatore celeste (prima pos)
    2. Fa partire la routine
    3. Bridge: capture → solve → ricorda RA/Dec_1
    4. Bridge: slew RA + 30 minuti → capture → solve → ricorda RA/Dec_2
    5. Bridge: slew RA + 30 min altri → capture → solve → ricorda RA/Dec_3
    6. Calcola errore polar AZ + ALT da delta dec tra punti

    Body:
      ra_offset_min: float (default 30, minuti d'arco RA tra ogni step)
      exposure_sec: float (default 5)
    """
    import asyncio
    import time
    cam = await _resolve_primary_camera(bridge, payload.get("camera_device"))
    mount_devs = await bridge.state.find_devices_by_role("EQUATORIAL_EOD_COORD")
    if not mount_devs:
        raise HTTPException(status_code=503, detail="no mount")
    mount = mount_devs[0]
    ra_offset_min = float(payload.get("ra_offset_min", 30))
    exposure = float(payload.get("exposure_sec", 5))

    run_id = f"pa_{int(time.time() * 1000)}"
    _POLAR_RUNS[run_id] = {
        "id": run_id,
        "status": "running",
        "step": 0,
        "samples": [],   # list of {ra, dec, ts}
        "az_error_arcmin": None,
        "alt_error_arcmin": None,
        "error": None,
    }
    asyncio.create_task(_run_polar_align(bridge, run_id, mount, cam,
                                          ra_offset_min, exposure))
    return {"ok": True, "run_id": run_id}


@router.get("/polar_align/{run_id}")
async def polar_align_status(run_id: str) -> dict:
    run = _POLAR_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/polar_align/{run_id}/abort")
async def polar_align_abort(run_id: str) -> dict:
    run = _POLAR_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    run["status"] = "aborting"
    return {"ok": True}


async def _run_polar_align(bridge: Bridge, run_id: str, mount: str, cam: str,
                           ra_offset_min: float, exposure: float) -> None:
    import asyncio
    run = _POLAR_RUNS[run_id]
    try:
        for step in range(3):
            if run["status"] == "aborting":
                run["status"] = "aborted"; return
            run["step"] = step + 1

            # Per step > 0, slew di ra_offset_min minuti d'arco RA
            if step > 0:
                # Leggi RA corrente
                coord = await bridge.state.get_property(mount, "EQUATORIAL_EOD_COORD")
                ra_now = None; dec_now = None
                for e in coord.get("elements", []):
                    if e["name"] == "RA": ra_now = float(e.get("value", 0))
                    if e["name"] == "DEC": dec_now = float(e.get("value", 0))
                if ra_now is None:
                    run["status"] = "failed"
                    run["error"] = "cannot read mount coords"
                    return
                # 30 min d'arco RA = 30/60 = 0.5 ore
                ra_target = ra_now + ra_offset_min / 60.0
                if ra_target >= 24: ra_target -= 24
                await bridge.indi.send_switch(mount, "ON_COORD_SET",
                    {"SLEW": False, "TRACK": True, "SYNC": False})
                await bridge.indi.send_number(mount, "EQUATORIAL_EOD_COORD",
                    {"RA": ra_target, "DEC": dec_now})
                # Attendi slew complete
                deadline = asyncio.get_event_loop().time() + 120
                while asyncio.get_event_loop().time() < deadline:
                    if run["status"] == "aborting":
                        run["status"] = "aborted"; return
                    p = await bridge.state.get_property(mount, "EQUATORIAL_EOD_COORD")
                    if p and p.get("state") == "Ok":
                        break
                    await asyncio.sleep(1)
                await asyncio.sleep(2)

            # Capture + solve
            await bridge.indi.send_number(cam, "CCD_EXPOSURE",
                {"CCD_EXPOSURE_VALUE": exposure})
            deadline = asyncio.get_event_loop().time() + exposure + 30
            while asyncio.get_event_loop().time() < deadline:
                if run["status"] == "aborting":
                    run["status"] = "aborted"; return
                p = await bridge.state.get_property(cam, "CCD_EXPOSURE")
                if p and p.get("state") in ("Ok", "Idle"):
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(1.5)
            snap = await bridge.state.snapshot()
            fits_path = (snap.get("last_frame") or {}).get("path")
            if not fits_path:
                run["status"] = "failed"
                run["error"] = f"step {step + 1}: frame non disponibile"
                return

            # Solve
            from pathlib import Path
            from ..config import get_settings
            p = Path(fits_path)
            if not p.is_absolute():
                p = get_settings().images_dir / p
            sv_id = f"sv_{int(asyncio.get_event_loop().time() * 1000)}"
            _SOLVE_RUNS[sv_id] = {"id": sv_id, "path": str(p), "status": "running",
                                  "result": None, "error": None, "stdout_tail": ""}
            await _run_solve(sv_id, p, {"hint_radius": 10.0})
            sv = _SOLVE_RUNS[sv_id]
            if sv["status"] != "done" or not sv["result"]:
                run["status"] = "failed"
                run["error"] = f"step {step + 1}: solve fallito ({sv.get('error')})"
                return

            run["samples"].append({
                "step": step + 1,
                "ra_hours": sv["result"]["ra_hours"],
                "dec_deg": sv["result"]["dec_deg"],
                "scale": sv["result"]["scale_arcsec_px"],
            })

        # Calcolo errore polar (drift-based semplice):
        # Dec deve essere costante se polar align OK. Drift = errore.
        decs = [s["dec_deg"] for s in run["samples"]]
        delta_dec_arcmin = (max(decs) - min(decs)) * 60.0
        # Heuristic: drift in dec è proporzionale a errore polar AZ (per stelle vicine al meridiano)
        # alt_error: difficile senza assunzioni, lasciato come delta dec / 2
        run["az_error_arcmin"] = delta_dec_arcmin
        run["alt_error_arcmin"] = delta_dec_arcmin / 2.0
        run["status"] = "done"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
