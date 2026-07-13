"""Stato condiviso del bridge: INDI properties + PHD2 + immagini + connessioni.

DoD:
- Aggregato in-memory aggiornato da INDI/PHD2/Images
- Lock asyncio per scritture coerenti
- Snapshot immutabile (dict) per broadcast WS
- Derivati per device "tipici" (mount, camera principale, focuser, filter wheel)
- Listener pattern: subscribe per tipo evento -> broadcast push

Errori prevenuti:
- E8: race -> tutto sotto lock per write; read produce copia
- Set di property con tipo cambiato -> ricreate Property dall'evento def successivo
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .indi.properties import (
    Element,
    PropPerm,
    PropState,
    PropType,
    Property,
    SwitchRule,
    parse_value_for_type,
)
from .indi.protocol import IndiEvent, decode_blob

log = logging.getLogger(__name__)


# Listener: riceve dict serializzabile (evento). Async.
StateListener = Callable[[dict], Awaitable[None]]
FrameListener = Callable[[dict, bytes], Awaitable[None]]  # (metadata, jpeg_bytes)


@dataclass
class ConnectionsView:
    indi: str = "disconnected"
    phd2: str = "disconnected"
    started_at: float = field(default_factory=time.time)


class StateManager:
    """Stato globale del bridge, fonte unica di verità per le route REST/WS."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._properties: dict[str, Property] = {}  # key = "device::name"
        self._devices: set[str] = set()
        self._messages: list[dict] = []  # ultimi messaggi INDI (cap 100)
        self._phd2_live: dict[str, Any] = {}
        self._connections = ConnectionsView()
        self._last_frame_meta: dict[str, Any] = {}
        # v0.3.15: conserva l'ultimo JPEG in RAM così l'app può recuperarlo
        # via REST (es. al rientro da background, quando la WS /ws/frames si è
        # riconnessa ma il bridge non re-invia l'ultimo frame già broadcastato).
        self._last_jpeg: bytes = b""
        self._listeners: list[StateListener] = []
        self._frame_listeners: list[FrameListener] = []

    # --- Listener mgmt ------------------------------------------------------

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: StateListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def add_frame_listener(self, listener: FrameListener) -> None:
        self._frame_listeners.append(listener)

    def remove_frame_listener(self, listener: FrameListener) -> None:
        if listener in self._frame_listeners:
            self._frame_listeners.remove(listener)

    async def _broadcast(self, event: dict) -> None:
        if not self._listeners:
            return
        # Copia listener per evitare mutazione concorrente durante iterazione
        ls = list(self._listeners)
        for l in ls:
            try:
                await l(event)
            except Exception:
                log.exception("state listener crashed")

    async def _broadcast_frame(self, meta: dict, jpeg: bytes) -> None:
        if not self._frame_listeners:
            return
        ls = list(self._frame_listeners)
        for l in ls:
            try:
                await l(meta, jpeg)
            except Exception:
                log.exception("frame listener crashed")

    # --- Connection state ---------------------------------------------------

    async def set_indi_connection(self, state: str) -> None:
        async with self._lock:
            self._connections.indi = state
        await self._broadcast({"type": "connection", "indi": state})

    async def set_phd2_connection(self, state: str) -> None:
        async with self._lock:
            self._connections.phd2 = state
        await self._broadcast({"type": "connection", "phd2": state})

    # --- INDI events ingest -------------------------------------------------

    async def handle_indi_event(self, ev: IndiEvent) -> None:
        if ev.kind == "def":
            await self._on_def(ev)
        elif ev.kind == "set":
            await self._on_set(ev)
        elif ev.kind == "del":
            await self._on_del(ev)
        elif ev.kind == "message":
            await self._on_message(ev)

    async def _on_def(self, ev: IndiEvent) -> None:
        prop = ev.property
        if prop is None:
            return
        async with self._lock:
            self._properties[prop.key] = prop
            self._devices.add(prop.device)
        await self._broadcast({
            "type": "property_def",
            "device": prop.device,
            "name": prop.name,
            "property": prop.to_dict(),
        })

    async def _on_set(self, ev: IndiEvent) -> None:
        key = f"{ev.device}::{ev.name}"
        blob_payloads: list[bytes] = []
        async with self._lock:
            prop = self._properties.get(key)
            if prop is None:
                # set prima del def -> ignoriamo, sarà sincronizzato al prossimo def
                return
            for elt_name, raw in ev.payload.get("values", []):
                if prop.type == PropType.BLOB:
                    # decode base64
                    blob = decode_blob(raw)
                    prop.update_value(elt_name, blob)
                    prop.update_state(ev.payload.get("state"), ev.payload.get("timestamp"))
                    # Mantieni i bytes per processing fuori dal lock
                    if blob and len(blob) > 1024:  # filtra blob piccoli
                        blob_payloads.append(blob)
                        log.info("INDI BLOB received device=%s prop=%s elt=%s size=%d bytes",
                                 ev.device, ev.name, elt_name, len(blob))
                    continue
                value = parse_value_for_type(prop.type, raw)
                prop.update_value(elt_name, value)
            prop.update_state(ev.payload.get("state"), ev.payload.get("timestamp"))
            snapshot = prop.to_dict()

        # Se è una BLOB di camera (CCD1/etc), processa in memoria e invia frame
        for blob in blob_payloads:
            # Avvio task asincrono fuori dal lock
            asyncio.create_task(self._process_blob_in_memory(ev.device, ev.name, blob))

        # broadcast solo se non era 100% blob
        if ev.payload.get("type") != "BLOB":
            await self._broadcast({
                "type": "property_set",
                "device": ev.device,
                "name": ev.name,
                "property": snapshot,
            })

    async def _process_blob_in_memory(self, device: str, prop_name: str, blob: bytes) -> None:
        """Processa BLOB FITS in memoria (no I/O su disco) e invia frame all'app.
        Chiamato quando il bridge riceve il BLOB via enableBLOB Also (parallelo a Ekos).
        """
        # Verifica che siano i primi byte di un FITS ('SIMPLE  =')
        if len(blob) < 80 or not blob.startswith(b"SIMPLE"):
            return
        try:
            from .images.processor import process_fits_bytes_async
            result = await process_fits_bytes_async(blob)
        except Exception as e:
            log.warning("BLOB process failed for %s::%s: %s", device, prop_name, e)
            return
        meta = {
            "path": f"<blob:{device}:{prop_name}>",
            "name": f"{device}_{prop_name}",
            "width": result.width, "height": result.height,
            "median": result.median, "vmin": result.vmin, "vmax": result.vmax,
            "hfr": result.hfr_approx, "stars": result.star_count,
            "is_color": result.is_color, "bayer": result.bayer_pattern,
            "exposure": result.exposure, "filter": result.filter_name,
            "frame_type": result.frame_type, "object": result.object_name,
            "source": "blob",
        }
        await self.handle_frame(meta["path"], result.jpeg, result.thumbnail, meta)

    async def _on_del(self, ev: IndiEvent) -> None:
        async with self._lock:
            if ev.name:
                self._properties.pop(f"{ev.device}::{ev.name}", None)
            else:
                # delete tutto il device
                for key in list(self._properties.keys()):
                    if key.startswith(f"{ev.device}::"):
                        self._properties.pop(key, None)
                self._devices.discard(ev.device)
        await self._broadcast({
            "type": "property_del",
            "device": ev.device,
            "name": ev.name,
        })

    async def _on_message(self, ev: IndiEvent) -> None:
        msg = {
            "device": ev.device,
            "message": ev.payload.get("message", ""),
            "timestamp": ev.payload.get("timestamp", ""),
            "ts": time.time(),
        }
        async with self._lock:
            self._messages.append(msg)
            if len(self._messages) > 100:
                self._messages = self._messages[-100:]
        await self._broadcast({"type": "indi_message", **msg})

    # --- PHD2 ingest --------------------------------------------------------

    async def handle_phd2_event(self, ev: dict) -> None:
        async with self._lock:
            self._phd2_live = dict(self._phd2_live)  # ricopia
        await self._broadcast({"type": "phd2_event", "event": ev})

    async def update_phd2_live(self, live: dict) -> None:
        async with self._lock:
            self._phd2_live = dict(live)
        await self._broadcast({"type": "phd2_live", "phd2": dict(live)})

    # --- Frames -------------------------------------------------------------

    async def handle_frame(self, source_path: str, jpeg: bytes,
                           thumbnail: bytes, meta: dict) -> None:
        async with self._lock:
            self._last_frame_meta = {
                "path": source_path,
                **meta,
                "ts": time.time(),
            }
            self._last_jpeg = jpeg  # v0.3.15: per recupero REST
            last = dict(self._last_frame_meta)
        # /ws/state riceve solo metadata (senza jpeg)
        await self._broadcast({"type": "frame_meta", "frame": last})
        # /ws/frames riceve jpeg+meta
        await self._broadcast_frame(last, jpeg)

    # --- Read API ----------------------------------------------------------

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "connections": {
                    "indi": self._connections.indi,
                    "phd2": self._connections.phd2,
                    "started_at": self._connections.started_at,
                },
                "devices": sorted(self._devices),
                "properties": [p.to_dict() for p in self._properties.values()],
                "phd2": dict(self._phd2_live),
                "last_frame": dict(self._last_frame_meta),
                "messages": list(self._messages[-20:]),
            }

    async def last_jpeg(self) -> tuple[bytes, dict]:
        """v0.3.15: ultimo JPEG intercettato + i suoi metadati. Per recupero
        REST quando la WS non lo ri-invia (es. rientro da background)."""
        async with self._lock:
            return self._last_jpeg, dict(self._last_frame_meta)

    async def get_device_properties(self, device: str) -> list[dict]:
        async with self._lock:
            return [p.to_dict() for p in self._properties.values() if p.device == device]

    async def get_property(self, device: str, name: str) -> Optional[dict]:
        async with self._lock:
            p = self._properties.get(f"{device}::{name}")
            return p.to_dict() if p else None

    async def list_devices(self) -> list[str]:
        async with self._lock:
            return sorted(self._devices)

    async def find_devices_by_role(self, prop_name: str) -> list[str]:
        """Trova tutti i device che espongono una certa property (es. EQUATORIAL_EOD_COORD per mount)."""
        async with self._lock:
            return sorted({p.device for p in self._properties.values() if p.name == prop_name})
