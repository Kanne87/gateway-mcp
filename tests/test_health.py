"""/health: Liveness ohne Auth, ohne Upstream-Call."""

import httpx
import pytest
from starlette.testclient import TestClient

import app


def test_health_ok_without_upstream(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("Upstream-Aufruf im Healthcheck")

    monkeypatch.setattr(app, "_request", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    with TestClient(app.build_app()) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
