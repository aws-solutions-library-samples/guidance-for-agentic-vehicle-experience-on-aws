#!/usr/bin/env python3
"""Smoke-test that the CVX AgentCore role can Retrieve from the ADP KB.

End-to-end proof for Task 7.B.2: confirms the CVX AgentCore execution role
(`AmazonBedrockAgentCoreSDKRuntime-us-west-2-<stage>`) is granted
`bedrock:Retrieve` on the ADP vehicle knowledge base (default `YULHRAUMNU`,
us-east-1) AND that the KB returns an answer for a reference query
(default "DTC P0420").

Role-name shape note: the role is named per the CDK source-of-truth at
`infrastructure/lib/vsa-core-stack.ts`, which constructs the role as
`AmazonBedrockAgentCoreSDKRuntime-${region}-${stage}` (no `-vsa-` segment).
A prior version of this script appended a stray `-vsa-` between region
and stage (e.g. `…-us-west-2-vsa-staging`); that role does not exist in
IAM, so `iam:simulate-principal-policy` raised `NoSuchEntityException`.
Fixed 2026-06-22 (issue 2026-06-22-cvx-demo-blockers-text-kb).

ACCESS PATH (read before trusting a PASS):
    The ADP KB was deployed WITHOUT a cross-account resource policy because
    CVX lives in the SAME AWS account today (resolved at runtime via STS).
    CVX therefore
    reaches the KB purely through its OWN IAM identity-policy grant — there
    is no KB-side principal allow-list. This test exercises exactly that
    grant.

TWO INDEPENDENT PROOFS, both must pass:

  1. ROLE-GRANT PROOF — "is the role allowed to call Retrieve on this KB?"
       Preferred: assume the CVX role via STS and call Retrieve with the
       role's own short-lived credentials (the gold standard; this is what
       happens inside the AgentCore runtime / CI).
       Local fallback: the role's trust policy only allows the
       `bedrock-agentcore.amazonaws.com` service to assume it, so a human
       principal cannot. When assume_role is denied, we fall back to
       `iam:simulate-principal-policy`, which evaluates the role's REAL
       attached identity policy for action `bedrock:Retrieve` on the KB
       ARN. Decision must be `allowed`. This deterministically proves the
       grant without assuming the role — and crucially catches a wrong
       IAM action namespace (e.g. `bedrock-agent-runtime:Retrieve`, which
       is the boto3 client id, NOT the `bedrock:` IAM action namespace).

  2. ANSWER PROOF — "does the KB actually return content?"
       Issue a live Retrieve (with role creds if assumed, else local creds)
       and assert at least one retrieval result comes back.

NOTE on the IAM action: the correct identity-policy action for the Retrieve
API is `bedrock:Retrieve` (and `bedrock:RetrieveAndGenerate`), per
https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-prereq-permissions-general.html
The boto3 service id `bedrock-agent-runtime` is NOT a valid IAM action
prefix; a policy granting `bedrock-agent-runtime:Retrieve` silently grants
nothing real. This script's simulation step is what surfaces that.

Usage:
    python3 scripts/smoke-test-kb-retrieve.py
    python3 scripts/smoke-test-kb-retrieve.py --stage staging --query "DTC P0420"
    python3 scripts/smoke-test-kb-retrieve.py --kb-id YULHRAUMNU --num-results 3

Prereqs:
    - Task 7.A.4 deployed: `cdk deploy vsa-staging-core -c adpKbId=<kb-id>`.
    - ADP KB `<kb-id>` live and synced in us-east-1.
    - boto3 installed in the venv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ADP Glue catalog + KB live in us-east-1 (per ADP foundation-v1 close-out).
ADP_REGION = "us-east-1"
# Same-account v1 topology: the KB is owned by the same account that runs CVX.
# The account id is resolved at runtime via STS get-caller-identity (or the
# --account-id flag / VSA_ADP_ACCOUNT_ID env) so no literal account id is
# inlined here — matches config.build_persona_loader_config() and the
# no-inline discipline in spec 2026-06-18-cvx-adp-kb-wireup.
# CVX AgentCore Runtime + execution role region.
ROLE_REGION = "us-west-2"
DEFAULT_KB_ID = os.environ.get("VSA_ADP_KB_ID") or "YULHRAUMNU"
DEFAULT_QUERY = "DTC P0420"
# The CORRECT IAM action for the Retrieve API (NOT bedrock-agent-runtime:*).
RETRIEVE_ACTION = "bedrock:Retrieve"


def _resolve_account_id(explicit: str | None) -> str:
    """Resolve the AWS account id without inlining a literal.

    Precedence: explicit --account-id / VSA_ADP_ACCOUNT_ID, else STS
    get-caller-identity (the operator's own account, which owns the KB in
    the same-account v1 topology).
    """
    if explicit:
        return explicit
    return boto3.client("sts", region_name=ADP_REGION).get_caller_identity()["Account"]


def _kb_arn(account_id: str, kb_id: str) -> str:
    return f"arn:aws:bedrock:{ADP_REGION}:{account_id}:knowledge-base/{kb_id}"


def _try_assume_role(role_arn: str) -> dict | None:
    """Assume the CVX role; return temp-credential kwargs, or None if not permitted."""
    sts = boto3.client("sts", region_name=ADP_REGION)
    try:
        a = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="cvx-kb-retrieve-smoke",
            DurationSeconds=900,
        )["Credentials"]
        return {
            "aws_access_key_id": a["AccessKeyId"],
            "aws_secret_access_key": a["SecretAccessKey"],
            "aws_session_token": a["SessionToken"],
        }
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "AccessDenied":
            return None
        raise


def _prove_grant_via_simulation(role_arn: str, account_id: str, kb_id: str) -> bool:
    """Evaluate the role's REAL identity policy for bedrock:Retrieve on the KB."""
    iam = boto3.client("iam")
    resp = iam.simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=[RETRIEVE_ACTION],
        ResourceArns=[_kb_arn(account_id, kb_id)],
    )
    results = resp.get("EvaluationResults", [])
    decision = results[0]["EvalDecision"] if results else "no-result"
    print(f"  simulate-principal-policy: action={RETRIEVE_ACTION} -> {decision}")
    if decision == "allowed":
        return True
    print(
        f"  ✗ Role is NOT allowed to call {RETRIEVE_ACTION} on {_kb_arn(account_id, kb_id)}.\n"
        f"    Common cause: the policy grants the boto3 client id\n"
        f"    'bedrock-agent-runtime:Retrieve' instead of the real IAM action\n"
        f"    'bedrock:Retrieve'. Fix the AdpBedrockKbRetrieveCrossAccount\n"
        f"    statement in infrastructure/lib/vsa-core-stack.ts and redeploy.",
        file=sys.stderr,
    )
    return False


