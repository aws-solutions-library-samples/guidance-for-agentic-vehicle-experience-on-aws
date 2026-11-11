"""API Gateway (REST, Lambda proxy, Cognito authorizer) in front of the real handlers.

Routes, methods and authorizers come from the live APIs (``apigateway get-resources``
and ``get-authorizers`` on ``vsa-staging-api`` and ``DmsApiStack-staging``, read
2026-10-10): every route uses a ``COGNITO_USER_POOLS`` authorizer on the
``Authorization`` header against the CMS pool. Behaviour reproduced:

- a path or method the API does not define answers 403
  ``{"message": "Missing Authentication Token"}`` (DMS's gateway response
  MISSING_AUTHENTICATION_TOKEN is 403; the AVX API has the default, also 403);
- a missing or unverifiable token answers 401 ``{"message": "Unauthorized"}``;
- the handler gets a REST proxy event: ``resource`` is the route template,
  ``pathParameters`` / ``queryStringParameters`` are ``None`` when empty, and
  ``requestContext.authorizer.claims`` holds the ID token's claims as strings
  (``cognito:groups`` space-separated, as DMS ``auth.py`` documents for this pool);
- an exception in the handler answers 502 ``{"message": "Internal server error"}``.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
import traceback
import urllib.parse
import uuid
from typing import Any, Callable

HandlerFn = Callable[[dict[str, Any], Any], dict[str, Any]]


class LambdaContext:
    def __init__(self, name: str, remaining_ms: int = 29_000) -> None:
        self.function_name = name
        self.aws_request_id = str(uuid.uuid4())
        self._remaining = remaining_ms
        self.memory_limit_in_mb = 512
        self.invoked_function_arn = f"arn:aws:lambda:us-west-2:123456789012:function:{name}"

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining


def _claims_as_strings(claims: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in claims.items():
        if k == "cognito:groups" and isinstance(v, list):
            out[k] = " ".join(v)
        elif isinstance(v, bool):
            out[k] = "true" if v else "false"
        else:
            out[k] = str(v)
    return out


class Route:
    def __init__(self, template: str, method: str, name: str, fn: HandlerFn | None) -> None:
        self.template, self.method, self.name, self.fn = template, method, name, fn
        pat = re.sub(r"\{([^}/]+)\}", r"(?P<\1>[^/]+)", template)
        self.regex = re.compile(f"^{pat}$")


class FakeRestApi:
    """One REST API stage served on 127.0.0.1; ``url`` ends in ``/<stage>/``."""

    def __init__(self, name: str, stage: str, verify_token: Callable[[str], dict | None],
                 invoke: Callable[[str, HandlerFn, dict[str, Any], LambdaContext], dict[str, Any]]) -> None:
        self.name, self.stage = name, stage
        self.routes: list[Route] = []
        self.requests: list[dict[str, Any]] = []
        self._verify = verify_token
        self._invoke = invoke
        api = self

        class _H(http.server.BaseHTTPRequestHandler):
            def _any(self) -> None:
                api._serve(self)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _any

            def log_message(self, *a: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/{stage}/"

    def add(self, template: str, method: str, name: str, fn: HandlerFn | None) -> None:
        self.routes.append(Route(template, method, name, fn))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ------------------------------------------------------------------
    def _send(self, h: Any, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            if k.lower() != "content-length":
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def _serve(self, h: Any) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        raw_body = h.rfile.read(length) if length else b""
        rec: dict[str, Any] = {"method": h.command, "path": parsed.path,
                               "query": parsed.query, "headers": dict(h.headers.items()),
                               "body": raw_body.decode() if raw_body else None}
        self.requests.append(rec)
        prefix = f"/{self.stage}"
        if not parsed.path.startswith(prefix + "/"):
            rec["status"] = 403
            return self._send(h, 403, {"message": "Missing Authentication Token"})
        rel = parsed.path[len(prefix):]
        route, params = None, {}
        for r in self.routes:
            m = r.regex.match(rel)
            if m and r.method == h.command:
                route, params = r, {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
                break
        if route is None:
            rec["status"] = 403
            return self._send(h, 403, {"message": "Missing Authentication Token"})
        auth = h.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
        claims = self._verify(token) if token else None
        if claims is None:
            rec["status"] = 401
            return self._send(h, 401, {"message": "Unauthorized"})
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        event = {
            "resource": route.template, "path": rel, "httpMethod": h.command,
            "headers": dict(h.headers.items()),
            "multiValueHeaders": {k: [v] for k, v in h.headers.items()},
            "queryStringParameters": {k: v[-1] for k, v in qs.items()} or None,
            "multiValueQueryStringParameters": qs or None,
            "pathParameters": params or None, "stageVariables": None,
            "requestContext": {
                "resourcePath": route.template, "httpMethod": h.command,
                "stage": self.stage, "requestId": str(uuid.uuid4()),
                "authorizer": {"claims": _claims_as_strings(claims)},
                "identity": {"sourceIp": "127.0.0.1"},
            },
            "body": raw_body.decode() if raw_body else None,
            "isBase64Encoded": False,
        }
        rec["route"] = route.template
        if route.fn is None:
            rec["status"] = 502
            return self._send(h, 502, {"message": f"world: route {route.name} not modeled"})
        try:
            resp = self._invoke(route.name, route.fn, event, LambdaContext(route.name))
        except Exception:  # noqa: BLE001 — Lambda error -> API Gateway 502
            rec["status"] = 502
            rec["error"] = traceback.format_exc()
            return self._send(h, 502, {"message": "Internal server error"})
        status = int(resp.get("statusCode", 200))
        rec["status"] = status
        body = resp.get("body")
        payload = (body.encode() if isinstance(body, str) else (body or b""))
        rec["response"] = payload.decode(errors="replace")[:2000]
        self._send(h, status, payload, resp.get("headers") or {})
