#!/usr/bin/env python3
"""Live-verify that the bidi voice runtime accepts a SigV4 WebSocket upgrade.

Reproduces the exact transport the iOS app uses, which is the one thing no unit test
can reach: a stub cannot fail the way a service fails, and the 2026-09-21 outage was
invisible to a 100%-green suite because the runtime container crashed at *import*,
server-side, while every local test passed.

What failed, and what this catches
----------------------------------
`issues/2026-09-21-bidi-voice-import-error-unpinned-sdk/`: an unpinned
`aws_sdk_bedrock_runtime>=0.0.2` resolved to 0.11.0 on redeploy, which renamed
`BedrockRuntimeClient`. `strands.experimental.bidi.models.nova_sonic` imports the old
name at module scope, so the container died before serving a request. The client saw
only `NSURLErrorDomain -1011 "bad response from the server"` -- a generic transport
error that named nothing -- and `get-agent-runtime` reported `READY` for 11 days.

This script turns that silent failure into an explicit HTTP status. A non-101 response
means the runtime is not serving, whatever the control plane claims.

Usage
-----
    LIVE_TESTS=1 .venv/bin/python scripts/live-verify-bidi-ws-upgrade.py
    LIVE_TESTS=1 .venv/bin/python scripts/live-verify-bidi-ws-upgrade.py --stage staging

Exit codes: 0 upgrade accepted (101) · 1 refused or error · 2 skipped (no LIVE_TESTS)

Credentials
-----------
Uses the ambient AWS identity, NOT Cognito. That is deliberate and it bounds what this
proves: it exercises the transport and the container, not the iOS app's identity-pool
path. The identity-pool role's grants were separately verified intact during the
incident (`report.md` § Investigation), and conflating the two is how the original
misdiagnosis nearly happened. If you need the full client path, drive it from the app.

Anti-vacuity: the caller must hold `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`.
If it does not, a 403 here means "your credentials", not "the runtime is broken" -- the
script says which, rather than reporting a generic failure.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse

REGION = "us-west-2"
SERVICE = "bedrock-agentcore"
# ACCOUNT is resolved at runtime via STS, not baked here.
# The same credential block the script already uses for `get_caller_identity`
# (see main()) is the source of truth -- keeping a literal here would race with
# any account migration and would fail the publish scanner.
# Override via AWS_ACCOUNT_ID if STS is unavailable in your environment.

# Runtime ids per stage. Sourced from .bedrock_agentcore.yaml; staging is the only
# CVX environment as of the 2026-09-02 prod teardown.
RUNTIME_IDS = {
    "staging": "vsa_supervisor_bidi_staging-cF6Xnf7PcX",
}


def _presigned_ws_url(runtime_arn: str, expires: int = 300) -> str:
    """SigV4 query-presign the runtime's /ws path, exactly as the iOS client does."""
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    # The ARN is a single path segment, so every ':' and '/' inside it must be escaped
    # -- `safe=''`. The iOS failure URL shows the same encoding (arn%3Aaws%3A...).
    quoted_arn = urllib.parse.quote(runtime_arn, safe="")
    https_url = f"https://{SERVICE}.{REGION}.amazonaws.com/runtimes/{quoted_arn}/ws"

    session = Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise SystemExit("No AWS credentials resolved. Configure a profile and retry.")

    request = AWSRequest(method="GET", url=https_url)
    SigV4QueryAuth(
        credentials.get_frozen_credentials(), SERVICE, REGION, expires=expires
    ).add_auth(request)

    return request.url.replace("https://", "wss://", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="staging", choices=sorted(RUNTIME_IDS))
    args = parser.parse_args()

    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run (this hits a deployed runtime).")
        return 2

    runtime_id = RUNTIME_IDS[args.stage]

    # Resolve the AWS account ID at runtime from STS (same credential the script uses
    # throughout). This avoids baking a literal account ID into a file that ships to
    # the public mirror.  Override via AWS_ACCOUNT_ID if STS is not reachable.
    import boto3

    account = os.environ.get("AWS_ACCOUNT_ID")
    try:
        who = boto3.client("sts", region_name=REGION).get_caller_identity()
        account = account or who["Account"]
        print(f"caller   : {who['Arn']}")
    except Exception as exc:  # pragma: no cover
        print(f"caller   : <could not resolve: {exc}>")

    if not account:
        print(
            "ERROR: could not resolve AWS account ID. "
            "Set AWS_ACCOUNT_ID=<12-digit-id> and retry.",
            file=sys.stderr,
        )
        return 1

    runtime_arn = f"arn:aws:{SERVICE}:{REGION}:{account}:runtime/{runtime_id}"

    print(f"stage    : {args.stage}")
    print(f"runtime  : {runtime_id}")

    url = _presigned_ws_url(runtime_arn)
    # Never print the signed URL: it carries X-Amz-Security-Token.
    print(f"target   : wss://{SERVICE}.{REGION}.amazonaws.com/runtimes/<arn>/ws")
    print("           (signed URL withheld — it embeds a session token)")
    print()

    from websockets.sync.client import connect

    try:
        with connect(url, open_timeout=30, close_timeout=5) as ws:
            print("RESULT   : 101 Switching Protocols — upgrade ACCEPTED")
            print("           The container is running and serving the WS surface.")
            print("           This is the state that was broken 2026-09-11 → 2026-09-21.")
            try:
                ws.close()
            except Exception:
                pass
        return 0

    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)

        print(f"RESULT   : upgrade REFUSED — {type(exc).__name__}: {exc}")
        if status is not None:
            print(f"           HTTP status: {status}")
        print()

        if status == 403:
            print("403 is ambiguous. Either the caller lacks")
            print("bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream on this")
            print("runtime, or the runtime rejected the signature. Check the caller ARN")
            print("above against the policy before concluding the runtime is broken.")
        elif status in (500, 502, 503):
            print("A 5xx here is the 2026-09-21 signature: the front door is up but the")
            print("container is not serving. Check for an import-time crash:")
            print()
            print(f"  aws logs filter-log-events --region {REGION} \\")
            print(
                f"    --log-group-name '/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT' \\"
            )
            print(
                "    --start-time $(python3 -c \"import time;print(int((time.time()-900)*1000))\") \\"
            )
            print("    --filter-pattern '\"cannot import name\"' --query 'length(events)'")
            print()
            print("Non-zero means a dependency broke the import chain. Do NOT trust")
            print("get-agent-runtime's status field — it reported READY throughout the")
            print("11-day outage.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
