"""Certification-tier hot read for DMS dealer AI activation (spec § Constraint 4).

DMS spec Risk R6 makes the dealer's certification tier the gate for AI narration:
only Gold and Platinum dealers get LLM-narrated responses from the DMS personas;
Bronze / Silver / UNKNOWN dealers get a deterministic non-AI payload
(``{"available": False, "reason": "AI features require Gold or Platinum
certification", "tier": ...}``).

The read has to be fast — a Tier 1 tool call has to return inside the ~1 s
Nova Sonic budget (see ``~/.kiro/steering/agentic-tiers.md``), and every DMS
tool invocation reads it — so we go Redis-first (hot path) and fall back to
DynamoDB (``dms-{stage}-certification-scores``).

**Client convention.**  The Redis client is a byte-for-byte match with the
convention already load-bearing in
``lambdas/api-vehicle-live-state/handler.py:70`` (``_get_redis``):

  - module-level ``_redis_client`` sentinel, lazy-constructed on first call
  - constructor kwargs ``socket_connect_timeout=2``, ``decode_responses=True``
  - ``.ping()`` on init
  - any raise from constructor or ping is swallowed to ``None`` and the caller
    degrades to DDB — the tool must never raise into a conversational turn

A second Redis convention in this repo would be drift.  Do NOT change these
without changing both.

**Fail-closed semantics.**  If both Redis and DynamoDB are unreachable, or the
DDB item is missing, ``check_dealer_certification`` returns
``("UNKNOWN", None)`` — never raises.  Callers MUST treat ``"UNKNOWN"`` as
below Gold.  ``require_gold_or_deterministic`` implements that policy so the
call-site stays literal.

Not a "warm cache" — the Redis miss path back-fills for 5 minutes so a hot
dealer's tier is available on the next turn without re-hitting DDB.  TTL is
short on purpose: the certification-scores table is authoritative and the
publishing cadence for tier changes is nightly, so a 5-minute stale read is
tolerable.  Longer would delay a re-certification from taking effect.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

import boto3
import redis
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

# The four tier strings the DMS side writes.  Ordered from lowest to highest so
# the ``_AI_TIERS`` set is derived, not restated.
_TIER_ORDER: tuple[str, ...] = ("Bronze", "Silver", "Gold", "Platinum")
_AI_TIERS: frozenset[str] = frozenset({"Gold", "Platinum"})

# Fail-closed sentinel — see module docstring.  Callers treat this as below
# Gold; ``require_gold_or_deterministic`` enforces that.
_UNKNOWN_TIER: str = "UNKNOWN"

# Redis-cache TTL for a positive tier read.  Short on purpose.
_TIER_CACHE_TTL_SECONDS: int = 300  # 5 minutes

# DDB fallback table + record shape.  The "OVERALL" sentinel is the interface_id
# used when the tier isn't scoped to a specific DMS interface (it is the
# dealership-wide certification score).  Documented in
# ``docs/tech.md § "DMS Persona Extension"``.
_DMS_STAGE: str = os.environ.get("VSA_DMS_STAGE", "staging")
_CERT_TABLE_TPL: str = "dms-{stage}-certification-scores"
_OVERALL_INTERFACE_ID: str = "OVERALL"


# ---------------------------------------------------------------------------
# Module-level lazy Redis client — matches api-vehicle-live-state/handler.py.
# ---------------------------------------------------------------------------

# Sentinel is None; first call constructs.  A single constructor site in this
# module means ``count("redis.Redis(") == 1`` — asserted by T2.6's Verify.
_redis_client: Optional[redis.Redis] = None

# ── TLS + AUTH toggles (2026-09-16) ────────────────────────────────────────
#
# Shape mirrors the api-vehicle-live-state sibling and the DMS
# `~/guidance-for-dealer-management-system-on-aws/source/_lib/redis_client.py`.
# Both toggles off by default so the current CMS-cluster posture (no
# transitEncryptionEnabled, no authToken) is byte-identical to pre-change.
#
# Strict-string TLS predicate: a `bool(os.environ.get(...))` implementation
# enables TLS on any misspelling ("ture", "yess", "0") and there is no visible
# symptom until the handshake fails. See the mutation-test guard in
# lambdas/api-vehicle-live-state/tests/test_get_redis_tls_auth.py.
_TLS_ENV_VAR: str = "REDIS_TLS_ENABLED"
_AUTH_SECRET_ARN_ENV_VAR: str = "REDIS_AUTH_TOKEN_SECRET_ARN"


def _tls_enabled() -> bool:
    """Return True iff ``REDIS_TLS_ENABLED`` is set to ``"true"`` (case-insensitive)."""
    return os.environ.get(_TLS_ENV_VAR, "").strip().lower() == "true"


def _auth_token_secret_arn() -> str:
    """Return the AUTH-token secret ARN, or the empty string when unset."""
    return os.environ.get(_AUTH_SECRET_ARN_ENV_VAR, "").strip()


def _fetch_auth_token(secret_arn: str) -> Optional[str]:
    """Fetch the Redis AUTH token from AWS Secrets Manager. Never raises.

    Returns the raw token string, or ``None`` on any fetch failure (IAM
    AccessDenied, ResourceNotFoundException, network, wrong ARN, binary-only
    secret). Fail-open per the module contract in ``_get_redis()``.

    The token is never logged and never included in exception messages: the
    log line names only the exception TYPE, not str(exc) — avoiding the
    footgun where a mid-handshake error message from a downstream library
    includes fragments of the credential.
    """
    try:
        sm = boto3.client("secretsmanager")
        response = sm.get_secret_value(SecretId=secret_arn)
        secret_string = response.get("SecretString")
        if not secret_string:
            log.warning(
                "dms_cert_cache: Redis AUTH secret has no SecretString "
                "(binary secret unsupported); AUTH disabled"
            )
            return None
        return str(secret_string)
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa: BLE001 — fail-open on ANY failure
        log.warning(
            "dms_cert_cache: Redis AUTH secret fetch failed (%s); AUTH disabled",
            type(exc).__name__,
        )
        return None


def _get_redis() -> Optional[redis.Redis]:
    """Return a live Redis client, or ``None`` when Redis is unreachable.

    Never raises.  On any construction or ``.ping()`` failure the sentinel
    stays ``None`` and the DDB fallback path takes over.  Warm invocations
    reuse the module-level client.

    TLS + AUTH
    ----------
    When ``REDIS_TLS_ENABLED=true`` (case-insensitive strict-string), the
    connection is upgraded to TLS with server-cert verification. When
    ``REDIS_AUTH_TOKEN_SECRET_ARN`` is set, the AUTH token is fetched from
    Secrets Manager and passed as ``password=``. Either toggle failing
    (TLS handshake, secret-fetch, binary secret) disables the cache without
    falling back to a plaintext/unauthenticated connection.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    endpoint = os.environ.get("REDIS_ENDPOINT")
    if not endpoint:
        # Runtime not configured with Redis — silently fall through to DDB.
        # This is common in local dev / unit tests.
        return None

    # Build kwargs incrementally so the default (TLS off, AUTH off) is
    # byte-identical to the pre-2026-09-16 constructor.
    kwargs: dict[str, Any] = {
        "host": endpoint,
        "port": int(os.environ.get("REDIS_PORT", "6379")),
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "decode_responses": True,
    }

    if _tls_enabled():
        # ssl_cert_reqs="required" — never weaken to "none"; that would turn TLS into
        # wire-encryption theater against an active attacker.
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = "required"

    auth_arn = _auth_token_secret_arn()
    if auth_arn:
        token = _fetch_auth_token(auth_arn)
        if token is None:
            log.warning(
                "dms_cert_cache: Redis AUTH configured but token fetch failed; "
                "cache disabled"
            )
            return None
        kwargs["password"] = token

    try:
        client = redis.Redis(**kwargs)
        client.ping()
    except (redis.RedisError, OSError) as exc:
        # Log the exception TYPE only, not str(exc) — redis-py has historically
        # echoed server error content back verbatim, which for a mid-handshake
        # failure can include partial credentials.
        log.warning("dms_cert_cache: Redis unreachable (%s) — falling back to DDB", type(exc).__name__)
        return None
    _redis_client = client
    return client


