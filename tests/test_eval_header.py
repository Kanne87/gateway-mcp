"""S4242 Eval-Trockenmodus: X-Eval-Run der MCP-Anfrage geht bis /execute durch.

Der Gateway prueft den Header (Lese-Allowlist, sonst HTTP 423 eval_blocked);
gateway-mcp muss ihn nur weiterreichen und den 423-Rumpf als data liefern.
"""

import json

import httpx
import pytest
from starlette.testclient import TestClient

import app

_RealAsyncClient = httpx.AsyncClient
_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
      "mcp-protocol-version": "2025-03-26"}
BLOCK = {"eval_blocked": True, "service": "whatsapp", "action": "send_message",
         "eval_run": "eval-gold-7", "hinweis": "Eval-Trockenmodus: ..."}


@pytest.fixture(autouse=True)
def _frischer_session_manager(monkeypatch):
    """Der StreamableHTTPSessionManager laeuft nur einmal je Instanz; jeder Test
    bekommt einen eigenen (build_app legt ihn neu an), das Original bleibt unberuehrt."""
    monkeypatch.setattr(app.mcp, "_session_manager", None)


@pytest.fixture
def upstream(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.headers.get("x-eval-run") and json.loads(request.content)["action"] == "send_message":
            return httpx.Response(423, json=BLOCK)
        return httpx.Response(200, json={"service": "s", "action": "a", "status_code": 200, "data": {"ok": 1}})

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*a, **kw)

    monkeypatch.setattr(app.httpx, "AsyncClient", factory)
    return seen


def _call(client, args, extra_headers=None):
    r = client.post("/mcp", headers={**_H, **(extra_headers or {})},
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "gateway_execute", "arguments": args}})
    assert r.status_code == 200, r.text
    body = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    result = json.loads(body)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_header_wird_durchgereicht(upstream):
    with TestClient(app.build_app()) as client:
        out = _call(client, {"service": "operator", "action": "recall", "body": {"q": "x"}},
                    {"X-Eval-Run": "eval-gold-7"})
    assert out["status_code"] == 200
    assert upstream[-1].headers["x-eval-run"] == "eval-gold-7"
    assert upstream[-1].headers["x-gateway-key"]  # Gateway-Key bleibt


def test_ohne_header_kein_eval_header(upstream):
    with TestClient(app.build_app()) as client:
        _call(client, {"service": "operator", "action": "recall"})
    assert "x-eval-run" not in upstream[-1].headers


def test_leerer_header_wird_nicht_gesendet(upstream):
    with TestClient(app.build_app()) as client:
        _call(client, {"service": "operator", "action": "recall"}, {"X-Eval-Run": "  "})
    assert "x-eval-run" not in upstream[-1].headers


def test_423_rumpf_als_data(upstream):
    with TestClient(app.build_app()) as client:
        out = _call(client, {"service": "whatsapp", "action": "send_message", "body": {"text": "hi"}},
                    {"X-Eval-Run": "eval-gold-7"})
    assert out["status_code"] == 423
    assert out["data"] == BLOCK


def test_ctx_nicht_im_tool_schema():
    with TestClient(app.build_app()) as client:
        r = client.post("/mcp", headers=_H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        body = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    props = {t["name"]: t for t in json.loads(body)["result"]["tools"]}["gateway_execute"]["inputSchema"]["properties"]
    assert "ctx" not in props


def test_eval_run_ohne_request_context():
    assert app._eval_run(None) is None
