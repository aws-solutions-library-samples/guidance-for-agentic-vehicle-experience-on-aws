"""Evidence builder for the FI recall and warranty agent (spec D8 step 3, task 3.1F).

Public API
----------
build_recall_evidence(row, item, attempt, rooftops, origin, open_item_ids,
                      diagnosis_source) -> list[dict]
    Build §1.6 evidence entries for one recall item.

compute_inputs_hash(evidence) -> str
    sha256 of canonical JSON, excluding volatile fields.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Fields excluded from the hash wherever they appear (top-level or nested).
# stock_as_of changes daily; diagnosis_source and recall.plan are volatile across runs.
_HASH_EXCLUDED: frozenset[str] = frozenset({
    "stock_as_of", "diagnosis_source",
    "recall.diagnosis_source", "warranty.diagnosis_source",
    "recall.plan",
})


def _strip_for_hash(v: Any) -> Any:
    """Recursively strip volatile keys and normalise values for hashing."""
    if isinstance(v, dict):
        return {
            k: _strip_for_hash(val)
            for k, val in v.items()
            if k not in _HASH_EXCLUDED and not k.startswith("_")
        }
    if isinstance(v, list):
        return [_strip_for_hash(item) for item in v]
    return v


def compute_inputs_hash(evidence: list[dict]) -> str:
    """sha256 of canonical JSON of evidence entries, excluding volatile fields.

    Excluded keys (wherever they appear, including nested): ``stock_as_of``,
    ``diagnosis_source``, ``recall.diagnosis_source`` (and any key starting
    with ``_`` for forward-compat with narration fields).

    Stable: two runs with the same vehicle state produce the same hash even when
    only volatile fields differ.
    """
    stripped = [_strip_for_hash(entry) for entry in evidence]
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: ADP's "no bound" model-year sentinels on an issued recall (engineering/interface.md § 12:
#: model_year_min is 0 and model_year_max 9999 unless the cause is a model year).
_NO_MIN_YEAR = 0
_NO_MAX_YEAR = 9999


def _year_bound(raw: Any, sentinel: int) -> Any:
    """The catalog's model-year bound, or None for ADP's "no bound" sentinel.

    Athena returns ints as strings ("0"); fixtures may pass ints. Anything else is
    returned unchanged, so Stage C's real years keep their exact form (and hash).
    """
    if raw is not None and str(raw).strip() == str(sentinel):
        return None
    return raw


def _lot(value: Any) -> dict[str, Any]:
    """``<supplier_id>/<supplier_lot>`` (ADP's lot cohort value) as a named pair.

    Split on the first ``/``; a value without one keeps the whole text as the lot and a
    null supplier, rather than dropping it from the evidence.
    """
    text = str(value).strip()
    supplier, sep, lot = text.partition("/")
    if not sep:
        return {"supplier_id": None, "supplier_lot": text}
    return {"supplier_id": supplier, "supplier_lot": lot}


def build_recall_evidence(
    row: dict,
    item: str,
    attempt: int,
    rooftops: list[dict],
    origin: Optional[tuple[float, float, str]],
    open_item_ids: list[str],
    diagnosis_source: str,
    plan: Optional[dict] = None,
) -> list[dict]:
    """Build §1.6 evidence entries for one recall item.

    Parameters
    ----------
    row:
        Athena row from ``select_unremedied_recalls``: recall + catalog columns.
    item:
        Item type string: ``book``, ``ground``, ``parts_expected``, or
        ``attempts_exhausted``.
    attempt:
        Integer attempt counter (1-based).
    rooftops:
        Up to three rooftop dicts from ``rank_rooftops``.
    origin:
        ``(latitude, longitude, location_id)`` of the vehicle's CMS assigned
        location (G3-10), or ``None`` when no location is assigned.
    open_item_ids:
        Finding ids of other open items for this vehicle (§1.6 vehicle.open_items).
    diagnosis_source:
        ``"model"`` or ``"rules"``.
    plan:
        Optional ``{urgency, combine_with}`` from the model or rules path.
        When None, emits default ``{urgency: None, combine_with: []}``.
        Always excluded from ``inputs_hash`` (volatile across runs).

    Returns
    -------
    list[dict]
        One dict per §1.6 evidence key, each carrying ``vin`` and
        ``provenance: "live"``.
    """
    vin = row["vin"]
    campaign_id = row["campaign_id"]
    _base = {"vin": vin, "provenance": "live"}

    entries: list[dict] = []

    # recall.campaign — catalog fields (§1.6)
    entries.append({
        **_base,
        "recall.campaign": {
            "campaign_id": campaign_id,
            "recall_class": row.get("recall_class"),
            "do_not_drive": row.get("do_not_drive"),
            "park_outside": row.get("park_outside"),
            "received_date": row.get("received_date"),
            "catalog_version": row.get("catalog_version"),
        },
    })

    # recall.rule — populated rule fields from the catalog row (§1.6).
    # FI stage D 9.3: an issued recall carries ADP's "no bound" year sentinels (0 / 9999,
    # engineering/interface.md § 12), written null here, like plants/build_* when unbounded;
    # and a lot recall names its lots, which is its real rule. A row without lots (every
    # Stage C campaign) gets no component_lots key, so its rule and hash don't change.
    rule: dict[str, Any] = {
        "rule_version": row.get("rule_version"),
        "models": row.get("models"),
        "model_year_min": _year_bound(row.get("model_year_min"), _NO_MIN_YEAR),
        "model_year_max": _year_bound(row.get("model_year_max"), _NO_MAX_YEAR),
        "plants": row.get("plants"),
        "build_from": row.get("build_from"),
        "build_to": row.get("build_to"),
    }
    lots = row.get("component_lots") or []
    if lots:
        rule["component_lots"] = [_lot(v) for v in lots]
    entries.append({**_base, "recall.rule": rule})

    # recall.match_basis
    entries.append({
        **_base,
        "recall.match_basis": {"match_basis": row.get("match_basis")},
    })

    # recall.confirming_signal — only when CONFIRMED
    if row.get("match_basis") == "CONFIRMED":
        entries.append({
            **_base,
            "recall.confirming_signal": {
                "confirming_code": row.get("confirming_code"),
                "confirming_seen_on": row.get("confirming_seen_on"),
                "confirming_source": row.get("confirming_source"),
            },
        })

    # recall.item
    entries.append({
        **_base,
        # Contract § 1.6 / call 11: item is book | ground | parts_expected |
        # attempts_exhausted (CMS groups on it); an exhausted item names its slot.
        "recall.item": {
            "campaign_id": campaign_id,
            "item": "attempts_exhausted" if item.endswith("_exhausted") else item,
            "attempt": attempt,
            **({"slot": item[: -len("_exhausted")]} if item.endswith("_exhausted") else {}),
        },
    })

    # recall.rooftops — top-three candidates
    entries.append({
        **_base,
        "recall.rooftops": rooftops,
    })

    # recall.position — assigned location (G3-10 supersedes telemetry wording)
    if origin is not None:
        lat, lon, loc_id = origin
        position = {
            "source": "assigned_location",
            "location_id": loc_id,
            "latitude": lat,
            "longitude": lon,
        }
    else:
        position = {"source": "none"}
    entries.append({**_base, "recall.position": position})

    # vehicle.open_items
    entries.append({
        **_base,
        "vehicle.open_items": {"finding_ids": open_item_ids},
    })

    # recall.plan — urgency and combine_with from the model or rules path (D50).
    # Excluded from inputs_hash like recall.diagnosis_source (volatile).
    entries.append({
        **_base,
        "recall.plan": {
            "urgency": plan["urgency"] if plan else None,
            "combine_with": plan.get("combine_with", []) if plan else [],
        },
    })

    # recall.diagnosis_source — volatile; included for completeness but excluded
    # from hash via _HASH_EXCLUDED
    entries.append({
        **_base,
        "recall.diagnosis_source": diagnosis_source,
        "diagnosis_source": diagnosis_source,
    })

    return entries
