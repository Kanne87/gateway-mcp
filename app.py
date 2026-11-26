"""
K-AI Gateway MCP Server
Wraps the API Gateway into 3 MCP tools for Claude.
Replaces 170+ individual MCP tool definitions with:
  1. gateway_services - list all available services
  2. gateway_service - get detail for one service
  3. gateway_execute - execute any endpoint on any service

Timeout-Kette (gateway_execute):
  Gateway ssh: connect_timeout 10 s + SSH_TIMEOUT 60 s = bis ~70 s, danach 504 vom Gateway
  < MCP-Proxy: GATEWAY_TIMEOUT_SECONDS (Default 90 s)
  < Cloudflare: 100 s
Der Proxy wartet also laenger als das laengste Gateway-Budget, damit dessen saubere
504-Antwort beim Client ankommt, bleibt aber unter der Cloudflare-Grenze.
Timeouts und HTTP-Fehler kommen als Umschlag-Dict zurueck, nicht als Exception
(ein leeres 'Error:' im MCP-Client war vorher das Bild eines Proxy-Timeouts).

Weg zum Gateway (S4214):
  GATEWAY_URL_INTERNAL (optional, z. B. http://<container>:8000 im Docker-Netz 'coolify')
  wird zuerst versucht. Nur bei Verbindungsfehlern (ConnectError, ConnectTimeout, DNS) folgt
  ein Rueckfall auf GATEWAY_URL (oeffentlich), mit Log-Zeile. Bei ReadTimeout gibt es NIE einen
  Rueckfall: der Aufruf ist dann schon beim Gateway angekommen, ein zweiter Versuch wuerde ihn
  doppelt ausfuehren.
  Je Ziel-URL gibt es genau einen httpx.AsyncClient mit Keep-Alive (lazy erzeugt, im Lifespan
  geschlossen), statt je Aufruf einen neuen TLS-Handshake.

Client-Auth (S4215, optional):
  MCP_CLIENT_TOKEN gesetzt -> /mcp braucht 'Authorization: Bearer <token>' oder 'X-API-Key: <token>'.
  SSE (/sse, /messages/) nur zusaetzlich mit MCP_CLIENT_TOKEN_SSE=true. /health immer offen.
  Leer/ungesetzt -> keine Pruefung (WARNING beim Start). Token wird nie geloggt.

Transporte (beide im selben Prozess, Port 8000):
  SSE:             GET /sse + POST /messages/   (claude.ai-Connector, Bridge-Jobs)
  Streamable HTTP: POST/GET/DELETE /mcp         (stateless)

fields (gateway_execute): Punkt-Pfade relativ zu data, Ganzzahl-Segmente indizieren Listen,
fehlende Pfade -> null; data wird zu {pfad: wert}. Semantik wie gw-CLI --fields.
"""

import contextlib
import hmac
import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("gateway-mcp")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://api-gateway.kailohmann.de").rstrip("/")
# Optionaler kurzer Weg im Docker-Netz; leer = nur GATEWAY_URL
GATEWAY_URL_INTERNAL = os.environ.get("GATEWAY_URL_INTERNAL", "").strip().rstrip("/")
GATEWAY_KEY = os.environ["GATEWAY_API_KEY"]

# httpx timeout fuer gateway_execute (s), Default 90: > Gateway ssh 10+60 s, < Cloudflare 100 s
GATEWAY_TIMEOUT_SECONDS = float(os.environ.get("GATEWAY_TIMEOUT_SECONDS", "90"))
SERVICES_TIMEOUT_SECONDS = 30.0
# Connect-Timeout nur fuer den internen Versuch: ein toter interner Weg soll den
# Rueckfall nicht erst nach dem vollen Lese-Budget ausloesen.
INTERNAL_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("GATEWAY_INTERNAL_CONNECT_TIMEOUT_SECONDS", "5"))
ERROR_TEXT_MAX_CHARS = 500

GATE_ENABLED = os.environ.get("GATE_ENABLED", "true").lower() != "false"
GATE_UPLOAD_CHARS = int(os.environ.get("GATE_UPLOAD_CHARS", "4096"))
GATE_DOWNLOAD_B64_CHARS = int(os.environ.get("GATE_DOWNLOAD_B64_CHARS", "8192"))

