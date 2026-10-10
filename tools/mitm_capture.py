"""mitmproxy addon: record which session endpoints the claude CLI calls.

Run:
    mitmdump -q -s tools/mitm_capture.py
Then, in another terminal:
    HTTPS_PROXY=http://127.0.0.1:8080 \
    NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem \
    claude --teleport            # open the picker, pick a session, then quit

Each request to *.anthropic.com is appended to ccweb-capture.jsonl (owner-only
permissions) with method, path, query parameter names, request headers, status
and the response's JSON *shape*. Model traffic (/v1/messages) is skipped so no
prompts are recorded. Credentials are redacted. Set CCWEB_CAPTURE_FULL=1 to
keep full response bodies; those contain your conversations, so keep the file local.
"""

import json
import os
import time

from mitmproxy import http

OUT = os.environ.get("CCWEB_CAPTURE", "ccweb-capture.jsonl")
FULL = os.environ.get("CCWEB_CAPTURE_FULL") == "1"
REDACT = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
SKIP_PREFIXES = ("/v1/messages", "/api/event_logging", "/api/hello")
ENUM_KEYS = {"type", "role", "status", "session_status", "state", "kind"}


def shape(value, key="", depth=0):
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: shape(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [f"{len(value)} items", shape(value[0], key, depth + 1)] if value else []
    if isinstance(value, str):
        return value if key in ENUM_KEYS else f"str({len(value)})"
    return type(value).__name__ if value is not None else None


class Capture:
    def response(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        if not req.pretty_host.endswith("anthropic.com") or req.path.startswith(SKIP_PREFIXES):
            return
        try:
            body = json.loads(flow.response.get_text() or "null")
        except (ValueError, TypeError):
            body = f"<{len(flow.response.content or b'')} bytes, not JSON>"
        record = {
            "time": time.strftime("%H:%M:%S"),
            "method": req.method,
            "host": req.pretty_host,
            "path": req.path.split("?", 1)[0],
            "query_keys": sorted(req.query.keys()),
            "request_headers": {
                k: ("<redacted>" if k.lower() in REDACT else v) for k, v in req.headers.items()
            },
            "status": flow.response.status_code,
            "response": body if FULL else shape(body),
        }
        fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


addons = [Capture()]
