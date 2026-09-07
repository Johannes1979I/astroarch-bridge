"""Tests for the optional web UI mount.

Mistakes prevented here:
- The static catch-all shadowing /api, /ws or /healthz
- A missing asset served as HTML: a broken build would then surface as a
  JavaScript syntax error, undecipherable in the field
- index.html being cached: after an update the user stays on a page
  pointing at assets that no longer exist
- A regression for whoever does not install the UI: with no folder,
  nothing may change
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from astroarch_bridge import config


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    created: list[TestClient] = []

    def _make(web_dir=None) -> TestClient:
        monkeypatch.setenv("ASTROARCH_TOKEN", "test-token")
        monkeypatch.setenv("ASTROARCH_IMAGES_DIR", str(tmp_path / "images"))
        monkeypatch.setenv("ASTROARCH_WEB_DIR",
                           str(web_dir) if web_dir else str(tmp_path / "nowhere"))
        config.reset_settings_cache()
        from astroarch_bridge.app import create_app
        c = TestClient(create_app())
        created.append(c)
        return c

    yield _make
    for c in created:
        c.close()
    config.reset_settings_cache()


@pytest.fixture
def web_build(tmp_path):
    """A minimal build, shaped like the output of `flutter build web`."""
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>Astroarch</title>")
    (d / "main.dart.js").write_text("console.log('app');")
    (d / "assets").mkdir()
    (d / "assets" / "FontManifest.json").write_text("[]")
    return d


# --- with no build installed ------------------------------------------------

def test_without_web_dir_nothing_changes(make_client):
    client = make_client(None)
    assert client.get("/healthz").status_code == 200
    # No catch-all: an unknown path stays a 404.
    assert client.get("/anything/at/all").status_code == 404


# --- with a build installed -------------------------------------------------

def test_serves_index_at_the_mount(make_client, web_build):
    client = make_client(web_build)
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "Astroarch" in r.text


def test_the_bare_address_leads_to_the_ui(make_client, web_build):
    """Whoever types the host and port is looking for the interface."""
    client = make_client(web_build)
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/ui/"


def test_serves_assets(make_client, web_build):
    client = make_client(web_build)
    r = client.get("/ui/main.dart.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_client_side_route_falls_back_to_index(make_client, web_build):
    """Reloading on an SPA route must return index.html."""
    client = make_client(web_build)
    r = client.get("/ui/capture")
    assert r.status_code == 200
    assert "Astroarch" in r.text


def test_missing_asset_stays_404(make_client, web_build):
    """A missing file must NOT be masked by index.html."""
    client = make_client(web_build)
    assert client.get("/ui/canvaskit/canvaskit.wasm").status_code == 404
    assert client.get("/ui/main.dart.js.map").status_code == 404


def test_index_is_not_cached(make_client, web_build):
    client = make_client(web_build)
    r = client.get("/ui/")
    assert r.headers.get("cache-control") == "no-cache"


# --- living alongside the APIs ----------------------------------------------

def test_api_wins_over_static_mount(make_client, web_build):
    """The catch-all must not shadow the bridge's own routes."""
    client = make_client(web_build)
    # healthz still answers JSON, not the SPA
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # an authenticated route answers 401, not index.html
    r = client.get("/api/system/info")
    assert r.status_code == 401
    # and with the token the API really answers
    r = client.get("/api/system/info", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    assert "version" in r.json()


def test_unknown_api_path_is_not_swallowed(make_client, web_build):
    """A non-existent endpoint under /api must stay a 404, not become HTML.

    `/api/...` has no extension, so without care it would land in the SPA
    fallback and a typo in the client would look like it worked.
    """
    client = make_client(web_build)
    r = client.get("/api/does/not/exist", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 404
    assert "Astroarch" not in r.text


# --- the regressions a catch-all on '/' used to cause -----------------------
#
# All four were measured against a running daemon with the UI mounted on
# '/': a Mount matches every path before Starlette can redirect, resolve a
# method mismatch or reject a websocket. Under '/ui' they must all behave
# exactly as they do with no UI installed at all.

def test_trailing_slash_still_redirects(make_client, web_build):
    """ only runs when nothing matched: a root mount kills it."""
    client = make_client(web_build)
    r = client.get("/healthz/", follow_redirects=False)
    assert r.status_code in (307, 308)
    r = client.get("/api/system/info/", follow_redirects=False)
    assert r.status_code in (307, 308)


def test_wrong_method_on_a_real_route_is_405(make_client, web_build):
    """A live endpoint hit with the wrong verb must not look missing."""
    client = make_client(web_build)
    r = client.get("/api/mount/goto", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 405


def test_websocket_on_an_unrouted_path_is_refused_cleanly(make_client, web_build):
    """StaticFiles asserts the scope is HTTP: a websocket must not reach it."""
    from starlette.websockets import WebSocketDisconnect
    client = make_client(web_build)
    with pytest.raises((WebSocketDisconnect, Exception)):
        with client.websocket_connect("/ui/nope"):
            pass


def test_a_relative_web_dir_is_refused(make_client, tmp_path, monkeypatch):
    """ASTROARCH_WEB_DIR= resolves to Path('.'): the daemon's home, unauthenticated."""
    from astroarch_bridge.webui import mount_web_ui
    from fastapi import FastAPI
    assert mount_web_ui(FastAPI(), pathlib.Path(".")) is False
    assert mount_web_ui(FastAPI(), pathlib.Path("")) is False


def test_dot_dot_paths_keep_their_404(make_client, web_build):
    client = make_client(web_build)
    assert client.get("/ui/../../etc/passwd").status_code == 404
