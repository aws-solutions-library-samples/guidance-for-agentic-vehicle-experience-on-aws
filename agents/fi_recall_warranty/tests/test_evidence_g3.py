"""Tests for evidence.build_recall_evidence and evidence.compute_inputs_hash
(task 3.1F, spec D8 step 3-4).

Mutation targets (listed in task hand-back):
  (c) stock_as_of included in the hash   → test_hash_stable_when_stock_as_of_changes
  (d) hash ignores the rooftop set       → test_hash_changes_when_rooftop_set_changes
"""
from __future__ import annotations

import pytest

from agents.fi_recall_warranty.evidence import (
    build_recall_evidence,
    compute_inputs_hash,
)


# ---------------------------------------------------------------------------
# Minimal recall row fixture
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"

_ROW = {
    "vin": _VIN,
    "campaign_id": "MRD-2024-001",
    "recall_class": "safety",
    "do_not_drive": True,
    "park_outside": False,
    "received_date": "2024-01-15",
    "catalog_version": "1.0.0",
    "match_basis": "CONFIRMED",
    "confirming_code": "P0420",
    "confirming_seen_on": "2024-03-01",
    "confirming_source": "service_records",
    "rule_version": "1.0.0",
    "models": "['Azimuth']",
    "model_year_min": "2022",
    "model_year_max": "2023",
    "plants": None,
    "build_from": None,
    "build_to": None,
}

_ROOFTOPS = [
    {"dealer_id": "D-01", "distance_miles": 5.2, "in_stock": True,  "stock_as_of": "2026-10-01"},
    {"dealer_id": "D-02", "distance_miles": 12.7, "in_stock": False, "stock_as_of": "2026-10-01"},
]

_ORIGIN = (37.7749, -122.4194, "loc-001")  # (lat, lon, location_id)


def _build(**kwargs):
    defaults = dict(
        row=_ROW,
        item="book",
        attempt=1,
        rooftops=_ROOFTOPS,
        origin=_ORIGIN,
        open_item_ids=["FND-AAA", "FND-BBB"],
        diagnosis_source="rules",
    )
    defaults.update(kwargs)
    return build_recall_evidence(**defaults)


# ---------------------------------------------------------------------------
# Every entry has vin and provenance
# ---------------------------------------------------------------------------

class TestBaseFields:
    def test_every_entry_has_vin_and_provenance(self):
        entries = _build()
        assert entries, "Expected at least one evidence entry"
        for entry in entries:
            assert entry.get("vin") == _VIN, f"Missing vin on {entry}"
            assert entry.get("provenance") == "live", f"Expected provenance='live' on {entry}"


# ---------------------------------------------------------------------------
# position key: assigned_location vs none
# ---------------------------------------------------------------------------

class TestPosition:
    def test_position_assigned_location(self):
        entries = _build()
        pos_entries = [e for e in entries if "recall.position" in e]
        assert len(pos_entries) == 1
        pos = pos_entries[0]["recall.position"]
        assert pos["source"] == "assigned_location"
        assert pos["location_id"] == "loc-001"
        assert pos["latitude"] == 37.7749
        assert pos["longitude"] == -122.4194

    def test_position_none_when_no_origin(self):
        entries = _build(origin=None)
        pos_entries = [e for e in entries if "recall.position" in e]
        assert len(pos_entries) == 1
        pos = pos_entries[0]["recall.position"]
        assert pos["source"] == "none"
        assert "location_id" not in pos
        assert "latitude" not in pos


# ---------------------------------------------------------------------------
# Hash stability: volatile fields excluded
# (mutation target c: stock_as_of included in hash)
# ---------------------------------------------------------------------------

class TestHashStability:
    def test_hash_stable_when_stock_as_of_changes(self):
        """Mutation (c) target: if stock_as_of is included in the hash, the two
        hashes differ and this assertion fails."""
        entries_day1 = _build(rooftops=[
            {"dealer_id": "D-01", "distance_miles": 5.2, "in_stock": True,
             "stock_as_of": "2026-10-01"},
        ])
        entries_day2 = _build(rooftops=[
            {"dealer_id": "D-01", "distance_miles": 5.2, "in_stock": True,
             "stock_as_of": "2026-10-02"},   # different stock_as_of
        ])
        assert compute_inputs_hash(entries_day1) == compute_inputs_hash(entries_day2), (
            "Hash must be stable when only stock_as_of changes"
        )

    def test_hash_stable_when_diagnosis_source_changes(self):
        """diagnosis_source is volatile; a model re-run must not trigger a spurious
        re-diagnosis because the hash changed."""
        entries_rules = _build(diagnosis_source="rules")
        entries_model = _build(diagnosis_source="model")
        assert compute_inputs_hash(entries_rules) == compute_inputs_hash(entries_model), (
            "Hash must be stable when only diagnosis_source changes"
        )


# ---------------------------------------------------------------------------
# Hash changes on meaningful input changes
# (mutation target d: hash ignores rooftop set)
# ---------------------------------------------------------------------------

class TestHashChanges:
    def test_hash_changes_when_match_basis_changes(self):
        row_confirmed = {**_ROW, "match_basis": "CONFIRMED"}
        row_population = {**_ROW, "match_basis": "POPULATION"}
        h1 = compute_inputs_hash(_build(row=row_confirmed))
        h2 = compute_inputs_hash(_build(row=row_population))
        assert h1 != h2, "Hash must differ when match_basis changes"

    def test_hash_changes_when_rooftop_set_changes(self):
        """Mutation (d) target: if the hash ignores rooftops, the two hashes are
        equal and this assertion fails."""
        entries_a = _build(rooftops=[
            {"dealer_id": "D-01", "distance_miles": 5.2, "in_stock": True,
             "stock_as_of": "2026-10-01"},
        ])
        entries_b = _build(rooftops=[
            {"dealer_id": "D-99", "distance_miles": 5.2, "in_stock": True,
             "stock_as_of": "2026-10-01"},
        ])
        assert compute_inputs_hash(entries_a) != compute_inputs_hash(entries_b), (
            "Hash must differ when the rooftop dealer_id changes"
        )
