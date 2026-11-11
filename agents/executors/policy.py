"""Action-kind policy table — spec D4, contract § 4.

`ACTION_KINDS` is the single source of truth for what each kind can do.
The approve route, the rule API, the rule evaluator and `dispatch()` all read it
so they cannot disagree.

An import-time check in both directions ensures no kind in the schema enum is
missing from the table and no table key is absent from the schema.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Optional

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "agents" / "tier2" / "schemas" / "action.schema.json"
)


@dataclass(frozen=True)
class PolicyRow:
    """One row per ``action_kind`` in the v2.5 enum.

    Attributes
    ----------
    target_system:
        The ``Action.target_system`` this kind dispatches to.
    executable:
        True iff a working executor exists in this deployment.
    operator_required:
        True iff a human deciding it must decide on operator standing
        (``decided_by.kind == "operator"``). All new FI kinds are ``True``; pre-existing
        kinds are ``False``. A rule (``decided_by.kind == "rule"``) is not a human decider:
        an ``auto_approvable`` kind may also be decided by one.
    auto_approvable:
        True iff an auto-approval rule may decide it.  ``acknowledge_only`` (token-free
        executor, Stage A) and, from stage E Group 8, ``move_vehicles``: the first
        limited kind, which a rule may approve only within its ``limit`` and the
        configured bounds (U6). A human deciding a kind still needs ``operator_required``.
    rule_limit_param:
        The parameter name that carries the per-rule numeric limit, or ``None``
        for kinds that have no quantitative bound. A rule for a kind with one must carry a
        ``limit``, validated against ``AUTO_APPROVAL_BOUNDS_PARAM``'s ``max_limit_usd``.
    """

    target_system: str
    executable: bool
    operator_required: bool
    auto_approvable: bool
    rule_limit_param: Optional[str] = None
    server_set: tuple[str, ...] = ()
    """Parameters the server fills from the recommendation (body must omit or match). Spec D11."""
    body_overridable: tuple[str, ...] = ()
    """Parameters the body may supply differently; doing so makes the decision 'overridden'."""


#: Every ``action_kind`` in the v2.5 enum. Spec D4, contract § 4 table.
#:
#: Spec 1a kinds keep today's behaviour exactly (review Cycle 1 C2).
#: Retail kinds: executor exists; the approve route's _RETAIL_KINDS runtime
#: gate still applies on top of this table.
#: FI kinds: all operator_required=True (spec D4/D5); only book_recall_service
#: is executable in Stage A.
ACTION_KINDS: dict[str, PolicyRow] = {
    # ---- Spec 1a: unchanged -----------------------------------------------
    "book_combined_visit": PolicyRow(
        target_system="dms", executable=True,
        operator_required=False, auto_approvable=False),
    "schedule_inspection": PolicyRow(
        target_system="dms", executable=True,
        operator_required=False, auto_approvable=False),
    "update_charging_schedule": PolicyRow(
        target_system="vehicle-command", executable=True,
        operator_required=False, auto_approvable=False),
    "file_warranty_pre": PolicyRow(
        target_system="none", executable=True,
        operator_required=False, auto_approvable=False),
    "surface_recall": PolicyRow(
        target_system="none", executable=True,
        operator_required=False, auto_approvable=False),
    "acknowledge_only": PolicyRow(
        target_system="none", executable=True,
        operator_required=False, auto_approvable=True),
    # ---- Retail kinds: _RETAIL_KINDS gate still applies -------------------
    "lead_capture": PolicyRow(
        target_system="retail", executable=True,
        operator_required=False, auto_approvable=False),
    "configurator_recommend": PolicyRow(
        target_system="retail", executable=True,
        operator_required=False, auto_approvable=False),
    # ---- FI kinds (Stage A) -----------------------------------------------
    # Only book_recall_service is executable; the rest wait for their stage.
    "book_recall_service": PolicyRow(
        target_system="dms", executable=True,
        operator_required=True, auto_approvable=False,
        server_set=("vehicle_vin", "recall_id"),
        body_overridable=("dealer_id",)),
    # submit_warranty_claim and ground_vehicle become executable in Stage C (spec D11).
    "submit_warranty_claim": PolicyRow(
        target_system="dms", executable=True,
        operator_required=True, auto_approvable=False,
        server_set=("vehicle_vin", "ro_id", "line_no", "dealer_id", "labor_op", "fault_code"),
        body_overridable=("notes",)),
    "ground_vehicle": PolicyRow(
        target_system="cms", executable=True,
        operator_required=True, auto_approvable=False,
        server_set=("vehicle_vin", "recall_id"),
        body_overridable=()),
    # Stage E Group 8 (U6): a rule may approve a move within its limit on
    # transfer_cost_usd and the configured bounds; the auto-approver sends it to CMS's
    # IAM-signed internal route, which re-checks the rule (group8-notes G8-D4..D8).
    "move_vehicles": PolicyRow(
        target_system="cms", executable=True,
        operator_required=True, auto_approvable=True,
        rule_limit_param="transfer_cost_usd",
        server_set=(
            "fleet_id", "from_location_id", "to_location_id", "forecast_week",
            "transfer_cost_usd", "predicted_on_rent_hours_gain",
            "predicted_turnaways_avoided",
        ),
        body_overridable=("vehicle_vins",)),
    "exempt_recall": PolicyRow(
        target_system="cms", executable=False,
        operator_required=True, auto_approvable=False),
    "complete_recall": PolicyRow(
        target_system="cms", executable=False,
        operator_required=True, auto_approvable=False),
    # apply_cost_action: target set by Stage B (spec D9, task 3.3).
    # operator_required=True: only fleet operators may decide (spec D9/D4).
    # auto_approvable=False: no rule may approve a cost action (spec D9).
    "apply_cost_action": PolicyRow(
        target_system="cms", executable=True,
        operator_required=True, auto_approvable=False),
    # ---- Stage D kinds (spec 2026-10-05-fi-fleet-health-stage-d) ----------
    # Executors land in task 3.7 (AdpExecutor) and are wired in registry.py at
    # that point. Policy rows are required by _check_policy_matches_schema() now.
    "book_fleet_service": PolicyRow(
        target_system="dms", executable=True,
        operator_required=True, auto_approvable=True,
        server_set=("vehicle_vin", "needs"),
        body_overridable=("dealer_id", "urgency")),
    # Engineering kinds: executable=True; operator_required=False because they
    # require decided_by.kind=="engineer" (G5), not an operator.  The approve
    # route checks product-engineer group before dispatching; G5 in registry.py
    # blocks any non-engineer that gets through.
    "record_engineering_decision": PolicyRow(
        target_system="adp", executable=True,
        operator_required=False, auto_approvable=False,
        server_set=("investigation_id", "pattern_id"),
        body_overridable=("option_id", "notes")),
    "issue_recall": PolicyRow(
        target_system="adp", executable=True,
        operator_required=False, auto_approvable=False,
        server_set=("thread_id", "draft_id"),
        body_overridable=()),
    "issue_software_campaign": PolicyRow(
        target_system="adp", executable=True,
        operator_required=False, auto_approvable=False,
        server_set=("thread_id", "draft_id"),
        body_overridable=()),
}


def _check_policy_matches_schema() -> None:
    """Import-time integrity check: the table must cover the schema enum exactly.

    A kind in the schema enum that is absent from ACTION_KINDS means dispatch()
    cannot route it.  A key in ACTION_KINDS that is absent from the schema means
    a policy row exists for a kind that cannot be written — dead code at best.
    """
    schema = json.loads(_SCHEMA_PATH.read_text())
    schema_kinds = set(schema["properties"]["action_kind"]["enum"])
    table_kinds = set(ACTION_KINDS)

    missing = schema_kinds - table_kinds
    if missing:
        raise RuntimeError(
            f"policy.ACTION_KINDS is missing kind(s) from the schema enum: "
            f"{sorted(missing)}. Add a row for each, or the dispatcher cannot "
            "route Actions of that kind."
        )

    extra = table_kinds - schema_kinds
    if extra:
        raise RuntimeError(
            f"policy.ACTION_KINDS contains kind(s) absent from the schema enum: "
            f"{sorted(extra)}. Remove the row(s), or add the kind(s) to the schema."
        )


_check_policy_matches_schema()


# ---------------------------------------------------------------------------
# Auto-approval bounds for limited kinds (stage E Group 8, U6; group8-notes G8-D5)
# ---------------------------------------------------------------------------

#: SSM parameter (``{stage}`` substituted), seeded from
#: ``deployment/config/avx-policy-defaults.json``. Read at run time by the rules API,
#: the auto-approver and CMS's internal move route; never at deploy.
AUTO_APPROVAL_BOUNDS_PARAM = "/vsa/{stage}/avx/fi-rebalancing/auto-approval-bounds"

#: Exactly these keys.
AUTO_APPROVAL_BOUND_KEYS = frozenset({"max_limit_usd", "max_vehicles_per_auto_move"})

#: Ceilings that turn a typo in the parameter into a refusal, not a wider rule.
_BOUND_LIMIT_CEILING_USD = 100_000
_BOUND_VEHICLES_CEILING = 50


class BoundsUnavailable(RuntimeError):
    """The auto-approval bounds are absent, unreadable or malformed. Callers fail closed."""


def finite_number(value: object) -> Optional[float]:
    """A finite float from an int, float or Decimal (never a bool or a string), else None."""
    import math  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        out = float(value)
    except OverflowError:  # an int beyond float range (json.loads makes one from 1e400 digits)
        return None
    return out if math.isfinite(out) else None


def parse_auto_approval_bounds(value: object) -> dict:
    """The validated bounds from a JSON string or dict, else :class:`BoundsUnavailable`.

    Exactly :data:`AUTO_APPROVAL_BOUND_KEYS`; ``max_limit_usd`` a number in (0, 100,000];
    ``max_vehicles_per_auto_move`` an integer from 1 to 50.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise BoundsUnavailable("bounds are not JSON") from exc
    if not isinstance(value, dict) or set(value) != AUTO_APPROVAL_BOUND_KEYS:
        raise BoundsUnavailable(
            f"bounds must be an object with exactly {sorted(AUTO_APPROVAL_BOUND_KEYS)}")
    limit = finite_number(value["max_limit_usd"])
    if limit is None or not 0 < limit <= _BOUND_LIMIT_CEILING_USD:
        raise BoundsUnavailable("max_limit_usd must be a number in (0, 100000]")
    veh = value["max_vehicles_per_auto_move"]
    if isinstance(veh, bool) or not isinstance(veh, int) or not 1 <= veh <= _BOUND_VEHICLES_CEILING:
        raise BoundsUnavailable(
            f"max_vehicles_per_auto_move must be an integer from 1 to {_BOUND_VEHICLES_CEILING}")
    return {"max_limit_usd": limit, "max_vehicles_per_auto_move": veh}


def load_auto_approval_bounds(ssm_client: object, name: object) -> dict:
    """Read and validate the bounds parameter named ``name``, or raise BoundsUnavailable."""
    if not isinstance(name, str) or not name.startswith("/") or "{" in name:
        raise BoundsUnavailable("no bounds parameter is configured")
    try:
        raw = ssm_client.get_parameter(Name=name)["Parameter"]["Value"]  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — any read failure fails closed
        raise BoundsUnavailable(type(exc).__name__) from exc
    return parse_auto_approval_bounds(raw)


def rule_limit_error(limit: object, bounds: dict) -> Optional[str]:
    """Why ``limit`` can't be a limited kind's rule limit under ``bounds``, or None."""
    number = finite_number(limit)
    if number is None:
        return "limit must be a number"
    if not 0 <= number <= bounds["max_limit_usd"]:
        return f"limit must be from 0 to {bounds['max_limit_usd']:g}"
    return None
