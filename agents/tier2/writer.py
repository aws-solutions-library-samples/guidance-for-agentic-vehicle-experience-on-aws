"""
Shared ``write_finding()`` helper — implements Addendum D2 in-place update algorithm.

Spec source:
    .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md
    § "Contract addenda (2026-09-18 revision)" → Addendum D2

Algorithm (D2 — four steps):
1. Compute ``finding_key = SHA-256(canonicalise(agent_id, scope_field, scope_value,
   finding_kind))``.  The key does not change as inputs drift — it identifies the
   OPEN condition on a scope.
2. DDB Query on the ``finding_key`` GSI filtered by ``expires_at > now()``.
3. If a row exists → ``UpdateItem`` preserving ``finding_id``, extending
   ``expires_at``, rewriting ``headline`` / ``detail`` / ``evidence``, updating
   ``inputs_hash`` + ``computed_at``, appending an entry to ``audit_trail`` with
   ``{at, agent_version, inputs_hash, from_expires_at, to_expires_at}``.
4. If no row exists → ``PutItem`` with a new ``finding_id``.

``inputs_hash``'s revised role (D2): retry-safety WITHIN a single execution
attempt.  If two attempts during the same run hash identically, the second is a
no-op (same-run deduplication via ``inputs_hash`` check before calling this
helper).  ``inputs_hash`` is NO LONGER the cross-run idempotency key; that role
belongs to ``finding_key``.

Finding key derivation
----------------------
``finding_key = SHA-256(JSON({"agent_id": ..., "scope_field": ...,
                               "scope_value": ..., "finding_kind": ...}))``

where ``scope_field`` is the single non-None key in ``finding["scope"]`` and
``scope_value`` is its value.  Keys are sorted in the JSON serialisation for
stability.

Design notes
------------
- The DDB client is injected; the writer is region-agnostic.
- The ``finding_key`` GSI (Group 3) is a non-sparse GSI — every Finding carries
  ``finding_key``.  The Query index name is ``by_finding_key``.
- ``expires_at`` and ``computed_at`` are ISO-8601 UTC strings ending in "Z".
- ``finding_id`` format: ``FIND-{uuid4}`` — stable across in-place updates.
- ``audit_trail`` is a list of :class:`FindingAuditEntry`-shaped dicts appended on
  every ``UpdateItem`` call.

Region / table wiring
---------------------
Neither the table name nor the DDB client is created here.  The runner
``_production_dependencies()`` and unit tests both inject them.  This satisfies
``~/.kiro/steering/cross-region-namespace.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL helpers (A1 retention spec)
# ---------------------------------------------------------------------------

#: Grace period added to expires_at to produce the DynamoDB TTL value.
#: 7 days = 604,800 seconds.  DynamoDB deletes the row roughly 48 h after
#: ttl passes, so this gives 7 days of overlap between logical expiry and
#: physical deletion — enough to diagnose "why did this row disappear?"
#: without cluttering every reader with stale data.
_TTL_GRACE_SECONDS: int = 604_800

#: Agents whose rows are kept indefinitely (no TTL attribute written).
#: - agent-fi-recall-warranty: attempt history is needed across expiry cycles
#:   (agents/fi_recall_warranty/attempts.py reads every by_vin row, including
#:   expired ones, to number attempts and honour a decline).
#: - agent-fi-quality: maker.py looks rows up by finding_key; they are kept.
_TTL_EXCLUDED_AGENTS: frozenset[str] = frozenset({
    "agent-fi-recall-warranty",
    "agent-fi-quality",
})

#: Storage-only attributes set by this module that are NOT part of the
#: projected scope used by consumers.  These attributes land on the DDB item
#: but must be stripped at every API response boundary so they do not appear
#: in Finding responses (``root additionalProperties: false``).
#:
#: The cross-repo guard test
#: (``test_strip_list_covers_every_writer_projected_attr`` in
#: ``lambdas/api-avx-findings/tests/test_handler.py``) parses this tuple at
#: source and fails CI if a new entry is added here without a matching update
#: to ``_PROJECTED_SCOPE_ATTRS`` in the Lambda handler.  Add a one-line entry
#: below whenever a new storage-only attribute is written by this module.
_STORAGE_ONLY_ATTRS: tuple[str, ...] = (
    "ttl",  # A1 retention (2026-10-27): DynamoDB epoch-seconds TTL.
            # Set by _put_item, _update_item, and _refresh_expiry for eligible
            # rows; stripped at the response boundary by the API Lambda.
    "ttl_exempt",  # FA2 retention (2026-10-27): sticky Boolean marker.
                   # SET by any write (put or update) where the stored row
                   # or the incoming artifact carries recommended_action;
                   # NEVER REMOVEd.  Prevents a row that was once an FI item
                   # from acquiring ttl on a subsequent informational re-emit
                   # — the "has carried recommended_action" rule.  Stripped
                   # at the response boundary alongside ttl.
)


def _compute_ttl(finding: dict[str, Any]) -> int | None:
    """Return the TTL epoch-seconds for an eligible finding, or ``None``.

    Eligible = the ``ttl_exempt`` marker is absent AND the row has no
    ``recommended_action`` AND ``agent_id`` is not in ``_TTL_EXCLUDED_AGENTS``.
    A malformed or naive ``expires_at`` returns ``None`` (fail safe: the row is
    kept rather than silently deleted too soon).

    The value is ``int(epoch(expires_at)) + _TTL_GRACE_SECONDS`` so the
    row lives for 7 days past its logical expiry before DynamoDB considers it
    eligible for background deletion.  The grace period is intentionally
    generous: every reader already filters on ``expires_at`` so the extra 7
    days of physical presence is invisible to callers, and it gives the
    purge script a working window to catch any row the TTL scanner missed.

    ``ttl_exempt`` (FA2 retention): a sticky storage-only Boolean marker SET by
    any write where the stored row or the incoming artifact carries
    ``recommended_action``.  Once present it is never removed, so a row that was
    once an FI queue item is permanently excluded from TTL even after its
    ``recommended_action`` is cleaned up by a subsequent informational re-emit.
    Callers that build a combined dict from the stored row and the artifact must
    include both ``recommended_action`` keys (either takes precedence) and the
    stored row's ``ttl_exempt`` before calling this function.
    """
    import datetime as _dt

    # Sticky marker: row has been an FI item at some point — kept indefinitely.
    if finding.get("ttl_exempt"):
        return None

    # FI queue items (rows with recommended_action) are kept indefinitely.
    if finding.get("recommended_action") is not None:
        return None

    # Rows belonging to excluded agents are kept indefinitely.
    if finding.get("agent_id") in _TTL_EXCLUDED_AGENTS:
        return None

    expires_at = finding.get("expires_at", "")
    if not expires_at:
        return None

    try:
        dt = _dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive datetime (no UTC offset): treat as malformed (fail-safe: row kept).
            # Lambda always runs in UTC, so a naive value is ambiguous at best.
            log.warning(
                "writer._compute_ttl: naive (no UTC offset) expires_at %r — "
                "ttl omitted (fail safe)",
                expires_at,
            )
            return None
        return int(dt.timestamp()) + _TTL_GRACE_SECONDS
    except (ValueError, OverflowError, OSError):
        # Malformed expires_at: fail safe — no TTL written, row is kept.
        log.warning(
            "writer._compute_ttl: malformed expires_at %r — ttl omitted (fail safe)",
            expires_at,
        )
        return None


# ---------------------------------------------------------------------------
# Serialisation helpers (shared with artifact_writer.py)
# ---------------------------------------------------------------------------

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _coerce_floats(obj: Any) -> Any:
    """Recursively convert Python floats to Decimal for TypeSerializer."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _coerce_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_floats(item) for item in obj]
    return obj


