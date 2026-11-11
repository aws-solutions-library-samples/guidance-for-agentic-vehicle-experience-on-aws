"""POST /acquire/leads  — create a lead record
GET  /acquire/leads/{leadId} — read back a lead record

Lead capture is feature-flagged OFF by default (per spec § Constraints:
"No PII by default — ships feature-flagged OFF"). When the flag is off,
POST returns 404 rather than 500.

On a show floor, lead data is real attendee contact info with consent
and retention obligations, so the flag must be explicitly enabled by an
operator before the endpoint becomes live.

Tenant clamp:
  POST — body.tenantId must match claim tenantId.
  GET  — the stored lead's tenantId must match claim tenantId.

Env vars:
    VSA_ACQUIRE_LEADS_TABLE            (default: vsa-dev-acquire-leads)
    VSA_ACQUIRE_LEAD_CAPTURE_ENABLED   (default: false / absent)
    AWS_REGION                         (default: us-east-1)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_LEADS_TABLE = os.environ.get("VSA_ACQUIRE_LEADS_TABLE", "vsa-dev-acquire-leads")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def _lead_capture_enabled() -> bool:
    """Read the feature-flag from the environment at call time (not at import)."""
    val = os.environ.get("VSA_ACQUIRE_LEAD_CAPTURE_ENABLED", "false").strip().lower()
    return val in ("1", "true", "yes", "on")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _idempotency_basis(body: dict[str, Any]) -> str:
    """Wall-clock-independent basis for the leadId hash.

    Prefers an explicit caller-supplied ``idempotencyKey``; otherwise derives one
    from the fields that define the lead. Either way the result is a pure function
    of the request, so a retried POST resolves to the same leadId.
    """
    explicit = body.get("idempotencyKey")
    if isinstance(explicit, str) and explicit.strip():
        return f"key:{explicit.strip()}"
    material = {
        "contactInfo": body.get("contactInfo"),
        "modelId": body.get("modelId"),
        "savedConfig": body.get("savedConfig"),
        "discoverSessionId": body.get("discoverSessionId"),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return f"payload:{canonical}"


def _seed_digest(tenant_id: str, session_id: str, basis: str) -> str:
    """Untruncated digest of the FULL leadId seed, stored on the item.

    Covers the same inputs as the leadId itself — `tenant|session|basis`, not just
    `basis`. Digesting the basis alone left one collision class the comparison could
    not see: two requests from different tenants or sessions whose basis happened to
    match would collide in the truncated leadId and then compare EQUAL on the digest,
    reproducing the false replay this exists to prevent (Cycle 2 Suggestion).

    Review Cycle 1 W2: the leadId hash is truncated to 8 hex chars (32 bits) in a
    namespace shared by every tenant within one UTC day, so two different requests can
    map to one leadId — ~1.2% at 10k leads/day, ~50% near 77k. Without a stored digest,
    `ConditionalCheckFailedException` cannot distinguish "the same request, retried"
    from "a different request that collided", and the second was being reported as a
    successful idempotent replay: the caller got a target_ref for a lead that is not
    theirs, and their own lead was never stored.

    Not truncated, so comparing it is decisive. Internal — see _INTERNAL_FIELDS.

    The tenant clamp on GET is what keeps a collision a correctness bug rather than a
    disclosure one: the second tenant receives a leadId they cannot read (403).
    """
    return hashlib.sha256(f"{tenant_id}|{session_id}|{basis}".encode()).hexdigest()


def _make_lead_id(tenant_id: str, session_id: str, captured_at: datetime, basis: str) -> str:
    """Generate a leadId from (tenantId, sessionId, idempotency basis).

    `captured_at` contributes ONLY the human-readable date prefix and is
    deliberately absent from the hash seed. It used to be in the seed at
    second-granularity, which made the idempotency window 1 second while
    `RetryPolicy.backoff_base_ms` is 1000 — so the first retry landed in the next
    second every time and minted a duplicate lead. See
    issues/2026-09-20-acquire-lead-id-not-idempotent/.

    Residual, accepted deliberately: a retry that crosses a UTC midnight gets a
    different date prefix, hence a different key, which the conditional write
    cannot detect. Worst-case retry sequence is ~6s, so the exposure is ~6 seconds
    per day (~0.007% of retries). Removing the date prefix would close it, at the
    cost of leaving one table carrying two leadId formats — judged worse than the
    residual. `capturedAt` remains on the item for date queries.
    """
    ts = captured_at.strftime("%Y%m%d")
    seed = f"{tenant_id}|{session_id}|{basis}"
    hex_suffix = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"LEAD-{ts}-{hex_suffix}"


# Fields returned to the client for a lead read-back
_CLIENT_FIELDS = frozenset({
    "leadId",
    "tenantId",
    "sessionId",
    "capturedAt",
    "contactInfo",
    "modelId",
    "discoverSessionId",
    # Review Cycle 1 W1: without this, POST accepted and persisted savedConfig but
    # GET stripped it, so following target_ref=leadId through this API returned a
    # lead with no configuration — reproducing the exact "owner's approved
    # configuration is lost" outcome that RetailExecutor's MissingSavedConfig guard
    # exists to prevent, one hop downstream. Not a privacy widening: this same
    # allowlist already exposes contactInfo, which is strictly more sensitive.
    "savedConfig",
})

#: Internal fields that must NEVER reach a client. `idempotencyDigest` is a hash of
#: the request basis, used to tell a true replay from a leadId collision; exposing it
#: would let a caller probe which basis produced an existing lead.
_INTERNAL_FIELDS = frozenset({"idempotencyDigest"})


def _public_lead(item: dict) -> dict:
    """Return only client-visible fields from a raw DDB lead item."""
    raw = _to_jsonable(item)
    return {k: raw[k] for k in _CLIENT_FIELDS if k in raw}


def _handle_post(event: dict, claims: dict) -> dict:
    """Handle POST /acquire/leads — create a lead."""
    # ── Feature flag ──────────────────────────────────────────────────────
    if not _lead_capture_enabled():
        return _resp(404, {"error": "Lead capture is not enabled"})

    # ── Parse request body ────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, ValueError):
        return _resp(400, {"error": "Body must be valid JSON"})

    # ── Validate required fields ──────────────────────────────────────────
    tenant_id_body = body.get("tenantId")
    if not tenant_id_body:
        return _resp(400, {"error": "Missing required field: tenantId"})

    session_id = body.get("sessionId") or ""

    # ── savedConfig shape ─────────────────────────────────────────────────
    # Review Cycle 1 W4. RetailExecutor forwards this field opaquely and MUST keep
    # doing so (it must not read inside a payload that may carry price or disclosure
    # strings), which makes this endpoint the field's only trust boundary. F2 was
    # about two writers disagreeing on the SHAPE of one concept; accepting the key
    # while tolerating any shape would agree on the name and not the shape.
    saved_config = body.get("savedConfig")
    if saved_config is not None and not isinstance(saved_config, dict):
        return _resp(400, {
            "error": "savedConfig must be an object "
                     f"(got {type(saved_config).__name__})"
        })

    # ── Tenant clamp ──────────────────────────────────────────────────────
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not claim_tenant or tenant_id_body != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    # ── Write lead ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    basis = _idempotency_basis(body)
    basis_digest = _seed_digest(claim_tenant, session_id, basis)
    lead_id = _make_lead_id(claim_tenant, session_id, now, basis)
    captured_at_iso = now.isoformat()

    item: dict[str, Any] = {
        "leadId": lead_id,
        "tenantId": claim_tenant,
        "capturedAt": captured_at_iso,
        # Internal; stripped from every client response. See _seed_digest.
        "idempotencyDigest": basis_digest,
    }
    if session_id:
        item["sessionId"] = session_id
    if body.get("contactInfo"):
        item["contactInfo"] = body["contactInfo"]
    if body.get("modelId"):
        item["modelId"] = body["modelId"]
    if body.get("savedConfig"):
        # F2 fix (spec 2026-09-20-avx-acquire-configurator-target): the Strands tool
        # `agents/supervisor/tools/lead_capture.py` has always written a nested
        # `savedConfig` {preferredCategory, preferredModelId}, while this endpoint
        # accepted only a flat `modelId` and no savedConfig at all — two writers
        # disagreeing on the shape of one concept in one table, which a reader
        # crossing both cannot reconcile. Accepting it here makes the endpoint agree
        # with the tool.
        #
        # Flat `modelId` above is retained deliberately for the dual-read window; it
        # is NOT derived from savedConfig, because synthesising one from the other
        # would invent a value the caller did not send. Convergence (readers move to
        # savedConfig, flat modelId retired) is tracked as a follow-on, not done here.
        item["savedConfig"] = body["savedConfig"]
    if body.get("discoverSessionId"):
        item["discoverSessionId"] = body["discoverSessionId"]

    try:
        _get_ddb().Table(_LEADS_TABLE).put_item(
            Item=item,
            # Makes the retry a no-op by construction rather than by winning a race
            # against a clock. Without this, an id collision would silently REPLACE
            # the stored lead.
            ConditionExpression="attribute_not_exists(leadId)",
        )
        log.info("api-acquire-leads: created leadId=%s tenantId=%s", lead_id, claim_tenant)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # A leadId already exists. Two very different causes, and reporting the
            # second as the first is Review Cycle 1 W2:
            #   (a) the SAME request, retried  -> idempotent replay, a success
            #   (b) a DIFFERENT request whose basis collided in the truncated 32-bit
            #       leadId suffix -> this caller's lead was never stored, and the
            #       leadId they would be handed belongs to someone else's lead
            # The stored digest is what distinguishes them.
            stored: dict[str, Any] = {}
            read_ok = False
            try:
                existing = _get_ddb().Table(_LEADS_TABLE).get_item(
                    Key={"leadId": lead_id},
                    # Cycle 2 Warning 2: this read WAS cosmetic (it only sourced a
                    # capturedAt for the response), and Cycle 1 raised eventual
                    # consistency as a mere Suggestion on that basis. The digest
                    # comparison promoted it to the sole evidence for a correctness
                    # gate, so a stale read can now mean claiming a replay that never
                    # happened. Strong consistency is the cost of that promotion.
                    ConsistentRead=True,
                )
                stored = (existing or {}).get("Item") or {}
                read_ok = True
            except Exception:  # noqa: BLE001
                log.warning(
                    "api-acquire-leads: replay read-back failed for leadId=%s", lead_id,
                )

            stored_digest = stored.get("idempotencyDigest")

            # Fail closed unless the stored digest PROVES this is the same request.
            #
            # Cycle 2 Warning 2: the previous version treated a missing digest as a
            # replay, collapsing "pre-digest row" (benign) with "the read failed, or
            # returned nothing" (unknown — possibly a collision). Only one of those is
            # safe, and nothing distinguished them. There is no pre-digest population
            # to protect: this endpoint is feature-flagged off and has no caller, so
            # every row it will ever replay against carries a digest.
            #
            # The cost is a 5xx on an unverifiable replay, which the executor retries
            # and then escalates. That is the right trade: a false "your lead was
            # captured" is worse than an operator looking at a lead that may need
            # re-submitting.
            if not read_ok or stored_digest is None or stored_digest != basis_digest:
                log.error(
                    "api-acquire-leads: cannot confirm replay for leadId=%s "
                    "tenantId=%s (read_ok=%s, digest_present=%s, digest_match=%s) — "
                    "lead NOT stored",
                    lead_id, claim_tenant, read_ok, stored_digest is not None,
                    stored_digest == basis_digest,
                )
                # 500 rather than 409 deliberately: a 4xx outside the client-error set
                # {401,403,404,405,415} — and outside the transient set {408,425,429} —
                # classifies as "refused" in agents/executors/base.py, which would
                # narrate a DEALER refusal for what is our own id-space collision. 5xx
                # is transient, so RetailExecutor retries, exhausts, and lands on
                # failed/executor_exhausted_retries, which escalates to an operator —
                # the correct destination. The lead is deliberately NOT rewritten under
                # a disambiguated id; that would break the determinism making retries
                # safe.
                return _resp(500, {"error": "Failed to persist lead"})

            log.info(
                "api-acquire-leads: idempotent replay confirmed for leadId=%s tenantId=%s",
                lead_id, claim_tenant,
            )
            return _resp(200, {
                "leadId": lead_id,
                "capturedAt": stored.get("capturedAt") or captured_at_iso,
                "idempotentReplay": True,
            })
        log.exception("api-acquire-leads: DDB PutItem failed for leadId=%s: %s", lead_id, exc)
        return _resp(500, {"error": "Failed to persist lead"})
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-leads: DDB PutItem failed for leadId=%s: %s", lead_id, exc)
        return _resp(500, {"error": "Failed to persist lead"})

    return _resp(201, {"leadId": lead_id, "capturedAt": captured_at_iso})


def _handle_get(event: dict, claims: dict) -> dict:
    """Handle GET /acquire/leads/{leadId} — read back a lead."""
    # ── Extract path param ────────────────────────────────────────────────
    path_params = event.get("pathParameters") or {}
    lead_id = path_params.get("leadId") if path_params else None
    if not lead_id:
        return _resp(400, {"error": "Missing required path parameter: leadId"})

    # ── Fetch lead ────────────────────────────────────────────────────────
    try:
        table = _get_ddb().Table(_LEADS_TABLE)
        result = table.get_item(Key={"leadId": lead_id})
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-leads: DDB GetItem failed for leadId=%s: %s", lead_id, exc)
        return _resp(500, {"error": "Failed to retrieve lead"})

    item = result.get("Item")
    if not item:
        return _resp(404, {"error": f"Lead '{lead_id}' not found"})

    # ── Tenant clamp ──────────────────────────────────────────────────────
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    lead_tenant = item.get("tenantId")
    if not claim_tenant or lead_tenant != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    return _resp(200, _public_lead(item))


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point — dispatches to POST or GET based on httpMethod."""
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )

    method = (event.get("httpMethod") or "GET").upper()
    if method == "POST":
        return _handle_post(event, claims)
    if method == "GET":
        return _handle_get(event, claims)

    return _resp(405, {"error": f"Method not allowed: {method}"})
