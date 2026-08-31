"""WS endpoint /ws/state -> clone live di tutti gli eventi (no BLOB).

Flusso:
1. Client si connette con ?token=...
2. Bridge invia subito uno snapshot completo
3. Bridge pusha eventi incrementali (property_def, property_set, indi_message,
   phd2_event, phd2_live, frame_meta, connection)
"""
from __future__ import annotations

import asyncio
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..auth import authenticate_ws
from ..state import StateManager
from .hub import WsHub

log = logging.getLogger(__name__)


def make_state_listener(hub: WsHub):
    async def _listener(event: dict) -> None:
        await hub.broadcast_json(event)
    return _listener


async def state_ws_endpoint(ws: WebSocket, hub: WsHub, state: StateManager) -> None:
    await ws.accept()
    if not await authenticate_ws(ws):
        return
    client = await hub.add(ws)
    if client is None:
        return
    try:
        # Snapshot iniziale CHUNKED:
        # - snapshot_begin: meta + connections + devices + phd2 + last_frame + messages
        # - property_def: una per property (riusa lo stesso handler dell'app)
        # - snapshot_end: marker fine stream iniziale
        # Questo evita di mandare un singolo frame WS di centinaia di KB,
        # che alcuni client (Android Tailscale) possono troncare o decodificare lentamente.
        snap = await state.snapshot()
        properties = snap.pop("properties", [])
        client.enqueue("json", _to_json({
            "type": "snapshot_begin",
            "connections": snap.get("connections", {}),
            "devices": snap.get("devices", []),
            "phd2": snap.get("phd2", {}),
            "last_frame": snap.get("last_frame", {}),
            "messages": snap.get("messages", []),
            "notifications": snap.get("notifications", []),
            "properties_count": len(properties),
        }))
        # Yield ogni 16 messaggi per dare modo al writer_loop di drenare
        for i, p in enumerate(properties):
            client.enqueue("json", _to_json({
                "type": "property_def",
                "device": p.get("device"),
                "name": p.get("name"),
                "property": p,
            }))
            if (i + 1) % 16 == 0:
                await asyncio.sleep(0)
        client.enqueue("json", _to_json({"type": "snapshot_end"}))
        # Mantieni la connessione attiva: leggi (e ignora) input dal client
        while client.alive:
            try:
                msg = await ws.receive_text()
                if msg == "ping":
                    client.enqueue("json", '{"type":"pong"}')
            except WebSocketDisconnect:
                break
    finally:
        await hub.remove(client)


def _to_json(obj: dict) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))
