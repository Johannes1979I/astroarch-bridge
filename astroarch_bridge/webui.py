"""Serve an already-built web interface from the same origin as the API.

Why from the bridge and not from external hosting: a page served over
HTTPS cannot call `http://astroarch.local:8765`, because the browser
blocks the mixed content with no remedy available in code. Serving the UI
from the bridge itself makes origin and API coincide: no CORS, no mixed
content, and in the field — the Raspberry's hotspot, no internet at all —
the user opens a URL and that is it, with nothing to install.

The mount is optional: if the folder is not there, the bridge stays
exactly what it was.
"""
from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

log = logging.getLogger(__name__)

# Namespaces that belong to the bridge rather than to the SPA.
RESERVED_PREFIXES = ("api/", "ws/", "healthz")


class SpaStaticFiles(StaticFiles):
    """StaticFiles with an index.html fallback for client-side routes.

    A single-page app handles its own routes in the browser: if the user
    reloads on `/capture`, that file does not exist on disk, but the
    request must still receive index.html, which then routes client-side.

    The fallback is not indiscriminate: see `_looks_like_route` for the two
    cases where a 404 must stay a 404.
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
        if served_index and response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _is_index(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in ("", "index.html")


def _looks_like_route(path: str) -> bool:
    """True for a client-side route path, False for a file.

    Two exclusions, both there to avoid hiding errors behind a 200:

    - A missing `main.dart.js` must stay an honest 404: answering it with
      HTML would mask a broken build behind an incomprehensible JavaScript
      syntax error.
    - A path under the bridge's own prefixes that reaches this far means no
      route picked it up. That is a real 404, not an SPA route: otherwise a
      typo in an API URL would return index.html with status 200, and the
      client would fail much later trying to read it as JSON.
    """
    normalized = path.lstrip("/")
    if normalized.startswith(RESERVED_PREFIXES):
        return False
    return "." not in PurePosixPath(path).name


def mount_web_ui(app: FastAPI, web_dir: Optional[Path]) -> bool:
    """Mount the UI on '/' if the folder holds a valid build.

    Must be called AFTER `include_router`: Starlette evaluates routes in
    order, so the APIs and the WebSockets win over the static catch-all.

    Returns True if something was mounted.
    """
    if web_dir is None:
        return False
    try:
        index = Path(web_dir) / "index.html"
        if not index.is_file():
            log.info("web UI not mounted: no index.html in %s", web_dir)
            return False
    except OSError as e:
        log.warning("web UI not mounted: cannot read %s: %s", web_dir, e)
        return False

    app.mount("/", SpaStaticFiles(directory=str(web_dir), html=True), name="webui")
    log.info("web UI mounted at / from %s", web_dir)
    return True