_UPLOAD_FIELDS = ("content_text", "content_base64", "content", "body")

mcp = FastMCP(
    "K-AI Gateway",
    instructions="Unified gateway to all services: Paperless, Firefly, Coolify, GitHub, Nextcloud, Outline, n8n",
    host="0.0.0.0",
    port=8000,
    # Stateless: keine Server-Sitzungen, die nach Abbruch oder Neustart verwaisen koennen
    stateless_http=True,
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
                        f"Zu gross fuer den Kontext ({len(val)} Zeichen, Limit {lim}). "
                        "Textdateien: bewusst mit params.force=true hochladen. "
                        "Binaerdateien nicht als base64 durch den Kontext schicken, sondern serverseitig erzeugen und ablegen "
                        "(Knoten service_cheatsheet, Feld sandbox_direktweg). "
                        "Kanal B (gw up) nur, wo verfuegbar."
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
        f"Datei zu gross fuer den Kontext (~{n * 3 // 4} Bytes). "
        "Inhalt serverseitig lesen (Lese-Sandbox laut dokument_lesen_ops_playbook, mistral.ocr) statt Bytes zu holen; "
        "Volltext nur bewusst mit params.force=true. "
        f"Kanal B (gw down {path_hint}) nur, wo verfuegbar."
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


def _envelope(service: str, action: str, status_code: int, data: Any) -> dict[str, Any]:
    """Response dict in the gateway's envelope format (used for proxy-side errors)."""
    return {
        "service": service,
        "action": action,
        "status_code": status_code,
        "data": data,
        "encoding": None,
        "filename": None,
        "content_type": None,
    }


# ---------------------------------------------------------------------------
# fields-Projektion (gespiegelt aus gw-CLI _project)
# ---------------------------------------------------------------------------


def parse_fields(fields: list[str] | str | None) -> list[str] | None:
    """Normalize fields to a list of non-empty paths; None if not set.

    Accepts a list of strings or a comma separated string (like gw --fields a,b).
    """
    if fields is None:
        return None
    raw = fields.split(",") if isinstance(fields, str) else [str(f) for f in fields]
    return [p for p in (s.strip() for s in raw) if p]


def project(data: Any, paths: list[str]) -> dict[str, Any]:
    """Project exactly the given paths out of data -> {pfad: wert}, in input order.

    Dot notation relative to data; an integer segment indexes a list (negative
    indices allowed, like gw). Missing path (unknown key, index out of range,
    segment on a scalar) -> None.
    """
    out: dict[str, Any] = {}
    for path in paths:
        cur = data
        for seg in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(seg)
            elif isinstance(cur, list):
                try:
                    idx = int(seg)
                except ValueError:
                    cur = None
                    break
                cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            else:
                cur = None
                break
        out[path] = cur
    return out


def apply_fields(resp: Any, paths: list[str] | None) -> Any:
    """Replace resp['data'] by its projection; envelope stays.

    Not filtered: gated responses and envelopes whose status_code is not 2xx.
    """
    if paths is None or not isinstance(resp, dict) or resp.get("gated"):
        return resp
    sc = resp.get("status_code")
    if isinstance(sc, int) and not 200 <= sc < 300:
        return resp
    return {**resp, "data": project(resp.get("data"), paths)}


# ---------------------------------------------------------------------------
# HTTP-Clients: einer je Ziel-URL, Keep-Alive, lazy
# ---------------------------------------------------------------------------

_clients: dict[str, httpx.AsyncClient] = {}

# Verbindungsfehler, bei denen der Request das Gateway sicher nicht erreicht hat.
# DNS-Fehler kommen bei httpx als ConnectError. ReadTimeout/WriteTimeout gehoeren
# bewusst NICHT dazu (Doppelausfuehrung).
_FALLBACK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


def _get_client(base_url: str) -> httpx.AsyncClient:
    client = _clients.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=GATEWAY_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60),
        )
        _clients[base_url] = client
    return client


async def close_clients() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for c in clients:
        await c.aclose()


def _targets() -> list[str]:
    if GATEWAY_URL_INTERNAL and GATEWAY_URL_INTERNAL != GATEWAY_URL:
        return [GATEWAY_URL_INTERNAL, GATEWAY_URL]
    return [GATEWAY_URL]


