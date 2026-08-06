"""Tire finding source — reads ADP ``tire_health`` via Athena.

**This is the only module in the CVX codebase where tire SQL and ADP
tire column names may appear.** Any other CVX file referencing
``tire_health``, ``needs_replacement``, ``wear_category``, or
``wear_rate_mm_per_1k_km`` is a spec § D3 violation and will fail the
review scanner.

Spec
----
See ``.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md`` § D3
and ``decisions.md`` § "T1.2 gate: tires are NOT buildable; battery is."

Data product
------------
ADP ``tire_health``, Glue catalog ``adp_{stage}_tire_health``.

**Unblocked 2026-08-04** when ADP published 6,000,800 rows of
``tire_health`` data.  Prior to that date this module was planned but
not buildable (see T1.2 gate, ``decisions.md`` § 2026-08-03).

Grain
-----
One row per ``(vin, tire_position, event_date)``.  The four tire
positions are ``FL`` (front-left), ``FR`` (front-right), ``RL``
(rear-left), ``RR`` (rear-right).

ADP-computed columns consumed
------------------------------
CVX reads — and does not recompute — three columns that ADP derives:

``needs_replacement``
    **Supervised label, not a deployed model output.**  ADP's own
    ``tire_health/schema.yaml:84`` describes this as: *"Supervised
    label: true when tread_depth_mm < 2.0 OR pressure anomaly indicates
    structural risk."*  This is the ML training target, not the output
    of an inference pipeline.  CVX consumes it as a data-platform signal
    and does not treat it as a prediction.  See spec § D3's stated
    limitation; the upgrade path (ADP publishing predictions as a
    catalogued product) is a separate ADP-side initiative.

``wear_category``
    ADP-computed ordinal categorisation of tread wear.  CVX reads the
    value; it does not recategorise or threshold it.

``wear_rate_mm_per_1k_km``
    ADP-computed metric describing rate of tread wear.  CVX reads the
    value; it does not extrapolate wear, predict remaining life, or fit
    any model.

CVX constraint: no thresholds, no models, no wear extrapolation
---------------------------------------------------------------
This module computes **nothing**.  It fetches the rows ADP already
produced.  Any calculation, threshold comparison, or model fit belongs
in ADP's inference pipeline.  If ``fetch_findings`` appears to need a
threshold to do its job, the seam is wrong — stop and escalate rather
than adding one.

Cross-account Athena access
---------------------------
ADP's lake and Glue catalog live in ``us-east-1`` for both CVX stages,
even though CVX staging compute runs in ``us-west-2``.  The caller must
supply an Athena client initialised in ``us-east-1``; do not derive the
region from Lambda's own runtime region.  The workgroup and results
bucket are resolved by the stack and injected as environment variables
(``ATHENA_WORKGROUP``, ``ATHENA_RESULTS_BUCKET``) per spec § "The
environment contract between the stack and the runner".

Access shape
------------
No ``assume_role`` is needed; the CVX execution role already has
cross-account Athena grants via the workgroup and results bucket,
proven end-to-end by ``scripts/verify-tier2-adp-reads.py``.

boto3 clients are injected by the caller so unit tests can supply
``botocore.stub.Stubber``-backed stubs without network access.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from agents.health._vin import sanitise_vin as _shared_sanitise_vin
from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Glue product identifier
# ---------------------------------------------------------------------------

#: The ADP Glue database name pattern.  ``{stage}`` is substituted at call
#: time.  This string is the canonical source of the product name for this
#: module — test_adp_tire_source.py asserts the SQL references it.
_GLUE_PRODUCT = "tire_health"

# ---------------------------------------------------------------------------
# Tire positions — the four valid values in ADP's schema
# ---------------------------------------------------------------------------

#: All valid tire positions in ``tire_health``.  The query filters to these
#: so unexpected schema additions do not silently expand the row set.
TIRE_POSITIONS: frozenset[str] = frozenset({"FL", "FR", "RL", "RR"})


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class AdpTireSource:
    """Fetch per-VIN tire findings from ADP ``tire_health``.

    Lifetime: one instance per Tier 2 run.  The caller supplies AWS clients
    so test doubles can be injected without network access.

    Parameters
    ----------
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.
    athena_client:
        A boto3 Athena client initialised in ``us-east-1`` (ADP's catalog
        region for both CVX stages).
    workgroup:
        Athena workgroup name, e.g. ``"cvx-staging-analytics"``.
    results_s3_path:
        S3 URI for Athena query results, e.g.
        ``"s3://cvx-staging-athena-results-{account}-us-east-1/tier2/"``.
    max_polls:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; controls
        the Athena poll ceiling.
    poll_interval:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; sleep
        between poll attempts (seconds).  Set to ``0`` in unit tests.
    max_vins:
        When no explicit VIN list is supplied via :meth:`fetch_findings`,
        bound the query to this many VINs, **selected deterministically**
        via ``ORDER BY vin``.  ``None`` means no bound (full fleet).
        Defaults to ``25`` which suits the default per-run budget.
    max_rows:
        Hard ceiling on total result rows forwarded to
        :func:`agents.tier2.adp_source.run_query`.  When ``None``, the default
        in ``adp_source._DEFAULT_MAX_ROWS`` applies.  The Tier 2 runner passes
        ``TIER2_MAX_ROWS_PER_QUERY`` from the environment so the stack-configured
        ceiling is actually enforced rather than ignored (spec T6b.1).
    """

    def __init__(
        self,
        stage: str,
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

    # -----------------------------------------------------------------------
    # FindingSource protocol
    # -----------------------------------------------------------------------

    def fetch_findings(
        self,
        vins: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return agent-facing tire readings for the given VINs.

        Translates ADP ``tire_health`` column names into agent-facing vocabulary
        at this boundary so the agent never sees an ADP column name.

        **ADP column → agent-facing key mapping** (translation lives here; no
        other CVX file may reference these ADP names as dict keys):

        ``vin``               → ``vin``              (unchanged; not an ADP-specific name)
        ``tire_position``     → ``position``
        ``event_date``        → ``event_date``        (unchanged; generic calendar field)
        ``needs_replacement`` → ``replacement_flagged``   (bool; None when ADP NULL)
        ``wear_category``     → ``wear_band``             (string; ADP-assigned ordinal)
        ``wear_rate_mm_per_1k_km`` → ``wear_rate_per_1k_km``  (string; cast by caller)

        Parameters
        ----------
        vins:
            Optional list of VINs to restrict the query.  If ``None`` or
            empty, all VINs in the product are returned (fleet-wide batch).

        Returns
        -------
        list[dict]
            One dict per tire reading with agent-facing keys.  May be empty
            if no data exists for the requested VINs.
        """
        sql = self._build_sql(vins)
        log.debug(
            "adp_tire_source: querying tire_health stage=%s vins=%s",
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
        return [self._normalise_row(r) for r in raw_rows]

    # -----------------------------------------------------------------------
    # Hash helper (public for artifact_writer interop)
    # -----------------------------------------------------------------------

    @staticmethod
    def compute_batch_hash(rows: list[dict[str, Any]]) -> str:
        """Return a stable SHA-256 over a batch of normalised tire rows.

        Stability guarantee
        -------------------
        - Rows are sorted by ``(vin, position, event_date)`` before
          hashing so row-ordering differences between Athena runs over
          identical data produce the same hash.
        - Numeric fields are serialised with six decimal places so
          floating-point representation does not vary across Python versions.
        - The final hash covers the UTF-8 bytes of a JSON-serialised list
          with sorted keys and no extra whitespace.

        This is the ``inputs_hash`` for the artifact writer (M6 idempotency).
        Rows are expected to carry agent-facing keys (``replacement_flagged``,
        ``wear_band``, ``wear_rate_per_1k_km``, ``position``) as translated
        by :meth:`_normalise_row`.
        """
        stable_rows = sorted(
            rows,
            key=lambda r: (r.get("vin") or "", r.get("position") or "", r.get("event_date") or ""),
        )
        normalised = []
        for row in stable_rows:
            normalised.append({
                "vin": row.get("vin"),
                "position": row.get("position"),
                "event_date": row.get("event_date"),
                "replacement_flagged": row.get("replacement_flagged"),
                "wear_band": row.get("wear_band"),
                "wear_rate_per_1k_km": _format_numeric(row.get("wear_rate_per_1k_km")),
            })
        payload = json.dumps(normalised, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_sql(self, vins: Optional[list[str]]) -> str:
        """Construct the Athena SQL for the tire_health product.

        SQL and ADP column names are isolated here.  Nothing outside this
        module may reference ``tire_health``, ``needs_replacement``,
        ``wear_category``, or ``wear_rate_mm_per_1k_km``.

        When *vins* is provided the query filters to those VINs exactly.
        When *vins* is ``None`` and ``max_vins`` is set, the query uses the
        deterministic distinct-VIN subquery ``ORDER BY vin LIMIT {max_vins}``
        so the same VINs are always selected, keeping ``inputs_hash`` stable
        (spec § D4/M6 idempotency).  Do **not** replace with TABLESAMPLE or
        rand() — both reintroduce per-run variation.
        """
        db = f"adp_{self._stage}_{_GLUE_PRODUCT}"
        base = f"""
            SELECT vin,
                   tire_position,
                   event_date,
                   needs_replacement,
                   wear_category,
                   wear_rate_mm_per_1k_km
            FROM {db}.{_GLUE_PRODUCT}
            WHERE tire_position IN ('FL', 'FR', 'RL', 'RR')
        """.strip()

        if vins:
            # Explicit VIN list takes precedence over max_vins.
            # Sanitised to alphanumeric + hyphens to prevent SQL injection.
            safe_vins = [_sanitise_vin(v) for v in vins]
            vin_list = ", ".join(f"'{v}'" for v in safe_vins)
            return base + f"\n            AND vin IN ({vin_list})"

        if self._max_vins is not None:
            # Deterministic bound: ORDER BY vin ensures the same VINs on every run.
            # TABLESAMPLE and rand() are explicitly forbidden — both reintroduce
            # per-run variation, making inputs_hash unstable and breaking M6.
            return (
                base
                + f"\n            AND vin IN ("
                + f"SELECT vin FROM ("
                + f"SELECT DISTINCT vin FROM {db}.{_GLUE_PRODUCT} "
                + f"ORDER BY vin LIMIT {self._max_vins}"
                + "))"
            )

        return base

    @staticmethod
    def _normalise_row(raw: dict[str, str | None]) -> dict[str, Any]:
        """Translate an Athena result row into agent-facing vocabulary.

        This is the translation boundary — ADP column names appear here and
        only here (inside this module).  The returned dict uses agent-facing
        keys so the caller never needs to reference ADP column names.

        ADP column          → agent-facing key
        ───────────────     ───────────────────
        vin                 → vin
        tire_position       → position
        event_date          → event_date
        needs_replacement   → replacement_flagged   (Python bool; None for NULL)
        wear_category       → wear_band
        wear_rate_mm_per_1k_km → wear_rate_per_1k_km
        """
        return {
            "vin": raw.get("vin"),
            "position": raw.get("tire_position"),
            "event_date": raw.get("event_date"),
            # replacement_flagged: translate ADP 'true'/'false' string to Python bool.
            "replacement_flagged": _parse_bool(raw.get("needs_replacement")),
            "wear_band": raw.get("wear_category"),
            # wear_rate_per_1k_km: numeric string from Athena; kept as string so
            # callers choose whether to cast to float.  CVX does not threshold it.
            "wear_rate_per_1k_km": raw.get("wear_rate_mm_per_1k_km"),
        }


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions; no ADP column names in signatures)
# ---------------------------------------------------------------------------


def _parse_bool(value: str | None) -> bool | None:
    """Convert ADP's string boolean representation to Python bool."""
    if value is None:
        return None
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return None  # unexpected value treated as absent


def _sanitise_vin(vin: str) -> str:
    """Allow only alphanumeric and hyphen characters in a VIN string.

    Delegates to :func:`agents.health._vin.sanitise_vin` — the single shared
    helper used by all three ADP health sources (tire, battery, brake).

    Security-review Cycle 7 W7: uniform SQL-injection defence across all
    three sources.  Previously the tire source had its own allow-list,
    battery had none, and brake had single-quote escaping only.  A shared
    helper ensures the same guard is applied consistently everywhere.

    Kept as a module-level name in ``adp_tire_source`` so existing tests
    that import ``_sanitise_vin`` from this module continue to work.

    Raises ``ValueError`` (without the VIN in the message) when no valid
    character survives sanitisation — security-review Cycle 7 W4 precedent.
    """
    return _shared_sanitise_vin(vin)


def _format_numeric(value: Any) -> str | None:
    """Serialise a numeric value to 6 d.p. for stable hashing."""
    if value is None:
        return None
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return str(value)
