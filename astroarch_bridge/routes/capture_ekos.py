"""Route /api/capture - integrazione con Ekos via DBus.

Permette di pianificare una sequenza in Ekos Capture caricando un file .esq.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..config import get_settings
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/capture", tags=["capture"], dependencies=[Depends(require_token)])
_logger = logging.getLogger("astroarch_bridge.capture_ekos")

EKOS_DBUS_SERVICE = "org.kde.kstars"
EKOS_CAPTURE_PATH = "/KStars/Ekos/Capture"

# Dove Ekos persiste le impostazioni Capture per train (KStars 3.x)
KSTARS_USERDB = Path.home() / ".local/share/kstars/userdb.sqlite"


def _read_ekos_capture_settings(train_id: int | None = None) -> dict:
    """Legge dalla userdb di KStars le impostazioni Capture configurate
    dall'utente (cartella di salvataggio, placeholder format, formato suffix).

    NON modifica niente — è SOLO una lettura. Serve per includere nell'ESQ
    gli stessi valori che l'utente vede nella sua UI Ekos, così l'app non
    sovrascrive mai i suoi path con valori vuoti.

    Args:
      train_id: opzionale, se None usa il primo train trovato

    Returns: dict con chiavi (può essere vuoto se db non leggibile):
      fits_dir            str   — cartella salvataggio (fileDirectoryT)
      placeholder_format  str   — pattern path (placeholderFormatT)
      placeholder_suffix  int   — formatSuffixN
      target_name         str   — targetNameT
      formats_list        list  — formatsList
      filters_list        list  — filtersList
    """
    if not KSTARS_USERDB.exists():
        _logger.warning("KStars userdb not found at %s", KSTARS_USERDB)
        return {}
    try:
        conn = sqlite3.connect(f"file:{KSTARS_USERDB}?mode=ro", uri=True,
                                timeout=2.0)
        try:
            cur = conn.cursor()
            if train_id is not None:
                cur.execute(
                    "SELECT settings FROM opticaltrainsettings "
                    "WHERE opticaltrain = ? LIMIT 1", (train_id,))
            else:
                # Prendi il primo train con opticaltrain piccolo (escludiamo
                # 4294967295 che è il "global default")
                cur.execute(
                    "SELECT settings FROM opticaltrainsettings "
                    "WHERE opticaltrain < 1000 "
                    "ORDER BY opticaltrain LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        settings = json.loads(row[0])
        # La struttura è {"0": {capture settings}, "1": {focus settings}, ...}
        # Capture è sotto la chiave "0"
        cap = settings.get("0") or {}
        out = {
            "fits_dir": cap.get("fileDirectoryT"),
            "placeholder_format": cap.get("placeholderFormatT"),
            "placeholder_suffix": cap.get("formatSuffixN"),
            "target_name": cap.get("targetNameT"),
            "formats_list": cap.get("formatsList") or [],
            "filters_list": cap.get("filtersList") or [],
        }
        _logger.info("Ekos capture settings from userdb: fits_dir=%r "
                     "placeholder=%r suffix=%s",
                     out["fits_dir"], out["placeholder_format"],
                     out["placeholder_suffix"])
        return out
    except Exception as e:
        _logger.warning("cannot read Ekos capture settings: %s", e)
        return {}


def _read_active_train_id() -> int | None:
    """Legge CaptureTrainID da ~/.config/kstarsrc."""
    cfg = Path.home() / ".config/kstarsrc"
    if not cfg.exists():
        return None
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CaptureTrainID="):
                v = line.split("=", 1)[1].strip()
                return int(v) if v.lstrip("-").isdigit() else None
    except Exception:
        return None
    return None


def _frame_type_label(ft: str) -> str:
    """FRAME_LIGHT -> Light"""
    return ft.replace("FRAME_", "").capitalize()


def _esq_for_jobs(jobs: list[dict], target_name: str = "",
                  fits_dir: str | None = None,
                  placeholder_format: str | None = None,
                  placeholder_suffix: int | None = None,
                  upload_mode: int | None = None) -> str:
    """Genera XML .esq compatibile Ekos 2.x da lista di job dict.

    NON-INVASIVENESS RULE (v0.2.25): se l'app NON passa un valore esplicito,
    NON includiamo il relativo tag nell'ESQ. In quel caso Ekos usa la sua
    impostazione configurata in Preferences → FITS Settings (FITS Default
    Folder) e il PlaceholderFormat globale. L'app è una GUI: deve essere
    trasparente, non sovrascrivere silenziosamente i path dell'utente.

    Args:
      fits_dir: se None, omette `<FITSDirectory>` → Ekos default
      placeholder_format: se None, omette `<PlaceholderFormat>` → Ekos default
      upload_mode: se None, omette `<UploadMode>` → Ekos default
                   (0=Client, 1=Local, 2=Both)
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             "<SequenceQueue version='2.6'>",
             "<GuideDeviation enabled='false'>2</GuideDeviation>",
             "<HFRCheck enabled='false'><HFRDeviation>2</HFRDeviation>"
             "<HFRCheckAlgorithm>0</HFRCheckAlgorithm>"
             "<HFRCheckThreshold>10</HFRCheckThreshold>"
             "<HFRCheckFrames>1</HFRCheckFrames></HFRCheck>",
             "<RefocusOnTemperatureDelta enabled='false'>1</RefocusOnTemperatureDelta>",
             "<RefocusEveryN enabled='false'>60</RefocusEveryN>",
             "<RefocusOnMeridianFlip enabled='false'/>"]
    for job in jobs:
        f = job.get("filter") or ""
        target = job.get("targetName") or target_name or ""
        cap_fmt = (job.get("captureFormat") or "RAW").upper()
        cap_fmt_label = "RAW 16" if cap_fmt == "RAW" else "RGB"
        encoding = (job.get("transferFormat") or "FITS").upper()
        if encoding == "NATIVE":
            encoding = "Native"
        gain = job.get("gain", 100)
        offset = job.get("offset", 50)
        bin_x = job.get("binX", 1)
        bin_y = job.get("binY", bin_x)
        count = int(job.get("count", 1))
        exp = float(job.get("exposureSec", 60))
        delay = float(job.get("delaySec", 0))
        ft_label = _frame_type_label(job.get("frameType", "FRAME_LIGHT"))
        dither = "1" if job.get("ditherEachFrame") else "0"
        # v0.3.3: setpoint temperatura per-job (opzionale).
        # Se il job ha `temperatureC` valorizzata, emettiamo i tag
        # <TemperatureValue> + <TemperatureEnforced>1 così Ekos attende
        # il setpoint prima di avviare lo scatto del job. Se None,
        # NON emettiamo nulla (Ekos parte alla T attuale del cooler —
        # non-invasiveness rule, vedi commento in _esq_for_jobs docstring).
        temp_c = job.get("temperatureC")
        if temp_c is None:
            # backward-compat: accetta anche la chiave plain "temperature"
            temp_c = job.get("temperature")
        parts.append("<Job>")
        parts.append(f"<Exposure>{exp:g}</Exposure>")
        parts.append(f"<Format>{escape(cap_fmt_label)}</Format>")
        parts.append(f"<Encoding>{escape(encoding)}</Encoding>")
        parts.append(f"<Binning><X>{bin_x}</X><Y>{bin_y}</Y></Binning>")
        parts.append("<Frame><X>0</X><Y>0</Y><W>0</W><H>0</H></Frame>")
        if f:
            parts.append(f"<Filter>{escape(f)}</Filter>")
        parts.append(f"<Type>{ft_label}</Type>")
        parts.append(f"<Count>{count}</Count>")
        parts.append(f"<Delay>{int(delay)}</Delay>")
        if target:
            parts.append(f"<TargetName>{escape(target)}</TargetName>")
        parts.append(f"<GuideDitherPerJob>{dither}</GuideDitherPerJob>")
        if temp_c is not None:
            try:
                temp_f = float(temp_c)
                parts.append(f"<TemperatureValue>{temp_f:g}</TemperatureValue>")
                parts.append("<TemperatureEnforced>1</TemperatureEnforced>")
            except (TypeError, ValueError):
                pass
        # NON-INVASIVENESS: tag opzionali, omessi se non specificati esplicitamente.
        # Quando omessi Ekos usa le impostazioni della sua UI/Preferenze.
        if fits_dir:
            parts.append(f"<FITSDirectory>{escape(fits_dir)}</FITSDirectory>")
        if placeholder_format:
            parts.append(f"<PlaceholderFormat>{escape(placeholder_format)}</PlaceholderFormat>")
            # Suffix: usa quello passato, default 1 (= numerazione)
            suf = placeholder_suffix if placeholder_suffix is not None else 1
            parts.append(f"<PlaceholderSuffix>{int(suf)}</PlaceholderSuffix>")
        if upload_mode is not None:
            parts.append(f"<UploadMode>{int(upload_mode)}</UploadMode>")
        # Gain/offset come PropertyVector
        parts.append("<Properties>")
        parts.append(f"<PropertyVector name='CCD_CONTROLS'>"
                     f"<OneElement name='Gain'>{gain:g}</OneElement></PropertyVector>")
        parts.append(f"<PropertyVector name='CCD_GAIN'>"
                     f"<OneElement name='GAIN'>{gain:g}</OneElement></PropertyVector>")
        parts.append(f"<PropertyVector name='CCD_OFFSET'>"
                     f"<OneElement name='OFFSET'>{offset:g}</OneElement></PropertyVector>")
        parts.append("</Properties>")
        parts.append("<Calibration><PreAction><Type>1</Type></PreAction>"
                     "<FlatDuration dark='false'><Type>ADU</Type>"
                     "<Value>25000</Value><Tolerance>1000</Tolerance>"
                     "<SkyFlat>false</SkyFlat></FlatDuration></Calibration>")
        parts.append("</Job>")
    parts.append("</SequenceQueue>")
    return "\n".join(parts)


