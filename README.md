# gateway-mcp
MCP server wrapping K-AI API Gateway - 3 tools replacing 170+

## Transporte

Ein Prozess, Port 8000, zwei Transporte nebeneinander:

| Transport | URL | Nutzer |
|-----------|-----|--------|
| SSE | `GET https://gateway-mcp.kailohmann.de/sse` + `POST /messages/?session_id=…` | claude.ai-Connector, Bridge-Jobs (unverändert) |
| Streamable HTTP | `POST/GET/DELETE https://gateway-mcp.kailohmann.de/mcp` | neu (S4214), stateless |

`/mcp` läuft **stateless** (`stateless_http=True`): Es gibt keine Server-Sitzungen, die nach
einem Abbruch oder Neustart verwaisen (vgl. verwaiste SSE-Sitzungen, Befund S3476).
Der Session-Manager startet im Lifespan der gemeinsamen Starlette-App (`build_app()`).

## fields-Filter

`gateway_execute(..., fields=[...])` gibt statt der vollen `data` nur die genannten Pfade zurück,
Semantik wie `gw x … --fields`:

- Liste von Strings oder kommagetrennter String (`"status,summary.events_count"`)
- Punkt-Pfade relativ zu `data`; ein Ganzzahl-Segment indiziert eine Liste (auch negativ:
  `events.-1.event.result`)
- fehlender Pfad (unbekannter Schlüssel, Index außerhalb, Segment auf Skalar) → `null`
- `data` wird zu `{pfad: wert}` in Eingabereihenfolge; der Umschlag (`service`, `action`,
  `status_code`, `encoding`, …) bleibt
- nicht gefiltert: `status_code` außerhalb 2xx und `gated`-Antworten

```json
gateway_execute(service="claude_bridge", action="get_job", params={"job_id": "…"},
                fields=["status", "summary.events_count"])
→ {"service": "claude_bridge", "action": "get_job", "status_code": 200,
   "data": {"status": "done", "summary.events_count": 42}, …}
```

## Weg zum Gateway

| Env-Var | Standard | Bedeutung |
|---------|----------|-----------|
| `GATEWAY_URL` | `https://api-gateway.kailohmann.de` | öffentlicher Weg (Cloudflare) |
| `GATEWAY_URL_INTERNAL` | leer | optionaler kurzer Weg im Docker-Netz `coolify` |
| `GATEWAY_INTERNAL_CONNECT_TIMEOUT_SECONDS` | `5` | Connect-Timeout nur des internen Versuchs |

Ist `GATEWAY_URL_INTERNAL` gesetzt, geht jeder Aufruf (`/execute`, `/services`) zuerst dorthin.
Nur bei Verbindungsfehlern (`ConnectError` inkl. DNS, `ConnectTimeout`) folgt der Rückfall auf
`GATEWAY_URL`, mit einer WARNING-Logzeile `interner Weg … nicht erreichbar …, Rueckfall auf …`.
Bei `ReadTimeout` gibt es **nie** einen Rückfall: Der Aufruf ist dann schon beim Gateway, ein
zweiter Versuch würde ihn doppelt ausführen.

Je Ziel-URL gibt es einen `httpx.AsyncClient` mit Keep-Alive (lazy erzeugt, im Lifespan
geschlossen) statt eines neuen Clients mit TLS-Handshake je Aufruf.

Hinweis (S4214): Coolify vergibt dem api-gateway im Netz `coolify` nur den Container-Namen
mit Zeitstempel (`<uuid>-<deploy-zeit>`) als Alias, die bloße App-UUID löst nicht auf. Ein
stabiler interner Name braucht erst einen festen Netzwerk-Alias am api-gateway; bis dahin
bleibt `GATEWAY_URL_INTERNAL` ungesetzt.

## Größen-Gate

`gateway_execute` schützt den Modell-Kontext vor großen Byte-Lasten in beiden Richtungen.

### Upload-Gate (vor dem Call)

Felder `content_text`, `content_base64`, `content`, `body` in `params` oder `body` werden geprüft.
Überschreitet ein String-Wert das Limit, wird der Call **nicht ausgeführt** und stattdessen zurückgegeben:

```json
{
  "gated": true,
  "reason": "upload_too_large_for_context",
  "field": "content_base64",
  "size_chars": 52000,
  "limit": 4096,
  "hint": "Zu gross fuer den Kontext (52000 Zeichen, Limit 4096). Textdateien: bewusst mit params.force=true hochladen. Binaerdateien nicht als base64 durch den Kontext schicken, sondern serverseitig erzeugen und ablegen (Knoten service_cheatsheet, Feld sandbox_direktweg). Kanal B (gw up) nur, wo verfuegbar."
}
```

### Download-Gate (nach dem Call)

Antwortet das Gateway mit `encoding == "base64"` und `len(data) > Limit`, wird `data` durch `""` ersetzt:

```json
{
  "gated": true,
  "reason": "download_too_large_for_context",
  "size_base64_chars": 120000,
  "approx_bytes": 90000,
  "limit": 8192,
  "hint": "Datei zu gross fuer den Kontext (~90000 Bytes). Inhalt serverseitig lesen (Lese-Sandbox laut dokument_lesen_ops_playbook, mistral.ocr) statt Bytes zu holen; Volltext nur bewusst mit params.force=true. Kanal B (gw down /Documents/file.pdf) nur, wo verfuegbar.",
  "status_code": 200,
  "filename": "file.pdf",
  "content_type": "application/pdf"
}
```

Enthält `params.path` einen Wert, wird er direkt in den Hint eingesetzt.

### Limits & Override

| Env-Var                  | Standard | Bedeutung                       |
|--------------------------|----------|---------------------------------|
| `GATE_UPLOAD_CHARS`      | `4096`   | Max. Zeichen für Upload-Felder  |
| `GATE_DOWNLOAD_B64_CHARS`| `8192`   | Max. Zeichen Base64-Antwort     |
| `GATE_ENABLED`           | `true`   | `false` deaktiviert beide Gates |

**Override:** `params.force=true` umgeht beide Gates für diesen Call (wird vor Weiterleitung entfernt).

### Kanal B

- Upload: `gw up <lokal> <nc-pfad>` oder `POST $GW/upload` multipart
- Download: `gw down <nc-pfad> -o <lokal>` oder `POST $GW/download` (streamt rohe Bytes)

## Timeout-Kette

| Glied | Grenze |
|-------|--------|
| api-gateway, ssh | `connect_timeout` 10 s + `SSH_TIMEOUT` 60 s = bis ~70 s, dann eigene Antwort `status_code: 504` (`SSH command timed out`) |
| MCP-Proxy (`gateway_execute`) | `GATEWAY_TIMEOUT_SECONDS`, Standard **90 s** |
| Cloudflare | 100 s (nur auf dem öffentlichen Weg) |

Der Proxy wartet länger als das längste Budget des Gateways, damit dessen saubere 504-Antwort
beim Client ankommt, und bleibt unter der Cloudflare-Grenze. `gateway_services` und
`gateway_service` behalten 30 s.

Fehler kommen bei `gateway_execute` als Umschlag-Dict statt als Exception (vorher sah der Client nur ein leeres `Error:`):

- Proxy-Timeout: `status_code: 504`, `data: "MCP-Proxy-Timeout nach 90 s; der Aufruf kann im Gateway weiterlaufen"`
- HTTP-Status ≠ 2xx vom Gateway (z. B. 502, Cloudflare 524): `status_code` = HTTP-Status, `data` = Antworttext, gekürzt auf 500 Zeichen

`encoding`, `filename` und `content_type` sind dabei `null`.
