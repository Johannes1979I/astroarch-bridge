"""Arresto automatico dei movimenti manuali della montatura ("uomo morto").

Il movimento manuale via INDI (TELESCOPE_MOTION_NS/WE) parte con un comando e
si ferma solo con un secondo comando. Se il secondo si perde — Tailscale che
cade, telefono in standby, controller Bluetooth scarico — la montatura continua
a girare finche' qualcuno non se ne accorge.

Il client che vuole questa protezione manda `ttl_ms` insieme al movimento e poi
lo ripete finche' il tasto resta premuto: ogni ripetizione sposta in avanti la
scadenza. Se le ripetizioni smettono di arrivare, alla scadenza il bridge ferma
quell'asse da solo. Senza `ttl_ms` il comportamento resta quello di sempre, cosi'
le versioni vecchie dell'app (che mandano un solo comando e lo tengono) non
vedono la montatura fermarsi a meta' pressione.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

TTL_MIN_S = 0.3
TTL_MAX_S = 5.0

StopFn = Callable[[], Awaitable[None]]


def clamp_ttl(ttl_ms: float) -> float:
    """ttl in millisecondi dal client -> secondi, entro limiti sensati.

    Il minimo evita che un valore minuscolo produca un movimento a scatti;
    il massimo evita che un valore enorme renda la protezione inutile.
    """
    return min(max(float(ttl_ms) / 1000.0, TTL_MIN_S), TTL_MAX_S)


@dataclass
class _Entry:
    direction: str
    deadline: float
    stop: StopFn
    task: asyncio.Task | None = None


class MotionWatchdog:
    """Una scadenza per ogni coppia (montatura, asse)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[tuple[str, str], _Entry] = {}

    def arm(self, device: str, axis: str, direction: str, ttl_s: float,
            stop: StopFn) -> bool:
        """Arma o rinnova la scadenza dell'asse.

        Ritorna True se l'asse era gia' armato nella stessa direzione: e' una
        ripetizione del client, e il comando di movimento non va rimandato.
        """
        key = (device, axis)
        deadline = self._clock() + ttl_s
        entry = self._entries.get(key)
        if entry is not None and entry.task is not None and not entry.task.done():
            same = entry.direction == direction
            entry.direction = direction
            entry.deadline = deadline
            entry.stop = stop
            return same
        entry = _Entry(direction=direction, deadline=deadline, stop=stop)
        self._entries[key] = entry
        entry.task = asyncio.create_task(self._watch(key, entry))
        return False

    def disarm(self, device: str, axis: str) -> None:
        entry = self._entries.pop((device, axis), None)
        if entry is not None and entry.task is not None:
            entry.task.cancel()

    def armed(self, device: str, axis: str) -> str | None:
        """Direzione armata sull'asse, o None."""
        entry = self._entries.get((device, axis))
        return entry.direction if entry is not None else None

    async def _watch(self, key: tuple[str, str], entry: _Entry) -> None:
        while True:
            remaining = entry.deadline - self._clock()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        # Scaduto: via dalla tabella PRIMA di fermare, cosi' una ripetizione
        # che arriva durante lo stop riparte da zero e rimanda il movimento.
        if self._entries.get(key) is entry:
            del self._entries[key]
        log.warning("slew %s/%s: nessuna conferma dal client, fermo l'asse", *key)
        try:
            await entry.stop()
        except Exception:  # noqa: BLE001 — un errore qui non deve uccidere il loop
            log.exception("slew %s/%s: arresto automatico fallito", *key)
