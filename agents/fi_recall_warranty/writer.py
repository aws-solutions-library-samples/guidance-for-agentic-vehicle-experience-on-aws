"""Recall finding writer for the FI recall and warranty agent (spec D9).

Spec source: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D9
Contract: docs/fi-recall-warranty-contract.md § 1

Public API
----------
build_items_from_band(band_result, model_plan) -> list
    Copy of band_result.items; model_plan is never read for items.

build_disclosure_txt(catalog_entry) -> str
    summary + "\\n\\n" + consequence + "\\n\\n" + remedy, byte for byte (§ 1.5).

build_headline(item_type, campaign_id, dealer_id, remedy_date) -> str
    Only the three § 1.4 templates; any other item_type raises ValueError.

build_recall_finding(...) -> dict
    Full Finding dict ready for agents/tier2/writer.write_finding().
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any, Optional

from agents.fi_recall_warranty.band import BandResult
from agents.tier2.writer import compute_finding_key

#: Agent identifier for all fi_recall_warranty Findings.
AGENT_ID: str = "agent-fi-recall-warranty"

#: Semver pinned per artifact write.
AGENT_VERSION: str = "0.1.0"

#: Finding kind for all recall Findings.
FINDING_KIND: str = "fleet.recall.vehicle"

#: TTL in days (contract § 1.8).
_EXPIRY_DAYS: int = 3

# Map item_type to the subject_key action segment (§ 1.2).
# Exhausted types use <slot>_exhausted; parts_expected has its own segment.
_SUBJECT_ACTION: dict[str, str] = {
    "ground": "ground",
    "book": "book",
    "parts_expected": "parts_expected",
    "book_exhausted": "book_exhausted",
    "ground_exhausted": "ground_exhausted",
}

_INFORMATIONAL_TYPES: frozenset[str] = frozenset(
    {"parts_expected", "book_exhausted", "ground_exhausted"}
)
_VALID_ITEM_TYPES: frozenset[str] = frozenset(_SUBJECT_ACTION.keys())



# ---------------------------------------------------------------------------
# 1. build_items_from_band
# ---------------------------------------------------------------------------

def build_items_from_band(band_result: BandResult, model_plan: Any) -> list:
    """Return a copy of band_result.items; model_plan is accepted but never read.

    Spec D9: the band table is deterministic configuration; the model cannot
    change severity or grounding.

    Mutation guard: any implementation that returns model_plan.get("items", ...)
    will fail test_writer_cannot_remove_grounding_from_band_result (band Row 1
    produces ["ground", "book"] but model_plan carries items=["book"]).
    """
    return list(band_result.items)



# ---------------------------------------------------------------------------
# 2. build_disclosure_txt
# ---------------------------------------------------------------------------

def build_disclosure_txt(catalog_entry: dict[str, Any]) -> str:
    """Build disclosure text from catalog fields (contract § 1.5).

    Returns ``summary + "\\n\\n" + consequence + "\\n\\n" + remedy``, byte for byte.
    No model output touches this field.  CMS shows it in full, never truncated.

    Mutation guard: returning ``remedy.strip()`` fails a test that checks
    ``txt.endswith(remedy)`` when ``remedy`` carries trailing whitespace.
    """
    return (
        catalog_entry["summary"]
        + "\n\n"
        + catalog_entry["consequence"]
        + "\n\n"
        + catalog_entry["remedy"]
    )



# ---------------------------------------------------------------------------
# 3. build_headline
# ---------------------------------------------------------------------------

def build_headline(
    item_type: str,
    campaign_id: str,
    dealer_id: Optional[str],
    remedy_date: Optional[str],
) -> str:
    """Return a fixed headline per contract § 1.4.

    Templates:
      ground           -> "Safety recall <id>: ground until the remedy is done"
      book             -> "Recall <id>: book the remedy at <rooftop>"  (dealer_id required)
      parts_expected   -> "Recall <id>: remedy parts expected <date>"  (remedy_date required)
      book_exhausted / ground_exhausted -> attempts._INFORMATIONAL_HEADLINE
    """
    if item_type == "ground":
        return f"Safety recall {campaign_id}: ground until the remedy is done"
    if item_type == "book":
        if dealer_id is None:
            raise ValueError(
                f"book item requires dealer_id; got None for campaign {campaign_id!r}"
            )
        return f"Recall {campaign_id}: book the remedy at {dealer_id}"
    if item_type == "parts_expected":
        if remedy_date is None:
            raise ValueError(
                f"parts_expected item requires remedy_date; got None for campaign {campaign_id!r}"
            )
        return f"Recall {campaign_id}: remedy parts expected {remedy_date}"
    if item_type in ("book_exhausted", "ground_exhausted"):
        from agents.fi_recall_warranty.attempts import _INFORMATIONAL_HEADLINE
        return _INFORMATIONAL_HEADLINE
    raise ValueError(f"No § 1.4 headline template for item_type={item_type!r}")



# ---------------------------------------------------------------------------
# 4. build_recall_finding
# ---------------------------------------------------------------------------

def _iso_add_days(iso_utc: str, days: int) -> str:
    """Return iso_utc + days as an ISO 8601 UTC string ending in 'Z'."""
    dt = datetime.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    future = dt + datetime.timedelta(days=days)
    return future.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_recall_finding(
    *,
    vin: str,
    campaign_id: str,
    item_type: str,
    attempt: int,
    band_result: BandResult,
    headline: str,
    detail: str,
    catalog_entry: dict[str, Any],
    dealer_id: Optional[str],
    computed_at: str,
    evidence: list[dict[str, Any]],
    confidence: str,
    inputs_hash: str,
) -> dict[str, Any]:
    """Build a full Finding dict for agents/tier2/writer.write_finding().

    Parameters
    ----------
    vin:          Vehicle identifier (scope.vin).
    campaign_id:  Recall campaign id (e.g. "MRD-2024-001").
    item_type:    "ground", "book", "parts_expected", "book_exhausted", or "ground_exhausted".
    attempt:      Integer >= 1.
    band_result:  BandResult from band.classify(); severity and items taken from here.
                  ``item_type="ground"`` requires ``"ground"`` in band_result.items.
    headline:     Pre-built via build_headline.
    detail:       Model-written detail (at most 600 chars).
    catalog_entry: Recall catalog record used for disclosure_txt.
    dealer_id:    Rooftop id for book recommended_action; None for ground/informational.
    computed_at:  ISO 8601 UTC string (e.g. "2024-01-01T00:00:00Z").
    evidence:     Evidence entries (§ 1.6), each with vin and provenance: live.
    confidence:   "low", "medium", or "high".
    inputs_hash:  64-char SHA-256 hex of the input bundle.

    Raises
    ------
    ValueError
        If item_type is not one of the four known types, or if item_type="ground"
        but "ground" is not in band_result.items (spec D9 fail-closed).

    Returns
    -------
    dict
        Finding matching agents/tier2/schemas/finding.schema.json.
    """
    if item_type not in _VALID_ITEM_TYPES:
        raise ValueError(
            f"Unknown item_type {item_type!r}; expected one of {sorted(_VALID_ITEM_TYPES)}"
        )
    if item_type == "ground" and "ground" not in band_result.items:
        raise ValueError(
            f"item_type='ground' requires 'ground' in band_result.items; "
            f"got items={band_result.items!r} (severity={band_result.severity!r})"
        )

    severity = (
        "informational" if item_type in _INFORMATIONAL_TYPES else band_result.severity
    )
    action = _SUBJECT_ACTION.get(item_type, item_type)
    subject_key = f"{campaign_id}/{action}/{attempt}"

    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": _iso_add_days(computed_at, _EXPIRY_DAYS),
        "confidence": confidence,
        "finding_kind": FINDING_KIND,
        "headline": headline,
        "detail": detail,
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        "disclosure_txt": build_disclosure_txt(catalog_entry),
    }

    # recommended_action per § 1.7; absent for informational types (parts_expected, *_exhausted).
    if item_type == "ground":
        finding["recommended_action"] = {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": vin,        # always from scope, never from model
                "recall_id": campaign_id,
            },
        }
    elif item_type == "book":
        params: dict[str, Any] = {
            "vehicle_vin": vin,            # always from scope, never from model
            "recall_id": campaign_id,
        }
        if dealer_id is not None:
            params["dealer_id"] = dealer_id
        finding["recommended_action"] = {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": params,
        }
    # parts_expected / book_exhausted / ground_exhausted: no recommended_action (informational)

    return finding