# ============================================================================
# v0.3.4: AUTO-DITHER tra scatti, bridge-native.
# ============================================================================
# Problema risolto:
#   Il tag <GuideDitherPerJob>1</GuideDitherPerJob> nell'ESQ funziona solo se
#   Ekos.Guide è collegato a PHD2 (`connectGuider`). Spesso quella connessione
#   non è stabilita (l'utente non ha mai cliccato "Connect external" in Ekos),
#   e il dither nativo Ekos non parte mai.
#
# Soluzione:
#   Il bridge intercetta via dbus-monitor il signal `Ekos.Capture.captureComplete`
#   (emesso dopo ogni frame salvato). Quando si attiva e il job ha
#   `ditherEachFrame=true`, il bridge:
#     1) Mette in PAUSA la sequenza Ekos (`Capture.pause`)
#     2) Chiama `phd2.dither()` direttamente sul bridge (RPC PHD2)
#     3) Aspetta che PHD2 abbia stabilizzato (settle_done event)
#     4) Riavvia la sequenza Ekos (`Capture.start(train)`)
#
# Funziona indipendentemente dal fatto che Ekos↔PHD2 siano collegati o no.
# Compatibile col dither nativo Ekos: se quello è attivo lo lasciamo fare,
# il bridge serve solo come SAFETY NET.
#
# Vive come task asincrono singleton — start/stop pilotati da /ekos_run e
# /ekos_abort.
# ============================================================================

_auto_dither_task: asyncio.Task | None = None
_auto_dither_bridge = None  # type: ignore  # populated by ekos_run
_auto_dither_state = {
    "enabled": False,
    "train": "",
    "frames_seen": 0,
    "last_dither_ts": None,
    "last_error": None,
    "amount": 3.0,
    "settle_pixels": 1.5,
    "settle_time": 10.0,
    "settle_timeout": 60.0,
    # v0.3.6: dither solo ogni N frame (default 1 = ad ogni scatto)
    "frequency": 1,
    "ra_only": False,
}


