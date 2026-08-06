"""Battery health data source — queries ADP ``energy_usage`` for ``state_of_health_pct``.

This is the **only** module in the CVX codebase that holds battery SQL or ADP battery
column names.  Any SQL or ADP column name appearing outside this module is a review
failure (spec § D3a).

ADP source: ``energy_usage``
-----------------------------
``energy_usage.state_of_health_pct`` is the correct column.  It is generated from
``battery_age_days`` with a ~2% annual decline plus ±0.5% noise, producing genuinely
age-correlated per-vehicle variation (range 85.76–100.0, stddev ~4.0 at 10M rows /
100k VINs, verified 2026-08-03 by T1.2).

Do **NOT** use ``vehicle_telemetry_aggregated.state_of_health_pct``.  That column is
driven by a global row index and is effectively identical for every VIN (fleet-wide span
~95→90).  Using it makes per-owner battery judgment impossible while appearing correct
(spec § D3a).

What this module returns
------------------------
Per-VIN summary rows from ``energy_usage``, translated to **agent-facing vocabulary**
so the caller (the health agent) never sees ADP column names:

ADP column              → agent-facing key
──────────────          ──────────────────
state_of_health_pct     → health_pct
battery_age_days        → age_days
vin                     → vin            (unchanged; not ADP-specific)

The translation is performed in :func:`_translate_row` and applied in
:func:`fetch_findings` before the rows leave this module.

CVX computes no thresholds and fits no degradation
model (spec § D3a, Q7).

The honest output on current ADP data is **reassurance**: every synthetic vehicle is
well above any typical warranty floor (~70%), so the agent will say "your battery is
at 92%, degrading normally for its age" rather than manufacturing urgency that the
data does not support (spec § D3a stated limitations).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from agents.health._vin import sanitise_vin
from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADP source product — the sole location this name appears in CVX code.
# A test in agents/health/tests/test_adp_battery_source.py asserts this
# constant equals "energy_usage" so the source cannot silently drift.
# ---------------------------------------------------------------------------

ADP_PRODUCT: str = "energy_usage"

# ---------------------------------------------------------------------------
# SQL — the sole location battery SQL lives in CVX code.
# Column names: vin, state_of_health_pct, battery_age_days from energy_usage.
# No thresholds, no degradation model — ADP already inferred; CVX judges.
# ---------------------------------------------------------------------------

_BATTERY_SQL_TEMPLATE: str = """
SELECT
    vin,
    state_of_health_pct,
    battery_age_days