def _to_ddb_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert plain-Python dict → DynamoDB AttributeValue map."""
    coerced = _coerce_floats(item)
    return {k: _serializer.serialize(v) for k, v in coerced.items()}


def _from_ddb_item(ddb_item: dict[str, Any]) -> dict[str, Any]:
    """Convert DynamoDB AttributeValue map → plain-Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in ddb_item.items()}


# ---------------------------------------------------------------------------
# Finding key derivation (D2)
# ---------------------------------------------------------------------------

def compute_finding_key(
    *,
    agent_id: str,
    scope_field: str,
    scope_value: str,
    finding_kind: str,
    subject_key: Optional[str] = None,
) -> str:
    """Compute ``finding_key`` per Addendum D2 and contract § 1.1 (v2.5).

    ``finding_key = SHA-256(JSON({"agent_id": ..., "finding_kind": ...,
                                   "scope_field": ..., "scope_value": ...}))``

    When ``subject_key`` is provided (non-None, non-empty) it is added to the
    canonical dict under the key ``"subject_key"``, allowing one fleet to hold
    several Findings of the same kind (e.g. one per move-pair).  When absent
    the digest is byte-identical to the four-field form — existing rows are
    unaffected (additive, per contract § 1.1: "Absent → the hash is byte-identical
    to the four-field form").

    Keys are sorted in the JSON serialisation for stability.  The result is a
    64-character lowercase hex string matching the schema pattern ``^[0-9a-f]{64}$``.

    Parameters
    ----------
    agent_id:
        Agent identifier (e.g. ``"agent-2-vehicle-health"``).
    scope_field:
        The single non-None key in the Finding's ``scope`` dict
        (e.g. ``"vin"``, ``"fleet_id"``).
    scope_value:
        The value of that scope field (e.g. the VIN string).
    finding_kind:
        Finding category (e.g. ``"health.battery"``).
    subject_key:
        Optional discriminator joining the hash.  ``None`` (default) or an empty
        string → the five-field form is NOT used; the result equals the four-field
        form.  A non-empty string → included in the canonical dict so that two
        Findings of one kind on one fleet get distinct keys.

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.
    """
    payload: dict[str, str] = {
        "agent_id": agent_id,
        "finding_kind": finding_kind,
        "scope_field": scope_field,
        "scope_value": scope_value,
    }
    # Only include subject_key when it is present and non-empty.
    # An absent or empty subject_key must NOT appear in the canonical JSON
    # (not even as null/None), to preserve byte-identity with the four-field form.
    if subject_key:
        payload["subject_key"] = subject_key
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_scope(finding: dict[str, Any]) -> tuple[str, str]:
    """Extract (scope_field, scope_value) from a Finding dict.

    The schema's oneOf constraint guarantees exactly one scope field is
    populated.  We find the first non-None value among the known scope
    field names.

    Raises
    ------
    ValueError
        If no scope field is populated (schema violation).
    """
    scope = finding.get("scope", {})
    for field in (
        "vin", "owner_id", "fleet_id", "dealership_id", "district_id",
        "population_id",   # Stage D: quality.investigation / quality.issue
    ):
        val = scope.get(field)
        if val is not None:
            return field, val
    raise ValueError(
        f"Finding has no populated scope field; scope={scope!r}. "
        "Schema requires exactly one of vin/owner_id/fleet_id/dealership_id/"
        "district_id/population_id."
    )


# ---------------------------------------------------------------------------
# Core D2 writer
# ---------------------------------------------------------------------------

#: GSI name for the finding_key lookup (Group 3 creates this index).
#: CDK declares this index as ``by_finding_key`` (vsa-tier2-stack.ts:222).
#: Do NOT rename the CDK index to match Python — Python moves to match CDK.
_FINDING_KEY_GSI = "by_finding_key"

#: Condition expression for retry-safety within a single execution attempt.
#: Guards the PutItem path only; UpdateItem is unconditional (preserves finding_id).
_CONDITION_INPUTS_HASH_UNCHANGED = (
    "attribute_not_exists(finding_id) OR inputs_hash = :current_hash"
)


