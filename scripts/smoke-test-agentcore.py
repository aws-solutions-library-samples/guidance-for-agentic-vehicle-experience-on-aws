#!/usr/bin/env python3
"""Smoke-test the supervisor agent against the deployed /triage endpoint.

Builds the supervisor IN-PROCESS (the same path `python -m
agents.supervisor.cli` uses — the demo's "rock-solid text CLI" surface),
points it at the deployed REST API via VSA_API_BASE, and asserts that a
P0-triggering prompt + VIN drives the supervisor to call the deterministic
`triage` classifier and stamp the resulting `classification` (P0–P3) onto
the SessionContext.

This is what the demo's text path actually exercises — and it covers every
surface the demo touches:
  - Cognito IdToken auth (cached or refreshed via VSA_DEMO_PASSWORD)
  - Bedrock InvokeModel for the supervisor LLM (Claude Sonnet 4-6 by
    default, resolved from VSA_SUPERVISOR_MODEL_ID)
  - Triage tool → POST {VSA_API_BASE}/triage → deterministic classifier
  - Strands tool-routing (the supervisor must choose to call `triage`
    before narrating any criticality, per VIRTUAL_SERVICE_AGENT.md §2)

Why we don't invoke a deployed AgentCore runtime here (history): an earlier
version of this smoke called `bedrock-agentcore:InvokeAgentRuntime` on the
agent ARN resolved from `.bedrock_agentcore.yaml`. That picked the legacy
us-east-1 `vsa_supervisor` text runtime, which has no `VSA_API_BASE` env
set on its container — so the runtime's triage tool POSTs to the bare path
"/triage" and raises `requests.exceptions.MissingSchema` (the same root
cause flagged in `issues/2026-06-19-cvx-staging-kb-grounding-local-fallback`
Finding 2). There is no staging text runtime; only the bidi runtime is
deployed for staging (us-west-2) and it speaks WebSockets, not the JSON
HTTP entrypoint. The local in-process supervisor is the demo's actual
text path, so this smoke now exercises exactly that.

Usage:
    python3 scripts/smoke-test-agentcore.py
    python3 scripts/smoke-test-agentcore.py --prompt "My brake warning light came on"
    python3 scripts/smoke-test-agentcore.py --vin 1HGBH41JXMN000024 --tenant meridian-fleet

Required env (typically sourced from .env):
    VSA_API_BASE            REST API base URL (auto-resolved from
                            vsa-staging-api CFN output if unset)
    VSA_TENANT_ID           Tenant slug (e.g. "vehicle-owner"; default: vehicle-owner)
    VSA_COGNITO_POOL_ID     For Cognito sign-in if no cached JWT
    VSA_COGNITO_CLIENT_ID   For Cognito sign-in if no cached JWT
    VSA_DEMO_PASSWORD       For Cognito sign-in if no cached JWT
    VSA_DEFAULT_VIN         VIN to triage (default: a Meridian Fleet-seeded VIN)
    AWS_REGION              Region for Cognito + Bedrock (default us-east-1)

A cached JWT in `~/.vsa/tokens.json` is sufficient — the script falls back
to USER_PASSWORD_AUTH only if no cache entry exists for the tenant.

Exit codes:
    0  classification ∈ {P0, P1, P2, P3} stamped on SessionContext.
    1  classification missing or not a valid severity.
    2  config / auth / Bedrock / network failure (with a clear message).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the agents/supervisor package importable from here.
sys.path.insert(0, str(REPO_ROOT))

from agents.supervisor.config import (  # noqa: E402
    Config,
    clear_cached_tokens,
    get_cached_id_token,
)
from agents.supervisor.session import SessionContext  # noqa: E402
from agents.supervisor.supervisor import build_supervisor  # noqa: E402

VALID_CLASSIFICATIONS = {"P0", "P1", "P2", "P3"}


def _auto_resolve_api_base() -> str | None:
    """Resolve VSA_API_BASE from the vsa-staging-api CFN output (Makefile pattern).

    Mirrors the deploy-staging logic in the Makefile. Returns the trimmed URL
    or None if CFN doesn't have it (e.g. caller is unauthenticated, the stack
    isn't deployed, or the output key isn't present).
    """
    try:
        out = subprocess.run(
            [
                "aws", "cloudformation", "describe-stacks",
                "--stack-name", "vsa-staging-api",
                "--region", "us-west-2",
                "--query",
                "Stacks[0].Outputs[?OutputKey=='RestApiUrl'].OutputValue",
                "--output", "text",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    val = (out.stdout or "").strip()
    if not val or val.lower() == "none":
        return None
    return val.rstrip("/")


def _extract_text(result) -> str:
    """Pull the final text response out of a Strands AgentResult."""
    msg = getattr(result, "message", None)
    if msg is None:
        return str(result)
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not content:
        return str(msg)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts) if parts else str(msg)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--prompt",
        default="Temperature gauge just spiked.",
        help='Triage-triggering prompt (default: "Temperature gauge just spiked.")',
    )
    parser.add_argument("--vin", default=None, help="VIN to seed (default: VSA_DEFAULT_VIN)")
    parser.add_argument("--tenant", default=None, help="Tenant slug (default: VSA_TENANT_ID)")
    parser.add_argument(
        "--force-reauth",
        action="store_true",
        help="Wipe ~/.vsa/tokens.json and re-fetch a JWT before invoking",
    )
    args = parser.parse_args()

    if args.force_reauth:
        clear_cached_tokens()

    # Auto-resolve VSA_API_BASE from CFN if not already in env (matches the
    # deploy-staging Makefile target). Sets it in os.environ BEFORE
    # Config.from_env() so the resolved value flows through cleanly.
    if not os.environ.get("VSA_API_BASE"):
        resolved = _auto_resolve_api_base()
        if resolved:
            os.environ["VSA_API_BASE"] = resolved
            print(f"→ VSA_API_BASE auto-resolved from CFN: {resolved}")

    try:
        cfg = Config.from_env()
    except KeyError as e:
        print(f"✗ Config error: {e}", file=sys.stderr)
        print(
            "  Set the required env var (typically by sourcing .env) and re-run.",
            file=sys.stderr,
        )
        return 2

    if not cfg.api_base:
        print(
            "✗ VSA_API_BASE is empty after resolution. The triage tool needs a"
            " scheme'd URL or it raises requests.MissingSchema. Either set"
            " VSA_API_BASE explicitly, or ensure the vsa-staging-api stack"
            " is deployed in us-west-2 with a 'RestApiUrl' output.",
            file=sys.stderr,
        )
        return 2

    tenant_id = args.tenant or cfg.tenant_id
    vin = args.vin or cfg.default_vin or "1HGBH41JXMN000024"  # Meridian Fleet demo VIN

    print(f"→ Tenant:    {tenant_id}")
    print(f"→ API base:  {cfg.api_base}")
    print(f"→ Model:     {cfg.supervisor_model_id}")
    print(f"→ Region:    {cfg.aws_region}")
    print(f"→ VIN:       {vin}")
    print(f"→ Prompt:    {args.prompt!r}")

    try:
        jwt = get_cached_id_token(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"✗ Failed to obtain Cognito IdToken: {e}", file=sys.stderr)
        print(
            "  If the cache is stale/expired, run with --force-reauth (and"
            " ensure VSA_DEMO_PASSWORD is set so USER_PASSWORD_AUTH can run).",
            file=sys.stderr,
        )
        return 2
    print(f"→ JWT len:   {len(jwt)}")

    ctx = SessionContext.new(
        tenant_id=tenant_id,
        jwt=jwt,
        persona_id="fleet_driver",
        vin=vin,
    )
    print(f"→ Session:   {ctx.session_id}")
    print()

    try:
        agent = build_supervisor(ctx, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"✗ Failed to build supervisor: {e}", file=sys.stderr)
        return 2

    t0 = time.time()
    try:
        result = agent(args.prompt)
    except Exception as e:  # noqa: BLE001
        print(f"✗ Agent invocation failed: {e}", file=sys.stderr)
        return 2
    elapsed = time.time() - t0

    print(f"→ Elapsed:   {elapsed:.2f}s")

    classification = ctx.triage_classification
    decision_id = ctx.triage_decision_id
    narration = _extract_text(result)

    print(f"✓ Classification: {classification}")
    print(f"✓ Decision id:    {decision_id}")
    print(f"✓ Narration (first 200 chars): {narration[:200]}")

    if classification not in VALID_CLASSIFICATIONS:
        print(
            f"✗ Unexpected classification: {classification!r} "
            f"(expected one of {sorted(VALID_CLASSIFICATIONS)}). The supervisor"
            f" did not call the triage classifier, or the classifier returned"
            f" an out-of-band value.",
            file=sys.stderr,
        )
        return 1

    print()
    print("🎉 Smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
