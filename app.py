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
from mcp.server.fastmcp import FastMCP

GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://api-gateway.kailohmann.de")
GATEWAY_KEY = os.environ["GATEWAY_API_KEY"]

mcp = FastMCP(
    "K-AI Gateway",
    instructions="Unified gateway to all services: Paperless, Firefly, Coolify, GitHub, Nextcloud, Outline, n8n",
    host="0.0.0.0",
    port=8000,
)

headers = {"X-Gateway-Key": GATEWAY_KEY}


@mcp.tool()
async def gateway_services() -> dict:
    """List all available services and their endpoints.
    Call this first to discover what actions are available.
    Returns a map of service_id -> {name, endpoints: {action_id -> {method, params, accepts_body}}}
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GATEWAY_URL}/services", headers=headers)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def gateway_service(service: str) -> dict:
    """Get detailed info about a specific service and its available endpoints.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify', 'github', 'nextcloud', 'outline', 'n8n')
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GATEWAY_URL}/services/{service}", headers=headers)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def gateway_execute(service: str, action: str, params: dict | None = None, body: dict | None = None) -> dict:
    """Execute an action on a service through the gateway.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify')
        action: Endpoint action ID (e.g. 'list_documents', 'search_transactions')
        params: URL/path parameters as key-value pairs (e.g. {"id": "123", "page_size": 10})
        body: Request body for POST/PUT/PATCH endpoints (e.g. {"title": "New doc"})
    """
    payload = {"service": service, "action": action}
    if params:
        payload["params"] = params
    if body:
        payload["body"] = body

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{GATEWAY_URL}/execute", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    mcp.run(transport="sse")
