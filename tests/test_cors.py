"""CORS: spec compliance, and X-* headers being visible to web clients.

Mistakes prevented here:
- allow_credentials=True together with allow_origins=["*"]: a combination
  the CORS spec forbids, and the browser discards the response on
  credentialed requests.
- Image metadata in X-* headers invisible to cross-origin JavaScript
  because they are not listed in Access-Control-Expose-Headers.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from astroarch_bridge import config
from astroarch_bridge.app import CORS_EXPOSE_HEADERS, create_app


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    """Builds the app without running the lifespan (no real INDI/PHD2).

    TestClient runs the lifespan only when used as a context manager: here
    it is instantiated directly, so the network clients never start.
    """
    created: list[TestClient] = []

    def _make(origins_env: str | None) -> TestClient:
        monkeypatch.setenv("ASTROARCH_TOKEN", "test-token")
        monkeypatch.setenv("ASTROARCH_IMAGES_DIR", str(tmp_path / "images"))
        if origins_env is None:
            monkeypatch.delenv("ASTROARCH_CORS_ORIGINS", raising=False)
        else:
            monkeypatch.setenv("ASTROARCH_CORS_ORIGINS", origins_env)
        config.reset_settings_cache()
        c = TestClient(create_app())
        created.append(c)
        return c

    yield _make

    for c in created:
        c.close()
    config.reset_settings_cache()


def test_wildcard_origin_does_not_allow_credentials(client_factory):
    """With origins=["*"] the middleware must NOT declare credentials."""
    client = client_factory(None)  # default: ["*"]
    r = client.get("/healthz", headers={"Origin": "http://astroarch.local:8765"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_explicit_origin_keeps_credentials(client_factory):
    """With an explicit list credentials stay allowed: a valid combination."""
    client = client_factory('["http://astroarch.local:8765"]')
    origin = "http://astroarch.local:8765"
    r = client.get("/healthz", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert r.headers["access-control-allow-credentials"] == "true"


def test_frame_metadata_headers_are_exposed(client_factory):
    """The X-* metadata headers must be readable from JavaScript."""
    client = client_factory(None)
    r = client.get("/healthz", headers={"Origin": "http://astroarch.local:8765"})
    exposed = {
        h.strip().lower()
        for h in r.headers.get("access-control-expose-headers", "").split(",")
        if h.strip()
    }
    # A meaningful sample: frame metadata plus sky map centre.
    for name in ("x-hfr", "x-stars", "x-center-ra-deg", "x-fov-deg"):
        assert name in exposed, f"{name} not exposed to web clients"
    assert exposed == {h.lower() for h in CORS_EXPOSE_HEADERS}


def test_preflight_allows_authorization_header(client_factory):
    """The preflight must allow Authorization, or no authenticated call can
    leave the browser at all."""
    client = client_factory(None)
    r = client.options(
        "/api/system/info",
        headers={
            "Origin": "http://astroarch.local:8765",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    allowed = r.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed
