import os

os.environ.setdefault("GATEWAY_API_KEY", "test-key")

import pytest  # noqa: E402

import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_clients(monkeypatch):
    """Jeder Test startet ohne zwischengespeicherte httpx-Clients (eigener Event-Loop je Test)."""
    monkeypatch.setattr(app, "_clients", {})
    monkeypatch.setattr(app, "GATEWAY_URL_INTERNAL", "")
    yield