FROM {database}.energy_usage
WHERE vin = '{vin}'
ORDER BY battery_age_days DESC
LIMIT 1
""".strip()

#: Batch SQL selecting the latest battery row per VIN across a fleet batch.
#: ``{database}`` is substituted to ``adp_{stage}_energy_usage``; the outer
#: DISTINCT + ORDER BY vin + LIMIT {max_vins} subquery makes the VIN set
#: deterministic so ``inputs_hash`` is stable across runs (spec § D4/M6).
#: When an explicit VIN list is supplied the subquery is replaced by a literal
#: IN clause — the WHERE clause fragment is appended after the base.
_BATCH_SQL_BASE: str = """
SELECT vin, state_of_health_pct, battery_age_days
FROM {database}.energy_usage
""".strip()

_BATCH_SQL_MAX_VINS_CLAUSE: str = (
    "AND vin IN ("
    "SELECT vin FROM ("
    "SELECT DISTINCT vin FROM {database}.energy_usage "
    "ORDER BY vin LIMIT {max_vins}"
    "))"
)


# ---------------------------------------------------------------------------
# Class API (batch / FindingSource protocol)
# ---------------------------------------------------------------------------


class AdpBatterySource:
    """Fetch per-VIN battery findings from ADP ``energy_usage`` (batch interface).

    Implements the ``FindingSource`` protocol consumed by
    ``agents.tier2.runner``.

    Lifetime: one instance per Tier 2 run.  AWS clients are injected so test
    doubles can be supplied without network access.

    Parameters
    ----------
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.  Used to resolve the
        Glue database name ``adp_{stage}_energy_usage``.
    athena_client:
        A boto3 Athena client initialised in ``us-east-1`` (ADP's catalog
        region for both CVX stages).
    workgroup:
        Athena workgroup name, e.g. ``"cvx-staging-analytics"``.
    results_s3_path:
        S3 URI for Athena query results.
    max_polls:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; controls the
        Athena poll ceiling.
    poll_interval:
        Sleep between poll attempts (seconds).  Set to ``0`` in unit tests.
    max_vins:
        When no explicit VIN list is supplied via :meth:`fetch_findings`, bound
        the query to this many VINs, **selected deterministically** via
        ``ORDER BY vin``.  ``None`` means no bound (full fleet — only safe at
        very small fleet sizes).  Defaults to ``25`` which suits the default
        per-run budget.
    max_rows:
        Hard ceiling on total result rows forwarded to
        :func:`agents.tier2.adp_source.run_query`.  When ``None``, the default
        in ``adp_source._DEFAULT_MAX_ROWS`` applies.  The Tier 2 runner passes
        ``TIER2_MAX_ROWS_PER_QUERY`` from the environment so the stack-configured
        ceiling is actually enforced rather than ignored (spec T6b.1).
    """

    def __init__(
        self,
        stage: str = "staging",
        *,
        athena_client: Any,
        workgroup: str,
        results_s3_path: str,
        max_polls: int = 60,
        poll_interval: float = 1.0,
        max_vins: Optional[int] = 25,
        max_rows: Optional[int] = None,
    ) -> None:
        self._stage = stage
        self._athena_client = athena_client
        self._workgroup = workgroup
        self._results_s3_path = results_s3_path
        self._max_polls = max_polls
        self._poll_interval = poll_interval
        self._max_vins = max_vins
        self._max_rows = max_rows
        self._database = f"adp_{stage}_energy_usage"

    # -----------------------------------------------------------------------
    # FindingSource protocol
    # -----------------------------------------------------------------------

    def fetch_findings(
        self,
        vins: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return agent-facing battery readings for the given VINs (or a deterministic
        subset when no VIN list is supplied).

        Translates ADP ``energy_usage`` column names into agent-facing vocabulary
        at this boundary so the agent never sees an ADP column name.

        **ADP column → agent-facing key mapping:**

        ``vin``                 → ``vin``              (unchanged)
        ``state_of_health_pct`` → ``health_pct``
        ``battery_age_days``    → ``age_days``

        Parameters
        ----------
        vins:
            Optional list of VINs to restrict the query.  If ``None`` (default),
            the query returns all VINs bounded by ``max_vins`` with
            ``ORDER BY vin`` for determinism — the same VINs are selected on every
            run so ``inputs_hash`` is stable (spec § D4/M6 idempotency).

        Returns
        -------
        list[dict]
            One dict per (VIN, age reading) with agent-facing keys.  May be empty.
        """
        sql = self._build_sql(vins)
        log.debug(
            "adp_battery_source: querying energy_usage stage=%s vins=%s",
            self._stage,
            len(vins) if vins else "all",
        )
        kwargs: dict = dict(
            athena_client=self._athena_client,
            workgroup=self._workgroup,
            results_s3_path=self._results_s3_path,
            max_polls=self._max_polls,
            poll_interval=self._poll_interval,
        )
        if self._max_rows is not None:
            kwargs["max_rows"] = self._max_rows
        raw_rows = run_query(sql, **kwargs)
        return [_to_agent_row(r) for r in raw_rows]

    # -----------------------------------------------------------------------
    # SQL builder (internal — ADP column names isolated here)
    # -----------------------------------------------------------------------

    def _build_sql(self, vins: Optional[list[str]]) -> str:
        """Construct the Athena SQL for the energy_usage product.

        SQL and ADP column names are isolated here.  Nothing outside this
        module may reference ``energy_usage``, ``state_of_health_pct``, or
        ``battery_age_days``.

        When *vins* is provided the query filters to those VINs exactly.
        When *vins* is ``None`` and ``max_vins`` is set, the query uses the
        deterministic distinct-VIN subquery ``ORDER BY vin LIMIT {max_vins}``
        so the same VINs are always selected, keeping ``inputs_hash`` stable.
        """
        base = _BATCH_SQL_BASE.format(database=self._database)

        if vins:
            # Explicit VIN list takes precedence over max_vins.
            safe_vins = [sanitise_vin(v) for v in vins]
            vin_list = ", ".join(f"'{v}'" for v in safe_vins)
            return base + f"\nWHERE vin IN ({vin_list})"

        if self._max_vins is not None:
            # Deterministic bound: ORDER BY vin ensures the same VINs on every run.
            # This is the load-bearing part for M6 idempotency — TABLESAMPLE or
            # rand() would reintroduce per-run variation.
            where_clause = _BATCH_SQL_MAX_VINS_CLAUSE.format(
                database=self._database,
                max_vins=self._max_vins,
            )
            return base + "\nWHERE " + where_clause[len("AND "):]

        # No VIN filter: full fleet.  Only safe for very small fleets.
        return base


# ---------------------------------------------------------------------------
# Public API (legacy per-VIN function — kept for backward compatibility)
# ---------------------------------------------------------------------------


