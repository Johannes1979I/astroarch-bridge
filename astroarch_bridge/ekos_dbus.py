"""Piccoli accessi DBus a Ekos condivisi fra i moduli del bridge.

Serve soprattutto per sapere **quale camera e' quella di guida**: l'euristica
sui nomi (ASI120/290/"guide"...) non copre camere come la ToupTek
GPCMOS02000KMA, mentre Ekos lo sa con certezza (`Ekos.Guide.camera`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)

_EKOS_SERVICE = "org.kde.kstars"
_EKOS_GUIDE_PATH = "/KStars/Ekos/Guide"
_EKOS_GUIDE_IFACE = "org.kde.kstars.Ekos.Guide"

# cache: il nome cambia solo se l'utente riconfigura il profilo
_cache_value: str | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 30.0
_lock = asyncio.Lock()


async def _qdbus(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    env = os.environ.copy()
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    try:
        proc = await asyncio.create_subprocess_exec(
            "qdbus6", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError:
        return -1, "qdbus6 non disponibile"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def guide_camera(force: bool = False) -> str | None:
    """Nome della camera usata dal modulo Guide di Ekos (None se non nota).

    Risultato in cache per ~30s: viene interrogato ad ogni frame BLOB, non
    vogliamo lanciare un processo qdbus per ogni immagine.
    """
    global _cache_value, _cache_ts
    now = time.monotonic()
    if not force and (now - _cache_ts) < _CACHE_TTL:
        return _cache_value
    async with _lock:
        # un'altra coroutine potrebbe averla appena aggiornata
        now = time.monotonic()
        if not force and (now - _cache_ts) < _CACHE_TTL:
            return _cache_value
        rc, out = await _qdbus(_EKOS_SERVICE, _EKOS_GUIDE_PATH,
                               f"{_EKOS_GUIDE_IFACE}.camera")
        _cache_value = out.strip() if (rc == 0 and out.strip()) else None
        _cache_ts = time.monotonic()
        return _cache_value
