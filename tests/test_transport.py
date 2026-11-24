"""Interner Weg mit Rueckfall, Client-Wiederverwendung, SSE + Streamable HTTP."""

import asyncio
import json

import httpx
import pytest

import app

INTERNAL = "http://api-gateway-intern:8000"
PUBLIC = "https://api-gateway.example"

_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def routed(monkeypatch):
    """Clients je base_url mit eigenem Handler; zaehlt Anfragen und erzeugte Clients."""
    monkeypatch.setattr(app, "GATEWAY_URL", PUBLIC)
    monkeypatch.setattr(app, "GATEWAY_URL_INTERNAL", INTERNAL)
    state = {"handlers": {}, "hits": [], "created": []}

    def factory(*a, **kw):
        base = str(kw["base_url"]).rstrip("/")
        state["created"].append(base)

        def handler(request):
            state["hits"].append(base)
            return state["handlers"][base](request)

        kw["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*a, **kw)

    monkeypatch.setattr(app.httpx, "AsyncClient", factory)
    return state


def _ok(request):
    return httpx.Response(200, json={"status_code": 200, "data": {"url": str(request.url)}})


def _exec():
    return asyncio.run(app.gateway_execute("ssh", "hetzner", params={"command": "true"}))


def test_internal_used_first(routed):
    routed["handlers"] = {INTERNAL: _ok, PUBLIC: _ok}
    out = _exec()
    assert routed["hits"] == [INTERNAL]
    assert out["data"]["url"] == INTERNAL + "/execute"


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout])
def test_fallback_to_public_on_connect_error(routed, exc, caplog):
    def down(request):
        raise exc("[Errno -2] Name or service not known", request=request)

    routed["handlers"] = {INTERNAL: down, PUBLIC: _ok}
    with caplog.at_level("WARNING", logger="gateway-mcp"):
        out = _exec()
    assert routed["hits"] == [INTERNAL, PUBLIC]
    assert out["data"]["url"] == PUBLIC + "/execute"
    assert any("Rueckfall" in r.message for r in caplog.records)


def test_internal_connect_timeout_short(routed):
    seen = {}

    def capture(name):
        def h(request):
            seen[name] = request.extensions["timeout"]
            return _ok(request)
        return h

    routed["handlers"] = {INTERNAL: capture("int")}
    _exec()
    assert seen["int"]["connect"] == app.INTERNAL_CONNECT_TIMEOUT_SECONDS == 5
    assert seen["int"]["read"] == app.GATEWAY_TIMEOUT_SECONDS


def test_no_fallback_on_read_timeout(routed):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    routed["handlers"] = {INTERNAL: slow, PUBLIC: _ok}
    out = _exec()
    assert routed["hits"] == [INTERNAL]
    assert out["status_code"] == 504
    assert "MCP-Proxy-Timeout" in out["data"]


def test_without_internal_only_public(routed, monkeypatch):
    monkeypatch.setattr(app, "GATEWAY_URL_INTERNAL", "")
    routed["handlers"] = {PUBLIC: _ok}
    _exec()
    assert routed["hits"] == [PUBLIC]


def test_services_also_falls_back(routed):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    routed["handlers"] = {INTERNAL: down, PUBLIC: lambda r: httpx.Response(200, json={"ok": True})}
    assert asyncio.run(app.gateway_services()) == {"ok": True}
    assert routed["hits"] == [INTERNAL, PUBLIC]


def test_client_is_reused(routed, monkeypatch):
    monkeypatch.setattr(app, "GATEWAY_URL_INTERNAL", "")
    routed["handlers"] = {PUBLIC: _ok}

    async def two_calls():
        await app.gateway_execute("a", "b")
        first = app._clients[PUBLIC]
        await app.gateway_execute("a", "b")
        await app.gateway_services()
        return first, app._clients[PUBLIC]

    first, second = asyncio.run(two_calls())
    assert first is second
    assert routed["created"] == [PUBLIC]
    assert routed["hits"] == [PUBLIC, PUBLIC, PUBLIC]
    assert first.headers["X-Gateway-Key"] == app.GATEWAY_KEY


def test_close_clients():
    async def run():
        c = app._get_client(PUBLIC)
        await app.close_clients()
        return c

    c = asyncio.run(run())
    assert c.is_closed
    assert app._clients == {}


# ---------------------------------------------------------------------------
# Transporte: SSE und Streamable HTTP in einer App
# ---------------------------------------------------------------------------


def test_routes_present():
    paths = {getattr(r, "path", None) for r in app.build_app().routes}
    assert {"/sse", "/messages", "/mcp"} <= paths


def test_sse_and_streamable_http():
    from starlette.testclient import TestClient

    with TestClient(app.build_app()) as client:
        # Streamable HTTP: initialize
        r = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}}},
        )
        assert r.status_code == 200, r.text
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
        msg = json.loads(body)
        assert msg["id"] == 1
        assert msg["result"]["serverInfo"]["name"] == "K-AI Gateway"

        # tools/list ueber Streamable HTTP (stateless) zeigt fields
        r = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                     "mcp-protocol-version": "2025-03-26"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert r.status_code == 200, r.text
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
        tools = {t["name"]: t for t in json.loads(body)["result"]["tools"]}
        assert "fields" in tools["gateway_execute"]["inputSchema"]["properties"]