def _read_ekos_dither_settings() -> dict:
    """Legge le impostazioni Dither che l'utente ha configurato in Ekos
    (~/.config/kstarsrc sezione [Guide]). NON le modifica.
    Mappa keys Ekos → keys del nostro state:
      DitherPixels → amount      (default 3.0 — quanto sposta in pixel)
      DitherSettle → settle_time (default 10.0 — secondi MINIMI di settling)

    NB: NON usiamo `DitherThreshold` come settle_pixels: in Ekos quella chiave
    è la soglia di errore guida sopra la quale Ekos halt/alerta — semantica
    diversa dal `settle.pixels` di PHD2 (RMS max tollerata in settling).
    Per `settle_pixels` lasciamo il default 1.5 (ragionevole per la maggior
    parte dei setup); l'app può ancora forzare un override via job."""
    cfg = Path.home() / ".config/kstarsrc"
    out = {}
    if not cfg.exists():
        return out
    try:
        in_guide = False
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_guide = (s == "[Guide]")
                continue
            if not in_guide or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip()
            if k == "DitherPixels":
                try: out["amount"] = float(v)
                except ValueError: pass
            elif k == "DitherSettle":
                try: out["settle_time"] = float(v)
                except ValueError: pass
    except Exception as e:
        _logger.warning("cannot read Ekos dither settings: %s", e)
    return out


def _read_ekos_dither_enabled() -> bool:
    """True se l'utente ha abilitato il Dither in Ekos (kstarsrc [Guide]
    DitherEnabled). Default True se la chiave manca (come Ekos di default).
    Letto read-only, non modifica nulla. Usato da /ekos_start_existing per
    decidere se armare il watcher quando l'app non passa un override."""
    cfg = Path.home() / ".config/kstarsrc"
    if not cfg.exists():
        return True
    try:
        in_guide = False
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_guide = (s == "[Guide]")
                continue
            if in_guide and s.startswith("DitherEnabled="):
                return s.split("=", 1)[1].strip().lower() in ("true", "1")
    except Exception as e:
        _logger.warning("cannot read Ekos DitherEnabled: %s", e)
    return True


def _read_active_train_name() -> str:
    """Risolve il NOME del train attivo dalla userdb di KStars usando
    CaptureTrainID di kstarsrc. Ritorna stringa vuota se non lo trova.

    Esempio: CaptureTrainID=1 → 'Principale'.
    Serve per chiamare Ekos.Capture.start(train_name) — passare ""
    fa fallire il restart su molte versioni Ekos."""
    train_id = _read_active_train_id()
    if train_id is None:
        return ""
    try:
        conn = sqlite3.connect(f"file:{KSTARS_USERDB}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM opticaltrains WHERE id = ?", (train_id,))
            row = cur.fetchone()
            return row[0] if row else ""
        finally:
            conn.close()
    except Exception as e:
        _logger.warning("cannot read train name: %s", e)
        return ""


async def _auto_dither_worker() -> None:
    """Long-running: ascolta dbus-monitor per `captureComplete` di Ekos.
    Ad ogni evento esegue il ciclo pause → dither → start.
    Restart automatico in caso di crash di dbus-monitor."""
    while _auto_dither_state["enabled"]:
        try:
            env = os.environ.copy()
            uid = os.getuid()
            env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                           f"unix:path=/run/user/{uid}/bus")
            proc = await asyncio.create_subprocess_exec(
                "dbus-monitor", "--session",
                "type='signal',interface='org.kde.kstars.Ekos.Capture',member='captureComplete'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            _logger.info("auto-dither: dbus-monitor started pid=%s", proc.pid)
            try:
                while _auto_dither_state["enabled"]:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    if b"captureComplete" in line:
                        _auto_dither_state["frames_seen"] += 1
                        frequency = max(1, int(_auto_dither_state.get("frequency") or 1))
                        # v0.3.6: dither solo ogni N frame.
                        # Es. frequency=3 → dither dopo i frame 3, 6, 9, ...
                        if _auto_dither_state["frames_seen"] % frequency != 0:
                            _logger.info("auto-dither: captureComplete #%d, "
                                         "skip (frequency=%d, dither ogni %d)",
                                         _auto_dither_state["frames_seen"],
                                         frequency, frequency)
                            continue
                        _logger.info("auto-dither: captureComplete intercepted "
                                     "(frame #%d, frequency=%d) → DITHER",
                                     _auto_dither_state["frames_seen"], frequency)
                        # Spawn cycle as task so we don't block the readline loop
                        asyncio.create_task(_auto_dither_cycle())
            finally:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.warning("auto-dither: monitor loop crashed: %s — retry in 3s", e)
            await asyncio.sleep(3)


async def _auto_dither_cycle() -> None:
    """Esegue un ciclo: pause Ekos → dither PHD2 → WAIT SettleDone → restart Ekos.

    v0.3.5: importante — la chiamata a `phd2.dither(...)` ritorna SUBITO
    con l'ACK JSON-RPC (~40ms). PHD2 fa il vero dither (move + settle) in
    asincrono, emettendo eventi `Settling` (start) e `SettleDone` (end).
    Prima riprendevamo Ekos appena tornata la ACK → Ekos partiva con il
    prossimo scatto MENTRE PHD2 era ancora in settling → trails.
    Adesso aspettiamo che `phd2.live.settling` torni False (con timeout).
    """
    bridge = _auto_dither_bridge
    if bridge is None:
        _logger.warning("auto-dither: bridge non disponibile")
        return
    train = _auto_dither_state["train"]
    try:
        # 1) Pausa Ekos.
        _logger.info("auto-dither: pausing Ekos.Capture (train=%r)", train)
        await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                          "org.kde.kstars.Ekos.Capture.pause", timeout=5.0)
        await asyncio.sleep(0.5)

        # 2) Dither PHD2 (la chiamata ritorna subito con l'ACK)
        amount = _auto_dither_state["amount"]
        settle_pixels = _auto_dither_state["settle_pixels"]
        settle_time = _auto_dither_state["settle_time"]
        settle_timeout = _auto_dither_state["settle_timeout"]
        ra_only = bool(_auto_dither_state.get("ra_only", False))
        _logger.info("auto-dither: dither RPC sent (amount=%.1fpx, settle_pixels=%.1fpx, "
                     "settle_time=%.0fs, ra_only=%s)",
                     amount, settle_pixels, settle_time, ra_only)
        try:
            await bridge.phd2.dither(
                amount=amount,
                ra_only=ra_only,
                settle_pixels=settle_pixels,
                settle_time=settle_time,
                settle_timeout=settle_timeout,
            )
        except Exception as e:
            _auto_dither_state["last_error"] = f"PHD2 dither ACK failed: {e}"
            _logger.warning("auto-dither: PHD2 dither ACK failed (%s), riprendo Ekos", e)
            # fallthrough to resume Ekos comunque
        else:
            # 2b) Aspettiamo l'evento SettleDone (settling torna False).
            # PHD2 prima setta settling=True (Settling event), poi False (SettleDone).
            # Aspettiamo che diventi True ENTRO 5s (PHD2 prende un attimo a partire)
            # poi che torni False ENTRO settle_timeout (max attesa configurata).
            try:
                # wait for Settling=True
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5.0:
                    if bridge.phd2.live.get("settling") is True:
                        break
                    await asyncio.sleep(0.2)
                # wait for Settling=False (SettleDone)
                t1 = time.monotonic()
                max_wait = float(settle_timeout) + 5.0
                while time.monotonic() - t1 < max_wait:
                    if bridge.phd2.live.get("settling") is not True:
                        break
                    await asyncio.sleep(0.3)
                else:
                    _logger.warning("auto-dither: settle timeout (%.0fs), riprendo comunque", max_wait)
                elapsed = time.monotonic() - t0
                _logger.info("auto-dither: PHD2 settled after %.1fs, resuming Ekos", elapsed)
                _auto_dither_state["last_dither_ts"] = time.time()
                _auto_dither_state["last_error"] = None
            except Exception as e:
                _auto_dither_state["last_error"] = f"settle wait failed: {e}"
                _logger.warning("auto-dither: settle wait failed (%s), riprendo", e)

        # 3) Riavvia Ekos.Capture passando il NOME del train (es. "Principale").
        # v0.3.5: prima passavamo train="" → start() falliva silenziosamente
        # e il job restava in pausa per sempre.
        if not train:
            train = _read_active_train_name()
            _auto_dither_state["train"] = train
            _logger.info("auto-dither: resolved train name → %r", train)
        await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                          "org.kde.kstars.Ekos.Capture.start", train, timeout=5.0)
    except Exception as e:
        _auto_dither_state["last_error"] = f"cycle failed: {e}"
        _logger.exception("auto-dither cycle failed")
        # Tentativo last-resort: prova a riavviare Ekos
        try:
            await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                              "org.kde.kstars.Ekos.Capture.start", train or "", timeout=5.0)
        except Exception:
            pass


