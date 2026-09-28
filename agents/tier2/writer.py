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
) -> str:
    """Compute ``finding_key`` per Addendum D2.

    ``finding_key = SHA-256(JSON({"agent_id": ..., "finding_kind": ...,
                                   "scope_field": ..., "scope_value": ...}))``

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

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.
    """
    canonical = json.dumps(
        {
            "agent_id": agent_id,
            "finding_kind": finding_kind,
            "scope_field": scope_field,
            "scope_value": scope_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
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
    for field in ("vin", "owner_id", "fleet_id", "dealership_id", "district_id"):
        val = scope.get(field)
        if val is not None:
            return field, val
    raise ValueError(
        f"Finding has no populated scope field; scope={scope!r}. "
        "Schema requires exactly one of vin/owner_id/fleet_id/dealership_id/district_id."
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
    if live_row is not None:
        existing_inputs_hash = live_row.get("inputs_hash", "")
        if existing_inputs_hash == finding.get("inputs_hash", ""):
            desired = _standing_projection(finding, standing)
            persisted = {
                attr: live_row[attr]
                for attr in _STANDING_ATTRS
                if live_row.get(attr) is not None
            }
            if desired == persisted:
                if refresh_on_noop:
                    refreshed = _refresh_expiry(
                        table_name=table_name,
                        dynamodb_client=dynamodb_client,
                        finding_id=live_row["finding_id"],
                        expires_at=finding["expires_at"],
                        computed_at=finding["computed_at"],
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
            log.info(
                "write_finding: event=standing_drift_converged finding_key=%s "
                "changed=%s",
                finding_key,
                sorted(set(desired) ^ set(persisted))
                or sorted(k for k in desired if desired[k] != persisted.get(k)),
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

        _update_item(
            table_name=table_name,
            dynamodb_client=dynamodb_client,
            finding_id=finding_id,
            finding=finding,
            audit_entry=audit_entry,
            standing=standing,
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
) -> bool:
    """Extend a live row's ``expires_at`` and set ``computed_at``; ``True`` if written.

    Conditional on the row existing and on ``expires_at`` moving later, so a keep-alive
    can never shorten a row another producer gave a longer life. A failed condition is
    not an error: the row is already at least that fresh.
    """
    try:
        dynamodb_client.update_item(
            TableName=table_name,
            Key={"finding_id": {"S": finding_id}},
            UpdateExpression="SET expires_at = :e, computed_at = :c",
            ConditionExpression="attribute_exists(finding_id) AND expires_at < :e",
            ExpressionAttributeValues={":e": {"S": expires_at}, ":c": {"S": computed_at}},
        )
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
    table has no TTL attribute (``vsa-tier2-stack.ts:222-275``), so expired
    rows accumulate without being removed and this is not merely theoretical:
    after a clear → expiry → reinject cycle the expired row is always the
    first entry on the index.

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

    Fail-closed. A value is projected only when it is a non-empty string distinct
    from the scope value. ``resolve_standing_facts`` already normalises to exactly
    this contract; the re-check here is deliberate defence in depth, because the
    resolver is an injected seam a future caller could replace.
    """
    scope_field, scope_value = _extract_scope(finding)
    if scope_field != "vin" or standing is None:
        return {}

    out: dict[str, str] = {}
    for attr in ("owner_id", "fleet_id", "vehicle_id"):
        raw = getattr(standing, attr, None)
        if not isinstance(raw, str):
            continue
        candidate = raw.strip()
        if not candidate or candidate == scope_value:
            # A VIN is not a party, a fleet, or a vehicle id.
            continue
        out[attr] = candidate
    return out


#: The standing attributes this module may project. Named once so ``_update_item``
#: can converge every one of them — a name present here but absent from the
#: REMOVE path would leave a stale projection authorizing a party the registry no
#: longer names, which is the fail-open drift the contract § 6 forbids.
_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id")


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
) -> None:
    """UpdateItem preserving ``finding_id``, appending to ``audit_trail``.

    Updates:
    - ``expires_at`` extended to new value
    - ``headline``, ``detail``, ``evidence`` rewritten
    - ``inputs_hash``, ``computed_at`` updated
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
        ":new_audit_entry": _serializer.serialize(
            _coerce_floats([audit_entry])
        ),
        ":empty_list": {"L": []},
        ":scope_value": {"S": scope_value},
    }

    # Converge the standing projection on VIN-scoped Findings.
    if scope_field == "vin":
        projected = _standing_projection(finding, standing)
        to_remove: list[str] = []
        for attr in _STANDING_ATTRS:
            ph = f"#{attr}"
            expr_names[ph] = attr
            if attr in projected:
                update_expr += f", {ph} = :{attr}"
                expr_values[f":{attr}"] = {"S": projected[attr]}
            else:
                to_remove.append(ph)
        if to_remove:
            # REMOVE clause comes after the whole SET clause, once.
            update_expr += " REMOVE " + ", ".join(to_remove)

    dynamodb_client.update_item(
        TableName=table_name,
        Key={"finding_id": {"S": finding_id}},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