def fetch_findings(
    vin: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    database: str,
) -> dict:
    """Query ADP ``energy_usage`` for battery health data for *vin*.

    Returns a findings dict with the raw ADP rows, an ``inputs_hash`` for
    idempotent artifact writes (spec § D4/M6), and structured evidence entries.

    The caller (the health agent) performs all judgment: what the SoH means for
    this owner, how urgent (if at all) it is, and what to say.  CVX computes no
    thresholds here (spec § D3a).

    Parameters
    ----------
    vin:
        Vehicle Identification Number to query.
    athena_client:
        A boto3 Athena client (or stub).  Injected so tests can supply a stubbed
        client without network access.
    workgroup:
        Athena workgroup, e.g. ``cvx-staging-analytics``.
    results_s3_path:
        S3 URI for query output, e.g.
        ``s3://cvx-staging-athena-results-{account}-us-east-1/tier2/``.
    database:
        Glue database name for the ADP ``energy_usage`` product, e.g.
        ``adp_staging_energy_usage``.

    Returns
    -------
    dict with keys:
        ``vin``          — the queried VIN
        ``rows``         — list of normalised row dicts from ``energy_usage``
                           (empty list when no data for this VIN)
        ``inputs_hash``  — SHA-256 over the normalised rows; stable across
                           re-runs over unchanged data (spec § D4/M6)
        ``evidence``     — list of evidence entries (spec § D4 provenance contract)

    Raises
    ------
    ``agents.tier2.adp_source.AthenaCursorError``
        Propagated if the Athena query reaches FAILED or CANCELLED.
    ``agents.tier2.adp_source.AthenaTimeoutError``
        Propagated if the Athena query does not complete within the poll ceiling.
    """
    # Log count only — never the VIN.  Tier 2 runs unattended; CloudWatch is
    # the widest exposure surface.  See security-review Cycle 7 Warning W3 +
    # the platform's test-enforced "no VINs in logs" contract in
    # agents/tier2/tests/test_budget.py::test_no_vins_in_budget_exception_repr.
    log.debug("adp_battery_source: querying energy_usage vin_count=1")

    # Allow-list the VIN before embedding it in the SQL literal.
    # Athena has no parameterised-query API; sanitise_vin rejects any
    # character outside [A-Z0-9-] to prevent SQL injection.
    # Security-review Cycle 7 W7 — uniform defence across all three sources.
    safe_vin = sanitise_vin(vin)
    sql = _BATTERY_SQL_TEMPLATE.format(database=database, vin=safe_vin)
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    # Hash and evidence are built from the RAW ADP rows so `inputs_hash` stays a
    # function of upstream data, not of our naming choices.
    inputs_hash = _compute_hash(rows)
    evidence = _build_evidence(rows)

    return {
        "vin": vin,
        "rows": [_to_agent_row(r) for r in rows],
        "inputs_hash": inputs_hash,
        "evidence": evidence,
    }


def _to_agent_row(raw: dict) -> dict:
    """Translate one ADP row into this module's agent-facing vocabulary.

    The agent must never see an ADP column name: spec § D3 makes each source a
    swappable interface, so an upstream rename has to stop here rather than
    reaching `agent.py`.  The tire source already worked this way; battery did
    not, and `test_agent_reads_only_keys_the_sources_emit` caught it.
    """
    return {
        "vin": raw.get("vin"),
        "health_pct": raw.get("state_of_health_pct"),
        "age_days": raw.get("battery_age_days"),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _compute_hash(rows: list[dict]) -> str:
    """Return a stable SHA-256 hex digest over *rows*.

    Normalisation (spec § D4):
    1. Sort rows by ``(vin, battery_age_days)`` for a stable ordering.
    2. Serialise each row's numeric fields with 6 decimal places of precision.
    3. Serialise to JSON with sorted keys, no extra whitespace.
    4. Hash the UTF-8 bytes of the joined JSON strings.

    An empty row list (no ADP data for this VIN) hashes to a fixed sentinel
    so the artifact writer can still record an ``absent``-provenance finding
    idempotently.
    """
    if not rows:
        return hashlib.sha256(b"energy_usage:empty").hexdigest()

    normalised = []
    for row in sorted(rows, key=lambda r: (r.get("vin", ""), r.get("battery_age_days", ""))):
        entry: dict = {}
        for k, v in sorted(row.items()):
            # Serialise numeric strings with fixed precision for hash stability.
            try:
                entry[k] = f"{float(v):.6f}" if v is not None else None
            except (TypeError, ValueError):
                entry[k] = v
        normalised.append(entry)

    serialised = json.dumps(normalised, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _build_evidence(rows: list[dict]) -> list[dict]:
    """Build evidence entries from ADP rows (spec § D4 provenance contract).

    Each evidence entry carries ``source``, ``value``, and
    ``provenance ∈ {live, absent}``.  S1 has no mocked inputs so every
    entry is ``live`` (data present) or ``absent`` (no rows for this VIN).
    """
    if not rows:
        return [
            {
                "source": "energy_usage.state_of_health_pct",
                "value": None,
                "provenance": "absent",
            },
            {
                "source": "energy_usage.battery_age_days",
                "value": None,
                "provenance": "absent",
            },
        ]

    # Use the first (most recent by ORDER BY battery_age_days DESC) row.
    row = rows[0]
    return [
        {
            "source": "energy_usage.state_of_health_pct",
            "value": row.get("state_of_health_pct"),
            "provenance": "live",
        },
        {
            "source": "energy_usage.battery_age_days",
            "value": row.get("battery_age_days"),
            "provenance": "live",
        },
    ]
