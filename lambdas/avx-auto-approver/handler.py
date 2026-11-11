"""Auto-approver Lambda — spec D9, contract § 6 and § 9.

Triggered by DynamoDB Streams (NEW_IMAGE) on ``vsa-{stage}-avx-findings``.
For each new Finding with a ``recommended_action`` and a ``fleet_id``, checks
for enabled auto-approval rules.  On a match it writes the Finding's one
decision Action and dispatches it through ``registry.dispatch()``.

One decision per Finding (contract § 4): the ``action_id`` is derived from the
``finding_id`` alone via ``derive_action_id()``, identical to the formula used
by the approve and decline routes.  A conditional ``put_item`` with
``attribute_not_exists(action_id)`` ensures only the first decision wins; a
replayed stream record or a concurrent operator decision both get
``ConditionalCheckFailedException`` and are no-ops.

Partial-batch failure reporting: per-record errors are caught and reported as
``batchItemFailures`` keyed by ``SequenceNumber``, so one bad record cannot
block the shard (contract § 9, spec D9).

Limited kinds (stage E Group 8, U6; spec group8-notes G8-D7): ``move_vehicles`` is
considered only when ``FI_AUTO_MOVES_ENABLED`` is exactly ``"true"`` (set by the AVX live
flag). A move is approved only when its fleet is the Finding's, its VIN list is non-empty,
unique and within ``max_vehicles_per_auto_move``, and an enabled rule's ``limit`` is a
number within ``max_limit_usd`` and at or above its ``transfer_cost_usd``; the bounds come
from SSM (``AVX_AUTO_APPROVAL_BOUNDS_PARAM``). Anything else, or unreadable bounds, writes
no Action: the item stays in the operator's queue.

Environment variables (the three table names are required; unset fails closed):
  AVX_ACTIONS_TABLE              — DynamoDB table for Actions
  AVX_RULES_TABLE                — DynamoDB table for approval rules
  AVX_AGENT_RUNS_TABLE           — DynamoDB table for agent run records
  VSA_AGENT_VERSION              — agent version string (default ``"0.1.0"``)
  FI_AUTO_MOVES_ENABLED          — ``"true"`` to consider move_vehicles (default off)
  AVX_AUTO_APPROVAL_BOUNDS_PARAM — SSM parameter with the limited kinds' bounds
  FI_AUTO_REBOOK_ENABLED         — ``"true"`` to consider rule rebookings (book_fleet_service,
                                   spec D7; set only when ``FI_AUTO_REBOOK_LIVE``), with
  AVX_FINDINGS_TABLE, CMS_GROUNDINGS_TABLE — the predicate's reads (unset → no rebook)
  FI_SIM_OPERATOR_PARAM          — the hold list's SSM parameter (predicate g; unset → no rebook)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Path setup — allow imports from agents/ and lambdas/_lib/
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.abspath(os.path.dirname(__file__))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Table names. Fail closed: no default stage. A default once pointed a test's stray
# write at `vsa-dev-*` with real credentials (spec Fix Group 4); a misconfigured
# Lambda must error (and alarm), not write to another stage's tables.
# ---------------------------------------------------------------------------
# Read at call time (`_table_name`), so the check in `handler()` and the names used
# can never disagree.
_REQUIRED_ENV = ("AVX_ACTIONS_TABLE", "AVX_RULES_TABLE", "AVX_AGENT_RUNS_TABLE")


def _table_name(env_var: str) -> str:
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(f"auto-approver: {env_var} is not set")
    return value
_AGENT_VERSION = os.environ.get("VSA_AGENT_VERSION", "0.1.0")

# ---------------------------------------------------------------------------
# Token-free kinds (spec D9, contract § 6)
# ---------------------------------------------------------------------------

#: Kinds the evaluator may dispatch without a caller token.
#: In Stage A only ``acknowledge_only`` qualifies (token-free ``AdvisoryExecutor``).
#: Guarded kinds (G1–G4) must never appear here; ``dispatch()`` would refuse them
#: anyway, but the evaluator must not even attempt it (defence in depth, D9).
TOKEN_FREE_KINDS = frozenset({"acknowledge_only"})

#: Limited kinds (stage E Group 8, U6): approved only within a rule's ``limit`` on the
#: policy row's ``rule_limit_param`` and the configured bounds, and executed through a
#: service identity (CMS's IAM-signed internal route), not a caller token.
#: Stage D rebooking (G8-AVX-1): book_fleet_service added; its limit is the rebook
#: predicate (spec D7), not a numeric bound.
LIMITED_KINDS = frozenset({"move_vehicles", "book_fleet_service"})

#: CMS's internal route refuses anything else (services/fleet_intelligence/moves.py
#: ``_VIN_RE`` and ``_RULE_ID_RE``); approving it would spend the Finding's one decision
#: on an Action that can't execute.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_RULE_ID_RE = re.compile(r"^[^/\s]{1,128}$")


def _auto_moves_enabled() -> bool:
    return os.environ.get("FI_AUTO_MOVES_ENABLED") == "true"


def _auto_rebook_enabled() -> bool:
    """Return True iff FI_AUTO_REBOOK_ENABLED is exactly ``"true"`` (spec D7 / G8-AVX-1)."""
    return os.environ.get("FI_AUTO_REBOOK_ENABLED") == "true"


_ssm = None


def _rebook_held_ids() -> Optional[frozenset]:
    """The ids of cars a rule rebooking must never book, or None when they can't be read.

    The hold list is the simulated fleet operator's (``FI_SIM_OPERATOR_PARAM``, the SSM
    String ``/cvx/{stage}/fi/sim-operator``): ``hold_vins`` plus ``hold_vehicle_ids``, the
    per-car list that keeps the field-video car out of every automated decision
    (DEPLOYMENT.md § Simulated fleet operator; prompt 347). Returned as one set of ids: a
    held vehicle id is VIN-shaped (CMS vehicleId = VIN) and names the car whose VIN it is.

    Fails closed (None, logged) when: the env var is unset; the parameter is absent or
    can't be read; its value isn't a JSON object; either list is present but isn't a list
    of strings; or a held vehicle id isn't VIN-shaped. This Lambda has no registry read
    to resolve a legacy id (the stand-in resolves those through CMS's registry), so such
    an id stops every rule rebooking rather than leave its car unheld.

    Relies on CMS's vehicleId = VIN rule: a VIN-shaped held id is taken as the VIN without a
    registry read (the stand-in reads the registry row's ``vin``; the two agree under that rule).
    An absent parameter also fails closed, so rule rebooking needs it even where the stand-in is
    off (``{"daily_cap": 0, "fleets": {}}`` holds nothing; decisions.md 2026-11-08, prompt 347).
    """
    global _ssm
    name = os.environ.get("FI_SIM_OPERATOR_PARAM", "")
    if not name:
        log.warning("auto-approver: FI_SIM_OPERATOR_PARAM not set; rebook predicate fails")
        return None
    try:
        if _ssm is None:
            import boto3  # noqa: PLC0415 — lazy, like _get_ddb

            _ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        raw = json.loads(_ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except Exception as exc:  # noqa: BLE001 — absent, unreadable or not JSON: fail closed
        log.warning("auto-approver: hold list unreadable (%s); rebook predicate fails",
                    type(exc).__name__)
        return None
    if not isinstance(raw, dict):
        log.warning("auto-approver: hold list parameter is not a JSON object; rebook predicate fails")
        return None
    held: set = set()
    for key in ("hold_vins", "hold_vehicle_ids"):
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            log.warning("auto-approver: %s is not a list of strings; rebook predicate fails", key)
            return None
        if key == "hold_vehicle_ids" and not all(_VIN_RE.fullmatch(v) for v in values):
            log.warning("auto-approver: a held vehicle id is not VIN-shaped; rebook predicate fails")
            return None
        held.update(values)
    return frozenset(held)


def _load_bounds() -> dict:
    """The limited kinds' bounds (policy.parse_auto_approval_bounds), or BoundsUnavailable."""
    global _ssm
    from agents.executors.policy import load_auto_approval_bounds  # noqa: PLC0415

    if _ssm is None:
        import boto3  # noqa: PLC0415 — lazy, like _get_ddb

        _ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return load_auto_approval_bounds(_ssm, os.environ.get("AVX_AUTO_APPROVAL_BOUNDS_PARAM", ""))


def _move_within_bounds(fleet_id: str, parameters: dict, bounds: dict) -> Optional[float]:
    """The move's transfer cost if a rule could approve it under ``bounds``, else None.

    The Action-level rules CMS's internal route re-checks (group8-notes G8-D4: completeness,
    VIN format, fleet, count, cost), so a rule never approves a move CMS would refuse on these
    grounds. The registry, grounding, origin and capacity checks are CMS's alone.
    """
    from agents.executors.policy import finite_number  # noqa: PLC0415

    if not isinstance(parameters, dict) or parameters.get("fleet_id") != fleet_id:
        return None
    vins = parameters.get("vehicle_vins")
    if (not isinstance(vins, list) or not vins
            or not all(isinstance(v, str) and v for v in vins)
            or len(set(vins)) != len(vins)
            or len(vins) > bounds["max_vehicles_per_auto_move"]):
        return None
    if not all(_VIN_RE.fullmatch(v) for v in vins):
        return None
    for key in ("from_location_id", "to_location_id", "forecast_week"):
        if not isinstance(parameters.get(key), str) or not parameters[key]:
            return None
    for key in ("predicted_on_rent_hours_gain", "predicted_turnaways_avoided"):
        if finite_number(parameters.get(key)) is None:
            return None
    cost = finite_number(parameters.get("transfer_cost_usd"))
    if cost is None or cost < 0 or cost > bounds["max_limit_usd"]:
        return None
    return cost


def _rule_covers(rule: dict, cost: float, bounds: dict) -> bool:
    """An enabled limited-kind rule whose id CMS accepts and whose limit is a valid number
    within ``max_limit_usd`` covering ``cost``."""
    from agents.executors.policy import finite_number  # noqa: PLC0415

    if not isinstance(rule.get("rule_id"), str) or not _RULE_ID_RE.fullmatch(rule["rule_id"]):
        return False
    limit = finite_number(rule.get("limit"))
    return limit is not None and 0 <= limit <= bounds["max_limit_usd"] and cost <= limit

# ---------------------------------------------------------------------------
# Audit-entry keys (schema ``$defs/audit_entry.properties``, verbatim)
# ---------------------------------------------------------------------------

#: Schema-valid keys for a persisted audit entry (``additionalProperties: false``).
#: ``last_error_class`` is not one of them: it is never stored as its own key, only
#: folded into ``note`` by ``_build_audit_entry``.
_AUDIT_ENTRY_KEYS = frozenset({
    "at", "by", "from_status", "to_status", "note",
    "failure_kind", "retry_count",
    "failure_reason", "error_status", "guardrail",
    "target_reason",  # the target's own refusal sentence (contract § 10b)
})

# ---------------------------------------------------------------------------
# DynamoDB lazy singleton (injectable for tests via patch.object)
# ---------------------------------------------------------------------------

_ddb = None


def _get_ddb() -> Any:
    global _ddb
    if _ddb is None:
        import boto3  # noqa: PLC0415 — lazy to avoid import-time AWS calls

        _ddb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return _ddb


# ---------------------------------------------------------------------------
# Deterministic action_id (contract § 4)
# ---------------------------------------------------------------------------


def derive_action_id(finding_id: str, action_kind: str, rule_id: str) -> str:
    """Derive the one decision ``action_id`` for a Finding.

    Contract § 4::

        h = sha256("{finding_id}|fi-decision").hexdigest()[:32]
        action_id = "AVX-ACT-" + h[0:8]+"-"+h[8:12]+"-"+h[12:16]+"-"+h[16:20]+"-"+h[20:32]

    ``action_kind`` and ``rule_id`` are accepted for API symmetry with the
    approve and decline routes but are **not used** in the derivation: all
    rules on the same Finding share one decision slot (contract § 4, "one
    decision per Finding, derived from the finding_id alone").
    """  # noqa: D301
    _ = action_kind, rule_id  # accepted but unused — see docstring
    raw = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return (
        "AVX-ACT-"
        + raw[0:8] + "-" + raw[8:12] + "-" + raw[12:16]
        + "-" + raw[16:20] + "-" + raw[20:32]
    )


# ---------------------------------------------------------------------------
# DynamoDB Streams deserializer
# ---------------------------------------------------------------------------

def _deserialize_image(image: dict) -> dict:
    """Convert a DynamoDB Streams ``NEW_IMAGE`` typed map to plain Python."""
    from boto3.dynamodb.types import TypeDeserializer  # noqa: PLC0415

    deser = TypeDeserializer()
    return {k: deser.deserialize(v) for k, v in image.items()}


# ---------------------------------------------------------------------------
# Rules loader (injectable via patch.object for tests)
# ---------------------------------------------------------------------------


def _get_rules_for_fleet_kind(fleet_id: str, action_kind: str) -> list:
    """Return enabled rules for ``(fleet_id, action_kind)`` from the rules table.

    Queries by ``fleet_id`` (the partition key) and filters by ``action_kind``
    and ``enabled=True`` client-side; a fleet has at most a handful of rules so
    a full-fleet scan is acceptable.
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415

    table = _get_ddb().Table(_table_name("AVX_RULES_TABLE"))
    # Strongly consistent (stage E Group 8 review c1): a rule disabled or its limit lowered a
    # moment ago must be seen, as CMS's internal route and the rules API see it.
    resp = table.query(KeyConditionExpression=Key("fleet_id").eq(fleet_id), ConsistentRead=True)
    return [
        r for r in resp.get("Items", [])
        if r.get("action_kind") == action_kind and r.get("enabled") is True
    ]


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC, millisecond precision, Z-suffixed — matches CMS _ISO_RE and schema fixtures."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Audit-entry builder (spec D14, contract § 10a)
# ---------------------------------------------------------------------------


def _build_audit_entry(result: Any, *, target_system: str, at: str) -> dict:
    """Project ``ExecuteResult.audit_entry()`` onto the schema's allowed keys.

    ``last_error_class`` is not a schema key; it is folded into ``note``, as the
    findings Lambda does.  ``failure_reason``
    and ``error_status`` are included when present; they are omitted (never
    null) by ``audit_entry()`` when not set.  The reason is written on every
    transition to ``declined`` or ``failed`` by ``registry.dispatch()``
    (spec D14 / contract § 10a).
    """
    raw = result.audit_entry(by=f"executor-{target_system}", at=at)
    entry = {k: raw[k] for k in _AUDIT_ENTRY_KEYS if k in raw}
    # Group 2 security review S4: same convention as the findings Lambda's
    # `_persisted_audit_entry`: the error class (a class name, never a message) is
    # folded into `note` so an operator sees it; the HTTP status is `error_status`.
    if raw.get("last_error_class") is not None:
        entry["note"] = "; ".join(
            filter(None, [entry.get("note"), f"last_error_class={raw['last_error_class']}"]))
    return entry


# ---------------------------------------------------------------------------
# Rebook predicate (spec D7 / G8-AVX-1)
# ---------------------------------------------------------------------------

#: ``subject_key`` pattern for a health-service booking Finding.  Capture group 1 is
#: the attempt number.  ``[1-9]\d*`` forbids leading zeros and ``n == 0``.
_REBOOK_SUBJECT_RE = re.compile(r"^service/([1-9]\d*)$")
#: Hard cap on GSI pages when scanning findings by VIN (defensive; the fleet's
#: health loop emits at most ~O(100) findings per VIN over its lifetime).
_MAX_FINDINGS_PAGES = 10


def _latest_attempt(candidates: list) -> Optional[dict]:
    """The Finding the fleet-health loop treats as the attempt: the latest ``computed_at``.

    Mirrors ``read_attempt_history_health``, which keeps, per segment, the highest attempt
    and on a tie the greater ``computed_at`` string. Here every candidate already has the
    same attempt number. Fails closed (``None``) on no candidates, on a candidate without
    a string ``computed_at``, and on two candidates sharing the latest one: the loop keeps
    whichever it read first, so the rule can't tell which attempt it followed.
    """
    if not candidates:
        return None
    stamps = [c.get("computed_at") for c in candidates]
    if any(not isinstance(s, str) or not s for s in stamps):
        return None
    latest = max(stamps)
    chosen = [c for c, s in zip(candidates, stamps) if s == latest]
    if len(chosen) != 1:
        return None
    return chosen[0]


def _previous_attempt(vin: str, prev_sk: str) -> Optional[dict]:
    """Predicate (c): the ``service/{n-1}`` Finding the new one follows, or ``None``.

    Reads every fleet-health Finding for the VIN (``by_vin``, paged, capped) and returns
    :func:`_latest_attempt` of those with ``subject_key == prev_sk``. ``None`` when the
    table name is unset, a read fails, the page cap leaves pages unread, or no single latest
    attempt exists (fail closed; spec D7). Read-only, so live checks can call it.
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415

    try:
        findings_table_name = _table_name("AVX_FINDINGS_TABLE")
    except RuntimeError:
        log.warning("auto-approver: AVX_FINDINGS_TABLE not set; rebook predicate fails")
        return None
    try:
        prev_findings: list = []
        gkw: dict = {
            "IndexName": "by_vin",
            "KeyConditionExpression": Key("vin").eq(vin),
        }
        pages = 0
        ftable = _get_ddb().Table(findings_table_name)
        while pages < _MAX_FINDINGS_PAGES:
            resp = ftable.query(**gkw)
            for item in resp.get("Items", []):
                # Every fleet's Findings are candidates, as in the loop's reader; the
                # caller checks the chosen one's fleet.
                if (item.get("agent_id") == "agent-fi-fleet-health"
                        and item.get("subject_key") == prev_sk):
                    prev_findings.append(item)
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            gkw["ExclusiveStartKey"] = last
            pages += 1
        else:
            # W3 fix: loop exhausted all pages without a break.  If the last page still
            # had a LastEvaluatedKey the read is partial — spec D7 says anything
            # unreadable writes no Action (fail closed).
            if resp.get("LastEvaluatedKey"):  # type: ignore[possibly-undefined]
                log.warning(
                    "auto-approver: rebook findings query hit page cap with pages remaining; "
                    "failing closed"
                )
                return None
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-approver: rebook findings query failed (%s)", type(exc).__name__)
        return None
    return _latest_attempt(prev_findings)


def _rebook_qualifies(finding: dict, fleet_id: str, parameters: dict) -> bool:
    """Return True iff the rebooking predicate passes (spec D7 / G8-AVX-1, all a–g).

    (g), the hold list, was added 2026-11-08 (prompt 347) before the flag went live.
    Any read error or missing env var → False (warning logged; never raises).
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415
    from agents.executors.registry import _is_valid_rule_decider, _is_valid_operator  # noqa: PLC0415
    from agents.executors.simulated_operator import is_simulated_decider as _is_sim  # noqa: PLC0415

    # a. agent_id, target_system, subject_key fullmatch service/([1-9]\d*), n ≥ 2.
    if finding.get("agent_id") != "agent-fi-fleet-health":
        return False
    if (finding.get("recommended_action") or {}).get("target_system") != "dms":
        return False
    subject_key = finding.get("subject_key", "")
    m = _REBOOK_SUBJECT_RE.fullmatch(subject_key)
    if not m:
        return False
    n = int(m.group(1))
    if n < 2:
        return False

    # b. VIN (17-char) and fleet_id.
    vin = parameters.get("vehicle_vin", "")
    if not isinstance(vin, str) or len(vin) != 17:
        return False

    # g. The car isn't on the hold list (prompt 347: the field-video car's booking must
    #    never be rebooked by a rule). Every id the Finding and its parameters give the car
    #    is checked; an unreadable list writes no Action.
    held = _rebook_held_ids()
    if held is None:
        return False
    car_ids = {
        vin,
        parameters.get("vehicle_id"),
        finding.get("vin"),
        finding.get("vehicle_id"),
        (finding.get("scope") or {}).get("vin"),
    }
    if any(isinstance(c, str) and c in held for c in car_ids):
        log.info("auto-approver: %r names a held car; not rebooked", finding.get("finding_id"))
        return False

    # new Finding's need ids (non-empty) — taken from the booked needs
    # (parameters.needs), NOT from evidence.  Evidence may carry needs that the
    # model deferred from the booking; comparing against evidence would let a
    # rebooking that adds work slip through the equality check (W1, review F1).
    new_need_ids = frozenset(
        n_item.get("need_id")
        for n_item in (parameters.get("needs") or [])
        if isinstance(n_item, dict) and n_item.get("need_id")
    )
    if not new_need_ids:
        return False

    # c. The attempt this Finding follows: of the VIN's fleet-health Findings with
    #    subject_key == service/{n-1}, the one with the latest computed_at, the same
    #    choice the fleet-health loop makes when it writes service/n
    #    (agents/fi_fleet_health/loop.py read_attempt_history_health). Older siblings
    #    are expired, undecided Findings the loop re-wrote under a new finding_id
    #    (prompt 359, issue 2026-11-09-rebook-counts-expired-siblings); they are not the
    #    attempt and no longer stop the rebook. Fails closed when there is none, when two
    #    share the latest computed_at, when it has no computed_at, or when it belongs to
    #    another fleet.
    prev_sk = f"service/{n - 1}"
    prev_finding = _previous_attempt(vin, prev_sk)
    if prev_finding is None:
        return False
    if prev_finding.get("fleet_id") != fleet_id:
        return False
    prev_finding_id = prev_finding.get("finding_id")
    if not isinstance(prev_finding_id, str) or not prev_finding_id:
        return False

    # d. Previous action: book_fleet_service, status == "cancelled", same fleet and VIN,
    #    decided by a valid operator or valid rule — not the simulated operator.
    prev_action_id = derive_action_id(prev_finding_id, "", "")
    try:
        atbl = _get_ddb().Table(_table_name("AVX_ACTIONS_TABLE"))
        prev_action = atbl.get_item(
            Key={"action_id": prev_action_id}, ConsistentRead=True,
        ).get("Item")
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-approver: rebook action lookup failed (%s)", type(exc).__name__)
        return False
    if prev_action is None:
        return False
    if prev_action.get("action_kind") != "book_fleet_service":
        return False
    if prev_action.get("status") != "cancelled":
        return False
    if prev_action.get("fleet_id") != fleet_id:
        return False
    prev_params = prev_action.get("parameters") or {}
    if prev_params.get("vehicle_vin") != vin:
        return False
    prev_db = prev_action.get("decided_by") or {}
    if _is_sim(prev_db):
        return False
    prev_db_kind = prev_db.get("kind")
    if prev_db_kind == "operator":
        # R3/suggestion fix: require a non-empty string id (not just any truthy value).
        if not _is_valid_operator(prev_db):
            return False
    elif prev_db_kind == "rule":
        if not _is_valid_rule_decider(prev_db):
            return False
    else:
        return False

    # e. Need ids match (both non-empty, equal sets).
    prev_need_ids = frozenset(
        n_item.get("need_id")
        for n_item in (prev_params.get("needs") or [])
        if isinstance(n_item, dict) and n_item.get("need_id")
    )
    if not prev_need_ids or new_need_ids != prev_need_ids:
        return False

    # f. Open FI fault grounding for the VIN in CMS groundings.
    try:
        groundings_table_name = _table_name("CMS_GROUNDINGS_TABLE")
    except RuntimeError:
        log.warning("auto-approver: CMS_GROUNDINGS_TABLE not set; rebook predicate fails")
        return False
    try:
        gkw2: dict = {
            "IndexName": "open-groundings-index",
            "KeyConditionExpression": Key("openFleetId").eq(fleet_id),
        }
        found_grounding = False
        gtbl = _get_ddb().Table(groundings_table_name)
        while True:
            gresp = gtbl.query(**gkw2)
            for row in gresp.get("Items", []):
                if (row.get("vin") == vin
                        and row.get("reason") == "fault"
                        and row.get("findingId")
                        and row.get("actionId")):
                    found_grounding = True
                    break
            if found_grounding:
                break
            last = gresp.get("LastEvaluatedKey")
            if not last:
                break
            gkw2["ExclusiveStartKey"] = last
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-approver: rebook groundings query failed (%s)", type(exc).__name__)
        return False
    return found_grounding


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------


def _process_record(record: dict) -> None:
    """Process one DynamoDB Streams record.

    Raises on any unrecoverable error so the outer loop can report it as a
    ``batchItemFailure``.  Returns silently when the record should be skipped
    (no ``NewImage``, no ``recommended_action``, no fleet, no matching rule).
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415
    from agents.executors import registry  # noqa: PLC0415
    from agents.executors.policy import ACTION_KINDS  # noqa: PLC0415

    if record.get("eventName") not in ("INSERT", "MODIFY"):
        # REMOVE (TTL expiry, deletes) carries no NewImage; it is not a decision
        # to make, and raising here would retry the shard forever.
        return None

    image = record.get("dynamodb", {}).get("NewImage")
    if not image:
        # An INSERT or MODIFY with no NewImage is a malformed record.
        raise ValueError(
            f"Record seq={record.get('dynamodb', {}).get('SequenceNumber')!r} "
            "has no NewImage"
        )

    finding = _deserialize_image(image)
    recommended = finding.get("recommended_action")
    if not recommended:
        return  # Not an FI queue item; nothing to evaluate

    fleet_id = finding.get("fleet_id")
    if not fleet_id:
        return  # No fleet → no rule can apply (fail-closed per D8)

    finding_id = finding.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError("Finding image has no finding_id")
    expires_at = finding.get("expires_at")
    # F1.5 S2: compare parsed UTC datetimes, not strings.
    # String comparison fails for seconds-precision expires_at values: the Z suffix
    # sorts after '.' (ord('Z')=90 > ord('.')=46), so "…:00Z" > "…:00.400Z" — a
    # recommendation that expired 400 ms ago would be incorrectly treated as live.
    # A malformed expires_at is treated as expired (fail-closed).
    if not isinstance(expires_at, str):
        return None  # undated → never auto-approve
    try:
        _expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if _expires_dt <= datetime.now(timezone.utc):
            return None  # expired
    except (ValueError, AttributeError, TypeError):
        # ValueError/AttributeError: fromisoformat failed (malformed string).
        # TypeError: fromisoformat succeeded but returned a naive datetime (no offset),
        # and comparing naive <= offset-aware raises TypeError.  Both cases are treated
        # as expired (fail-closed) — G2.2 S3/security-S1.
        return None  # malformed or naive expires_at → treat as expired (fail-closed)
    action_kind = recommended.get("action_kind", "")
    parameters = recommended.get("parameters") or {}

    # Only token-free kinds, and limited kinds when switched on, may be auto-approved
    # (spec D9, contract § 6; G8-D7). Defence in depth: dispatch() would refuse guarded
    # kinds anyway.
    limited = action_kind in LIMITED_KINDS
    if action_kind not in TOKEN_FREE_KINDS and not (
        limited and (
            (action_kind == "move_vehicles" and _auto_moves_enabled()) or
            (action_kind == "book_fleet_service" and _auto_rebook_enabled())
        )
    ):
        return

    # Policy table double-check: kind must be marked auto_approvable.
    policy_row = ACTION_KINDS.get(action_kind)
    if policy_row is None or not policy_row.auto_approvable:
        return

    # S6 fix: take target_system from the policy row, not the Finding's recommendation.
    # A Finding whose recommended target_system disagrees with the policy table is a
    # data-integrity issue; skip rather than dispatch to the wrong system. For
    # move_vehicles (stage E Group 8) this is what keeps a rule's move on CMS.
    target_system = policy_row.target_system
    finding_target = recommended.get("target_system", "none")
    if finding_target != target_system:
        log.warning(
            "auto-approver: skipping finding %r — recommended target %r disagrees "
            "with policy target %r for kind %r",
            finding_id, finding_target, target_system, action_kind,
        )
        return  # data-integrity mismatch; skip, do not dispatch

    if action_kind == "move_vehicles" and policy_row.rule_limit_param != "transfer_cost_usd":
        return None  # move_vehicles must name the parameter its rule limits (a rebook has none)

    rules = _get_rules_for_fleet_kind(fleet_id, action_kind)
    if not rules:
        return
    if action_kind == "move_vehicles":
        # Bounds only once a fleet has an enabled rule for the kind: most move Findings
        # belong to fleets with none, and each read is an SSM call.
        try:
            bounds = _load_bounds()
        except Exception as exc:  # noqa: BLE001 — fail closed: the item stays queued
            log.warning("auto-approver: bounds unavailable (%s); %r not auto-approved",
                        type(exc).__name__, finding_id)
            return None
        cost = _move_within_bounds(fleet_id, parameters, bounds)
        if cost is None:
            return None
        rules = sorted((r for r in rules if _rule_covers(r, cost, bounds)),
                       key=lambda r: str(r.get("rule_id", "")))
        if not rules:
            return
    elif action_kind == "book_fleet_service":
        # Rebook predicate: all conditions a–g (spec D7 / G8-AVX-1; g, the hold list, prompt 347) must hold.
        if not _rebook_qualifies(finding, fleet_id, parameters):
            return None
        # W2 fix (review F1 / spec D10e): validate and complete parameters via the
        # server-set rule before writing the Action.  A p0 booking with no candidate
        # rooftop carries dealer_id=None; writing without this check spends the
        # Finding's one decision and then _fleet_service_body returns None
        # (MissingDispatchParameter).  Skip on error: item stays in the queue.
        from agents.executors.server_set import check_server_set_params  # noqa: PLC0415
        _completed_params, _ss_err, _ = check_server_set_params(finding, action_kind, {})
        if _ss_err:
            log.warning(
                "auto-approver: rebook server-set check failed (%s); %r not auto-approved",
                _ss_err, finding_id,
            )
            return None
        parameters = _completed_params

    # Pick the first matching enabled rule.  All matching rules share the same
    # action_id slot (one decision per Finding); the first conditional put wins.
    rule = rules[0]
    rule_id = rule["rule_id"]

    action_id = derive_action_id(finding_id, action_kind, rule_id)
    decided_by = {"kind": "rule", "id": f"{fleet_id}/{rule_id}"}
    created_by = f"rule-{fleet_id}/{rule_id}"
    now = _now_iso()

    action = {
        "action_id": action_id,
        "finding_id": finding_id,
        "action_kind": action_kind,
        "target_system": target_system,
        "parameters": parameters,
        # Critical 1 fix (review FG2-lambdas): evidence is required by action.schema.json.
        # Copy the Finding's evidence at decision time; [] when absent (fleet-scoped Findings
        # may carry no per-vehicle evidence). The approve route does the same (§ 7).
        "evidence": finding.get("evidence") or [],
        "status": "pending_execution",
        "decided_by": decided_by,
        "created_by": created_by,
        "decision": "auto_approved",
        "fleet_id": fleet_id,   # storage-only attribute (contract § 7)
        "audit_trail": [],
        "created_at": now,
        "updated_at": now,
    }

    actions_table = _get_ddb().Table(_table_name("AVX_ACTIONS_TABLE"))

    # Conditional put: the first decision wins (contract § 4).
    # A replay or a concurrent operator approval both get CCF → noop.
    try:
        actions_table.put_item(
            Item=action,
            ConditionExpression="attribute_not_exists(action_id)",
        )
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return "duplicate"  # Already decided: replay or race → noop
        raise

    # Dispatch through registry so guardrails G1–G4 fire (defence in depth).
    result = registry.dispatch(action, {})

    # Guard against a test mock returning None.
    if result is None:
        return "written"

    # Advance the Action with the dispatch result.
    at = _now_iso()
    entry = _build_audit_entry(result, target_system=target_system, at=at)

    update_expr = "SET #s = :to, #u = :now, #a = list_append(#a, :entry)"
    update_vals: dict = {
        ":to": result.status,
        ":now": at,
        ":entry": [entry],
        ":pending": "pending_execution",
        ":in_progress": "in_progress",
    }
    if getattr(result, "target_ref", None):
        update_expr += ", #r = :ref"
        update_vals[":ref"] = result.target_ref

    try:
        actions_table.update_item(
            Key={"action_id": action_id},
            UpdateExpression=update_expr,
            ConditionExpression=(
                "#s = :pending OR (#s = :in_progress AND attribute_not_exists(#r))"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#u": "updated_at",
                "#a": "audit_trail",
                "#r": "target_ref",
            },
            ExpressionAttributeValues=update_vals,
        )
    except ClientError as exc:
        # If the action moved out of pending_execution (e.g. cancelled), the
        # update is a noop; log but don't propagate.
        log.warning(
            "auto-approver: could not advance action %s after dispatch (%s)",
            action_id, type(exc).__name__,
        )
    return "written"


# ---------------------------------------------------------------------------
# Run record writer
# ---------------------------------------------------------------------------


def _write_run_record(
    *,
    started_at: str,
    finished_at: str,
    artifacts_written: int,
    duplicates_skipped: int,
) -> None:
    """Write one run record per batch (spec D10, contract § 8)."""
    from agents.tier2.run_records import write_run_record  # noqa: PLC0415

    write_run_record(
        agent_id="agent-fi-evaluator",
        component="evaluator",
        started_at=started_at,
        finished_at=finished_at,
        status="completed",
        artifacts_written=artifacts_written,
        duplicates_skipped=duplicates_skipped,
        agent_version=_AGENT_VERSION,
        ddb_client=_get_ddb(),
        table_name=_table_name("AVX_AGENT_RUNS_TABLE"),
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:  # noqa: ARG001
    """DynamoDB Streams Lambda handler (spec D9).

    Returns ``{"batchItemFailures": [...]}`` for any record that could not be
    processed.  The partial-batch-failure contract ensures a bad record does not
    block the shard while healthy records are skipped.
    """
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"auto-approver: required environment variable(s) unset: {missing}")
    batch_failures: list[dict] = []
    started_at = _now_iso()
    artifacts_written = 0
    duplicates_skipped = 0

    for record in event.get("Records", []):
        seq: Optional[str] = record.get("dynamodb", {}).get("SequenceNumber")
        try:
            outcome = _process_record(record)
            if outcome == "written":
                artifacts_written += 1
            elif outcome == "duplicate":
                duplicates_skipped += 1
        except Exception as exc:  # noqa: BLE001
            log.error(
                "auto-approver: failed to process record seq=%s (%s)",
                seq, type(exc).__name__,
            )
            if seq:
                batch_failures.append({"itemIdentifier": seq})

    finished_at = _now_iso()
    try:
        _write_run_record(
            started_at=started_at,
            finished_at=finished_at,
            artifacts_written=artifacts_written,
            duplicates_skipped=duplicates_skipped,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto-approver: could not write run record (%s)", type(exc).__name__
        )

    return {"batchItemFailures": batch_failures}