def _cert_key(dealer_id: str) -> str:
    """Redis key for a dealer's tier cache."""
    return f"dms:cert:{dealer_id}"


def _cert_table_name() -> str:
    return _CERT_TABLE_TPL.format(stage=_DMS_STAGE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_dealer_certification(dealer_id: str) -> tuple[str, Optional[str]]:
    """Look up a dealer's overall certification tier.

    Returns ``(tier, computed_at)`` where ``tier`` is one of
    ``"Bronze" | "Silver" | "Gold" | "Platinum" | "UNKNOWN"`` and
    ``computed_at`` is the ISO 8601 timestamp on the record, or ``None``.

    Order of operations:

    1.  Redis hot read.  Hit → return.  Miss → step 2.  Redis error is treated
        as miss (the DDB fallback covers it) and logged.
    2.  DynamoDB read of ``dms-{stage}-certification-scores`` at
        ``(dealer_id, OVERALL)``.  Hit → back-fill Redis with a 5-minute TTL
        and return.
    3.  DDB miss or ``ClientError`` → return ``("UNKNOWN", None)`` (fail
        closed).  ``require_gold_or_deterministic`` treats this as below Gold.

    Never raises.  Every exception path degrades to ``("UNKNOWN", None)``.
    """
    if not dealer_id:
        return _UNKNOWN_TIER, None

    # (1) Redis hot read.
    client = _get_redis()
    if client is not None:
        try:
            raw = client.hgetall(_cert_key(dealer_id))
        except redis.RedisError as exc:
            log.warning(
                "dms_cert_cache: Redis HGETALL failed for %s (%s) — trying DDB",
                dealer_id,
                exc,
            )
            raw = None
        # decode_responses=True means keys/values are already strs.
        if raw:
            tier = raw.get("tier") or _UNKNOWN_TIER
            computed_at = raw.get("computed_at") or None
            return tier, computed_at

    # (2) DDB fallback.
    try:
        ddb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("VSA_DMS_REGION", "us-west-2"),
        )
        tbl = ddb.Table(_cert_table_name())
        resp = tbl.get_item(
            Key={"dealer_id": dealer_id, "interface_id": _OVERALL_INTERFACE_ID}
        )
    except (BotoCoreError, ClientError) as exc:
        log.warning(
            "dms_cert_cache: DDB lookup failed for %s (%s) — returning UNKNOWN",
            dealer_id,
            exc,
        )
        return _UNKNOWN_TIER, None

    item = resp.get("Item")
    if not item:
        # No score published for this dealer — fail closed.  Do NOT cache the
        # negative result: a dealer new to the platform can be certified within
        # a business day and the 5-minute TTL would keep the gate shut.
        return _UNKNOWN_TIER, None

    tier = str(item.get("tier") or _UNKNOWN_TIER)
    computed_at = item.get("computed_at")
    computed_at_str = str(computed_at) if computed_at is not None else None

    # (3) Back-fill Redis for the next call.  Best-effort; a HSET failure
    # doesn't affect the return.
    if client is not None:
        try:
            client.hset(
                _cert_key(dealer_id),
                mapping={"tier": tier, "computed_at": computed_at_str or ""},
            )
            client.expire(_cert_key(dealer_id), _TIER_CACHE_TTL_SECONDS)
        except redis.RedisError as exc:
            log.warning(
                "dms_cert_cache: Redis backfill failed for %s (%s) — non-fatal",
                dealer_id,
                exc,
            )

    return tier, computed_at_str