def write_finding(
    finding: dict[str, Any],
    *,
    table_name: str,
    dynamodb_client: Any,
    utc_now_fn: Any = None,
    standing: Any = None,
    refresh_on_noop: bool = False,
    update_on_same_hash: bool = False,
) -> dict[str, Any]:
    """Write a Finding using the D2 in-place-update algorithm.

    Parameters
    ----------
    finding:
        Plain-Python dict conforming to the AVX Finding schema.  Must carry
        ``finding_key``, ``agent_id``, ``scope``, ``finding_kind``,
        ``inputs_hash``, ``finding_id``, ``expires_at``, ``computed_at``,
        ``headline``, ``detail``, ``evidence``, ``agent_version``,
        and ``audit_trail``.
    table_name:
        DynamoDB table name.  Injected; never hardcoded here.
    dynamodb_client:
        boto3 low-level DynamoDB client with the appropriate permissions.
    utc_now_fn:
        Optional callable ``() -> str`` returning an ISO-8601 UTC "Z" string.
        Injected in tests to produce deterministic timestamps.  Defaults to
        the real-time helper.
    standing:
        A :class:`agents.tier2.owner_resolver.StandingFacts` for the Finding's
        VIN. Its resolved fields are projected as top-level ``owner_id`` /
        ``fleet_id`` / ``vehicle_id`` DDB attributes for VIN-scoped Findings only,
        and **only onto the persisted item** — never onto *finding*, never into
        ``inputs_hash``, never into the schema-validated artifact.  ``None`` (the
        default) or an all-``None`` StandingFacts means no standing is provable:
        the attributes are omitted on insert and removed on update, which is the
        fail-closed state the approve check relies on.
    refresh_on_noop:
        When the live row already carries this ``inputs_hash`` and standing, extend its
        ``expires_at`` (and set ``computed_at``) instead of doing nothing, and return
        ``"refresh"``. Only the short-TTL ``diagnostics`` component asks for this: its
        Findings expire 30 minutes after their last write, so an unchanged fault must
        keep its card open, and must keep its ``finding_id`` rather than come back as a
        new card (and a new push) every half hour. No ``audit_trail`` entry is appended:
        a 5-minute keep-alive would add ~288 entries a day and outgrow the 400 KB item
        limit in about a week. The update is conditional on ``expires_at`` moving
        later, so it never shortens a longer-lived row. Default ``False`` keeps every
        existing caller byte-identical.
    update_on_same_hash:
        When the live row already carries this ``inputs_hash``, update its content in
        place anyway (``finding_id`` kept, one ``audit_trail`` entry) instead of the no-op
        or refresh. Only the FI recall/warranty agent asks for this, when a model-written
        plan replaces a rules-written item whose inputs did not change (stage C call 59):
        ``inputs_hash`` deliberately excludes how the text was written, so without this
        the model's content would never land. Default ``False``.

    Returns
    -------
    dict
        ``{"action": "put" | "update" | "noop", "finding_id": str}``

        - ``"put"``    — new row written (new ``finding_id`` minted).
        - ``"update"`` — existing row updated in place (``finding_id`` preserved).
        - ``"noop"``   — same-run retry: ``inputs_hash`` unchanged; no write.
        - ``"refresh"`` — ``refresh_on_noop`` only: unchanged content, expiry extended.

    Algorithm
    ---------
    Step 1: compute ``finding_key`` (already present in the Finding; verified).
    Step 2: Query the ``by_finding_key`` GSI filtered by ``expires_at > now()``.
    Step 3: If a live row exists → ``UpdateItem`` (preserve ``finding_id``).
    Step 4: If no live row → ``PutItem`` (new ``finding_id``).
    """
    import datetime

    if utc_now_fn is None:
        def utc_now_fn() -> str:
            return (
                datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

    now_str = utc_now_fn()

    # ── Step 1: finding_key must already be on the Finding ──────────────────
    finding_key = finding.get("finding_key")
    if not finding_key:
        raise ValueError(
            "Finding is missing 'finding_key'. "
            "Compute it via compute_finding_key() before calling write_finding()."
        )

    # ── Step 2: Query the finding_key GSI for a live row ────────────────────
    live_row = _query_live_row(
        finding_key=finding_key,
        table_name=table_name,
        dynamodb_client=dynamodb_client,
        now_str=now_str,
    )

    # ── Retry-safety (D2 narrowed role for inputs_hash) ─────────────────────
    # If the existing row already carries the SAME inputs_hash, the Finding's
    # CONTENT is unchanged and there is nothing to rewrite — a duplicate call.
    #
    # Review cycle 1 of Fix Group 25 (C1) found this returned before
    # _update_item and therefore skipped standing convergence entirely. Standing
    # is deliberately excluded from inputs_hash (D2 idempotency), and inputs_hash
    # is computed from telemetry with no date component, so a vehicle that changed
    # fleet kept its stale projected fleet_id for the remainder of the 8-day TTL —
    # authorizing the PREVIOUS fleet's operators for ~8 days. Exactly the
    # fail-open drift the update path's REMOVE clause exists to prevent, reached
    # by a route that never runs the update path.
    #
    # The comment here used to say "within a single execution attempt", which
    # misdescribed the condition: nothing scopes it to one run. Same-run retry and
    # cross-run drift are different situations that happened to share a predicate.
    # So the no-op now additionally requires the STANDING to match what is
    # persisted; when it differs, fall through to UpdateItem.
    #
    # It also requires the SEVERITY to match (issue
    # 2026-10-23-tier2-writer-frozen-severity). inputs_hash covers the input rows,
    # not how they were judged, so a new agent version or classifier table can
    # change the severity of unchanged inputs. A no-op there would keep a stale
    # badge, and an escalation into p0 would never reach the broker.
    if live_row is not None and not update_on_same_hash:
        existing_inputs_hash = live_row.get("inputs_hash", "")
        if existing_inputs_hash == finding.get("inputs_hash", ""):
            desired = _standing_projection(finding, standing)
            persisted = {
                attr: live_row[attr]
                for attr in _STANDING_ATTRS
                if live_row.get(attr) is not None
            }
            severity_unchanged = live_row.get("severity") == finding.get("severity")
            if desired == persisted and severity_unchanged:
                if refresh_on_noop:
                    # Use the STORED row's recommended_action, agent_id, and
                    # ttl_exempt to determine TTL eligibility — not the incoming
                    # artifact's alone — so a live FI row never has ttl SET by a
                    # noop refresh from an informational re-emit.
                    # Also check the artifact's recommended_action (suggestion:
                    # "stored or incoming" rule holds on every path).
                    #
                    # Note (FA3 Suggestion 5): the refresh path does NOT SET
                    # ttl_exempt, even though the Accept says "any write" should.
                    # This is safe because:
                    # (a) recommended_action is removed only by the update path,
                    #     which sets the marker in the same write; so any row
                    #     that had RA already has ttl_exempt=True before refresh
                    #     is ever reached.
                    # (b) A refresh writes no content changes (no new artifact
                    #     fields land), so an artifact-only recommended_action
                    #     can never appear here — the refresh branch requires the
                    #     same inputs_hash as the live row, meaning the artifact
                    #     carries no new content.
                    # Together these mean the refresh path can never encounter a
                    # row that needs the marker and does not already have it.
                    _live_for_ttl = {
                        "recommended_action": (
                            live_row.get("recommended_action")
                            or finding.get("recommended_action")
                        ),
                        "agent_id": live_row.get("agent_id", finding.get("agent_id", "")),
                        "expires_at": finding["expires_at"],
                        "ttl_exempt": live_row.get("ttl_exempt"),
                    }
                    _new_ttl = _compute_ttl(_live_for_ttl)
                    refreshed = _refresh_expiry(
                        table_name=table_name,
                        dynamodb_client=dynamodb_client,
                        finding_id=live_row["finding_id"],
                        expires_at=finding["expires_at"],
                        computed_at=finding["computed_at"],
                        new_ttl=_new_ttl,
                        remove_ttl=_new_ttl is None,
                    )
                    return {"action": "refresh" if refreshed else "noop",
                            "finding_id": live_row.get("finding_id", "")}
                log.debug(
                    "write_finding: same inputs_hash and unchanged standing; no-op "
                    "finding_key=%s inputs_hash=%s",
                    finding_key,
                    finding.get("inputs_hash"),
                )
                return {"action": "noop", "finding_id": live_row.get("finding_id", "")}
            if desired != persisted:
                log.info(
                    "write_finding: event=standing_drift_converged finding_key=%s "
                    "changed=%s",
                    finding_key,
                    sorted(set(desired) ^ set(persisted))
                    or sorted(k for k in desired if desired[k] != persisted.get(k)),
                )
            if not severity_unchanged:
                log.info(
                    "write_finding: event=severity_changed_same_inputs finding_key=%s "
                    "from=%s to=%s",
                    finding_key, live_row.get("severity"), finding.get("severity"),
                )

    if live_row is not None:
        # ── Step 3: UpdateItem — in-place update ──────────────────────────
        finding_id = live_row["finding_id"]
        from_expires_at = live_row.get("expires_at", "")
        to_expires_at = finding["expires_at"]

        audit_entry = {
            "at": now_str,
            "agent_version": finding["agent_version"],
            "inputs_hash": finding["inputs_hash"],
            "from_expires_at": from_expires_at,
            "to_expires_at": to_expires_at,
        }
        from_severity = live_row.get("severity")
        to_severity = finding.get("severity")
        if from_severity != to_severity:
            # Human-readable history only: nothing parses this note (the broker reads
            # the Finding's severity field, never the audit trail).
            audit_entry["note"] = f"severity {from_severity} -> {to_severity}"
            log.info(
                "write_finding: event=severity_changed finding_id=%s from=%s to=%s",
                finding_id, from_severity, to_severity,
            )

        _update_item(
            table_name=table_name,
            dynamodb_client=dynamodb_client,
            finding_id=finding_id,
            finding=finding,
            audit_entry=audit_entry,
            standing=standing,
            live_row=live_row,
        )
        log.debug(
            "write_finding: updated in place finding_id=%s finding_key=%s",
            finding_id,
            finding_key,
        )
        return {"action": "update", "finding_id": finding_id}
    else:
        # ── Step 4: PutItem — new row ──────────────────────────────────────
        finding_id = finding["finding_id"]
        _put_item(
            table_name=table_name,
            dynamodb_client=dynamodb_client,
            finding=finding,
            standing=standing,
        )
        log.debug(
            "write_finding: new row finding_id=%s finding_key=%s",
            finding_id,
            finding_key,
        )
        return {"action": "put", "finding_id": finding_id}


# ---------------------------------------------------------------------------
# DynamoDB operation helpers
# ---------------------------------------------------------------------------


def _refresh_expiry(
    *,
    table_name: str,
    dynamodb_client: Any,
    finding_id: str,
    expires_at: str,
    computed_at: str,
    new_ttl: int | None = None,
    remove_ttl: bool = False,
) -> bool:
    """Extend a live row's ``expires_at`` and set ``computed_at``; ``True`` if written.

    Conditional on the row existing and on ``expires_at`` moving later, so a keep-alive
    can never shorten a row another producer gave a longer life. A failed condition is
    not an error: the row is already at least that fresh.

    ``new_ttl`` (A1 retention): when provided and non-None, also SETs the ``ttl``
    attribute in the same write.  The caller computes the value from the STORED
    row's or the incoming artifact's eligibility (recommended_action / agent_id /
    ttl_exempt) with the new expires_at — whichever is more restrictive — so a
    live FI row never gets ttl SET by a noop refresh from an informational artifact.

    ``remove_ttl`` (A1 retention): when True (and new_ttl is None), REMOVEs ttl
    from the row.  Used when the stored row is ineligible (is or was an FI item).
    REMOVE on an absent attribute is a DynamoDB no-op so this is safe on rows
    that never had ttl.
    """
    update_expr = "SET expires_at = :e, computed_at = :c"
    eav: dict[str, Any] = {":e": {"S": expires_at}, ":c": {"S": computed_at}}
    ean: dict[str, str] = {}
    if new_ttl is not None:
        # ``ttl`` is a DynamoDB reserved word; use ``#ttl`` to avoid ValidationException.
        update_expr += ", #ttl = :ttl"
        ean["#ttl"] = "ttl"
        eav[":ttl"] = {"N": str(new_ttl)}
    elif remove_ttl:
        # REMOVE ttl from an ineligible row (FI item, excluded agent, or
        # malformed expires_at from a previous write).  REMOVE is appended
        # after the SET clause; ``#ttl`` must be in ExpressionAttributeNames.
        ean["#ttl"] = "ttl"
        update_expr += " REMOVE #ttl"
    try:
        call: dict[str, Any] = {
            "TableName": table_name,
            "Key": {"finding_id": {"S": finding_id}},
            "UpdateExpression": update_expr,
            "ConditionExpression": "attribute_exists(finding_id) AND expires_at < :e",
            "ExpressionAttributeValues": eav,
        }
        if ean:
            call["ExpressionAttributeNames"] = ean
        dynamodb_client.update_item(**call)
        return True
    except Exception as exc:  # noqa: BLE001 — only the condition failure is expected
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return False
        raise


def _query_live_row(
    *,
    finding_key: str,
    table_name: str,
    dynamodb_client: Any,
    now_str: str,
) -> dict[str, Any] | None:
    """Query the ``by_finding_key`` GSI for a live (non-expired) row.

    Returns the first matching item as a plain-Python dict, or ``None`` if
    no live row exists.

    The GSI is keyed on ``finding_key`` (hash key).  The filter expression
    ``expires_at > :now`` excludes expired rows from the result; an expired
    row with the same ``finding_key`` is treated as "no row" (D2: expired
    condition → new ``finding_id`` on next write).

    DynamoDB applies ``Limit`` **before** the ``FilterExpression``.  This
    means a ``Limit=1`` call can consume a page slot on an expired row and
    return ``Items=[]`` even when a live row follows on the next page.  The
    table's TTL scanner removes expired rows only asynchronously (within
    ~48 h of expiry), so during the window between logical expiry and physical
    deletion an expired row is still present on the index, and this is not
    merely theoretical: after a clear → expiry → reinject cycle the expired
    row is always the first entry on the index.

    Fix (C1, review Cycle 1): remove ``Limit`` and follow ``LastEvaluatedKey``
    until a live item is found or the index is exhausted.

    Parameters
    ----------
    finding_key:
        64-char hex SHA-256 string.
    table_name:
        DynamoDB table name.
    dynamodb_client:
        boto3 low-level DDB client.
    now_str:
        ISO-8601 UTC "Z" string for the expiry comparison.
    """
    kwargs: dict[str, Any] = {
        "TableName": table_name,
        "IndexName": _FINDING_KEY_GSI,
        "KeyConditionExpression": "finding_key = :fk",
        "FilterExpression": "expires_at > :now",
        "ExpressionAttributeValues": {
            ":fk": {"S": finding_key},
            ":now": {"S": now_str},
        },
    }
    while True:
        response = dynamodb_client.query(**kwargs)
        items = response.get("Items", [])
        if items:
            return _from_ddb_item(items[0])
        last = response.get("LastEvaluatedKey")
        if not last:
            return None
        kwargs["ExclusiveStartKey"] = last


def _standing_projection(
    finding: dict[str, Any],
    standing: Any,
) -> dict[str, str]:
    """Return the standing attributes to project onto the DDB item.

    Fix Group 25 / the identity-and-standing contract § 6. Generalises the
    owner-only projection: the authorizer needs one fact per standing route, and
    all three come from the same CMS vehicle row.

    Returns ``{attr: value}`` for whichever of ``owner_id`` / ``fleet_id`` /
    ``vehicle_id`` resolved. Empty dict means no standing is provable, which the
    approve path MUST treat as deny.

    Applies **only to VIN-scoped Findings**, for two reasons that both still hold
    now that three attributes are projected instead of one:

    * An owner-scoped Finding already gets top-level ``owner_id`` from T10.2
      (``owner_id`` *is* its scope field), and a fleet-scoped Finding gets
      ``fleet_id``. Projecting here as well would give one attribute two writers,
      and in ``_update_item`` would SET and REMOVE the same path in one
      expression — which DynamoDB rejects outright.
    * Fleet / dealership / district scopes have no single vehicle to resolve
      standing for.

    None of the three attribute names can equal the VIN scope field (``"vin"``),
    so within the VIN branch there is no collision with T10.2's projection.

    Fail-closed. A value is projected only when it is a non-empty string, and, for
    ``owner_id`` and ``fleet_id``, distinct from the scope value: a VIN is never a
    party or a fleet. ``vehicle_id`` may equal the VIN, and since the 2026-11
    CMS migration (spec ``2026-10-28-vehicleid-equals-vin``) it always does; refusing
    it dropped ``fleet_driver`` standing on every moved car (issue
    ``2026-11-07-standing-dropped-when-vehicleid-equals-vin``).
    ``resolve_standing_facts`` already normalises to exactly this contract; the
    re-check here is deliberate defence in depth, because the resolver is an
    injected seam a future caller could replace.
    """
    scope_field, scope_value = _extract_scope(finding)
    if scope_field != "vin" or standing is None:
        return {}

    out: dict[str, str] = {}
    for attr in _STANDING_ATTRS:
        raw = getattr(standing, attr, None)
        if not isinstance(raw, str):
            continue
        candidate = raw.strip()
        if not candidate:
            continue
        if candidate == scope_value and attr not in _MAY_EQUAL_SCOPE_VIN:
            # A VIN is not a party or a fleet.
            continue
        out[attr] = candidate
    return out


#: The standing attributes this module may project. Named once so ``_update_item``
#: can converge every one of them — a name present here but absent from the
#: REMOVE path would leave a stale projection authorizing a party the registry no
#: longer names, which is the fail-open drift the contract § 6 forbids.
_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id")

#: The standing attributes whose value may equal the Finding's scope VIN. Only the
#: vehicle id: it IS the VIN on every registry row since the CMS migration.
_MAY_EQUAL_SCOPE_VIN: frozenset[str] = frozenset({"vehicle_id"})

#: FI fields that are present on some Findings and absent on others.  On
#: re-emit (UpdateItem) each is SET when the new artifact carries it and
#: REMOVED when it does not, so the override baseline stays current
#: (contract § 1.1 v2.5 / spec T2.5 Accept).
#:
#: - ``recommended_action``: present only on FI queue items (Findings that
#:   propose an action).  Must be removed if a re-emitted artifact drops it.
#: - ``location``: present on move/gap Findings; absent on cost/recall ones.
#: - ``subject_key``: the discriminator that lets one fleet hold several
#:   Findings of one kind (e.g. one per move-pair).  Absent on the
#:   four-field form; must be removed if a re-emitted artifact drops it.
#:
#: ``disclosure_txt`` is intentionally absent from this tuple: it may only be
#: refreshed for FI Findings (``agent_id`` starting with ``"agent-fi-"``).  See
#: ``_FI_ESCALATION_ATTRS`` and the guarded branch in ``_update_item``.
#: ``severity`` is not here either: it is required by the schema and is SET on every
#: in-place update of every Finding (see ``_update_item``), so it has no REMOVE arm.
_FI_REEMIT_ATTRS: tuple[str, ...] = (
    "recommended_action",
    "location",
    "subject_key",
)

#: Refreshed in-place ONLY for FI Findings (``agent_id`` starting with
#: ``"agent-fi-"``).  For every other Finding ``disclosure_txt`` is left as written
#: (outside issue 2026-10-23-tier2-writer-frozen-severity, which moved ``severity``
#: out of this tuple: every Finding's severity is now refreshed in place, and the
#: broker pushes an escalation into ``p0`` once through its p0 ledger instead of
#: relying on the severity being frozen).
_FI_ESCALATION_ATTRS: tuple[str, ...] = (
    "disclosure_txt",
)

#: SET on every in-place update of every Finding when the artifact carries them (prompt
#: 361, issue 2026-11-09-tier2-writer-contract-fields-on-update): the run's confidence
#: and inference cost, so they describe the text stored beside them. Schema-required, so
#: never REMOVEd.
_RUN_ATTRS: tuple[str, ...] = (
    "confidence",
    "tokens_used",
    "cost_usd",
)


def _put_item(
    *,
    table_name: str,
    dynamodb_client: Any,
    finding: dict[str, Any],
    standing: Any = None,
) -> None:
    """Unconditional PutItem for a new Finding row.

    No ConditionExpression: the D2 algorithm already checked (via Query) that
    no live row exists before calling this.  A race between two concurrent
    writers with the same ``finding_key`` would produce a last-writer-wins
    overwrite, which is acceptable — both are writing the same logical condition
    and the ``finding_id`` from the first write would be lost, but the Finding
    content is equivalent.  Full two-phase conditional-put locking is not
    required by the spec.

    T10.2 (scope denormalisation):
    The persisted DDB item carries a top-level attribute named for the active
    scope field (e.g. ``"vin"``, ``"fleet_id"``) with the same value as the
    nested ``scope.<field>``.  This makes the five sparse scope GSIs queryable
    without a schema change — the GSIs key on top-level attributes, not nested
    ones, and Boto3 cannot project nested attributes into a GSI partition key.

    The denormalised attribute lives ONLY in the DDB item, NEVER in:
    - the artifact dict returned to callers (schema-validated Finding),
    - ``inputs_hash`` (computed from ADP input rows before this function runs),
    - the JSON schema (frozen cross-repo contract; ``additionalProperties: false``
      applies to the artifact, not the storage shape).

    Standing projection (F11.2, broadened by Fix Group 25):
    For VIN-scoped Findings only, additional top-level ``owner_id`` / ``fleet_id``
    / ``vehicle_id`` attributes are projected for whichever standing facts
    resolved.  Same boundary as above — DDB item only.
    """
    scope_field, scope_value = _extract_scope(finding)
    # Build the item with the denormalised top-level scope attribute.
    # Do NOT mutate the caller's finding dict — create a shallow copy and add
    # the attribute there.  _to_ddb_item() produces the wire-format map.
    item_with_scope = {**finding, scope_field: scope_value}

    # Project the resolved standing facts for VIN-scoped Findings.  Each is
    # omitted entirely when unresolvable — never written as "" or as the VIN,
    # because the approve check compares these against caller claims and an empty
    # attribute would be matched by an empty claim.
    item_with_scope.update(_standing_projection(finding, standing))

    # A1 retention: set ttl for eligible rows (no recommended_action, non-excluded
    # agent).  ttl is storage-only — NOT in the schema dict, NOT in inputs_hash.
    # Added to item_with_scope here so it lands on the wire-format item passed to
    # PutItem, after schema validation has already run on the Finding artifact.
    _ttl = _compute_ttl(finding)
    if _ttl is not None:
        item_with_scope["ttl"] = _ttl

    # FA2 retention: if the new row has recommended_action it is an FI item and
    # must be permanently exempt from TTL.  SET ttl_exempt = True so that
    # subsequent informational re-emits cannot acquire ttl even after
    # recommended_action is removed from the stored row.
    if finding.get("recommended_action") is not None:
        item_with_scope["ttl_exempt"] = True

    dynamodb_client.put_item(
        TableName=table_name,
        Item=_to_ddb_item(item_with_scope),
    )


def _update_item(
    *,
    table_name: str,
    dynamodb_client: Any,
    finding_id: str,
    finding: dict[str, Any],
    audit_entry: dict[str, Any],
    standing: Any = None,
    live_row: dict[str, Any] | None = None,
) -> None:
    """UpdateItem preserving ``finding_id``, appending to ``audit_trail``.

    Updates:
    - ``expires_at`` extended to new value
    - ``headline``, ``detail``, ``evidence`` rewritten
    - ``severity`` rewritten (both ways; issue 2026-10-23-tier2-writer-frozen-severity)
    - ``inputs_hash``, ``computed_at`` updated
    - ``agent_version`` set to the version of the run that last rewrote the row (equal
      to the new ``audit_trail`` entry's), for every agent and scope (issue
      2026-11-06-tier2-writer-stale-agent-version). Before this a rewritten Finding kept
      the version of its first write. Where an emitter carries text forward from the live
      row (reused rebalancing moves, recall/warranty ``refresh_from_live``), that text may
      come from an earlier version; strict text provenance there is the emitter's job.
      The no-op and ``refresh_on_noop`` paths rewrite nothing, so they keep the stored
      version.
    - ``confidence``, ``tokens_used`` and ``cost_usd`` set from the same run, so the stored
      confidence and cost describe the stored text (issue
      2026-11-09-tier2-writer-contract-fields-on-update). The no-op paths keep them too.
    - ``audit_trail`` appended with the new entry
    - denormalised top-level scope attribute (T10.2 — see _put_item docstring)

    The item is keyed on ``finding_id`` (the primary key of the findings
    table, per Group 3 spec).

    Note: the ``finding_id`` primary key (``pk``) is used for the Key
    expression.  The full Finding passed in carries the updated values.

    T10.2 scope denormalisation:
    ``_update_item`` also writes the top-level scope attribute so that rows
    written before this change (or rows whose scope theoretically changed)
    converge to the correct denormalised state.  The attribute name and value
    are extracted via the existing ``_extract_scope()`` — one resolution path,
    not two.

    Standing projection (F11.2, broadened by Fix Group 25):
    For VIN-scoped Findings **every** attribute in ``_STANDING_ATTRS`` converges
    on every update — SET when resolvable, **REMOVE when not**.  Removing matters:
    a row written while the registry knew the owner, whose registry row is later
    deleted, would otherwise keep authorizing that owner forever.  Leaving a stale
    projection in place is fail-open drift, so the update path drives it back to
    absent.  ``REMOVE`` on an absent attribute is a DynamoDB no-op, so this is
    safe on rows that never had one.

    Converging the WHOLE set rather than only what resolved is the point: a
    vehicle moved between fleets yields a resolvable ``fleet_id`` and must
    overwrite; a vehicle whose fleet assignment was cleared yields ``None`` and
    must remove. Iterating ``_STANDING_ATTRS`` makes those the same code path.

    Non-VIN scopes are untouched here.  An owner-scoped Finding's top-level
    ``owner_id`` is written by the T10.2 scope clause above; emitting a REMOVE for
    it in the same expression would make the statement SET and REMOVE one path,
    which DynamoDB rejects.
    """
    scope_field, scope_value = _extract_scope(finding)

    # Build UpdateExpression for all mutable fields, including the denormalised
    # top-level scope attribute (T10.2).  The attribute name placeholder
    # ``#scope_field`` avoids a conflict if the field name happens to be a
    # DynamoDB reserved word.
    update_expr = (
        "SET expires_at = :new_expires_at, "
        "headline = :headline, "
        "detail = :detail, "
        "evidence = :evidence, "
        "inputs_hash = :inputs_hash, "
        "computed_at = :computed_at, "
        "agent_version = :agent_version, "
        "audit_trail = list_append("
        "  if_not_exists(audit_trail, :empty_list), "
        "  :new_audit_entry"
        "), "
        "#scope_field = :scope_value"
    )

    expr_names: dict[str, str] = {"#scope_field": scope_field}
    expr_values: dict[str, Any] = {
        ":new_expires_at": {"S": finding["expires_at"]},
        ":headline": {"S": finding.get("headline", "")},
        ":detail": {"S": finding.get("detail", "")},
        ":evidence": _serializer.serialize(_coerce_floats(finding.get("evidence", []))),
        ":inputs_hash": {"S": finding["inputs_hash"]},
        ":computed_at": {"S": finding["computed_at"]},
        # The version of the run that last rewrote the row, same value as the new audit
        # entry's (issue 2026-11-06-tier2-writer-stale-agent-version). Required by the
        # schema, so no default.
        ":agent_version": {"S": finding["agent_version"]},
        ":new_audit_entry": _serializer.serialize(
            _coerce_floats([audit_entry])
        ),
        ":empty_list": {"L": []},
        ":scope_value": {"S": scope_value},
    }

    # ── severity: SET on every in-place update of every Finding ─────────────
    #
    # Issue 2026-10-23-tier2-writer-frozen-severity. The badge follows the same
    # run as the text above, both ways, so a Finding first written ``soon`` that
    # worsens to ``p0`` shows ``p0`` and reaches the broker as a ``p0`` MODIFY;
    # the broker pushes that escalation once (its p0 ledger). ``severity`` is a
    # required schema field, so a well-formed artifact always carries it; one
    # that does not is left as stored rather than REMOVEd.
    if finding.get("severity") is not None:
        expr_names["#severity"] = "severity"
        update_expr += ", #severity = :severity"
        expr_values[":severity"] = {"S": finding["severity"]}

    # ── confidence, tokens_used, cost_usd: SET on every in-place update ──────
    #
    # Prompt 361, issue 2026-11-09-tier2-writer-contract-fields-on-update. The run's
    # confidence and inference cost sit beside the text it wrote: ``confidence`` is an
    # artifact-contract field, and ``tokens_used``/``cost_usd`` are 0 iff the
    # deterministic fallback ran. All three are schema-required, so a well-formed artifact
    # carries them; one that doesn't is left as stored rather than REMOVEd, as severity is.
    for attr in _RUN_ATTRS:
        val = finding.get(attr)
        if val is not None:
            expr_names[f"#run_{attr}"] = attr
            update_expr += f", #run_{attr} = :run_{attr}"
            expr_values[f":run_{attr}"] = _serializer.serialize(_coerce_floats(val))

    # ── FI optional fields (T2.5): SET when present, REMOVE when absent ─────
    #
    # The three fields in ``_FI_REEMIT_ATTRS`` (``recommended_action``,
    # ``location``, ``subject_key``) may change across re-emits and are
    # refreshed for EVERY Finding.  Without explicit SET/REMOVE they would
    # stay at whatever value the original PutItem stored.
    #
    # ``severity`` is SET above for every Finding; ``disclosure_txt`` is in
    # ``_FI_ESCALATION_ATTRS`` and is only refreshed for FI Findings (see guarded
    # block below).
    #
    # REMOVE is skipped for owner-scoped Findings: T10.2's scope clause already
    # writes the top-level ``owner_id`` attribute, and adding REMOVE entries for
    # it would make DynamoDB reject the statement with "SET and REMOVE on one
    # path" (same constraint as the standing-attrs guard below).  Owner-scoped
    # Findings do not carry FI optional fields in practice, so the omission is
    # safe.  Any other non-owner scope (fleet_id, vin, dealership_id,
    # district_id) can safely receive REMOVE.
    fi_to_remove: list[str] = []
    fi_optional = _FI_REEMIT_ATTRS
    for attr in fi_optional:
        ph = f"#fi_{attr}"
        val = finding.get(attr)
        if val is not None:
            # Register name only when it appears in an expression (W5: unused
            # names cause a DynamoDB ValidationException).
            expr_names[ph] = attr
            update_expr += f", {ph} = :fi_{attr}"
            expr_values[f":fi_{attr}"] = _serializer.serialize(_coerce_floats(val))
        elif scope_field != "owner_id":
            # Only register + REMOVE for non-owner scopes (same guard as before).
            expr_names[ph] = attr
            fi_to_remove.append(ph)

    # ── FI-only field: disclosure_txt (agent-fi- only) ──────────────────────
    #
    # Refreshed only when ``agent_id`` starts with ``"agent-fi-"``.  For every
    # other Finding ``disclosure_txt`` is left as written (unchanged by issue
    # 2026-10-23-tier2-writer-frozen-severity, which moved ``severity`` out of
    # this branch).
    if finding.get("agent_id", "").startswith("agent-fi-"):
        for attr in _FI_ESCALATION_ATTRS:
            ph = f"#fi_{attr}"
            val = finding.get(attr)
            if val is not None:
                expr_names[ph] = attr
                update_expr += f", {ph} = :fi_{attr}"
                expr_values[f":fi_{attr}"] = _serializer.serialize(_coerce_floats(val))
            elif scope_field != "owner_id":
                expr_names[ph] = attr
                fi_to_remove.append(ph)

    # Converge the standing projection on VIN-scoped Findings.
    standing_to_remove: list[str] = []
    if scope_field == "vin":
        projected = _standing_projection(finding, standing)
        for attr in _STANDING_ATTRS:
            ph = f"#{attr}"
            expr_names[ph] = attr
            if attr in projected:
                update_expr += f", {ph} = :{attr}"
                expr_values[f":{attr}"] = {"S": projected[attr]}
            else:
                standing_to_remove.append(ph)

    # ── A1/FA2 retention: SET or REMOVE ttl; SET ttl_exempt when exempt ────
    #
    # ttl is a DynamoDB Number (epoch seconds) used by the table TTL mechanism.
    # It is set on eligible rows and REMOVED on all others.  ``ttl`` is a DDB
    # reserved word; ``#ttl`` avoids the ValidationException.
    #
    # Eligibility is determined from BOTH the stored row and the incoming artifact:
    #
    #   - If the stored row (live_row) carries ``recommended_action`` or
    #     ``ttl_exempt``, the row is ineligible regardless of the artifact.
    #   - If the artifact carries ``recommended_action``, the row is ineligible.
    #
    # ttl_exempt (FA2): a sticky Boolean marker that is SET whenever either
    # source carries ``recommended_action``.  It is NEVER REMOVEd, so a row
    # that was once an FI item is permanently excluded from TTL even after
    # recommended_action is stripped from the stored row by a later re-emit.
    #
    # REMOVE is emitted even when the row never had ttl: DynamoDB treats REMOVE
    # on an absent attribute as a no-op, and it cannot collide with a SET of
    # the same path because only one branch is taken.
    _ttl_check: dict[str, Any] = dict(finding)
    if live_row:
        # Propagate the stored row's ttl_exempt marker and recommended_action
        # into the eligibility dict so _compute_ttl sees the full picture.
        if live_row.get("ttl_exempt"):
            _ttl_check["ttl_exempt"] = True
        if live_row.get("recommended_action") is not None:
            _ttl_check["recommended_action"] = live_row["recommended_action"]

    _ttl = _compute_ttl(_ttl_check)

    # Determine whether to SET ttl_exempt on this write.
    _should_set_exempt = (
        finding.get("recommended_action") is not None
        or (live_row is not None and live_row.get("recommended_action") is not None)
        or (live_row is not None and live_row.get("ttl_exempt"))
    )
    if _should_set_exempt:
        expr_names["#ttl_exempt"] = "ttl_exempt"
        update_expr += ", #ttl_exempt = :ttl_exempt_val"
        expr_values[":ttl_exempt_val"] = {"BOOL": True}

    if _ttl is not None:
        expr_names["#ttl"] = "ttl"
        update_expr += ", #ttl = :ttl"
        expr_values[":ttl"] = {"N": str(_ttl)}
    else:
        # Ineligible row: register #ttl so the REMOVE clause can reference it.
        expr_names["#ttl"] = "ttl"

    # Emit a single REMOVE clause: standing attrs first (so
    # ``"REMOVE #owner_id"`` is a substring; existing tests pin this), then
    # FI optional attrs, then ttl when ineligible.
    ttl_to_remove: list[str] = [] if _ttl is not None else ["#ttl"]
    all_to_remove = standing_to_remove + fi_to_remove + ttl_to_remove
    if all_to_remove:
        # REMOVE clause comes after the whole SET clause, once.
        update_expr += " REMOVE " + ", ".join(all_to_remove)

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"finding_id": {"S": finding_id}},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