async def _start_auto_dither(bridge, train: str) -> None:
    """Avvia il worker di auto-dither (idempotente)."""
    global _auto_dither_task, _auto_dither_bridge
    _auto_dither_bridge = bridge
    _auto_dither_state["enabled"] = True
    _auto_dither_state["train"] = train
    _auto_dither_state["frames_seen"] = 0
    _auto_dither_state["last_error"] = None
    if _auto_dither_task is None or _auto_dither_task.done():
        _auto_dither_task = asyncio.create_task(_auto_dither_worker())
        _logger.info("auto-dither: worker started for train=%r", train)


async def _stop_auto_dither() -> None:
    """Ferma il worker di auto-dither (idempotente)."""
    global _auto_dither_task
    _auto_dither_state["enabled"] = False
    if _auto_dither_task is not None and not _auto_dither_task.done():
        _auto_dither_task.cancel()
        try:
            await _auto_dither_task
        except (asyncio.CancelledError, Exception):
            pass
    _auto_dither_task = None
    _logger.info("auto-dither: worker stopped")


# ============================================================================
# v0.3.18: capture-preview worker — mostra nell'app i frame della SEQUENZA
# anche quando la camera è in upload mode LOCAL (Ekos salva i FITS su disco e
# NON li trasmette via INDI → il bridge non riceve BLOB). Fa polling della
# cartella di cattura di Ekos e, ad ogni nuova posa salvata, la invia all'app —
# SALVO che sia arrivato un BLOB di recente (upload CLIENT/BOTH: il frame è già
# stato mostrato, niente doppione). È read-only sul disco: nessuna modifica
# alla config o ai file dell'utente.
# ============================================================================
_capture_preview_state = {"enabled": False}
_capture_preview_task: "asyncio.Task | None" = None
_capture_preview_bridge = None

_FITS_EXTS = (".fits", ".fit", ".fts")


def _latest_fits_in(root: str) -> "tuple[str, float] | None":
    """(path, mtime) del FITS più recente sotto `root` (ricorsivo), o None."""
    latest_path = None
    latest_mtime = -1.0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower().endswith(_FITS_EXTS):
                    fp = os.path.join(dirpath, fn)
                    try:
                        m = os.path.getmtime(fp)
                    except OSError:
                        continue
                    if m > latest_mtime:
                        latest_mtime = m
                        latest_path = fp
    except Exception as e:
        _logger.warning("capture-preview: scan %s failed: %s", root, e)
    if latest_path is None:
        return None
    return latest_path, latest_mtime


async def _capture_preview_worker() -> None:
    """Polling della cartella di cattura: invia all'app l'ultima posa salvata
    su disco quando la camera è in upload LOCAL (nessun BLOB). Read-only."""
    bridge = _capture_preview_bridge
    # Marker iniziale: il file già presente più recente NON va re-inviato
    # (non è una posa nuova di questa sequenza).
    last_mtime = 0.0
    try:
        settings = _read_ekos_capture_settings(_read_active_train_id())
        cur = _latest_fits_in(settings.get("fits_dir") or "")
        if cur:
            last_mtime = cur[1]
    except Exception:
        pass
    idle_cycles = 0
    _logger.info("capture-preview: worker started")
    while _capture_preview_state["enabled"]:
        await asyncio.sleep(4.0)
        try:
            settings = _read_ekos_capture_settings(_read_active_train_id())
            fits_dir = settings.get("fits_dir")
            cur = _latest_fits_in(fits_dir) if fits_dir else None
            if cur and cur[1] > last_mtime + 0.001:
                path, mtime = cur
                last_mtime = mtime
                idle_cycles = 0
                if bridge is None:
                    pass
                elif bridge.state.seconds_since_last_blob() <= 8.0:
                    # Upload CLIENT/BOTH: frame già mostrato via BLOB → no doppione.
                    _logger.info("capture-preview: nuova posa ma BLOB recente → skip")
                else:
                    # Upload LOCAL: leggi il file da disco e invialo all'app.
                    await asyncio.sleep(0.6)  # attende la fine scrittura
                    await bridge.state.push_fits_file(path)
            else:
                idle_cycles += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.warning("capture-preview: loop error: %s", e)
            idle_cycles += 1
        # auto-stop dopo ~20 min senza nuove pose (sequenza probabilmente finita)
        if idle_cycles > 300:
            _logger.info("capture-preview: nessuna nuova posa per ~20min → stop")
            break
    _capture_preview_state["enabled"] = False
    _logger.info("capture-preview: worker stopped")