def is_ai_activated(tier: str) -> bool:
    """True when ``tier`` is one of the AI-activation tiers (Gold, Platinum).

    ``"UNKNOWN"`` — the fail-closed sentinel — is deliberately NOT in the set.
    A dealer whose tier we can't read gets the deterministic response.
    """
    return tier in _AI_TIERS


def deterministic_response_for_tier(tier: str) -> dict[str, Any]:
    """The exact deterministic non-AI payload every DMS tool returns when a
    caller's tier is below Gold.

    Shape is fixed so downstream tests can assert on it byte-for-byte.
    """
    return {
        "available": False,
        "reason": "AI features require Gold or Platinum certification",
        "tier": tier,
    }


def require_gold_or_deterministic(
    tier: str,
    ai_fn: Callable[..., Any],
    deterministic_fn: Optional[Callable[..., Any]] = None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Gate helper — the literal call-site expression of the AI-activation rule.

    If ``tier`` is Gold or Platinum, run ``ai_fn(*args, **kwargs)``.
    Otherwise, run ``deterministic_fn(*args, **kwargs)`` if supplied, or return
    the shared ``deterministic_response_for_tier(tier)`` shape.

    Keeping the gate as a helper — rather than an ``if tier in {...}`` scattered
    across every tool — means every DMS tool has one code path that the
    reviewer can grep for, and a future tier addition (e.g. a fifth
    "Diamond" tier) is a one-line change here rather than five.
    """
    if is_ai_activated(tier):
        return ai_fn(*args, **kwargs)
    if deterministic_fn is not None:
        return deterministic_fn(*args, **kwargs)
    return deterministic_response_for_tier(tier)


__all__ = [
    "check_dealer_certification",
    "deterministic_response_for_tier",
    "is_ai_activated",
    "require_gold_or_deterministic",
    "resolve_dealer_id",
    "no_access_response",
    "dealer_in_scope",
    "district_in_scope",
]


# ---------------------------------------------------------------------------
# Caller-to-target scope checks (spec § Constraints — "the agent does not
# widen scope; if a tool reads nothing, the answer is 'you have no access
# to that dealer', not a broader query")
# ---------------------------------------------------------------------------


def no_access_response(target_id: str = "") -> dict[str, Any]:
    """The exact shape every DMS tool returns when the caller's claims do
    not authorize the requested target.  Fixed phrase per spec § Constraints.

    Returned SHAPE is identical whether the target exists or not — a caller
    fishing for existence by comparing 'no access' vs 'not found' should
    not be able to tell.  Enforcement: prefer this check BEFORE the DDB
    read wherever the scope can be validated from claims alone (dealer /
    district).  When the scope check requires the record's own fields
    (e.g. a warranty claim's dealer_id), read then check.
    """
    return {
        "available": False,
        "reason": "you have no access to that dealer",
    }


def dealer_in_scope(ctx: Any, dealer_id: str) -> bool:
    """True iff ``dealer_id`` is in the caller's ``dealer_ids`` claim set.

    Empty ``dealer_ids`` on the context — the current reality for callers
    whose Cognito profile has no ``custom:dealerIds`` — returns False.  This
    fail-closed default means a mis-configured caller can't reach any DMS
    data.  When the spec's Cognito plumbing lands (custom:dealerIds threaded
    from JWT claims into SessionContext), the check activates transparently.

    Empty ``dealer_id`` argument returns False (a tool never has a legitimate
    reason to ask "am I authorized for nothing?").
    """
    if not dealer_id:
        return False
    scope = getattr(ctx, "dealer_ids", None) or []
    if not isinstance(scope, (list, tuple)):
        return False
    return str(dealer_id) in {str(d) for d in scope}


def district_in_scope(ctx: Any, district_id: str) -> bool:
    """True iff ``district_id`` is in the caller's ``district_ids`` claim set.

    Mirrors ``dealer_in_scope`` — same fail-closed default when the claims
    are absent, same string-normalization to prevent int/str mismatch.
    """
    if not district_id:
        return False
    scope = getattr(ctx, "district_ids", None) or []
    if not isinstance(scope, (list, tuple)):
        return False
    return str(district_id) in {str(d) for d in scope}


# ---------------------------------------------------------------------------
# Shared session-context helper — dealer_id resolution
# ---------------------------------------------------------------------------


def resolve_dealer_id(ctx: Any, explicit_dealer_id: str = "") -> str:
    """Pick the dealer_id used for the certification-tier lookup.

    Prefers ``explicit_dealer_id`` when supplied (the tool's kwarg), otherwise
    probes the session context for any dealer identifier surfaced from the
    caller's Cognito claims — ``dealer_id`` / ``dealerId`` / ``primary_dealer_id``
    or the first entry of ``dealer_ids`` / ``dealerIds`` when present.

    Returns the empty string when neither surfaces.  That yields an
    ``UNKNOWN`` tier lookup, which correctly gates to the deterministic
    response — a caller without a dealer identity is not Gold-certified by
    definition.

    Consolidated here (rather than duplicated per-tool) so a change to how
    dealer identity is surfaced from the session — e.g. a rename or a new
    claim field — is a one-line change here, not a three-file edit.
    """
    if explicit_dealer_id:
        return str(explicit_dealer_id)
    for attr in ("dealer_id", "dealerId", "primary_dealer_id"):
        val = getattr(ctx, attr, None)
        if val:
            return str(val)
    ids = getattr(ctx, "dealer_ids", None) or getattr(ctx, "dealerIds", None)
    if isinstance(ids, (list, tuple)) and ids:
        return str(ids[0])
    return ""
