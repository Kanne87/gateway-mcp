"""fields-Projektion (Semantik wie gw-CLI --fields)."""

import asyncio

import httpx

import app
from app import apply_fields, parse_fields, project

JOB = {
    "status": "done",
    "summary": {"events_count": 42, "cost": {"usd": 1.5}},
    "events": [{"event": {"result": "ok"}}, {"event": {"result": "last"}}],
}

_RealAsyncClient = httpx.AsyncClient


def test_parse_fields_list_and_string():
    assert parse_fields(None) is None
    assert parse_fields("status, summary.events_count,,") == ["status", "summary.events_count"]
    assert parse_fields(["status", " a.b ", ""]) == ["status", "a.b"]


def test_project_nested_dict():
    assert project(JOB, ["status", "summary.events_count", "summary.cost.usd"]) == {
        "status": "done",
        "summary.events_count": 42,
        "summary.cost.usd": 1.5,
    }


def test_project_list_index():
    assert project(JOB, ["events.0.event.result", "events.-1.event.result"]) == {
        "events.0.event.result": "ok",
        "events.-1.event.result": "last",
    }
    assert project([{"a": 1}], ["0.a"]) == {"0.a": 1}


def test_project_missing_paths_are_none():
    out = project(JOB, ["nope", "summary.nope", "events.9.event", "events.x", "status.deeper"])
    assert out == {
        "nope": None,
        "summary.nope": None,
        "events.9.event": None,
        "events.x": None,
        "status.deeper": None,
    }


def test_project_keeps_input_order():
    assert list(project(JOB, ["summary", "status"])) == ["summary", "status"]


def test_apply_fields_keeps_envelope():
    resp = {"service": "claude_bridge", "action": "get_job", "status_code": 200, "data": JOB,
            "encoding": None, "filename": None, "content_type": None}
    out = apply_fields(resp, ["status"])
    assert out["data"] == {"status": "done"}
    assert {k: v for k, v in out.items() if k != "data"} == {k: v for k, v in resp.items() if k != "data"}


def test_apply_fields_skips_non_2xx_and_gated():
    err = {"status_code": 404, "data": {"detail": "not found"}}
    assert apply_fields(err, ["status"]) is err
    gated = {"status_code": 200, "gated": True, "data": ""}
    assert apply_fields(gated, ["status"]) is gated
    ok = {"status_code": 200, "data": JOB}
    assert apply_fields(ok, None) is ok


def _run(monkeypatch, handler, **kwargs):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*a, **kw)

    monkeypatch.setattr(app.httpx, "AsyncClient", factory)
    return asyncio.run(app.gateway_execute("claude_bridge", "get_job", params={"job_id": "1"}, **kwargs))


def test_gateway_execute_with_fields(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"service": "claude_bridge", "action": "get_job",
                                         "status_code": 200, "data": JOB})

    out = _run(monkeypatch, handler, fields=["status", "summary.events_count"])
    assert out == {"service": "claude_bridge", "action": "get_job", "status_code": 200,
                   "data": {"status": "done", "summary.events_count": 42}}

    out = _run(monkeypatch, handler, fields="status,events.0.event.result")
    assert out["data"] == {"status": "done", "events.0.event.result": "ok"}


def test_gateway_execute_fields_not_applied_to_proxy_error(monkeypatch):
    def handler(request):
        return httpx.Response(502, text="Bad Gateway")

    out = _run(monkeypatch, handler, fields=["status"])
    assert out["status_code"] == 502
    assert out["data"] == "Bad Gateway"
