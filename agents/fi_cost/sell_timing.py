"""
Sell-timing arithmetic for the fi_cost Tier 2 component — D10.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D10

D10: "One arithmetic."  The crossover function is ported from
``services/fleet_intelligence/lifecycle.py`` as a pure function with no
lifecycle-module dependencies.  Both repos run the same golden vectors
(``fixtures/crossover_golden.json``, sha256-pinned in test_sell_timing.py).

The port trims the CMS function to the fields this stage needs:
- Drops ``provenance`` handling (this module takes plain dicts, not CMS shapes).
- Drops ``input_series`` from the return dict (D10 doesn't ask for it).
- Drops ``r_squared`` numeric value from the return dict (D10 doesn't use it;
  ``r_squared_meaningful`` is kept for feature-parity with the golden fixture).
- Keeps the same ``_linear_fit``, ``_add_months``, ``crossover_offset``
  internal functions verbatim so the arithmetic is identical.

``build_sell_timing_finding`` builds the ``fleet.cost.sell_timing`` Finding
dict with fixed narration (U3: no model call for sell timing in this stage).

Sell timing writes a Finding only when the crossover is still ahead (user decision
2026-10-07, option (b), ``decisions.md``): the fitted maintenance at the data month
must still be below monthly depreciation, and the crossover must fall in the coming
1..3 months.  A vehicle already at or past it gets none.  That rule lives in the write
decision (``crossed_by_data_month`` and ``should_write_sell_timing_finding``), not in
the ported crossover arithmetic, which stays identical to CMS.

Mutation targets (task 3.1 constraints; option (b)):
- ``should_write_sell_timing_finding``: removing the ``<= horizon_months``
  check makes the function return True for crossovers outside the horizon;
  the boundary tests catch it.  Removing the ``already_crossed`` check writes
  vehicles already past the crossover (463 instead of 126 on 2026-09 data).
- ``crossed_by_data_month``: ``>=`` to ``>`` lets a vehicle exactly at
  depreciation in the data month through; index ``n - 1`` to ``n`` tests the
  next month instead of the data month.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import uuid
from pathlib import Path
from typing import Any

from agents.fi_cost.diagnose import AGENT_VERSION
from agents.fi_cost.evidence import compute_inputs_hash
from agents.fi_cost.finding_keys import (
    AGENT_ID,
    FINDING_KIND_SELL_TIMING,
    SCOPE_FIELD,
    cost_finding_key,
)
from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# Straight-line depreciation life — identical to CMS lifecycle.py (D10: one arithmetic).
STRAIGHT_LINE_LIFE_MONTHS: int = 120

# Minimum series length for r_squared to be meaningful (same as lifecycle.py).
_MIN_SERIES_FOR_FIT: int = 3

# D10 horizon — must equal _golden["_horizon_months"] in the fixture.
SELL_TIMING_HORIZON_MONTHS: int = 3

# Sell-timing Finding TTL in days (D8: 35 days).
_EXPIRES_DAYS: int = 35

# The inputs query returns one row per managed vehicle per month (about 4,250 vehicles
# x 18 months on staging).  Above the Tier 2 health ceiling on purpose; still a hard
# ceiling, so a runaway result raises rather than silently truncating a series.
SELL_TIMING_MAX_ROWS: int = 250_000

_INPUTS_SQL_TEMPLATE: str = (Path(__file__).parent / "sql" / "sell_timing_inputs.sql").read_text()
_STAGE_RE = re.compile(r"^(staging|prod)$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


# ---------------------------------------------------------------------------
# Internal arithmetic — ported verbatim from lifecycle.py (D10: one arithmetic)
# ---------------------------------------------------------------------------


def _linear_fit(ys: list[float]) -> tuple[float, float, float]:
    """OLS fit of ys against x = 0, 1, ..., n-1.

    Returns (slope, intercept, r_squared).  r_squared clamped to [0, 1].
    For a constant series (zero y-variance) r_squared is 1.0.

    Identical to CMS lifecycle.py._linear_fit.
    """
    n = len(ys)
    if n == 0:
        raise ValueError("Cannot fit an empty maintenance series.")
    if n == 1:
        return 0.0, float(ys[0]), 1.0

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    ss_yy = sum((y - mean_y) ** 2 for y in ys)

    slope = ss_xy / ss_xx if ss_xx != 0 else 0.0
    intercept = mean_y - slope * mean_x

    if ss_yy == 0:
        r_squared = 1.0
    else:
        ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        r_squared = 1.0 - ss_res / ss_yy

    r_squared = max(0.0, min(1.0, r_squared))
    return slope, intercept, r_squared


def _add_months(year_month: str, months: int) -> str:
    """Add *months* to a ``'YYYY-MM'`` string, returning ``'YYYY-MM'``.

    Identical to CMS lifecycle.py._add_months.
    """
    year, month = (int(p) for p in year_month.split("-")[:2])
    zero_based = (year * 12 + (month - 1)) + months
    return f"{zero_based // 12:04d}-{zero_based % 12 + 1:02d}"


def _crossover_offset(
    intercept: float,
    slope: float,
    n: int,
    monthly_depreciation: float,
    horizon_months: int,
) -> int | None:
    """Months from the series end at which the fitted line reaches depreciation.

    Identical to CMS lifecycle.py.crossover_offset.

    The smallest k in 1..horizon_months with
    ``intercept + slope * ((n - 1) + k) >= monthly_depreciation``, or None.
    """
    for k in range(1, horizon_months + 1):
        projected_index = (n - 1) + k
        projected_cost = intercept + slope * projected_index
        if projected_cost >= monthly_depreciation:
            return k
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_crossover_month(
    purchase_price: float,
    maintenance_series: list[dict[str, Any]],
    horizon_months: int,
) -> dict[str, Any]:
    """Port of CMS lifecycle.analyze_sell_timing, trimmed to the fields D10 needs.

    Returns::

        {
          "crossover_month": str | None,
          "r_squared_meaningful": bool,
          "series_length": int,
          "maintenance_fit": {"slope": float, "intercept": float},
          "depreciation_params": {
              "purchase_price": float,
              "monthly_depreciation": float,
              "life_months": int,
          },
        }

    Output must match ``fixtures/crossover_golden.json`` vector-for-vector
    (D10: one arithmetic; sha256-pinned in test_sell_timing.py).

    Differences from lifecycle.analyze_sell_timing:
    - Accepts plain dicts (no ``provenance`` key expected).
    - Drops ``input_series`` and ``r_squared`` from the return dict.
    - Drops ``horizon_months`` from the return dict (not in the golden fixture).
    """
    if len(maintenance_series) == 0:
        raise ValueError("maintenance_series must contain at least one month.")

    ys = [float(p["maintenanceCost"]) for p in maintenance_series]
    slope, intercept, r_squared = _linear_fit(ys)

    monthly_depreciation = purchase_price / STRAIGHT_LINE_LIFE_MONTHS
    n = len(maintenance_series)
    series_end_month = maintenance_series[-1]["yearMonth"]

    k = _crossover_offset(intercept, slope, n, monthly_depreciation, horizon_months)
    crossover_month = _add_months(series_end_month, k) if k is not None else None

    return {
        "crossover_month": crossover_month,
        "r_squared_meaningful": n >= _MIN_SERIES_FOR_FIT,
        "series_length": n,
        "maintenance_fit": {"slope": slope, "intercept": intercept},
        "depreciation_params": {
            "purchase_price": purchase_price,
            "monthly_depreciation": monthly_depreciation,
            "life_months": STRAIGHT_LINE_LIFE_MONTHS,
        },
    }


def crossed_by_data_month(result: dict[str, Any]) -> bool:
    """True when the fitted maintenance is already at or over depreciation in the data month.

    ``result`` is :func:`compute_crossover_month`'s return.  The data month is the
    series end, index ``n - 1`` of the fitted line, i.e. k = 0 of the line
    :func:`_crossover_offset` searches from k = 1.  The comparison is the same ``>=``,
    so a vehicle exactly at depreciation in the data month has already crossed.

    ``_crossover_offset`` starts at k = 1, so a vehicle that crossed before the data
    month reports a crossover one month ahead.  This check is what tells the two apart
    (user decision 2026-10-07, option (b): Findings only for crossovers still ahead).
    """
    fit = result["maintenance_fit"]
    n = result["series_length"]
    fitted_now = fit["intercept"] + fit["slope"] * (n - 1)
    return fitted_now >= result["depreciation_params"]["monthly_depreciation"]


def should_write_sell_timing_finding(
    crossover_month: str | None,
    series_end_month: str,
    horizon_months: int,
    *,
    already_crossed: bool,
) -> bool:
    """True when the crossover is still ahead and within ``horizon_months`` of ``series_end_month``.

    D10 as amended 2026-10-07 (option (b)): a Finding only for a vehicle whose
    crossover is still ahead, in the coming 1..``horizon_months`` months.  "Within" is
    inclusive of the boundary (crossover at exactly +3 months → True; at +4 months →
    False).  A vehicle already at or over depreciation in the data month
    (``already_crossed``, from :func:`crossed_by_data_month`) gets none, whatever
    ``crossover_month`` says.

    Mutation targets: removing the ``<= horizon_months`` guard (always True for any
    non-None crossover) makes the boundary tests fail; removing the
    ``already_crossed`` check makes the already-crossed tests fail.

    Parameters
    ----------
    crossover_month:
        ``"YYYY-MM"`` or None.
    series_end_month:
        ``"YYYY-MM"`` of the last point in the maintenance series.
    horizon_months:
        Look-ahead window (D10 default = SELL_TIMING_HORIZON_MONTHS = 3).
    already_crossed:
        Keyword-only and required, so a caller cannot skip the check.  Anything other
        than ``False`` means no Finding (fail closed).
    """
    if already_crossed is not False:
        return False
    if crossover_month is None:
        return False

    # Compute the number of months from series_end_month to crossover_month.
    end_year, end_mon = (int(p) for p in series_end_month.split("-")[:2])
    cross_year, cross_mon = (int(p) for p in crossover_month.split("-")[:2])

    months_ahead = (cross_year * 12 + cross_mon) - (end_year * 12 + end_mon)

    # Mutation target: changing <= to < drops the at-exactly-horizon case.
    return 0 < months_ahead <= horizon_months


def build_sell_timing_finding(
    vin: str,
    fleet_id: str,
    crossover_month: str,
    purchase_price: float,
    monthly_depreciation: float,
    year_month: str,
) -> dict[str, Any]:
    """Build a ``fleet.cost.sell_timing`` Finding dict (contract § 1.1, D10).

    ``recommended_action.parameters.cost_action = "reassign_or_remarket"`` (D10).
    No model call; narration is fixed text per U3.
    ``subject_key = year_month`` (D8: one item per vehicle per data month).

    Parameters
    ----------
    vin:
        Vehicle VIN string.
    fleet_id:
        Fleet identifier (for the fixed narration and finding scope).
    crossover_month:
        ``"YYYY-MM"`` at which projected maintenance crosses depreciation.
    purchase_price:
        Vehicle purchase price in USD.
    monthly_depreciation:
        Straight-line monthly depreciation = purchase_price / 120.
    year_month:
        Data month (``"YYYY-MM"``); becomes the Finding's ``subject_key``.
    """
    now_utc = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=_EXPIRES_DAYS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    finding_id = f"FIND-{uuid.uuid4()}"
    fk = cost_finding_key(vin=vin, finding_kind=FINDING_KIND_SELL_TIMING, year_month=year_month)

    # Fixed narration (U3: no model call for sell timing in this stage)
    headline = (
        f"Sell-timing crossover for {vin}: maintenance projected to exceed "
        f"depreciation in {crossover_month}"
    )
    detail = (
        f"Monthly straight-line depreciation: ${monthly_depreciation:.0f}. "
        f"Maintenance trend is projected to cross this in {crossover_month}. "
        f"Consider reassigning or remarketing the vehicle before that date."
    )

    # Evidence entries for sell-timing Finding — schema requires source/source_ref/computed_at.
    # These carry the lifecycle inputs that drove the crossover recommendation (W5/W6).
    evidence_entries = [
        {
            "kind": "lifecycle.crossover_month",
            "value": crossover_month,
            "vin": vin,
            "provenance": "live",
            "source": "agent-fi-cost",
            "source_ref": f"sell-timing/{year_month}",
            "computed_at": now_utc,
        },
        {
            "kind": "lifecycle.purchase_price",
            "value": purchase_price,
            "vin": vin,
            "provenance": "live",
            "source": "adp",
            "source_ref": f"adp-evidence/{year_month}",
            "computed_at": now_utc,
        },
        {
            "kind": "lifecycle.monthly_depreciation",
            "value": round(monthly_depreciation, 2),
            "vin": vin,
            "provenance": "live",
            "source": "agent-fi-cost",
            "source_ref": f"sell-timing/{year_month}",
            "computed_at": now_utc,
        },
        {
            "kind": "cost.month",
            "value": year_month,
            "vin": vin,
            "provenance": "live",
            "source": "adp",
            "source_ref": f"adp-evidence/{year_month}",
            "computed_at": now_utc,
        },
    ]

    return {
        "finding_id": finding_id,
        "finding_key": fk,
        "finding_kind": FINDING_KIND_SELL_TIMING,
        "agent_id": AGENT_ID,
        "scope": {SCOPE_FIELD: vin},
        "severity": "routine",
        "subject_key": year_month,
        "headline": headline,
        "detail": detail,
        "computed_at": now_utc,
        "expires_at": expires_at,
        "agent_version": AGENT_VERSION,
        "confidence": "high",     # deterministic arithmetic, always high (W5 fix)
        # Over the inputs that decide the recommendation (fixed number formatting, D11), so a
        # re-run with the same inputs skips and a changed crossover updates the item.
        "inputs_hash": compute_inputs_hash({
            "vin": vin, "crossover_month": crossover_month, "purchase_price": purchase_price,
            "monthly_depreciation": monthly_depreciation, "year_month": year_month,
        }),
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "evidence": evidence_entries,
        "recommended_action": {
            "action_kind": "apply_cost_action",
            "target_system": "cms",
            "parameters": {
                "cost_action": "reassign_or_remarket",
                "vehicle_vin": vin,
                "month": year_month,
                "cause": "low_utilisation",
            },
        },
    }



# ---------------------------------------------------------------------------
# Inputs and planning (task 4.1: the run loop's sell-timing step)
# ---------------------------------------------------------------------------


def fetch_sell_timing_inputs(
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    *,
    year_month: str,
    stage: str = "staging",
    max_polls: int | None = None,
) -> list[dict[str, Any]]:
    """One Athena query: each managed vehicle's monthly maintenance and purchase price.

    Rows up to and including ``year_month`` from ``vehicle_cost_monthly`` joined to
    ``vehicle_pricing``.  Paged to the end by ``run_query``; raises
    ``AthenaResultTooLargeError`` past :data:`SELL_TIMING_MAX_ROWS` rather than
    returning a truncated series.  ``stage`` and ``year_month`` are validated before
    they are templated into SQL.
    """
    if not _STAGE_RE.match(stage):
        raise ValueError(f"stage must be 'staging' or 'prod', got {stage!r}")
    if not _MONTH_RE.match(year_month):
        raise ValueError(f"year_month must be YYYY-MM, got {year_month!r}")
    sql = _INPUTS_SQL_TEMPLATE.replace("${stage}", stage).replace("${year_month}", year_month)
    kwargs: dict[str, Any] = {"max_rows": SELL_TIMING_MAX_ROWS}
    if max_polls is not None:
        kwargs["max_polls"] = max_polls
    return run_query(
        sql=sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        **kwargs,
    )


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def plan_sell_timing(
    rows: list[dict[str, Any]],
    *,
    year_month: str,
    horizon_months: int = SELL_TIMING_HORIZON_MONTHS,
) -> tuple[list[dict[str, Any]], int]:
    """Which vehicles get a ``fleet.cost.sell_timing`` Finding for ``year_month`` (D10).

    Groups ``rows`` by VIN, sorts each series by month and runs
    :func:`compute_crossover_month`.  A vehicle qualifies when its series ends at
    ``year_month`` (it is still managed in the data month), its fitted maintenance is
    still below depreciation in that month (:func:`crossed_by_data_month`), and its
    crossover is in the coming 1..``horizon_months`` months
    (:func:`should_write_sell_timing_finding`).  A vehicle already past the crossover
    gets no Finding (user decision 2026-10-07, option (b)).

    A vehicle with an unreadable value (non-numeric cost or price, a price that is not
    positive, conflicting prices, a malformed month) is left out and counted, never
    guessed at.

    Returns ``(candidates, rejected)``.  Each candidate carries the keyword arguments
    :func:`build_sell_timing_finding` takes.  Candidates are ordered by VIN, so a run
    that stops early stops at the same place each time.
    """
    series: dict[str, list[dict[str, Any]]] = {}
    price: dict[str, float] = {}
    fleet: dict[str, str] = {}
    bad: set[str] = set()
    for row in rows:
        vin = row.get("vin")
        if not vin:
            continue
        if not isinstance(vin, str) or not _VIN_RE.match(vin):
            # The VIN becomes the Finding's scope and its narration; a malformed one is
            # counted and left out, as on the anomaly path (security review cycle 1).
            bad.add(str(vin))
            continue
        month = row.get("year_month")
        cost = _finite_float(row.get("maintenance_cost"))
        p = _finite_float(row.get("purchase_price_usd"))
        if not isinstance(month, str) or not _MONTH_RE.match(month) or cost is None \
                or p is None or p <= 0:
            bad.add(vin)
            continue
        if vin in price and price[vin] != p:
            bad.add(vin)
            continue
        price[vin] = p
        if month == year_month and row.get("fleet_id"):
            fleet[vin] = str(row["fleet_id"])
        series.setdefault(vin, []).append({"yearMonth": month, "maintenanceCost": cost})

    candidates: list[dict[str, Any]] = []
    for vin in sorted(series):
        if vin in bad:
            continue
        points = sorted(series[vin], key=lambda pt: pt["yearMonth"])
        months = [pt["yearMonth"] for pt in points]
        if len(set(months)) != len(months) or months[-1] != year_month:
            # A duplicated month would be counted twice in the fit; a series that does
            # not reach the data month is a vehicle no longer managed.
            if len(set(months)) != len(months):
                bad.add(vin)
            continue
        result = compute_crossover_month(price[vin], points, horizon_months)
        if not should_write_sell_timing_finding(
            result["crossover_month"], year_month, horizon_months,
            already_crossed=crossed_by_data_month(result),
        ):
            continue
        candidates.append({
            "vin": vin,
            "fleet_id": fleet.get(vin, ""),
            "crossover_month": result["crossover_month"],
            "purchase_price": price[vin],
            "monthly_depreciation": result["depreciation_params"]["monthly_depreciation"],
            "year_month": year_month,
        })
    return candidates, len(bad)
