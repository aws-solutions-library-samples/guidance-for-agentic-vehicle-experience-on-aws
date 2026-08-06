#!/usr/bin/env python3
"""Smoke-test the deployed POST /assistant/chat route end-to-end.

Verifies the Group 3 vsa-api wiring + the Stage A handler are live by:
  1. Fetching a CMS Cognito IdToken (reuses scripts/get-cached-id-token.py).
  2. POSTing a simple prompt to {VSA_API_URL}/assistant/chat.
  3. Asserting HTTP 200, JSON body has `result` (non-empty), and that
     no AccessDenied / 401 / botocore-internals leak in the response.

This is the architect's transport-smoke for the CVX web UI surfacing
spec (initiative 2026-06-19-cvx-web-ui-surfacing). The grounding-quality
UAT (PO-led) is a separate gate, blocked on `ADP VKB content fill`.

Usage:
    python3 scripts/smoke-test-assistant-chat.py
        --api-url https://<rest-api-id>.execute-api.us-west-2.amazonaws.com/staging
        --tenant michelin
        [--prompt "Hello, can you confirm you're online?"]
        [--vin 1FMCU0F70MUA12345]

Env vars (alternative to flags):
    VSA_API_URL  — base URL of vsa-staging-api
    VSA_TENANT   — tenant id (default: michelin)
    AWS_REGION   — region for Cognito client (default: us-west-2)

Exits 0 on PASS, 1 on FAIL. Stdout is human-readable; stderr is empty
on PASS, contains the failure context on FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from urllib import error as urlerr
from urllib import request as urlreq


def _post_chat(api_url: str, id_token: str, body: dict, timeout_s: int = 60) -> tuple[int, dict, dict]:
    """POST to {api_url}/assistant/chat with the IdToken as Bearer auth.

    Returns (status_code, response_headers, response_body_dict). Raises
    on connection / timeout failures; HTTP 4xx/5xx are returned (not raised).
    """
    url = api_url.rstrip("/") + "/assistant/chat"
    req = urlreq.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {id_token}",
        },
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=timeout_s) as resp:
            return (
                resp.status,
                dict(resp.headers),
                json.loads(resp.read().decode("utf-8")),
            )
    except urlerr.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"_raw": "<non-JSON error body>"}
        return e.code, dict(e.headers or {}), payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api-url", default=os.environ.get("VSA_API_URL", ""))
    parser.add_argument("--tenant", default=os.environ.get("VSA_TENANT", "michelin"))
    parser.add_argument("--prompt", default="Hello, can you confirm you're online and tell me your persona role in one sentence?")
    parser.add_argument("--vin", default=None)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--id-token",
        default=None,
        help=(
            "Emergency override only — prefer VSA_ID_TOKEN env var or the "
            "cached helper. CLI args land in shell history and `ps`."
        ),
    )
    args = parser.parse_args()

    if not args.api_url:
        print("✗ --api-url (or VSA_API_URL env) is required", file=sys.stderr)
        return 1

    print(f"→ API URL:  {args.api_url}")
    print(f"→ Tenant:   {args.tenant}")
    print(f"→ Prompt:   {args.prompt!r}")
    if args.vin:
        print(f"→ VIN:      {args.vin}")

    # IdToken resolution order: CLI flag (emergency) > env var > cached helper.
    # The env-var path is preferred for operator runs because (a) env vars do
    # not appear in shell history, and (b) `ps` on Linux >=4.x scrubs env-var
    # values. The cached helper is the documented happy-path (no secret on
    # the operator's terminal at all).
    id_token: str | None = args.id_token or os.environ.get("VSA_ID_TOKEN")
    if id_token:
        source = "cli" if args.id_token else "env"
        print(f"→ IdToken:  <{source}-supplied>")
    else:
        # Canonical pattern — matches scripts/smoke-test-agentcore.py:50-73.
        # Adds the repo root to sys.path so `agents.supervisor.config` is
        # importable when this script is run directly.
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo_root))
        try:
            from agents.supervisor.config import Config, get_cached_id_token  # noqa: E402
        except ImportError as e:
            print(
                "✗ Could not import agents.supervisor.config. "
                "Provide --id-token or set VSA_ID_TOKEN env var.",
                file=sys.stderr,
            )
            print(f"  Detail: {e}", file=sys.stderr)
            return 1

        cfg = Config.from_env()
        if args.tenant and args.tenant != cfg.tenant_id:
            from dataclasses import replace as _dc_replace
            cfg = _dc_replace(cfg, tenant_id=args.tenant)
        try:
            id_token = get_cached_id_token(cfg)
        except Exception as e:  # noqa: BLE001
            print(
                "✗ get_cached_id_token failed. Provide --id-token or VSA_ID_TOKEN env var.",
                file=sys.stderr,
            )
            print(f"  Detail: {e}", file=sys.stderr)
            return 1
        print(f"→ IdToken:  cached ({len(id_token)} chars)")

    body = {
        "prompt": args.prompt,
        "sessionId": f"smoke-{uuid.uuid4().hex[:12]}",
    }
    if args.vin:
        body["vin"] = args.vin

    print(f"→ POST body: {json.dumps(body)}")

    status, headers, resp_body = _post_chat(args.api_url, id_token, body)

    print(f"→ HTTP {status}")
    print(f"→ Response: {json.dumps(resp_body, default=str)[:500]}")

    # ── Acceptance checks ────────────────────────────────────────────────
    if status == 401:
        print(
            "✗ 401 — token not accepted by vsa-api authorizer. "
            "Confirm EXTRA_USER_POOL_IDS includes the CMS pool (<cms-pool-id>).",
            file=sys.stderr,
        )
        return 1
    if status == 403:
        print(
            "✗ 403 — IAM AccessDenied on invoke_agent_runtime. "
            "Confirm vsa-staging-api stack deploy applied the new InvokeAgentRuntime policy.",
            file=sys.stderr,
        )
        return 1
    if status >= 500:
        print(
            f"✗ 5xx — upstream failure. Body: {resp_body}. "
            "Check CloudWatch logs for vsa-staging-api-assistant-chat Lambda.",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(f"✗ Unexpected status {status}: {resp_body}", file=sys.stderr)
        return 1

    # 200 OK — verify body contract
    if not isinstance(resp_body, dict):
        print(f"✗ Response body is not a dict: {type(resp_body)}", file=sys.stderr)
        return 1
    result = resp_body.get("result")
    if not isinstance(result, str) or not result.strip():
        print("✗ Missing or empty `result` in response body", file=sys.stderr)
        return 1
    if "sessionId" not in resp_body:
        print("✗ Missing `sessionId` in response body", file=sys.stderr)
        return 1

    # Security check: no internal leakage in the body (mirrors security-review
    # cycle 1 Warning + cycle 2 verification).
    body_text = json.dumps(resp_body)
    if "123456789012" in body_text:
        print("✗ Response body leaks the AWS account ID — security regression", file=sys.stderr)
        return 1
    if "arn:aws:bedrock-agentcore" in body_text:
        print("✗ Response body leaks the runtime ARN — security regression", file=sys.stderr)
        return 1

    print()
    print(f"✓ Result (first 200 chars): {result[:200]}")
    print(f"✓ sessionId:  {resp_body.get('sessionId')}")
    if resp_body.get("classification"):
        print(f"✓ classification: {resp_body['classification']}")
    if resp_body.get("citations"):
        print(f"✓ citations: {len(resp_body['citations'])} item(s)")
    print()
    print("🎉 /assistant/chat smoke test passed (transport).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
