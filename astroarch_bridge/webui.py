"""Serve an already-built web interface from the same origin as the API.

Why from the bridge and not from external hosting: a page served over
HTTPS cannot call `http://astroarch.local:8765`, because the browser
blocks the mixed content with no remedy available in code. Serving the UI
from the bridge itself makes origin and API coincide: no CORS, no mixed
content, and in the field — the Raspberry's hotspot, no internet at all —
the user opens a URL and that is it, with nothing to install.

The mount is optional: if the folder is not there, the bridge stays
exactly what it was.

It is mounted under `/ui`, not under `/`. A catch-all mount on the root
matches *every* path before Starlette can do anything else, and that costs
three things that were all measured on a running daemon rather than
guessed:

  - `/api/system/info/` and `/healthz/` (trailing slash) stop redirecting
    and start 404ing, because `redirect_slashes` only runs when nothing
    matched at all;
  - a wrong verb on a real endpoint (`GET /api/mount/goto`) turns from an
    honest 405 into a 404, so a live route looks like it does not exist;
  - a WebSocket to any unrouted path reaches `StaticFiles`, which asserts
    the scope is HTTP, and the client gets a 500 with a traceback in the
    journal instead of a clean rejection.

Under `/ui` none of that applies: same origin, no CORS, no mixed content,
and the rest of the app behaves exactly as it did before.

Note on access: every router carries `Depends(require_token)`, this mount
carries nothing — a browser cannot send a bearer token when it fetches its
own first page. Whatever `ASTROARCH_WEB_DIR` points at is therefore
readable by anyone who can reach port 8765. Point it at a UI build and at
nothing else.
"""
from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

log = logging.getLogger(__name__)

MOUNT_PATH = "/ui"


class SpaStaticFiles(StaticFiles):
    """StaticFiles with an index.html fallback for client-side routes.

    A single-page app handles its own routes in the browser: if the user
    reloads on `/ui/capture`, that file does not exist on disk, but the
    request must still receive index.html, which then routes client-side.

    The fallback is not indiscriminate: see `_looks_like_route`.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        served_index = _is_index(path)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            # StaticFiles signals a missing file by raising rather than by
            # returning a 404 response: without catching it here, the
            # fallback would never fire.
            if exc.status_code != 404 or not _looks_like_route(path):
                raise
            response = await super().get_response("index.html", scope)
            served_index = True
        else:
            if response.status_code == 404 and _looks_like_route(path):
                response = await super().get_response("index.html", scope)
                served_index = True
        # index.html must not be cached: after a package update, a user
        # holding the old copy would load assets that no longer exist, and
        # in the field they have no way of telling why.
        if served_index and response.status_code in (200, 304):
            response.headers["Cache-Control"] = "no-cache"
        return response


class HttpOnly:
    """Let only HTTP scopes reach the wrapped app.

    `StaticFiles.__call__` asserts `scope["type"] == "http"`. A WebSocket
    opened on a path under the mount would trip that assert and surface as
    a 500 with a traceback; a websocket gets a clean close instead.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1000})
            return


def _is_index(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in ("", "index.html")


def _looks_like_route(path: str) -> bool:
    """True for a client-side route path, False for a file.

    A missing `main.dart.js` must stay an honest 404: answering it with
    HTML would mask a broken build behind an incomprehensible JavaScript
    syntax error. And a path carrying a `..` segment is not a route the SPA
    would ever produce, so it keeps its 404 rather than quietly returning
    index.html with status 200.
    """
    parts = PurePosixPath(path).parts
    if ".." in parts:
        return False
    return "." not in PurePosixPath(path).name


def mount_web_ui(app: FastAPI, web_dir: Path) -> bool:
    """Mount the UI under `/ui` if the folder holds a valid build.

    Must be called AFTER `include_router`, so the API routes are matched
    first even for paths that happen to start with the mount prefix.

    Returns True if something was mounted.
    """
    path = Path(web_dir)
    if not str(path).strip() or not path.is_absolute():
        # ASTROARCH_WEB_DIR= (empty) resolves to Path('.'), and the systemd
        # user unit sets no WorkingDirectory: that would publish the home
        # directory of whoever runs the bridge, unauthenticated.
        log.warning("web UI not mounted: ASTROARCH_WEB_DIR must be an "
                    "absolute path, got %r", str(web_dir))
        return False
    try:
        if not (path / "index.html").is_file():
            log.info("web UI not mounted: no index.html in %s", path)
            return False
    except OSError as e:
        log.warning("web UI not mounted: cannot read %s: %s", path, e)
        return False

    app.mount(MOUNT_PATH,
              HttpOnly(SpaStaticFiles(directory=str(path), html=True)),
              name="webui")

    @app.get("/", include_in_schema=False)
    async def _web_ui_root() -> RedirectResponse:
        """Whoever opens the bare address is looking for the interface."""
        return RedirectResponse(url=MOUNT_PATH + "/")

    log.info("web UI mounted at %s/ from %s", MOUNT_PATH, path)
    return True