async def _start_capture_preview(bridge) -> None:
    """Avvia il capture-preview worker (idempotente)."""
    global _capture_preview_task, _capture_preview_bridge
    _capture_preview_bridge = bridge
    _capture_preview_state["enabled"] = True
    if _capture_preview_task is None or _capture_preview_task.done():
        _capture_preview_task = asyncio.create_task(_capture_preview_worker())


async def _stop_capture_preview() -> None:
    """Ferma il capture-preview worker (idempotente)."""
    global _capture_preview_task
    _capture_preview_state["enabled"] = False
    if _capture_preview_task is not None and not _capture_preview_task.done():
        _capture_preview_task.cancel()
        try:
            await _capture_preview_task
        except (asyncio.CancelledError, Exception):
            pass
    _capture_preview_task = None


async def _dbus_call(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Esegue qdbus6 e ritorna (returncode, stdout)."""
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


@router.get("/ekos_user_settings")
async def ekos_user_settings() -> dict:
    """Restituisce le impostazioni Capture che l'utente ha settato dentro
    Ekos sul desktop (cartella di salvataggio, placeholder format, ...).

    Letti read-only dal database utente di KStars (~/.local/share/kstars/
    userdb.sqlite, tabella opticaltrainsettings). NON modifichiamo niente
    — la app è una GUI, può solo leggere.

    Utile per:
      - mostrare nella UI dell'app quale cartella verrà usata
      - debug del problema "Ekos mi ha cancellato la cartella" (era un
        bug, fix in v0.2.28)
    """
    train_id = _read_active_train_id()
    settings = _read_ekos_capture_settings(train_id)
    return {
        "train_id": train_id,
        "userdb_path": str(KSTARS_USERDB),
        "userdb_exists": KSTARS_USERDB.exists(),
        **settings,
    }


@router.get("/ekos_alive")
async def ekos_alive() -> dict:
    """Verifica se Ekos è raggiungibile via DBus."""
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getJobCount")
    return {
        "alive": rc == 0,
        "job_count": int(out) if rc == 0 and out.isdigit() else None,
        "raw": out,
    }


@router.post("/ekos_run")
async def ekos_run(payload: dict = Body(...), bridge: Bridge = Depends(get_bridge)) -> dict:
    """Genera ESQ dai jobs ricevuti, lo carica in Ekos Capture, avvia.

    Body:
      jobs: list di CaptureJob serializzati (vedi /api/capture/ekos_run/preview_esq)
      target: str (target name, default "AstroarchInterface")
      fits_dir: str opzionale
      train: str (optical train, default "")
      master: bool (default true)
      auto_start: bool (default true)
    """
    jobs = payload.get("jobs") or []
    if not jobs:
        raise HTTPException(status_code=400, detail="jobs required")
    target = payload.get("target") or "AstroarchInterface"
    train = payload.get("train") or ""
    master = bool(payload.get("master", True))
    auto_start = bool(payload.get("auto_start", True))

    # ========================================================================
    # v0.2.28: LEGGE LE IMPOSTAZIONI UTENTE DA EKOS (userdb.sqlite)
    # ========================================================================
    # Prima di v0.2.25 il bridge forzava fits_dir a
    #   ~/Pictures/Ekos/AstroarchInterface/
    # In v0.2.25 abbiamo rimosso il default ma omettendo i tag dall'ESQ —
    # Ekos al loadSequenceQueue si trovava i campi VUOTI e li sovrascriveva
    # nella sua UI (cartella e formato cancellati, job si fermava).
    #
    # FIX DEFINITIVO: leggiamo `fileDirectoryT` e `placeholderFormatT` dal
    # database utente di KStars (tabella opticaltrainsettings) per il train
    # attivo. Sono ESATTAMENTE i valori che l'utente vede nella sua UI Ekos.
    # Li mettiamo nell'ESQ così l'app non modifica mai le sue scelte.
    # L'app può ancora forzare override esplicito passandoli nel payload.
    user_settings = _read_ekos_capture_settings(_read_active_train_id())
    fits_dir = payload.get("fits_dir") or user_settings.get("fits_dir")
    placeholder_format = (payload.get("placeholder_format")
                          or user_settings.get("placeholder_format"))
    placeholder_suffix = (payload.get("placeholder_suffix")
                          if payload.get("placeholder_suffix") is not None
                          else user_settings.get("placeholder_suffix"))
    upload_mode = payload.get("upload_mode")
    _logger.info("ekos_run: using fits_dir=%r placeholder=%r suffix=%s",
                 fits_dir, placeholder_format, placeholder_suffix)

    # Genera ESQ
    esq = _esq_for_jobs(jobs, target_name=target, fits_dir=fits_dir,
                       placeholder_format=placeholder_format,
                       placeholder_suffix=placeholder_suffix,
                       upload_mode=upload_mode)

    # L'ESQ è un FILE temporaneo di servizio: lo salviamo in /tmp, non
    # nella cartella immagini dell'utente. Sopravvive solo finché Ekos
    # lo legge (qualche secondo) ed è rigenerato ad ogni run.
    save_dir = Path("/tmp/astroarch_bridge")
    save_dir.mkdir(parents=True, exist_ok=True)
    esq_path = save_dir / f"sequence_{int(time.time())}.esq"
    esq_path.write_text(esq, encoding="utf-8")

    # Carica in Ekos via DBus
    rc1, out1 = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.clearSequenceQueue",
    )
    rc2, out2 = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.loadSequenceQueue",
        str(esq_path),
        train,
        "true" if master else "false",
        target,
    )
    if rc2 != 0 or out2.lower() == "false":
        raise HTTPException(status_code=500, detail=f"loadSequenceQueue failed: {out2}")

    started = False
    start_msg = ""
    if auto_start:
        rc3, out3 = await _dbus_call(
            EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
            "org.kde.kstars.Ekos.Capture.start", train,
        )
        started = rc3 == 0
        start_msg = out3
    # v0.3.4–0.3.6: auto-dither bridge-native.
    # Se almeno un job ha `ditherEachFrame=true`, accendiamo il watcher che
    # via dbus-monitor intercetta `captureComplete` e chiama PHD2.dither().
    any_dither = any(bool(j.get("ditherEachFrame")) for j in jobs)
    # Priorità dei parametri dither:
    #   1) override TOP-LEVEL del payload (app v0.2.36+: pannello UI dither)
    #   2) override per-job (CaptureJob.ditherAmount, raro)
    #   3) Ekos kstarsrc [Guide] (DitherPixels/DitherSettle)
    #   4) default ragionevoli
    ekos_dither = _read_ekos_dither_settings()
    if ekos_dither.get("amount") is not None:
        _auto_dither_state["amount"] = float(ekos_dither["amount"])
    if ekos_dither.get("settle_time") is not None:
        _auto_dither_state["settle_time"] = float(ekos_dither["settle_time"])
    # v0.3.6 default frequency: legge GuideDitherPerJobFrequency da [Capture] kstarsrc
    cfg_kstars = Path.home() / ".config/kstarsrc"
    if cfg_kstars.exists():
        try:
            in_cap = False
            for line in cfg_kstars.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    in_cap = (s == "[Capture]"); continue
                if in_cap and s.startswith("GuideDitherPerJobFrequency="):
                    try: _auto_dither_state["frequency"] = max(1, int(s.split("=",1)[1]))
                    except ValueError: pass
        except Exception:
            pass
    # 2) Override per-job (raro)
    for j in jobs:
        if j.get("ditherAmount") is not None:
            _auto_dither_state["amount"] = float(j["ditherAmount"])
        if j.get("ditherSettlePixels") is not None:
            _auto_dither_state["settle_pixels"] = float(j["ditherSettlePixels"])
        if j.get("ditherSettleTime") is not None:
            _auto_dither_state["settle_time"] = float(j["ditherSettleTime"])
        break
    # 1) Override TOP-LEVEL del payload (massima priorità — pannello UI app)
    if payload.get("ditherAmount") is not None:
        _auto_dither_state["amount"] = float(payload["ditherAmount"])
    if payload.get("ditherSettleTime") is not None:
        _auto_dither_state["settle_time"] = float(payload["ditherSettleTime"])
    if payload.get("ditherSettlePixels") is not None:
        _auto_dither_state["settle_pixels"] = float(payload["ditherSettlePixels"])
    if payload.get("ditherFrequency") is not None:
        _auto_dither_state["frequency"] = max(1, int(payload["ditherFrequency"]))
    if payload.get("ditherRaOnly") is not None:
        _auto_dither_state["ra_only"] = bool(payload["ditherRaOnly"])
    # v0.3.5: risolvi nome reale del train per il Capture.start() di restart
    # (se l'app passa train vuoto, leggiamo CaptureTrainID → nome dal userdb)
    effective_train = train or _read_active_train_name() or ""
    _logger.info("ekos_run: dither config — amount=%.1fpx settle_pixels=%.1f "
                 "settle_time=%.0fs freq=%d ra_only=%s train=%r",
                 _auto_dither_state["amount"],
                 _auto_dither_state["settle_pixels"],
                 _auto_dither_state["settle_time"],
                 _auto_dither_state["frequency"],
                 _auto_dither_state["ra_only"],
                 effective_train)
    if any_dither and started:
        await _start_auto_dither(bridge, effective_train)
    else:
        await _stop_auto_dither()
    # v0.3.18: capture-preview worker — mostra i frame della sequenza nell'app
    # anche in upload LOCAL. Attivo per tutta la sequenza, indipendente dal dither.
    if started:
        await _start_capture_preview(bridge)
    return {
        "ok": True,
        "esq_path": str(esq_path),
        "loaded": rc2 == 0,
        "load_response": out2,
        "started": started,
        "start_response": start_msg,
        "jobs_count": len(jobs),
        "auto_dither_enabled": any_dither,
    }


@router.post("/ekos_start_existing")
async def ekos_start_existing(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """v0.3.17: avvia la sequenza GIÀ caricata nella Capture queue di Ekos,
    SENZA clear/load dei job dell'app.

    A differenza di /ekos_run (che fa clearSequenceQueue + loadSequenceQueue
    sovrascrivendo la coda con i job dell'app), questo endpoint rispetta
    ESATTAMENTE la sequenza che l'utente ha pianificato a mano nella UI di
    Ekos sul desktop. Il bridge si limita a:
      1. verificare che una sequenza sia effettivamente caricata (job_count>0);
      2. Capture.start(train) sul train attivo;
      3. armare l'auto-dither bridge-native se il dither è abilitato in Ekos.

    NON tocca montatura, guida PHD2, cooler né la sequenza: è la modalità
    più non-invasiva possibile ("premi play su quello che ho già preparato").

    Body (tutto opzionale):
      train: str — optical train; se vuoto risolve quello attivo dal userdb.
      auto_dither: bool|null — forza on/off del watcher dither bridge-native.
                   Se assente/null legge DitherEnabled da Ekos [Guide].
      dither* : override espliciti dei parametri (come /ekos_run).
    """
    train = payload.get("train") or ""

    # 1. Deve esserci una sequenza già pianificata a mano in Ekos.
    rc_jc, val_jc = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.getJobCount")
    job_count = int(val_jc) if rc_jc == 0 and val_jc.isdigit() else 0
    if job_count <= 0:
        raise HTTPException(
            status_code=409,
            detail="Nessuna sequenza pianificata in Ekos. Apri Ekos sul "
                   "desktop e aggiungi almeno un job alla Capture queue, "
                   "oppure usa 'Via Ekos' per inviare i job dell'app.")

    # 2. Risolvi il nome del train e avvia la sequenza esistente.
    #    Usiamo il nome risolto (non "") perché su molte versioni di Ekos
    #    Capture.start("") fallisce silenziosamente (vedi commento v0.3.5).
    effective_train = train or _read_active_train_name() or ""
    rc_st, out_st = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.start", effective_train)
    started = rc_st == 0
    if not started:
        raise HTTPException(status_code=500,
                            detail=f"Capture.start fallito: {out_st}")

    # 3. Auto-dither bridge-native.
    #    on/off: override esplicito del payload, altrimenti DitherEnabled di
    #    Ekos. I parametri (px, settle, freq) vengono SEMPRE da Ekos —
    #    coerente con "usa esattamente la mia configurazione Ekos".
    want_dither = payload.get("auto_dither")
    if want_dither is None:
        want_dither = _read_ekos_dither_enabled()
    want_dither = bool(want_dither)

    ekos_dither = _read_ekos_dither_settings()
    if ekos_dither.get("amount") is not None:
        _auto_dither_state["amount"] = float(ekos_dither["amount"])
    if ekos_dither.get("settle_time") is not None:
        _auto_dither_state["settle_time"] = float(ekos_dither["settle_time"])
    # frequency: GuideDitherPerJobFrequency da [Capture] (come /ekos_run)
    cfg_kstars = Path.home() / ".config/kstarsrc"
    if cfg_kstars.exists():
        try:
            in_cap = False
            for line in cfg_kstars.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    in_cap = (s == "[Capture]"); continue
                if in_cap and s.startswith("GuideDitherPerJobFrequency="):
                    try: _auto_dither_state["frequency"] = max(1, int(s.split("=", 1)[1]))
                    except ValueError: pass
        except Exception:
            pass
    # override espliciti dal payload (massima priorità, come /ekos_run)
    if payload.get("ditherAmount") is not None:
        _auto_dither_state["amount"] = float(payload["ditherAmount"])
    if payload.get("ditherSettleTime") is not None:
        _auto_dither_state["settle_time"] = float(payload["ditherSettleTime"])
    if payload.get("ditherSettlePixels") is not None:
        _auto_dither_state["settle_pixels"] = float(payload["ditherSettlePixels"])
    if payload.get("ditherFrequency") is not None:
        _auto_dither_state["frequency"] = max(1, int(payload["ditherFrequency"]))
    if payload.get("ditherRaOnly") is not None:
        _auto_dither_state["ra_only"] = bool(payload["ditherRaOnly"])

    _logger.info("ekos_start_existing: job_count=%d train=%r auto_dither=%s "
                 "(amount=%.1fpx settle=%.0fs freq=%d)",
                 job_count, effective_train, want_dither,
                 _auto_dither_state["amount"], _auto_dither_state["settle_time"],
                 _auto_dither_state["frequency"])

    if want_dither:
        await _start_auto_dither(bridge, effective_train)
    else:
        await _stop_auto_dither()

    # v0.3.18: capture-preview worker (mostra i frame della sequenza in app
    # anche in upload LOCAL), indipendente dal dither.
    if started:
        await _start_capture_preview(bridge)

    return {
        "ok": True,
        "started": started,
        "start_response": out_st,
        "job_count": job_count,
        "train": effective_train,
        "auto_dither_enabled": want_dither,
    }


@router.get("/ekos_status")
async def ekos_status() -> dict:
    """Stato live della sequenza Ekos Capture."""
    out: dict = {}
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getJobCount")
    out["job_count"] = int(val) if rc == 0 and val.isdigit() else 0
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getActiveJobID")
    out["active_job_id"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getActiveJobRemainingTime")
    out["remaining_seconds"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getOverallRemainingTime")
    out["overall_remaining_seconds"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    if out.get("active_job_id") is not None and out["active_job_id"] >= 0:
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobImageProgress",
                                    str(out["active_job_id"]))
        out["job_image_progress"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobImageCount",
                                    str(out["active_job_id"]))
        out["job_image_count"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobState",
                                    str(out["active_job_id"]))
        out["job_state"] = val if rc == 0 else None
    return out


@router.post("/ekos_abort")
async def ekos_abort() -> dict:
    # v0.3.4: ferma anche il watcher auto-dither
    await _stop_auto_dither()
    # v0.3.18: ferma anche il capture-preview worker
    await _stop_capture_preview()
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.abort", "")
    return {"ok": rc == 0, "raw": out}


@router.post("/auto_dither_arm")
async def auto_dither_arm(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """v0.3.5: arming MANUALE del watcher auto-dither.

    Utile dopo un restart del bridge mentre una sequenza Ekos è già in corso
    (la sequenza non viene interrotta dal restart, ma il watcher sì perché
    il task asincrono viene perso). Senza questo endpoint l'utente dovrebbe
    abort+restart della sequenza dall'app per ri-armare.

    Body:
      enabled: bool — true accende il watcher, false lo spegne
      train:   str  — nome del train (es. 'Principale'); se omesso, viene
                       letto dal userdb tramite CaptureTrainID
    """
    enabled = bool(payload.get("enabled", True))
    if not enabled:
        await _stop_auto_dither()
        return {"ok": True, "enabled": False}

    train = payload.get("train") or _read_active_train_name() or ""
    # Re-read dither settings da Ekos (in case the user has changed them
    # tra restart e arm)
    ekos_dither = _read_ekos_dither_settings()
    if ekos_dither.get("amount") is not None:
        _auto_dither_state["amount"] = float(ekos_dither["amount"])
    if ekos_dither.get("settle_time") is not None:
        _auto_dither_state["settle_time"] = float(ekos_dither["settle_time"])
    await _start_auto_dither(bridge, train)
    return {
        "ok": True, "enabled": True, "train": train,
        "amount": _auto_dither_state["amount"],
        "settle_time": _auto_dither_state["settle_time"],
        "settle_pixels": _auto_dither_state["settle_pixels"],
    }


@router.get("/ekos_dither_settings")
async def ekos_dither_settings() -> dict:
    """v0.3.6: ritorna le impostazioni Dither che l'utente ha configurato
    in Ekos (`~/.config/kstarsrc` sezione [Guide]).

    Letti read-only. NON modifichiamo niente. Serve all'app come "sync
    iniziale" del pannello Dither config: l'app può chiamare questo
    endpoint per popolare i suoi slider con i valori Ekos correnti.

    Ritorna:
      amount        float — DitherPixels (px) — default 3.0 se mancante
      settle_time   float — DitherSettle (s) — default 10.0
      frequency     int   — GuideDitherPerJobFrequency — default 1
      dither_enabled bool — DitherEnabled
    """
    cfg = Path.home() / ".config/kstarsrc"
    out = {
        "amount": 3.0, "settle_time": 10.0, "settle_pixels": 1.5,
        "frequency": 1, "dither_enabled": True, "ra_only": False,
    }
    if not cfg.exists():
        return {**out, "userdb_path": str(cfg), "exists": False}
    try:
        section = None
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s
                continue
            if "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip()
            if section == "[Guide]":
                if k == "DitherPixels":
                    try: out["amount"] = float(v)
                    except ValueError: pass
                elif k == "DitherSettle":
                    try: out["settle_time"] = float(v)
                    except ValueError: pass
                elif k == "DitherEnabled":
                    out["dither_enabled"] = v.lower() in ("true", "1")
            elif section == "[Capture]":
                if k == "GuideDitherPerJobFrequency":
                    try: out["frequency"] = int(v)
                    except ValueError: pass
    except Exception as e:
        _logger.warning("ekos_dither_settings parse error: %s", e)
    return {**out, "userdb_path": str(cfg), "exists": True}


@router.get("/auto_dither_status")
async def auto_dither_status() -> dict:
    """v0.3.4: stato del watcher auto-dither bridge-native.

    Espone:
      - enabled: se il watcher è attivo
      - train: optical train che sta seguendo
      - frames_seen: quanti captureComplete intercettati dalla partenza
      - last_dither_ts: timestamp Unix dell'ultimo dither riuscito
      - last_error: ultimo errore (None se tutto ok)
      - amount / settle_*: parametri correnti del dither
    """
    return dict(_auto_dither_state)


@router.post("/ekos_clear")
async def ekos_clear() -> dict:
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.clearSequenceQueue")
    return {"ok": rc == 0, "raw": out}


# ============================================================================
# v0.3.14: listing + caricamento sequenze .esq esistenti sul Pi (P6 Tucniak)
# ============================================================================

@router.get("/list_sequences")
async def list_sequences() -> dict:
    """Elenca i file sequenza Ekos (.esq) presenti sul Pi nelle cartelle
    comuni (dentro la home utente). Per il picker 'Carica sequenza dal Pi'.
    Restituisce path relativi alla home + size/mtime."""
    home = Path.home().resolve()
    search_dirs = [
        home / "Pictures" / "Ekos",
        home / "Desktop",
        home / ".local" / "share" / "kstars",
        home / "Documents",
        home,
    ]
    found = {}
    for d in search_dirs:
        try:
            if not d.exists():
                continue
            # ricerca non ricorsiva profonda: max 2 livelli per non impallarsi
            for p in list(d.glob("*.esq")) + list(d.glob("*/*.esq")):
                rp = p.resolve()
                if rp in found:
                    continue
                try:
                    rel = str(rp.relative_to(home))
                except ValueError:
                    continue
                found[rp] = {
                    "name": p.name, "path": rel,
                    "dir": str(p.parent.relative_to(home)) if p.parent != home else "",
                    "size": p.stat().st_size if p.exists() else 0,
                    "mtime": p.stat().st_mtime if p.exists() else 0.0,
                }
        except OSError:
            continue
    items = sorted(found.values(), key=lambda x: x["mtime"], reverse=True)
    return {"home": str(home), "count": len(items), "items": items}


@router.post("/load_sequence_file")
async def load_sequence_file(payload: dict = Body(...)) -> dict:
    """Carica in Ekos un file .esq ESISTENTE sul Pi (path relativo alla home).
    A differenza di /ekos_run (che genera un ESQ dai job dell'app), qui si
    riusa una sequenza già salvata dall'utente. SICUREZZA: il path deve
    restare dentro la home e avere estensione .esq.

    Body: {"path": "Pictures/Ekos/M31.esq", "train": "", "auto_start": false}
    """
    rel = payload.get("path") or ""
    home = Path.home().resolve()
    full = (home / rel).resolve()
    # protezione path-traversal + estensione
    try:
        full.relative_to(home)
    except ValueError:
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if full.suffix.lower() != ".esq":
        raise HTTPException(status_code=400, detail="non è un file .esq")
    if not full.exists():
        raise HTTPException(status_code=404, detail="file non trovato")

    train = payload.get("train") or _read_active_train_name() or ""
    target = payload.get("target") or ""
    master = bool(payload.get("master", True))
    auto_start = bool(payload.get("auto_start", False))

    await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                     "org.kde.kstars.Ekos.Capture.clearSequenceQueue")
    rc2, out2 = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.loadSequenceQueue",
        str(full), train, "true" if master else "false", target)
    if rc2 != 0 or out2.lower() == "false":
        raise HTTPException(status_code=500, detail=f"loadSequenceQueue failed: {out2}")

    started = False
    if auto_start:
        rc3, _ = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                  "org.kde.kstars.Ekos.Capture.start", train)
        started = rc3 == 0
    return {"ok": True, "loaded": True, "file": str(full),
            "started": started}


@router.post("/preview_esq")
async def preview_esq(payload: dict = Body(...)) -> dict:
    """Genera ESQ ma non lo invia. Utile per debug.

    Come /ekos_run: per default legge fits_dir/placeholder_format/suffix
    dalla userdb di KStars (ciò che l'utente vede in Ekos). Override
    espliciti possibili.
    """
    jobs = payload.get("jobs") or []
    target = payload.get("target") or ""
    user_settings = _read_ekos_capture_settings(_read_active_train_id())
    return {"esq": _esq_for_jobs(jobs, target_name=target,
        fits_dir=payload.get("fits_dir") or user_settings.get("fits_dir"),
        placeholder_format=(payload.get("placeholder_format")
                            or user_settings.get("placeholder_format")),
        placeholder_suffix=(payload.get("placeholder_suffix")
                            if payload.get("placeholder_suffix") is not None
                            else user_settings.get("placeholder_suffix")),
        upload_mode=payload.get("upload_mode"))}
