"""Factory FastAPI app + lifespan.

DoD:
- Crea state manager, indi/phd2 client, ws hubs, file watcher
- Avvia tutto in lifespan startup; shutdown ordinato
- Registra route REST + WS endpoints
- CORS configurabile (default *)
- Healthcheck pubblico /healthz (no auth)

Errori prevenuti:
- Avvio parziale -> shutdown ordinato anche su fallimento
- Frame jpeg molto grandi su /ws/state -> separati su /ws/frames hub
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocket

from . import __version__
from .config import get_settings
from .deps import Bridge
from .images.watcher import FitsWatcher
from .indi.client import IndiClient
from .indi.protocol import IndiEvent
from .phd2.client import Phd2Client
from .notify.listener import UdpNotifyListener
from .routes import (
    align, camera, capture_ekos, files, filter_wheel, focuser, focuser_ekos,
    guide, indi_panel, mount, notify, observation, observatory, scheduler,
    setup, skymap, system,
)
from .state import StateManager
from .webui import mount_web_ui
from .ws.frame_stream import frame_ws_endpoint, make_frame_listener
from .ws.hub import WsHub
from .ws.state_stream import make_state_listener, state_ws_endpoint

log = logging.getLogger(__name__)

# Non-standard response headers the bridge uses for image metadata. A
# browser hides them from JavaScript unless they are listed in
# Access-Control-Expose-Headers, so a cross-origin web client could not
# read HFR, dimensions or the map centre coordinates.
# Emitted by routes/system.py::last_frame and routes/skymap.py::view.
CORS_EXPOSE_HEADERS = (
    "X-ts", "X-width", "X-height", "X-hfr", "X-stars", "X-filter",
    "X-exposure", "X-frame_type", "X-object",
    "X-Center-RA-Deg", "X-Center-Dec-Deg", "X-FOV-Deg",
)


def create_app() -> FastAPI:
    settings = get_settings()

    state = StateManager()
    state_hub = WsHub(settings.ws_state_max_clients, settings.ws_send_queue)
    frames_hub = WsHub(settings.ws_frames_max_clients, settings.ws_send_queue)
    state.add_listener(make_state_listener(state_hub))
    state.add_frame_listener(make_frame_listener(frames_hub))

    # Closure: trasforma evento INDI in chiamata async dello state manager
    def _indi_event_sync(ev: IndiEvent) -> None:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(state.handle_indi_event(ev), loop=loop)

    def _indi_state_sync(s: str) -> None:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(state.set_indi_connection(s), loop=loop)

    def _phd2_event_sync(ev: dict) -> None:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(state.handle_phd2_event(ev), loop=loop)
        # Aggiorna anche il "live" derivato
        asyncio.ensure_future(state.update_phd2_live(phd2.live), loop=loop)

    def _phd2_state_sync(s: str) -> None:
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(state.set_phd2_connection(s), loop=loop)

    indi = IndiClient(
        host=settings.indi_host,
        port=settings.indi_port,
        on_event=_indi_event_sync,
        on_connection_state=_indi_state_sync,
        reconnect_min=settings.indi_reconnect_min,
        reconnect_max=settings.indi_reconnect_max,
    )
    phd2 = Phd2Client(
        host=settings.phd2_host,
        port=settings.phd2_port,
        on_event=_phd2_event_sync,
        on_connection_state=_phd2_state_sync,
        reconnect_min=settings.phd2_reconnect_min,
        reconnect_max=settings.phd2_reconnect_max,
    )

    bridge = Bridge(
        state=state, indi=indi, phd2=phd2,
        state_hub=state_hub, frames_hub=frames_hub,
        images_dir=str(settings.images_dir),
    )

    async def _on_frame(path: Path, result) -> None:
        meta = {
            "path": str(path),
            "name": path.name,
            "width": result.width,
            "height": result.height,
            "median": result.median,
            "vmin": result.vmin,
            "vmax": result.vmax,
            "hfr": result.hfr_approx,
            "stars": result.star_count,
            "is_color": result.is_color,
            "bayer": result.bayer_pattern,
            "exposure": result.exposure,
            "filter": result.filter_name,
            "frame_type": result.frame_type,
            "object": result.object_name,
        }
        await state.handle_frame(str(path), result.jpeg, result.thumbnail, meta)

    # Notifications from external programs (astro_monitor & co.), offline.
    notify_listener = UdpNotifyListener(
        host=settings.notify_udp_host,
        port=settings.notify_udp_port,
        on_notification=state.handle_notification,
    )

    watcher = FitsWatcher(
        images_dir=settings.images_dir,
        on_result=_on_frame,
        max_dim=settings.image_max_dim,
        thumbnail_dim=settings.image_thumbnail_dim,
        jpeg_quality=settings.image_jpeg_quality,
        stabilize_ms=settings.image_stabilize_ms,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("astroarch-bridge %s starting", __version__)
        await indi.start()
        if settings.phd2_enabled:
            await phd2.start()
        await watcher.start()
        if settings.notify_udp_enabled:
            await notify_listener.start()
        try:
            yield
        finally:
            log.info("astroarch-bridge shutting down")
            await notify_listener.stop()
            await watcher.stop()
            if settings.phd2_enabled:
                await phd2.stop()
            await indi.stop()

    app = FastAPI(
        title="Astroarch Bridge",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.bridge = bridge

    if settings.cors_origins:
        # allow_credentials may be enabled ONLY with an explicit list of
        # origins: the CORS spec forbids combining it with "*", and the
        # browser discards the response when Access-Control-Allow-Origin is
        # "*" and the request is credentialed. The bridge authenticates with
        # a bearer token in a header, not with cookies, so it does not need
        # credentials at all.
        allow_all_origins = "*" in settings.cors_origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=not allow_all_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(CORS_EXPOSE_HEADERS),
        )

    # Healthcheck (no auth)
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__,
                "indi": indi.state, "phd2": phd2.state}

    # REST routers
    for r in (system.router, indi_panel.router, mount.router, camera.router,
              focuser.router, focuser_ekos.router,
              filter_wheel.router, guide.router, notify.router,
              observatory.router, files.router, align.router,
              scheduler.router, setup.router, capture_ekos.router,
              observation.router, skymap.router):
        app.include_router(r)

    # WebSocket endpoints
    # (registered before the static mount: the catch-all on '/' goes last)
    @app.websocket("/ws/state")
    async def _ws_state(ws: WebSocket):
        await state_ws_endpoint(ws, state_hub, state)

    @app.websocket("/ws/frames")
    async def _ws_frames(ws: WebSocket):
        await frame_ws_endpoint(ws, frames_hub)

    # Optional web UI, mounted last so that /api, /ws and /healthz keep
    # winning over the catch-all.
    mount_web_ui(app, settings.web_dir)

    return app