def _live_retrieve(creds: dict, kb_id: str, query: str, num_results: int,
                   source_category: str | None = None):
    """Issue a live Retrieve; return (results, elapsed_ms) or raise.

    When `source_category` is given, the retrieval is filtered to documents
    carrying that `source_category` metadata attribute. This filtering exists
    because the KB previously held parallel two-wheeler and four-wheeler corpora
    whose documents shared filenames and structure (`bike-manufacturing/04-sub-assembly.md` vs
    `auto-manufacturing/04-sub-assembly.md`), creating the potential for an unfiltered query about
    vehicle manufacturing to legitimately return the wrong vehicle class —
    this was observed on 2026-08-03 with bike scoring 0.6449 against auto's 0.6031 for
    the same prompt. The runtime sets the equivalent filter per tenant via
    VSA_ACQUIRE_KB_SOURCE_CATEGORY / VSA_DISCOVER_KB_SOURCE_CATEGORY.
    """
    client = boto3.client("bedrock-agent-runtime", region_name=ADP_REGION, **creds)
    vector_cfg: dict = {"numberOfResults": num_results}
    if source_category:
        vector_cfg["filter"] = {
            "equals": {"key": "source_category", "value": source_category}
        }
    t0 = time.time()
    resp = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_cfg},
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    return resp.get("retrievalResults") or [], elapsed_ms


# ── Audit-set mode ──────────────────────────────────────────────────────────

