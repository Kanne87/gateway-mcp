"""Functional tests for the upload/download gate logic in app.py."""

import os

os.environ.setdefault("GATEWAY_API_KEY", "test-key")

from app import check_upload, gate_download  # noqa: E402

LIMIT_UP = 100
LIMIT_DN = 200

SHORT = "x" * LIMIT_UP
LONG = "x" * (LIMIT_UP + 1)
SHORT_DN = "A" * LIMIT_DN
LONG_DN = "A" * (LIMIT_DN + 1)


# ---------------------------------------------------------------------------
# check_upload
# ---------------------------------------------------------------------------


def test_upload_pass_when_no_large_fields():
    assert check_upload({"id": "123"}, {"title": "ok"}, limit=LIMIT_UP) is None


def test_upload_pass_when_fields_at_limit():
    assert check_upload({"content": SHORT}, None, limit=LIMIT_UP) is None


def test_upload_gate_content_in_params():
    result = check_upload({"content": LONG}, None, limit=LIMIT_UP)
    assert result is not None
    assert result["gated"] is True
    assert result["reason"] == "upload_too_large_for_context"
    assert result["field"] == "content"
    assert result["size_chars"] == len(LONG)
    assert result["limit"] == LIMIT_UP
    assert "gw up" in result["hint"]


def test_upload_gate_content_text_in_body():
    result = check_upload(None, {"content_text": LONG}, limit=LIMIT_UP)
    assert result is not None
    assert result["field"] == "content_text"


def test_upload_gate_content_base64_in_body():
    result = check_upload(None, {"content_base64": LONG}, limit=LIMIT_UP)
    assert result is not None
    assert result["field"] == "content_base64"


def test_upload_gate_body_field_in_body():
    result = check_upload(None, {"body": LONG}, limit=LIMIT_UP)
    assert result is not None
    assert result["field"] == "body"


def test_upload_gate_prefers_params_over_body():
    result = check_upload({"content": LONG}, {"content": LONG}, limit=LIMIT_UP)
    assert result is not None
    # params is checked first
    assert result["field"] == "content"


def test_upload_ignores_non_string_values():
    assert check_upload({"content": 12345}, {"content_text": ["a"] * 5000}, limit=LIMIT_UP) is None


def test_upload_gate_content_in_body_not_params():
    result = check_upload({"other": LONG}, {"content": LONG}, limit=LIMIT_UP)
    # "other" is not a watched field; content in body should be caught
    assert result is not None
    assert result["field"] == "content"


# ---------------------------------------------------------------------------
# gate_download
# ---------------------------------------------------------------------------


def test_download_pass_non_base64():
    resp = {"encoding": "utf-8", "data": "A" * 9999, "status_code": 200}
    assert gate_download(resp, limit=LIMIT_DN) is resp


def test_download_pass_small_base64():
    resp = {"encoding": "base64", "data": SHORT_DN, "status_code": 200}
    assert gate_download(resp, limit=LIMIT_DN) is resp


def test_download_pass_at_limit():
    resp = {"encoding": "base64", "data": SHORT_DN, "status_code": 200}
    assert gate_download(resp, limit=LIMIT_DN) is resp


def test_download_gate_large_base64():
    resp = {
        "encoding": "base64",
        "data": LONG_DN,
        "status_code": 200,
        "filename": "doc.pdf",
        "content_type": "application/pdf",
        "service": "paperless",
        "action": "download",
    }
    result = gate_download(resp, limit=LIMIT_DN)
    assert result["gated"] is True
    assert result["reason"] == "download_too_large_for_context"
    assert result["data"] == ""
    assert result["size_base64_chars"] == len(LONG_DN)
    assert result["approx_bytes"] == len(LONG_DN) * 3 // 4
    assert result["limit"] == LIMIT_DN
    assert "gw down" in result["hint"]


def test_download_gate_preserves_other_fields():
    resp = {
        "encoding": "base64",
        "data": LONG_DN,
        "status_code": 200,
        "filename": "doc.pdf",
        "content_type": "application/pdf",
        "service": "paperless",
        "action": "download",
    }
    result = gate_download(resp, limit=LIMIT_DN)
    assert result["status_code"] == 200
    assert result["filename"] == "doc.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["service"] == "paperless"
    assert result["action"] == "download"


def test_download_gate_uses_path_in_hint():
    resp = {"encoding": "base64", "data": LONG_DN}
    result = gate_download(resp, path="/Documents/report.pdf", limit=LIMIT_DN)
    assert "/Documents/report.pdf" in result["hint"]


def test_download_gate_placeholder_when_no_path():
    resp = {"encoding": "base64", "data": LONG_DN}
    result = gate_download(resp, limit=LIMIT_DN)
    assert "<nc-pfad>" in result["hint"]


def test_download_pass_when_no_data_field():
    resp = {"encoding": "base64", "status_code": 200}
    assert gate_download(resp, limit=LIMIT_DN) is resp