async def _request(method: str, path: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
    """Send a request, internal URL first, public URL on connect errors only."""
    targets = _targets()
    for i, base in enumerate(targets):
        has_fallback = i + 1 < len(targets)
        t: float | httpx.Timeout = (
            httpx.Timeout(timeout, connect=min(INTERNAL_CONNECT_TIMEOUT_SECONDS, timeout)) if has_fallback else timeout
        )
        try:
            return await _get_client(base).request(method, path, timeout=t, **kwargs)
        except _FALLBACK_ERRORS as e:
            if not has_fallback:
                raise
            log.warning(
                "gateway-mcp: interner Weg %s nicht erreichbar (%s: %s), Rueckfall auf %s",
                base, type(e).__name__, e, targets[i + 1],
            )
    raise AssertionError("unreachable")  # pragma: no cover


def _fmt_seconds(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


@mcp.tool()
async def gateway_services() -> dict[str, Any]:
    """List all available services and their endpoints.
    Call this first to discover what actions are available.
    Returns a map of service_id -> {name, endpoints: {action_id -> {method, params, accepts_body}}}
    """
    r = await _request("GET", "/services", timeout=SERVICES_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def gateway_service(service: str) -> dict[str, Any]:
    """Get detailed info about a specific service and its available endpoints.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify', 'github', 'nextcloud', 'outline', 'n8n')
    """
    r = await _request("GET", f"/services/{service}", timeout=SERVICES_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()


EVAL_HEADER = "X-Eval-Run"


def _eval_run(ctx: Context | None) -> str | None:
    """S4242: Wert des Headers X-Eval-Run der MCP-Anfrage (nicht leer), sonst None.

    Eval-Sessions (claude-bridge, Eval-Harness) setzen den Header in ihrer
    MCP-Konfiguration; der Gateway laesst dann nur Lesen zu (HTTP 423 sonst).
    """
    try:
        request = ctx.request_context.request if ctx is not None else None
        value = request.headers.get(EVAL_HEADER) if request is not None else None
    except Exception:  # noqa: BLE001 - ausserhalb eines HTTP-Requests (stdio, Tests)
        return None
    value = (value or "").strip()
    return value or None


@mcp.tool()
async def gateway_execute(
    service: str,
    action: str,
    params: dict | None = None,
    body: dict | None = None,
    fields: list[str] | str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Execute an action on a service through the gateway.

    Args:
        service: Service ID (e.g. 'paperless', 'firefly', 'coolify')
        action: Endpoint action ID (e.g. 'list_documents', 'search_transactions')
        params: URL/path parameters as key-value pairs (e.g. {"id": "123", "page_size": 10})
        body: Request body for POST/PUT/PATCH endpoints (e.g. {"title": "New doc"})
        fields: Optional. Nur diese Pfade aus data zurueckgeben (wie gw --fields), als Liste
            oder kommagetrennter String. Punkt-Pfade relativ zu data, Ganzzahl-Segmente
            indizieren Listen (events.0.event.result), fehlende Pfade -> null. data wird zu
            {pfad: wert}; service, action, status_code bleiben. Nicht gefiltert werden
            Antworten mit status_code ausserhalb 2xx und gated-Antworten.
            Beispiel: service='claude_bridge', action='get_job', params={'job_id': '...'},
            fields=['status', 'summary.events_count']
            -> data == {'status': 'done', 'summary.events_count': 42}

    Grosse Uploads/Downloads werden abgelehnt (gated). Dann serverseitig arbeiten
    (Knoten service_cheatsheet, Feld sandbox_direktweg); Textdateien bewusst mit params.force=true.
    Kanal B (gw up / gw down) nur, wo verfuegbar.
    """
    paths = parse_fields(fields)
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

    eval_run = _eval_run(ctx)
    extra_headers = {EVAL_HEADER: eval_run} if eval_run else None

    try:
        r = await _request(
            "POST", "/execute", timeout=GATEWAY_TIMEOUT_SECONDS, json=payload, headers=extra_headers,
        )
        if r.status_code == 423:
            # S4242 Eval-Trockenmodus: Sperr-Rumpf {eval_blocked, ...} als data durchreichen.
            try:
                return _envelope(service, action, 423, r.json())
            except ValueError:
                pass
        r.raise_for_status()
        resp = r.json()
    except httpx.TimeoutException:
        return _envelope(
            service, action, 504,
            f"MCP-Proxy-Timeout nach {_fmt_seconds(GATEWAY_TIMEOUT_SECONDS)} s; der Aufruf kann im Gateway weiterlaufen",
        )
    except httpx.HTTPStatusError as e:
        return _envelope(service, action, e.response.status_code, e.response.text[:ERROR_TEXT_MAX_CHARS])

    if GATE_ENABLED and not force:
        path = params.get("path") if params else None
        resp = gate_download(resp, path)

    return apply_fields(resp, paths)


# ---------------------------------------------------------------------------
# ASGI-App: SSE (/sse, /messages/) und Streamable HTTP (/mcp) in einem Prozess
# ---------------------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    """Liveness ohne Auth und ohne Upstream-Call (Gateway-Ausfall darf den Container nicht neu starten)."""
    return JSONResponse({"status": "ok"})

# ---------------------------------------------------------------------------
# Client-Auth fuer eingehende MCP-Clients (S4215)
# ---------------------------------------------------------------------------

_MCP_PREFIXES = ("/mcp",)
_SSE_PREFIXES = ("/sse", "/messages")


def _path_matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """Exakter Pfad oder Unterpfad (/mcp, /mcp/, /mcp/x), nicht /mcpfoo."""
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class ClientAuthMiddleware:
    """Reine ASGI-Middleware (kein BaseHTTPMiddleware, damit SSE-Streams ungepuffert bleiben).

    Prueft nur HTTP-Requests auf geschuetzten Pfaden; lifespan und alles andere geht durch.
    """

    def __init__(self, app: Any, token: str, protect_sse: bool = False) -> None:
        self.app = app
        self._token = token.encode("utf-8")
        self._prefixes = _MCP_PREFIXES + (_SSE_PREFIXES if protect_sse else ())

    def _authorized(self, scope: dict) -> bool:
        presented: list[bytes] = []
        for name, value in scope.get("headers") or ():
            if name == b"authorization":
                scheme, _, cred = value.strip().partition(b" ")
                if scheme.lower() == b"bearer":
                    presented.append(cred.strip())
            elif name == b"x-api-key":
                presented.append(value.strip())
        ok = False
        for cand in presented:
            # kein Kurzschluss: jeder Kandidat wird zeitkonstant verglichen
            ok = hmac.compare_digest(cand, self._token) or ok
        return ok

    @staticmethod
    def _client_ip(scope: dict) -> str:
        hdrs = dict(scope.get("headers") or ())
        for h in (b"cf-connecting-ip", b"x-forwarded-for", b"x-real-ip"):
            v = hdrs.get(h)
            if v:
                return v.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "-"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _path_matches(scope.get("path", ""), self._prefixes):
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        # Nur Pfad und Client-IP loggen, nie Header oder Token
        log.warning("gateway-mcp: 401 unauthorized path=%s client=%s", scope.get("path", ""), self._client_ip(scope))
        resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        await resp(scope, receive, send)


def client_auth_middleware() -> list[Middleware]:
    """Middleware-Liste aus der Umgebung (beim Bau der App gelesen)."""
    token = os.environ.get("MCP_CLIENT_TOKEN", "").strip()
    if not token:
        log.warning(
            "gateway-mcp: MCP_CLIENT_TOKEN nicht gesetzt - eingehende Clients werden NICHT authentifiziert (/mcp, /sse offen)"
        )
        return []
    protect_sse = _env_true("MCP_CLIENT_TOKEN_SSE")
    log.info(
        "gateway-mcp: Client-Auth aktiv fuer /mcp%s",
        ", /sse, /messages" if protect_sse else " (SSE offen, MCP_CLIENT_TOKEN_SSE nicht gesetzt)",
    )
    return [Middleware(ClientAuthMiddleware, token=token, protect_sse=protect_sse)]


def build_app() -> Starlette:
    sse = mcp.sse_app()
    streamable = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await close_clients()

    return Starlette(
        routes=[Route("/health", health, methods=["GET"]), *sse.routes, *streamable.routes],
        middleware=client_auth_middleware(),
        lifespan=lifespan,
    )


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(build_app(), host=mcp.settings.host, port=mcp.settings.port, log_level="info")