def _load_audit_set(path: str) -> dict:
    """Load and minimally validate the audit set JSON."""
    p = Path(path)
    if not p.exists():
        print(f"✗ Audit set not found: {path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(p.read_text())
    if "prompts" not in data or not isinstance(data["prompts"], list):
        print(f"✗ Audit set missing 'prompts' list: {path}", file=sys.stderr)
        sys.exit(1)
    return data


def _dry_run_audit(audit_set: dict) -> int:
    """Print all prompts and their expected documents without calling AWS."""
    prompts = audit_set["prompts"]
    threshold = audit_set.get("threshold", 0.55)
    print(f"── Audit set dry run ── {len(prompts)} prompts  threshold={threshold}")
    print()
    # Group by category for readability
    by_cat: dict[str, list] = {}
    for entry in prompts:
        cat = entry.get("category", "unknown")
        by_cat.setdefault(cat, []).append(entry)

    for cat in sorted(by_cat):
        entries = by_cat[cat]
        print(f"  [{cat}]  ({len(entries)} prompts)")
        for e in entries:
            pid = e.get("id", "?")
            prompt = e.get("prompt", "")
            expected = e.get("expected_doc", "")
            print(f"    {pid:20s}  {expected}")
            print(f"                        prompt: {prompt!r}")
        print()

    # Summary
    counts: dict[str, int] = {}
    for e in prompts:
        cat = e.get("category", "unknown")
        counts[cat] = counts.get(cat, 0) + 1
    print("Prompt count by category:")
    for cat in sorted(counts):
        print(f"  {cat}: {counts[cat]}")
    print(f"  TOTAL: {len(prompts)}")
    print()
    print("✓ Dry run complete — no AWS calls made.")
    return 0


def _match_top_hit(results: list, expected_doc: str) -> tuple[bool, float, str]:
    """Return (matched, top_score, top_uri) comparing top result to expected_doc.

    `expected_doc` is a LOCAL repo path, e.g.
    `corpora/meridian_lineup_narrative/01-windrose.md`. The KB's S3 layout is
    different in two ways: a different parent prefix
    (`knowledge/vehicle_knowledge_base/sources/`) and hyphenated category
    directories (`meridian-lineup-narrative`) rather than the underscored local
    ones. So a naive `s3_uri.endswith(expected_doc)` can never match — it
    reported 31/31 FAIL on a corpus that was actually correctly ingested and
    retrievable.

    Matching on basename alone would be wrong in the other direction: filenames
    are NOT unique across categories. During pre-Meridian staging, the two-wheeler
    and four-wheeler manufacturing corpora both contained filenames like
    `01-order-placed.md`, so basename-only matching would have let a two-wheeler
    document satisfy a four-wheeler prompt. This strict category+basename matching
    exists to prevent that class of false match.

    So compare on `<hyphenated-category>/<basename>`, which is strict on both
    axes.
    """
    if not results:
        return False, 0.0, ""
    top = results[0]
    top_score = top.get("score", 0.0)
    loc = top.get("location") or {}
    s3_uri = ((loc.get("s3Location") or {}).get("uri")) or ""

    parts = expected_doc.rstrip("/").split("/")
    basename = parts[-1]
    category_dir = parts[-2] if len(parts) >= 2 else ""
    # Local corpora dirs use underscores; the S3 source prefixes use hyphens.
    s3_category = category_dir.replace("_", "-")
    needle = f"/{s3_category}/{basename}" if s3_category else f"/{basename}"
    matched = s3_uri.endswith(needle)
    return matched, top_score, s3_uri


def _run_audit(audit_set: dict, creds: dict, kb_id: str, num_results: int,
               category_filter: bool = False) -> int:
    """Run the full audit set against the live KB and print a results table.

    `category_filter` mirrors the runtime's per-tenant `source_category` filter.
    Run BOTH ways and record both outcomes: unfiltered shows how the corpora
    compete with each other, filtered shows what a correctly-configured tenant
    actually sees.
    """
    prompts = audit_set["prompts"]
    threshold = audit_set.get("threshold", 0.55)
    mode = "FILTERED by source_category" if category_filter else "UNFILTERED"
    print(f"── Audit set run ── {len(prompts)} prompts  kb={kb_id}  threshold={threshold}")
    print(f"── Mode: {mode}")
    print()

    col_w = 22
    header = f"  {'ID':<{col_w}}  {'Score':>6}  {'Pass':>4}  {'Top URI'}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    passes = 0
    fails = 0
    errors = 0
    near_miss_margin = 0.02  # flag scores within this margin above threshold

    for entry in prompts:
        pid = entry.get("id", "?")
        prompt = entry.get("prompt", "")
        expected_doc = entry.get("expected_doc", "")
        try:
            results, elapsed_ms = _live_retrieve(
                creds, kb_id, prompt, num_results,
                source_category=entry.get("category") if category_filter else None,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "?")
            print(f"  {pid:<{col_w}}  {'ERR':>6}       ✗  Retrieve failed: {code}")
            errors += 1
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  {pid:<{col_w}}  {'ERR':>6}       ✗  {exc}")
            errors += 1
            continue

        matched, top_score, top_uri = _match_top_hit(results, expected_doc)
        score_above = top_score >= threshold
        ok = matched and score_above

        score_str = f"{top_score:.4f}"
        pass_str = "✓" if ok else "✗"
        short_uri = top_uri.split("/")[-1] if top_uri else "(no results)"
        warn = ""
        if ok and (top_score - threshold) <= near_miss_margin:
            warn = f"  ⚠ near-miss (margin={top_score - threshold:.4f})"

        print(f"  {pid:<{col_w}}  {score_str:>6}  {pass_str:>4}  {short_uri}{warn}")
        if not matched:
            exp_name = expected_doc.split("/")[-1]
            print(f"  {'':>{col_w}}        Expected: {exp_name}")
        if not score_above and results:
            print(f"  {'':>{col_w}}        Score {top_score:.4f} < threshold {threshold} — rewrite doc to share query vocabulary")

        if ok:
            passes += 1
        else:
            fails += 1

    print()
    print(f"Results: {passes} PASS  {fails} FAIL  {errors} ERROR  of {len(prompts)} prompts")

    if fails > 0 or errors > 0:
        print(
            f"\n✗ Audit FAILED — {fails} prompt(s) did not return the expected document "
            f"above the {threshold} threshold.",
            file=sys.stderr,
        )
        print(
            "  Fix: rewrite the document so it shares the vocabulary of the failing query.",
            file=sys.stderr,
        )
        print("  Do NOT lower the threshold to make a document pass.", file=sys.stderr)
        return 1

    print(f"\n🎉 Audit PASSED — all {passes} prompts returned expected top hits ≥ {threshold}.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--stage", default="staging", help="Deployment stage (default: staging)")
    p.add_argument("--kb-id", default=DEFAULT_KB_ID,
                   help=f"ADP Bedrock KB id (default: env VSA_ADP_KB_ID or {DEFAULT_KB_ID})")
    p.add_argument("--query", default=DEFAULT_QUERY,
                   help=f'Retrieval query text (default: "{DEFAULT_QUERY}")')
    p.add_argument("--num-results", type=int, default=5,
                   help="numberOfResults for the vector search (default: 5)")
    p.add_argument("--account-id", default=os.environ.get("VSA_ADP_ACCOUNT_ID"),
                   help="AWS account id owning the KB + role (default: env "
                        "VSA_ADP_ACCOUNT_ID, else resolved via STS get-caller-identity)")
    p.add_argument(
        "--audit-set",
        metavar="PATH",
        default=None,
        help="Path to a meridian_audit_set.json. Runs every prompt against the KB "
             "and asserts top hit is the expected document with score >= threshold. "
             "Combine with --dry-run to list prompts without calling AWS.",
    )
    p.add_argument(
        "--category-filter",
        action="store_true",
        default=os.environ.get("VSA_KB_SOURCE_CATEGORY_FILTER", "").lower()
        in ("1", "true", "yes"),
        help="With --audit-set: filter each retrieval to the prompt's own "
             "source_category, mirroring the runtime's per-tenant filter. "
             "Defaults from VSA_KB_SOURCE_CATEGORY_FILTER. Run the audit BOTH "
             "ways: unfiltered shows how the two-wheeler and four-wheeler "
             "corpora compete, filtered shows what a configured tenant sees.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="With --audit-set: list all prompts and expected documents without "
             "calling AWS. Exits 0 when the audit set is well-formed.",
    )
    args = p.parse_args()

    # ── Audit-set mode ──
    if args.audit_set:
        audit_set = _load_audit_set(args.audit_set)
        if args.dry_run:
            return _dry_run_audit(audit_set)
        # Live audit run — resolve account, role, and credentials first.
        account_id = _resolve_account_id(args.account_id)
        role_arn = (
            f"arn:aws:iam::{account_id}:role/"
            f"AmazonBedrockAgentCoreSDKRuntime-{ROLE_REGION}-{args.stage}"
        )
        role_creds = _try_assume_role(role_arn)
        retrieve_creds = role_creds if role_creds is not None else {}
        return _run_audit(audit_set, retrieve_creds, args.kb_id, args.num_results,
                          category_filter=args.category_filter)

    # ── Original single-query smoke test (unchanged) ──
    account_id = _resolve_account_id(args.account_id)
    role_arn = (
        f"arn:aws:iam::{account_id}:role/"
        f"AmazonBedrockAgentCoreSDKRuntime-{ROLE_REGION}-{args.stage}"
    )

    print(f"role:        {role_arn}")
    print(f"kb id:       {args.kb_id}")
    print(f"kb arn:      {_kb_arn(account_id, args.kb_id)}")
    print(f"query:       {args.query!r}")
    print(f"access path: CVX role IAM grant (KB has no cross-account resource policy)")
    print()

    # ── PROOF 1: role-grant. Prefer assume-role; fall back to simulation. ──
    print("PROOF 1 — role-grant")
    role_creds = _try_assume_role(role_arn)
    if role_creds is not None:
        print("  ✓ assumed the CVX role (live credentials) — gold-standard path")
        grant_ok = True
        retrieve_creds = role_creds
        grant_method = "assumed-role + live Retrieve"
    else:
        print("  • assume_role not permitted for this principal "
              "(role trusts only bedrock-agentcore.amazonaws.com)")
        print("  • falling back to IAM policy simulation of the role's real policy")
        grant_ok = _prove_grant_via_simulation(role_arn, account_id, args.kb_id)
        retrieve_creds = {}  # local creds for the answer proof
        grant_method = "simulate-principal-policy (bedrock:Retrieve -> allowed)"
    print()

    if not grant_ok:
        print("✗ FAILED at PROOF 1 — the CVX role does NOT have a working "
              "Retrieve grant on this KB.", file=sys.stderr)
        return 1

    # ── PROOF 2: the KB returns an answer. ──
    print("PROOF 2 — KB answer")
    try:
        results, elapsed_ms = _live_retrieve(
            retrieve_creds, args.kb_id, args.query, args.num_results
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        print(f"  ✗ Retrieve failed: {code} — {e}", file=sys.stderr)
        if code == "ResourceNotFoundException":
            print(f"    KB '{args.kb_id}' not found in {ADP_REGION} — deployed/synced?",
                  file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ Retrieve raised unexpectedly: {e}", file=sys.stderr)
        return 1

    answer_via = "role creds" if role_creds else "local creds"
    print(f"  Retrieve via {answer_via}: {len(results)} results in {elapsed_ms}ms")
    for i, r in enumerate(results[:3]):
        snippet = (r.get("content", {}).get("text", "") or "").replace("\n", " ")
        score = r.get("score", 0)
        # Surface the source URI so the live-KB signal is visible (the demo
        # preflight harness greps stdout for `s3://` to flip from WARN to GREEN
        # — without the URI, a clean live-KB hit looks like a local-corpora
        # fallback). Bedrock KB results carry the source S3 URI under
        # location.s3Location.uri (Retrieve API; see AWS Bedrock KB docs).
        loc = r.get("location") or {}
        s3_uri = ((loc.get("s3Location") or {}).get("uri")) or ""
        print(f"    [{i}] score={score:.4f}  {snippet[:120]}")
        if s3_uri:
            print(f"        source: {s3_uri}")
    print()

    if not results:
        print("  ✗ KB returned ZERO results — IAM/connectivity OK but no content "
              "matched. Check the KB is synced and the query matches indexed data.",
              file=sys.stderr)
        return 1

    # ── Verdict. ──
    print("🎉 KB Retrieve smoke test PASSED.")
    print(f"   PROOF 1 (role grant): {grant_method}")
    print(f"   PROOF 2 (KB answer):  {len(results)} results for {args.query!r}")
    print(f"   Proven: role reaches KB {args.kb_id} via its own IAM grant, "
          f"with NO cross-account resource policy.")
    if role_creds is None:
        print("   Note: answer proof used local creds because a human principal "
              "cannot assume the runtime-only role; the grant itself was proven "
              "against the role's real policy via simulation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
