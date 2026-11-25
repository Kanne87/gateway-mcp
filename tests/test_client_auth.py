"""Client-Auth fuer eingehende MCP-Clients (MCP_CLIENT_TOKEN, S4215)."""

import asyncio
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

import app

TOKEN = "dummy-client-token-123"
WRONG = "dummy-wrong-token-456"

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("MCP_CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("MCP_CLIENT_TOKEN_SSE", raising=False)
    # frischer Session-Manager je App (er ist nur einmal startbar)
    monkeypatch.setattr(app.mcp, "_session_manager", None)

    async def boom(*a, **kw):
        raise AssertionError("Upstream-Aufruf")

    monkeypatch.setattr(app, "_request", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)


def _init(client, headers=None):
    return client.post("/mcp", headers={**_MCP_HEADERS, **(headers or {})}, json=_INIT)


def _assert_401(r):
    assert r.status_code == 401, r.text
    assert r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == "Bearer"


def _assert_init_ok(r):
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    assert json.loads(body)["result"]["serverInfo"]["name"] == "K-AI Gateway"


# ---------------------------------------------------------------------------
# ohne Env: offen wie bisher
# ---------------------------------------------------------------------------


def test_without_env_open_and_warns(caplog):
    with caplog.at_level("WARNING", logger="gateway-mcp"):
        a = app.build_app()
    assert any("MCP_CLIENT_TOKEN nicht gesetzt" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)
    with TestClient(a) as client:
        _assert_init_ok(_init(client))
        # SSE-Messages ohne session_id: 400 vom Transport, nicht 401
        assert client.post("/messages/", json={}).status_code == 400
        assert client.get("/health").status_code == 200


def test_empty_env_is_unset(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKEN", "   ")
    assert app.client_auth_middleware() == []


# ---------------------------------------------------------------------------
# mit Env: /mcp geschuetzt
# ---------------------------------------------------------------------------


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKEN", TOKEN)
    with TestClient(app.build_app()) as client:
        yield client


def test_mcp_without_header_401(secured):
    _assert_401(_init(secured))


@pytest.mark.parametrize("method", ["GET", "DELETE", "POST"])
def test_mcp_all_methods_and_subpaths_401(secured, method):
    for path in ("/mcp", "/mcp/", "/mcp/x/y"):
        _assert_401(secured.request(method, path))


def test_mcp_with_bearer_ok(secured):
    _assert_init_ok(_init(secured, {"Authorization": f"Bearer {TOKEN}"}))
    _assert_init_ok(_init(secured, {"Authorization": f"bearer {TOKEN}"}))


def test_mcp_with_x_api_key_ok(secured):
    _assert_init_ok(_init(secured, {"X-API-Key": TOKEN}))


@pytest.mark.parametrize("headers", [
    {"Authorization": f"Bearer {WRONG}"},
    {"X-API-Key": WRONG},
    {"Authorization": TOKEN},                 # ohne Bearer-Schema
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": "Bearer "},
    {"X-API-Key": ""},
    {"Authorization": f"Bearer {TOKEN}x"},
])
def test_mcp_wrong_token_401(secured, headers):
    _assert_401(_init(secured, headers))


def test_health_always_open(secured):
    r = secured.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_sse_open_without_flag(secured):
    # Transport antwortet (400 ohne session_id) -> Middleware hat durchgelassen
    assert secured.post("/messages/", json={}).status_code == 400


def test_sse_get_open_without_flag(monkeypatch):
    """GET /sse streamt endlos; daher mit Stub-App hinter der Middleware pruefen."""
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await app.JSONResponse({"ok": True})(scope, receive, send)

    async def run(mw, path, headers=()):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "method": "GET", "path": path, "headers": list(headers),
                 "query_string": b"", "client": ("203.0.113.9", 1234)}
        await mw(scope, receive, send)
        return sent[0]["status"]

    open_mw = app.ClientAuthMiddleware(inner, token=TOKEN, protect_sse=False)
    assert asyncio.run(run(open_mw, "/sse")) == 200
    closed_mw = app.ClientAuthMiddleware(inner, token=TOKEN, protect_sse=True)
    assert asyncio.run(run(closed_mw, "/sse")) == 401
    assert asyncio.run(run(closed_mw, "/sse", [(b"x-api-key", TOKEN.encode())])) == 200
    assert asyncio.run(run(closed_mw, "/health")) == 200
    assert reached == ["/sse", "/sse", "/health"]


def test_sse_with_flag_401(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_CLIENT_TOKEN_SSE", "true")
    with TestClient(app.build_app()) as client:
        _assert_401(client.get("/sse"))
        _assert_401(client.post("/messages/?session_id=abc", json={}))
        # mit Token kommt der Request beim Transport an (400 ohne session_id)
        assert client.post("/messages/", json={}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400
        assert client.get("/health").status_code == 200
        _assert_401(_init(client))


def test_sse_flag_without_token_stays_open(monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_TOKEN_SSE", "true")
    assert app.client_auth_middleware() == []


# ---------------------------------------------------------------------------
# Logging: Token nie im Log, 401 mit Pfad und IP
# ---------------------------------------------------------------------------


def test_token_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("MCP_CLIENT_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_CLIENT_TOKEN_SSE", "true")
    with caplog.at_level(logging.DEBUG):
        with TestClient(app.build_app()) as client:
            _init(client)
            _init(client, {"Authorization": f"Bearer {WRONG}"})
            _init(client, {"X-API-Key": TOKEN})
            _init(client, {"Authorization": f"Bearer {TOKEN}"})
            client.get("/sse")
            client.post("/messages/", json={}, headers={"X-API-Key": WRONG})
    assert caplog.records
    for rec in caplog.records:
        text = rec.getMessage() + repr(rec.args) + repr(rec.__dict__)
        for secret in (TOKEN, WRONG, TOKEN[:8], WRONG[:8]):
            assert secret not in text, rec.getMessage()
    denied = [r.getMessage() for r in caplog.records if "401" in r.getMessage()]
    assert any("path=/mcp" in m and "client=" in m for m in denied)
    assert any("path=/sse" in m for m in denied)
