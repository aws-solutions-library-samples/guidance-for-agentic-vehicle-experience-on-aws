"""Investigation bundle builder for the FI quality agent — Stage D task 3.3.

Spec D10 step 1: the bundle carries fleet_type as a cohort label (e.g.
``rental``, ``delivery``, ``field_service``, ``city``) and strips all
fleet-private fields recursively. No fleet id, name, driver, owner,
position, trip, cost or dealer survives into the bundle.

Public API
----------
build_bundle(pattern, failure_modes, fleet_type, affected_vins,
             controls, service_history, *, candidates=None) -> dict
    Build an investigation bundle from corpus and pattern data, with all
    fleet-private fields removed.

_FORBIDDEN_FIELDS
    The set of field names that must never appear in a bundle at any depth.
    Exposed for tests and documentation.
"""
from __future__ import annotations

from typing import Any, Optional

#: Fields that must never appear in the bundle at any nesting depth.
#: ``fleet_type`` is NOT in this set — it is carried as a cohort label.
#: Mutation guard (test_bundle_contains_no_fleet_private_fields):
#:   adding fleet_id to build_bundle's output causes the recursive scan to fire.
_FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "fleet_id",
    "fleet_name",
    "driver_id",
    "driver_name",
    "owner_id",
    "customer_id",
    "latitude",
    "longitude",
    "trip_id",
    "cost",
    # W5 fix: the service-history reader returns total_cost_usd; add it to the
    # denylist so it is always stripped regardless of the caller's fixture names.
    "total_cost_usd",
    "dealer_id",
})

#: Allowlist of keys that are safe for the service_history section.
#: Derived from readers._SERVICE_HISTORY_SQL SELECT list:
#:   vin, service_date, service_type, dtc_codes_csv, total_cost_usd, outcome.
#: ``total_cost_usd`` is excluded by design (cost data stays server-side).
#: ``source_ro_id`` was allowlisted here until 2026-10-26: ADP's recall plants wrote it on
#: supplement rows, but service_records has no such column and the reader never selected it.
#: ADP stopped writing it (FI sweep 2026-10-26), so the entry is gone; the allowlist is the
#: reader's SELECT list less cost (pinned by test_job_investigation.py).
#:
#: W5 b fix: was {vin, service_type, labor_op_code, completion_date, dtc_codes_csv,
#: outcome} — ``completion_date`` and ``labor_op_code`` are not reader column names
#: and ``service_date`` was missing, so every row lost its date.
_SERVICE_HISTORY_ALLOWED: frozenset[str] = frozenset({
    "vin",
    "service_date",    # readers._SERVICE_HISTORY_SQL column (was wrong: completion_date)
    "service_type",
    "dtc_codes_csv",
    # total_cost_usd: excluded — cost data is server-side only
    "outcome",
})


def _allowlist_service_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return service history rows with only allowlisted keys.

    This is stricter than the denylist: any key NOT in _SERVICE_HISTORY_ALLOWED
    is removed, including any future cost column with a new name.

    Mutation guard (test_bundle_strips_service_history_to_allowlist):
        A row with ``total_cost_usd`` passes through the denylist only if it's
        in _FORBIDDEN_FIELDS.  The allowlist approach catches any unlisted key.
    """
    return [
        {k: v for k, v in row.items() if k in _SERVICE_HISTORY_ALLOWED}
        for row in rows
        if isinstance(row, dict)
    ]


def _strip_forbidden(obj: Any) -> Any:
    """Recursively remove _FORBIDDEN_FIELDS from any dict/list structure.

    Dicts: remove keys in _FORBIDDEN_FIELDS; recurse into remaining values.
    Lists: recurse into each element.
    Other types: returned unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_forbidden(v)
            for k, v in obj.items()
            if k not in _FORBIDDEN_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_forbidden(item) for item in obj]
    return obj


def build_bundle(
    *,
    pattern: dict[str, Any],
    failure_modes: list[dict[str, Any]],
    fleet_type: str,
    affected_vins: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    service_history: list[dict[str, Any]],
    candidates: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build an investigation bundle, stripping all fleet-private fields.

    Spec D10 step 1: the bundle feeds the model context and the fallback
    selector.  The maker sees fleet_type as a cohort label so the model
    can reason about use-pattern differences (rental vs delivery) without
    seeing any fleet identity.

    Parameters
    ----------
    pattern:
        Fault pattern dict from ADP ``fault_patterns`` (or an equivalent
        structure with at least ``code_family``, ``dimension``, ``value``).
    failure_modes:
        Failure mode rows from ``engineering_failure_modes`` (with nested
        ``remedy_options``).
    fleet_type:
        Cohort label — the only fleet attribute carried into the bundle.
        One of ``rental``, ``delivery``, ``field_service``, ``city``, or
        an empty string when not known.
    affected_vins:
        Affected VIN rows from ``fault_patterns`` / ``vehicle_build_components``.
        May carry fleet-private fields from their source query; those are
        stripped here.
    controls:
        Matched control VIN rows (unaffected vehicles with the same cohort
        profile).  Also stripped.
    service_history:
        Service history rows (``readers.read_service_history``).
        Reduced to the allowlisted columns: no RO id, dealer, cost, driver, etc.
    candidates:
        Optional pre-built ranked candidates list.  When absent, an empty
        list is used (the model path builds candidates from the pattern; the
        fallback path uses the top-ranked candidate from fault_patterns rows
        fed to ``candidates.build_candidates_from_pattern_rows``).

    Returns
    -------
    dict
        Investigation bundle with structure:
        ``{pattern, candidates, failure_modes, fleet_type,
           affected_vins, controls, service_history}``
        All fleet-private fields stripped recursively.

    Notes
    -----
    Mutation guard: the test ``test_bundle_contains_no_fleet_private_fields``
    feeds inputs that contain every forbidden field class and asserts none
    survives into the result.  A build_bundle that returns inputs verbatim
    fails that test.
    """
    return {
        "pattern": _strip_forbidden(pattern),
        "candidates": _strip_forbidden(candidates if candidates is not None else []),
        "failure_modes": _strip_forbidden(failure_modes),
        "fleet_type": fleet_type,          # cohort label only — NOT a fleet id
        "affected_vins": _strip_forbidden(affected_vins),
        "controls": _strip_forbidden(controls),
        # W5 fix: service_history is built from an allowlist (not just denylist)
        # so that cost columns with any name (total_cost_usd, cost, etc.) are
        # excluded even if _FORBIDDEN_FIELDS is not updated.
        "service_history": _allowlist_service_history(service_history),
    }
