"""
Evidence gathering and inputs_hash for the fi_cost Tier 2 component — D6 step 2.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 2, § D8, § D11
Decisions: decisions.md 2026-10-04 "Group 1 findings" item 5 (single query);
           decisions.md 2026-10-05 (energy-rule evidence keys: cost.flag,
           cost.energy_ratio_own, cost.energy_ratio_cohort, cost.energy_excess_usd,
           cost.energy_method)

``compute_inputs_hash`` implements D11: sha256 of canonical JSON (sorted keys,
fixed number formatting) computed BEFORE any Bedrock call.  Narration fields
(keys starting with ``"_"``) are excluded so that re-phrasing never changes the
hash and triggers a spurious re-diagnosis.  Number values are normalised to a
fixed 6-decimal-place representation before hashing so that a last-digit
difference from a distributed AVG cannot change the hash.

``gather_evidence`` issues a SINGLE Athena query for ALL selected VINs (D6 step 2;
decisions item 5).  D13 notes queue times of 79–354 s; 50 sequential queries cannot
fit the 15-minute Lambda budget.

Input validation (defence in depth; VINs come from ADP data):
  ``stage`` must match ``^(staging|prod)$``; every VIN must match
  ``^[A-HJ-NPR-Z0-9]{17}$``.  Both are checked BEFORE any Athena call so that
  neither value is ever interpolated un-validated into SQL.

Postprocessing (pure Python, no Athena):
  - ``cost.flag``: ``"total"`` / ``"energy"`` / ``"both"`` from the raw
    ``is_outlier`` / ``is_energy_outlier`` boolean columns.
  - ``delta.*_usd``: per-category dollar delta = per-mile delta × miles
    (decisions cycle 3, item 1).
  - ``cost.month`` = ``year_month`` (string form for evidence entries).

Column name cross-reference lives in sql/gather_evidence.sql.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

_EVIDENCE_SQL_PATH: Path = Path(__file__).parent / "sql" / "gather_evidence.sql"
_EVIDENCE_SQL_TEMPLATE: str = _EVIDENCE_SQL_PATH.read_text()

# Validation patterns
_STAGE_RE = re.compile(r"^(staging|prod)$")
# Standard 17-character VIN: excludes I (i), O (o), Q (q) per ISO 3779.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Evidence keys carrying dollar deltas (per-mile × miles).
# Decisions cycle 3, item 1: AVX computes the dollar amounts.
_DELTA_PER_MILE_KEYS: tuple[tuple[str, str], ...] = (
    ("delta.energy", "delta.energy_usd"),
    ("delta.maintenance", "delta.maintenance_usd"),
    ("delta.lease", "delta.lease_usd"),
    ("delta.insurance", "delta.insurance_usd"),
)


# ---------------------------------------------------------------------------
# Number normalisation for hash stability (D11)
# ---------------------------------------------------------------------------


def _normalize_value_for_hash(v: Any) -> Any:
    """Normalise a value for canonical JSON hashing (D11 fixed number formatting).

    Rules (spec D11):
    - ``None`` → ``None`` (JSON null).
    - ``bool`` → unchanged (checked before int/float because bool is a subclass of int;
      ``True`` must NOT be treated as the number 1).
    - Finite ``int`` or ``float`` → ``format(round(float(v), 6), ".6f")``.
    - Numeric string (Athena returns numbers as strings) → same as above.
    - Non-finite float (inf, nan) → left as-is (rare; hash stability not guaranteed).
    - Any other type → unchanged.

    This ensures that a last-digit difference from a distributed AVG cannot
    change the hash and trigger a spurious re-diagnosis.
    """
    if v is None:
        return None
    # MUST check bool before int/float: True == 1 and False == 0 in Python.
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        if math.isfinite(f):
            return format(round(f, 6), ".6f")
        return v  # non-finite: return as-is
    if isinstance(v, str):
        try:
            f = float(v)
            if math.isfinite(f):
                return format(round(f, 6), ".6f")
        except (ValueError, TypeError):
            pass
        return v  # non-numeric string: unchanged
    return v  # other types unchanged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_inputs_hash(evidence: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the evidence rows.

    Canonical: sorted keys, fixed number formatting (D11).  Keys beginning with
    ``"_"`` are excluded — these are narration fields (headline, detail shaped
    values) that must not affect the hash.  The model's narration is not hashed
    so re-phrasing never triggers a spurious re-diagnosis (D11).

    Number values are normalised via ``_normalize_value_for_hash``: every finite
    numeric value (int, float, or numeric string as Athena returns them) is
    formatted as ``format(round(float(x), 6), ".6f")``.  Booleans and
    non-numeric strings are unchanged.  This prevents a last-digit difference
    from a distributed AVG from changing the hash.

    Must be called BEFORE the Bedrock call.  The Finding's ``inputs_hash``
    attribute must equal the return value of this function computed over the
    same evidence dict, so a second run with unchanged evidence finds the same
    hash and skips the call.

    Parameters
    ----------
    evidence:
        Flat evidence dict, one per vehicle.  May contain ``_``-prefixed keys
        (not stripped by the caller — this function strips them internally so
        that an implementation that hashes all keys, mutation M2, produces a
        DIFFERENT hash and is caught by the idempotency test).

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.
    """
    # Strip narration-shaped keys — done HERE, not by the caller, so that the
    # M2 mutant (hashes every key without stripping) produces a different hash
    # and the idempotency test catches it.
    # Normalise numeric values for hash stability (D11).
    canonical_evidence = {
        k: _normalize_value_for_hash(v)
        for k, v in evidence.items()
        if not k.startswith("_")
    }

    # Canonical JSON: sorted keys, no extra whitespace.
    canonical_json = json.dumps(
        canonical_evidence,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def gather_evidence(
    selected_vins: list[str],
    year_month: str,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    stage: str = "staging",
    max_polls: int | None = None,
) -> list[dict[str, Any]]:
    """One Athena query for ALL selected VINs — D6 step 2.

    D6 step 2 fetches evidence for all selected VINs in a SINGLE query, not one
    query per VIN.  With D13's 79–354 s Athena queue time, 50 sequential queries
    cannot fit the 15-minute Lambda budget.

    Input validation (defence in depth — VINs come from ADP data):
    - ``stage`` must match ``^(staging|prod)$``; raises ``ValueError`` otherwise.
    - Every VIN must match ``^[A-HJ-NPR-Z0-9]{17}$``; raises ``ValueError`` naming
      the first invalid VIN, BEFORE any Athena call.

    After the query, pure-Python postprocessing adds:
    - ``cost.flag``: ``"total"`` / ``"energy"`` / ``"both"`` from the raw
      boolean flags.
    - ``delta.*_usd``: per-mile delta × miles (decisions cycle 3, item 1).
    - ``cost.month``: same as ``year_month`` (string evidence label).

    Parameters
    ----------
    selected_vins:
        List of VIN strings from ``select_anomalies``.
    year_month:
        ``"YYYY-MM"`` of the latest complete month.
    athena_client:
        Boto3 Athena client or stub.
    workgroup:
        Athena workgroup name.
    results_s3_path:
        S3 URI for query results.
    stage:
        ADP stage name (templates database names).  Must be ``"staging"`` or
        ``"prod"``; any other value raises ``ValueError`` before the Athena call.

    Returns
    -------
    list[dict[str, Any]]
        One evidence dict per VIN, ready for ``compute_inputs_hash`` and the
        Bedrock call.  Keys match the interface.md evidence-key table.

    Raises
    ------
    ValueError
        If ``stage`` does not match ``^(staging|prod)$``, or if any VIN does not
        match ``^[A-HJ-NPR-Z0-9]{17}$``.
    """
    if not selected_vins:
        return []

    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", year_month):
        raise ValueError(f"year_month must be YYYY-MM, got {year_month!r}")

    # Validate stage before interpolating it into SQL.
    if not _STAGE_RE.match(stage):
        raise ValueError(
            f"stage must be 'staging' or 'prod', got {stage!r}"
        )

    # Validate every VIN before interpolating into SQL (defence in depth).
    for vin in selected_vins:
        if not _VIN_RE.match(vin):
            raise ValueError(
                f"Invalid VIN {vin!r}: must be 17 characters from [A-HJ-NPR-Z0-9]"
            )

    # Build a safe quoted VIN list (validation above ensures alphanumeric-only)
    vin_list = ", ".join(f"'{v}'" for v in selected_vins)

    sql = (
        _EVIDENCE_SQL_TEMPLATE
        .replace("${stage}", stage)
        .replace("${year_month}", year_month)
        .replace("${vin_list}", vin_list)
    )

    log.info(
        "fi_cost.evidence: gathering evidence for %d VINs year_month=%s",
        len(selected_vins),
        year_month,
    )

    kwargs: dict[str, Any] = {}
    if max_polls is not None:
        # The run's remaining wall time decides how long this query may queue
        # (spec D13: Athena queue time is unbounded; a query past its deadline
        # aborts the run).
        kwargs["max_polls"] = max_polls
    rows = run_query(
        sql=sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        **kwargs,
    )

    return [_postprocess_row(row, year_month) for row in rows]


# ---------------------------------------------------------------------------
# Postprocessing helpers
# ---------------------------------------------------------------------------


def _postprocess_row(row: dict[str, Any], year_month: str) -> dict[str, Any]:
    """Add derived evidence keys to a raw Athena row.

    - ``cost.flag``: from boolean flags.
    - ``delta.*_usd``: per-mile delta × miles.
    - ``cost.month``: the data month string.
    """
    evidence = dict(row)

    # cost.month = data month (D8 evidence label)
    evidence["cost.month"] = year_month

    # cost.flag: "total" / "energy" / "both" (decisions 2026-10-05, item 6)
    is_outlier = _truthy(evidence.get("is_outlier"))
    is_energy_outlier = _truthy(evidence.get("is_energy_outlier"))
    if is_outlier and is_energy_outlier:
        evidence["cost.flag"] = "both"
    elif is_energy_outlier:
        evidence["cost.flag"] = "energy"
    else:
        evidence["cost.flag"] = "total"

    # Dollar deltas: per-mile delta × miles (decisions cycle 3, item 1)
    miles = _float_or_none(evidence.get("cost.miles"))
    for per_mile_key, usd_key in _DELTA_PER_MILE_KEYS:
        delta_per_mile = _float_or_none(evidence.get(per_mile_key))
        if delta_per_mile is not None and miles is not None:
            evidence[usd_key] = round(delta_per_mile * miles, 2)
        else:
            evidence[usd_key] = None

    return evidence


def _truthy(value: Any) -> bool:
    """Convert Athena string booleans to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _float_or_none(value: Any) -> float | None:
    """Convert a value to float or return None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
