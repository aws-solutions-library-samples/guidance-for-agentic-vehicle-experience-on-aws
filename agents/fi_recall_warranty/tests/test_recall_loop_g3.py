"""Tests for run_recall_loop — task 3.1 G2b (rules path).

Coverage
--------
- Three VINs: p0 do-not-drive (ground+book), row-3 population (book only),
  non-safety non-confirm (routine book).
- Every written Finding validates against agents/tier2/schemas/finding.schema.json.
- standing passed to write_finding equals standing_fn's own object.
- Grounded VIN → no ground item.
- A bad row (missing recall_class) skipped, the rest written.
- Identical second run with model-written live rows → zero writes.
- Identical second run with rules-written live rows → rewritten.
- diagnosis_source=rules and confidence=low on every item.

Mutation anchors
----------------
(a) standing not passed to write_finding → test_standing_is_passed_verbatim
(b) bad row aborts the run → test_bad_row_skipped_rest_continue
(c) skip ignores diagnosis_source → test_identical_run_rules_written_rewrites
(d) ground items call plan_fn → test_ground_item_never_calls_plan_fn
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional

import pytest


# ---------------------------------------------------------------------------
# Fixtures — plain fakes that record calls
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 10, 7)
_COMPUTED_AT = "2026-10-07T05:00:00Z"

_VIN_P0 = "MRDVEH00000000001"   # safety + do_not_drive → p0 [ground, book]
_VIN_ROW3 = "MRDVEH00000000002"  # safety + not dnd + POPULATION → attention [book]
_VIN_NSAFE = "MRDVEH00000000003"  # non_safety + not dnd + POPULATION → routine [book]

_FLEET_ID = "fleet-meridian-01"
_LOCATION_ID = "loc-001"

# Catalog text common to all rows.
_SUMMARY = "The brake caliper may seize."
_CONSEQUENCE = "Reduced braking increases crash risk."
_REMEDY = "Dealers will replace the caliper at no charge."


def _base_row(vin: str, campaign_id: str, recall_class: str,
              do_not_drive: bool = False, match_basis: str = "POPULATION") -> dict:
    return {
        "vin": vin,
        "campaign_id": campaign_id,
        "recall_class": recall_class,
        "do_not_drive": do_not_drive,
        "match_basis": match_basis,
        "remedy_available_from": None,
        "remedy_part_numbers": [],
        "summary": _SUMMARY,
        "consequence": _CONSEQUENCE,
        "remedy": _REMEDY,
        "received_date": "2026-01-01",
        "catalog_version": "1",
        "rule_version": "1",
        "models": None,
        "model_year_min": None,
        "model_year_max": None,
        "plants": None,
        "build_from": None,
        "build_to": None,
        "park_outside": False,
        "confirming_code": None,
        "confirming_seen_on": None,
        "confirming_source": None,
    }


class _StandingFake:
    """Minimal object that write_finding receives for the standing parameter."""
    def __init__(self, vin: str):
        self.vin = vin
        self.fleet_id = _FLEET_ID


class _Fakes:
    """All injected callables as recording fakes."""

    def __init__(self):
        self.written: list[tuple[dict, Any]] = []   # (finding, standing)
        self._live: dict[str, dict] = {}             # finding_key -> live row (for skip rule)
        self._plan_fn_calls: list[dict] = []         # records plan_fn invocations
        self._grounded_vins: set[str] = set()        # vehicleIds grounded

    # vehicle_ids_fn
    def vehicle_ids_fn(self, vin: str) -> Optional[tuple]:
        return (f"vid-{vin}", _FLEET_ID, _LOCATION_ID)

    # locations_fn
    def locations_fn(self, fleet_id: str) -> dict:
        return {_LOCATION_ID: (37.7749, -122.4194)}

    # grounded_fn
    def grounded_fn(self, vehicle_id: str) -> tuple[bool, int]:
        return (vehicle_id in self._grounded_vins, 0)

    # history_fn — no prior history by default
    def history_fn(self, vin: str) -> tuple[dict, int]:
        return ({}, 0)

    # standing_fn — returns a real StandingFake keyed on vin
    def standing_fn(self, vin: str) -> _StandingFake:
        return _StandingFake(vin)

    # lookup_live — returns a pre-seeded live row or None
    def lookup_live(self, finding_key: str) -> Optional[dict]:
        return self._live.get(finding_key)

    # write_finding — records (finding, standing), returns a mock result
    def write_finding(self, finding: dict, standing: Any, **kwargs) -> dict:
        self.written.append((finding, standing))
        return {"action": "put"}

    # optional plan_fn override
    def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None) -> dict:
        self._plan_fn_calls.append({"item_type": item_type})
        return {
            "dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
            "combine_with": [],
            "urgency": band_result.urgency,
            "detail": "custom plan",
            "confidence": "low",
            "diagnosis_source": "rules",
        }


_STOCK_ROWS = [
    {
        "dealer_id": "dealer-a",
        "part_number": "PART-001",
        "qty_on_hand": "5",
        "latitude": "37.33",
        "longitude": "-122.03",
        "stock_as_of": "2026-10-07",
    }
]


def _run(rows, fakes: _Fakes, **kwargs) -> dict:
    from agents.fi_recall_warranty.recall import run_recall_loop
    return run_recall_loop(
        recall_rows=rows,
        stock_rows=kwargs.pop("stock_rows", _STOCK_ROWS),
        today=_TODAY,
        computed_at=_COMPUTED_AT,
        vehicle_ids_fn=fakes.vehicle_ids_fn,
        locations_fn=fakes.locations_fn,
        grounded_fn=fakes.grounded_fn,
        history_fn=fakes.history_fn,
        standing_fn=fakes.standing_fn,
        lookup_live=fakes.lookup_live,
        write_finding=fakes.write_finding,
        **kwargs,
    )


def _validate(finding: dict) -> None:
    """Validate against finding.schema.json using Draft202012Validator."""
    from jsonschema import Draft202012Validator
    schema_path = (
        Path(__file__).resolve().parents[2] / "tier2" / "schemas" / "finding.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(finding))
    assert not errors, (
        f"Finding failed schema validation ({len(errors)} error(s)): "
        + "; ".join(e.message for e in errors)
    )


# ---------------------------------------------------------------------------
# Test: three VINs → expected items and severities
# ---------------------------------------------------------------------------

class TestThreeVins:
    """p0 do-not-drive → ground+book; row-3 attention → book; routine → book."""

    def _rows(self) -> list[dict]:
        return [
            _base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                      match_basis="CONFIRMED"),
            _base_row(_VIN_ROW3, "MRD-2024-002", "safety", do_not_drive=False,
                      match_basis="POPULATION"),
            _base_row(_VIN_NSAFE, "MRD-2024-003", "non_safety", do_not_drive=False,
                      match_basis="POPULATION"),
        ]

    def test_p0_writes_ground_and_book(self):
        fakes = _Fakes()
        stats = _run(self._rows(), fakes)
        types_p0 = [
            f["finding"]["item_type"] if "item_type" in f else None
            for (f, _) in fakes.written
            if f["scope"]["vin"] == _VIN_P0
        ]
        # Use subject_key to detect item types.
        p0_findings = [f for (f, _) in fakes.written if f["scope"]["vin"] == _VIN_P0]
        subjects = [f["subject_key"] for f in p0_findings]
        assert any("ground" in s for s in subjects), (
            f"p0 do-not-drive must produce a ground item; got subjects={subjects}"
        )
        assert any("book" in s for s in subjects), (
            f"p0 do-not-drive must produce a book item; got subjects={subjects}"
        )

    def test_p0_ground_severity_is_p0(self):
        fakes = _Fakes()
        _run(self._rows(), fakes)
        p0_ground = [
            f for (f, _) in fakes.written
            if f["scope"]["vin"] == _VIN_P0 and "ground" in f["subject_key"]
        ]
        assert p0_ground, "Expected at least one p0 ground finding"
        assert p0_ground[0]["severity"] == "p0", (
            f"p0 ground item must have severity='p0'; got {p0_ground[0]['severity']!r}"
        )

    def test_row3_writes_book_not_ground(self):
        fakes = _Fakes()
        _run(self._rows(), fakes)
        row3_findings = [f for (f, _) in fakes.written if f["scope"]["vin"] == _VIN_ROW3]
        subjects = [f["subject_key"] for f in row3_findings]
        assert any("book" in s for s in subjects), (
            f"Row-3 attention must produce a book item; got subjects={subjects}"
        )
        assert not any("ground" in s for s in subjects), (
            "Row-3 (not do_not_drive) must NOT produce a ground item"
        )

    def test_nsafe_writes_book(self):
        fakes = _Fakes()
        _run(self._rows(), fakes)
        nsafe = [f for (f, _) in fakes.written if f["scope"]["vin"] == _VIN_NSAFE]
        assert nsafe, "Non-safety VIN must produce at least one finding"
        subjects = [f["subject_key"] for f in nsafe]
        assert any("book" in s for s in subjects), (
            f"Non-safety must produce a book item; got subjects={subjects}"
        )

    def test_total_items_written(self):
        fakes = _Fakes()
        stats = _run(self._rows(), fakes)
        # p0: 2 (ground+book), row3: 1, nsafe: 1 → 4
        assert stats["items_written"] == 4, (
            f"Expected 4 items_written; got {stats['items_written']}"
        )


# ---------------------------------------------------------------------------
# Test: every written Finding validates against the schema
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_all_findings_validate(self):
        fakes = _Fakes()
        rows = [
            _base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                      match_basis="CONFIRMED"),
        ]
        _run(rows, fakes)
        assert fakes.written, "Expected at least one finding to be written"
        for finding, _ in fakes.written:
            _validate(finding)


# ---------------------------------------------------------------------------
# Test: standing passed verbatim to write_finding (mutation guard a)
# ---------------------------------------------------------------------------

class TestStandingIsPassedVerbatim:
    def test_standing_is_passed_verbatim(self):
        """write_finding must receive the exact StandingFake object from standing_fn.

        Mutation (a): passing None instead of standing causes this assertion to fail.
        """
        fakes = _Fakes()
        rows = [_base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                          match_basis="CONFIRMED")]
        _run(rows, fakes)
        assert fakes.written, "Expected at least one write"
        for finding, standing in fakes.written:
            assert isinstance(standing, _StandingFake), (
                f"write_finding must receive a _StandingFake, got {type(standing).__name__!r}. "
                "Mutation (a): passing None or dropping the standing arg fails here."
            )
            assert standing.vin == finding["scope"]["vin"], (
                "StandingFake.vin must match the finding's scope.vin. "
                "Mutation (a): a mismatched standing object fails here."
            )


# ---------------------------------------------------------------------------
# Test: grounded VIN → no ground item
# ---------------------------------------------------------------------------

class TestGroundedVin:
    def test_grounded_vin_no_ground_item(self):
        fakes = _Fakes()
        fakes._grounded_vins.add("vid-" + _VIN_P0)
        rows = [_base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                          match_basis="CONFIRMED")]
        stats = _run(rows, fakes)
        p0_ground = [
            f for (f, _) in fakes.written
            if f["scope"]["vin"] == _VIN_P0 and "ground" in f["subject_key"]
        ]
        assert not p0_ground, (
            "A grounded VIN must NOT receive a new ground item; "
            f"found subjects={[f['subject_key'] for f in p0_ground]}"
        )


# ---------------------------------------------------------------------------
# Test: bad row skipped, rest continue (mutation guard b)
# ---------------------------------------------------------------------------

class TestBadRowSkipped:
    def test_bad_row_skipped_rest_continue(self):
        """A row with a missing recall_class (raises KeyError in decide_items)
        must be skipped and counted; the next row must still be written.

        Mutation (b): aborting on the bad row means the second VIN never writes,
        failing the items_written assertion.
        """
        bad_row = dict(_base_row(_VIN_P0, "MRD-BAD", "safety",
                                 do_not_drive=True, match_basis="CONFIRMED"))
        del bad_row["recall_class"]          # will raise KeyError in decide_items
        good_row = _base_row(_VIN_ROW3, "MRD-2024-002", "safety",
                              do_not_drive=False, match_basis="POPULATION")

        fakes = _Fakes()
        stats = _run([bad_row, good_row], fakes)

        assert stats["rows_refused"] >= 1, (
            "Bad row must increment rows_refused. "
            "Mutation (b): aborting the run means rows_refused would be 0 because "
            "no row is counted before the abort."
        )
        assert stats["items_written"] >= 1, (
            "Good row must still be written after bad row is skipped. "
            "Mutation (b): aborting on the bad row means items_written=0."
        )
        good_findings = [f for (f, _) in fakes.written if f["scope"]["vin"] == _VIN_ROW3]
        assert good_findings, (
            "write_finding must have been called for the good row VIN. "
            "Mutation (b): abort skips all subsequent rows."
        )


# ---------------------------------------------------------------------------
# Test: identical second run with model-written rows → zero writes
# ---------------------------------------------------------------------------

class TestModelWrittenSkip:
    def test_identical_run_model_written_skips(self):
        """Second run with the same inputs and model-written live rows → no writes."""
        rows = [_base_row(_VIN_ROW3, "MRD-2024-002", "safety",
                          do_not_drive=False, match_basis="POPULATION")]
        fakes = _Fakes()
        # First run: writes the finding.
        _run(rows, fakes)
        assert fakes.written, "First run must write at least one finding"

        # Seed the live store with model-written findings.
        for finding, _ in fakes.written:
            live = dict(finding)
            # Schema-format evidence with kind=="recall.diagnosis_source" and value=="model"
            live["evidence"] = [
                {
                    "kind": "recall.diagnosis_source",
                    "source": "agent-fi-recall-warranty",
                    "source_ref": "fi-recall-warranty-run",
                    "value": "model",
                    "computed_at": _COMPUTED_AT,
                    "provenance": "live",
                }
            ]
            fakes._live[finding["finding_key"]] = live

        # Second run: no write counted as new, every item refreshed from live (call 52).
        fakes.written.clear()
        stats = _run(rows, fakes)
        assert stats["items_written"] == 0, (
            f"Second run with model-written live rows must write nothing new; "
            f"got items_written={stats['items_written']}"
        )
        assert stats["items_skipped_unchanged"] >= 1, (
            "items_skipped_unchanged must be incremented for model-written same-hash items"
        )
        # CHANGED by FI stage C Group 4 (call 52): the skipped item is still written once,
        # as a refresh, so it doesn't expire unrefreshed.
        assert stats["items_refreshed_from_live"] == stats["items_skipped_unchanged"]
        assert len(fakes.written) == stats["items_refreshed_from_live"]


# ---------------------------------------------------------------------------
# Test: identical second run with rules-written rows → rewritten (mutation guard c)
# ---------------------------------------------------------------------------

class TestRulesWrittenRewrites:
    def test_identical_run_rules_written_rewrites(self):
        """Second run with rules-written live rows with the same hash must rewrite.

        Mutation (c): ignoring diagnosis_source (always skipping same-hash items)
        makes items_written=0 on the second run, failing this assertion.
        """
        rows = [_base_row(_VIN_ROW3, "MRD-2024-002", "safety",
                          do_not_drive=False, match_basis="POPULATION")]
        fakes = _Fakes()
        _run(rows, fakes)
        assert fakes.written, "First run must write at least one finding"

        # Seed with rules-written findings (same inputs_hash).
        for finding, _ in fakes.written:
            live = dict(finding)
            live["evidence"] = [
                {
                    "kind": "recall.diagnosis_source",
                    "source": "agent-fi-recall-warranty",
                    "source_ref": "fi-recall-warranty-run",
                    "value": "rules",
                    "computed_at": _COMPUTED_AT,
                    "provenance": "live",
                }
            ]
            fakes._live[finding["finding_key"]] = live

        fakes.written.clear()
        stats = _run(rows, fakes)
        assert stats["items_written"] >= 1, (
            f"Second run with rules-written same-hash live rows must rewrite; "
            f"got items_written={stats['items_written']}. "
            "Mutation (c): ignoring diagnosis_source always skips same-hash items."
        )


# ---------------------------------------------------------------------------
# Test: diagnosis_source=rules and confidence=low on every item
# ---------------------------------------------------------------------------

class TestRulesPathLabels:
    def test_every_item_has_rules_diagnosis_source(self):
        rows = [
            _base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                      match_basis="CONFIRMED"),
            _base_row(_VIN_ROW3, "MRD-2024-002", "safety",
                      do_not_drive=False, match_basis="POPULATION"),
        ]
        fakes = _Fakes()
        _run(rows, fakes)
        assert fakes.written
        for finding, _ in fakes.written:
            # Evidence is in schema format: {kind: "recall.diagnosis_source", value: "rules"}
            ds = None
            for entry in finding.get("evidence", []):
                if entry.get("kind") == "recall.diagnosis_source":
                    ds = entry.get("value")
                    break
            assert ds == "rules", (
                f"diagnosis_source must be 'rules' on every item; got {ds!r} "
                f"for finding {finding['subject_key']!r}"
            )

    def test_every_item_has_low_confidence(self):
        rows = [_base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                          match_basis="CONFIRMED")]
        fakes = _Fakes()
        _run(rows, fakes)
        assert fakes.written
        for finding, _ in fakes.written:
            assert finding["confidence"] == "low", (
                f"Rules path must set confidence='low'; "
                f"got {finding['confidence']!r} for {finding['subject_key']!r}"
            )


# ---------------------------------------------------------------------------
# Test: ground items never call plan_fn (mutation guard d)
# ---------------------------------------------------------------------------

class TestGroundNoPlanFn:
    def test_ground_item_never_calls_plan_fn(self):
        """plan_fn must not be called for ground items.

        Mutation (d): calling plan_fn for ground items adds a plan_fn_calls
        entry with item_type='ground', failing the assertion below.
        """
        fakes = _Fakes()
        rows = [_base_row(_VIN_P0, "MRD-2024-001", "safety", do_not_drive=True,
                          match_basis="CONFIRMED")]
        _run(rows, fakes, plan_fn=fakes.plan_fn)

        ground_plan_calls = [c for c in fakes._plan_fn_calls if c["item_type"] == "ground"]
        assert not ground_plan_calls, (
            f"plan_fn must NOT be called for ground items; got {ground_plan_calls}. "
            "Mutation (d): a plan_fn call for 'ground' adds an entry here."
        )
        # book item MAY call plan_fn (expected).
        book_calls = [c for c in fakes._plan_fn_calls if c["item_type"] == "book"]
        # p0 has both ground and book; at least the book item should call plan_fn.
        assert book_calls, (
            "plan_fn should be called for book items when supplied; "
            f"got plan_fn_calls={fakes._plan_fn_calls}"
        )
