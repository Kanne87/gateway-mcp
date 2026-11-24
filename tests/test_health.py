"""/health: Liveness ohne Auth, ohne Upstream-Call."""

import httpx
from starlette.testclient import TestClient

import app


def test_health_ok_without_upstream(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("Upstream-Aufruf im Healthcheck")

    monkeypatch.setattr(app, "_request", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    # ohne Kontextmanager: kein Lifespan, der Session-Manager (nur einmal startbar) bleibt unberuehrt
    r = TestClient(app.build_app()).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
