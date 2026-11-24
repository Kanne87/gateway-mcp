"""Timeout- und HTTP-Fehlerverhalten von gateway_execute (Umschlag-Dict statt Exception)."""

import asyncio
import os

os.environ.setdefault("GATEWAY_API_KEY", "test-key")

import httpx  # noqa: E402

import app  # noqa: E402

ENVELOPE_KEYS = {"service", "action", "status_code", "data", "encoding", "filename", "content_type"}

_RealAsyncClient = httpx.AsyncClient


def _run_with_transport(monkeypatch, handler):
    """Run gateway_execute with httpx.AsyncClient backed by a MockTransport."""
    seen = {}

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(app.httpx, "AsyncClient", factory)
    result = asyncio.run(app.gateway_execute("ssh", "hetzner", params={"command": "sleep 80"}))
    return result, seen


def test_default_timeout_is_90():
    assert app.GATEWAY_TIMEOUT_SECONDS == 90


def test_read_timeout_returns_504_envelope(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    result, seen = _run_with_transport(monkeypatch, handler)
    assert seen["timeout"] == 90
    assert set(result) == ENVELOPE_KEYS
    assert result["status_code"] == 504
    assert result["service"] == "ssh"
    assert result["action"] == "hetzner"
    assert result["data"] == "MCP-Proxy-Timeout nach 90 s; der Aufruf kann im Gateway weiterlaufen"
    assert result["encoding"] is None
    assert result["filename"] is None
    assert result["content_type"] is None


def test_timeout_env_override(monkeypatch):
    monkeypatch.setattr(app, "GATEWAY_TIMEOUT_SECONDS", 45.0)

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    result, seen = _run_with_transport(monkeypatch, handler)
    assert seen["timeout"] == 45.0
    assert "nach 45 s" in result["data"]


def test_http_502_returns_envelope(monkeypatch):
    def handler(request):
        return httpx.Response(502, text="Bad Gateway " + "x" * 1000)

    result, _ = _run_with_transport(monkeypatch, handler)
    assert set(result) == ENVELOPE_KEYS
    assert result["status_code"] == 502
    assert result["data"].startswith("Bad Gateway ")
    assert len(result["data"]) == 500


def test_http_200_passes_through(monkeypatch):
    body = {"service": "ssh", "action": "hetzner", "status_code": 504, "data": "SSH command timed out",
            "encoding": None, "filename": None, "content_type": None}

    def handler(request):
        return httpx.Response(200, json=body)

    result, _ = _run_with_transport(monkeypatch, handler)
    assert result == body
