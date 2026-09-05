"""
K-AI Gateway MCP Server
Wraps the API Gateway into 3 MCP tools for Claude.
Replaces 170+ individual MCP tool definitions with:
  1. gateway_services - list all available services
  2. gateway_service - get detail for one service
  3. gateway_execute - execute any endpoint on any service
"""

import os
import httpx
from typing import Any
from mcp.server.fastmcp import FastMCP

GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://api-gateway.kailohmann.de")
GATEWAY_KEY = os.environ["GATEWAY_API_KEY"]

GATE_ENABLED = os.environ.get("GATE_ENABLED", "true").lower() != "false"
GATE_UPLOAD_CHARS = int(os.environ.get("GATE_UPLOAD_CHARS", "4096"))
GATE_DOWNLOAD_B64_CHARS = int(os.environ.get("GATE_DOWNLOAD_B64_CHARS", "8192"))

_UPLOAD_FIELDS = ("content_text", "content_base64", "content", "body")

mcp = FastMCP(
    "K-AI Gateway",
    instructions="Unified gateway to all services: Paperless, Firefly, Coolify, GitHub, Nextcloud, Outline, n8n",
    host="0.0.0.0",
    port=8000,
)

headers = {"X-Gateway-Key": GATEWAY_KEY}


def check_upload(
    params: dict | None,
    body: dict | None,
    limit: int | None = None,
) -> dict | None:
    """Return a gated response dict if any upload field exceeds the limit, else None."""
    lim = limit if limit is not None else GATE_UPLOAD_CHARS
    for source in (params, body):
        if not source:
            continue
        for field in _UPLOAD_FIELDS:
            val = source.get(field)
            if isinstance(val, str) and len(val) > lim:
                return {
                    "gated": True,
                    "reason": "upload_too_large_for_context",
                    "field": field,
                    "size_chars": len(val),
                    "limit": lim,
                    "hint": (
                        "Datei im Container erzeugen und per Kanal B senden: "
                        "gw up <lokal> <nc-pfad>   "
                        "(Fallback: curl -sf -H 'X-Gateway-Key: $GW_KEY' -X POST $GW/upload "
                        "-F service=<service> -F action=<action> -F 'params={...}' -F file=@<lokal>). "
                        "Override nur bewusst: params.force=true."
                    ),
                }
    return None


def gate_download(
    resp: dict,
    path: str | None = None,
    limit: int | None = None,
) -> dict:
    """Strip oversized base64 data from a gateway response and mark it as gated."""
    lim = limit if limit is not None else GATE_DOWNLOAD_B64_CHARS
    data = resp.get("data", "")
    if resp.get("encoding") != "base64" or not isinstance(data, str) or len(data) <= lim:
        return resp
    n = len(data)
    path_hint = path if path else "<nc-pfad>"
    hint = (
        f"Bytes per Kanal B holen: gw down {path_hint} -o <lokal>   "
        "(Fallback: POST $GW/download mit demselben JSON-Body, streamt rohe Bytes; "
        "oder /execute + base64-Decode im Container). "
        "Override nur bewusst: params.force=true."
    )
    return {
        **resp,
        "data": "",
        "gated": True,
        "reason": "download_too_large_for_context",
        "size_base64_chars": n,
        "approx_bytes": n * 3 // 4,
        "limit": lim,
        "hint": hint,
    }


@mcp.tool()
async def gateway_services() -> dict[str, Any]:
    """List all available services and their endpoints.
    Call this first to discover what actions are available.
    Returns a map of service_id -> {name, endpoints: {action_id -> {method, params, accepts_body}}}
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GATEWAY_URL}/services", headers=headers)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def gateway_service(service: str) -> dict[str, Any]:
    """Get detailed info about a specific service and its available endpoints.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify', 'github', 'nextcloud', 'outline', 'n8n')
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GATEWAY_URL}/services/{service}", headers=headers)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def gateway_execute(service: str, action: str, params: dict | None = None, body: dict | None = None) -> dict[str, Any]:
    """Execute an action on a service through the gateway.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify')
        action: Endpoint action ID (e.g. 'list_documents', 'search_transactions')
        params: URL/path parameters as key-value pairs (e.g. {"id": "123", "page_size": 10})
        body: Request body for POST/PUT/PATCH endpoints (e.g. {"title": "New doc"})

    Grosse Uploads/Downloads werden abgelehnt (gated) — dann Kanal B (gw up / gw down) nutzen;
    params.force=true ist der bewusste Override.
    """
    force = False
    if params and params.get("force") is True:
        force = True
        params = {k: v for k, v in params.items() if k != "force"}

    if GATE_ENABLED and not force:
        gate = check_upload(params, body)
        if gate:
            return gate

    payload = {"service": service, "action": action}
    if params:
        payload["params"] = params
    if body:
        payload["body"] = body

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{GATEWAY_URL}/execute", headers=headers, json=payload)
        r.raise_for_status()
        resp = r.json()

    if GATE_ENABLED and not force:
        path = params.get("path") if params else None
        resp = gate_download(resp, path)

    return resp


if __name__ == "__main__":
    mcp.run(transport="sse")
