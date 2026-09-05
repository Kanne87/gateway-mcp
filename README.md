# gateway-mcp
MCP server wrapping K-AI API Gateway - 3 tools replacing 170+

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
  "hint": "gw up <lokal> <nc-pfad> ..."
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
  "hint": "gw down /Documents/file.pdf -o /tmp/file.pdf ...",
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
