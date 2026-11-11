"""Tests for run_recall_loop model path — task 4.1 S2b group 4.

Mutation anchors (note.md):
  M1: cap ignored            → test_cap_60_drops_one_lowest_severity
  M2: severity order reversed → test_cap_60_drops_one_lowest_severity
  M3: reserve check removed  → test_clock_below_reserve_defers_to_rules
  M4: refresh path calls plan_fn → test_same_hash_model_live_no_plan_fn_call
  M5: refresh path writes rules plan → test_same_hash_model_live_writes_live_content
  M6: update_on_same_hash not passed → test_same_hash_rules_live_update_on_same_hash
  M7: dealer check removed   → test_plan_with_unknown_dealer_uses_rules_plan
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from agents.fi_recall_warranty import writer as _writer
from agents.fi_recall_warranty.recall import MODEL_CALL_RESERVE_S, run_recall_loop
from agents.fi_recall_warranty.tests.test_recall_loop_g3 import (
    _Fakes as _G3Fakes,
    _COMPUTED_AT,
    _LOCATION_ID,
    _STOCK_ROWS,
    _TODAY,
    _base_row,
)

# ---------------------------------------------------------------------------
# G4 fakes: extends g3 with write_finding kwargs tracking + richer plan_fn
# ---------------------------------------------------------------------------

_DEALER = _STOCK_ROWS[0]["dealer_id"]  # "dealer-a"


class _Fakes(_G3Fakes):
    """G4 extension: records write_finding **kwargs and plan_fn vin+bundle."""

    def __init__(self):
        super().__init__()
        self._write_kwargs: list[dict] = []

    def write_finding(self, finding: dict, standing: Any, **kwargs) -> dict:
        self.written.append((finding, standing))
        self._write_kwargs.append(kwargs)
        return {"action": "put"}

    def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None) -> dict:
        self._plan_fn_calls.append({
            "item_type": item_type,
            "vin": row["vin"],
            "campaign_id": row["campaign_id"],
            "bundle": bundle,
        })
        return {
            "dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
            "combine_with": [],
            "urgency": band_result.urgency,
            "detail": "model plan",
            "confidence": "medium",
            "diagnosis_source": "model",
        }

    def open_items_fn(self, vin: str) -> list[dict]:
        return self._open_items if hasattr(self, "_open_items") else []


# ---------------------------------------------------------------------------
# Run helper
# ---------------------------------------------------------------------------

def _run(rows: list[dict], fakes: _Fakes, **kwargs) -> dict:
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


def _ev_value(finding: dict, kind: str) -> Any:
    """Extract a value from schema evidence by kind."""
    for entry in finding.get("evidence") or []:
        if entry.get("kind") == kind:
            return entry.get("value")
    return None


# ---------------------------------------------------------------------------
# Helper: build attention / soon / routine rows
# ---------------------------------------------------------------------------

def _att_row(n: int) -> dict:
    return _base_row(f"MRDATT{n:03d}", f"CAM-ATT-{n:03d}", "safety",
                     do_not_drive=False, match_basis="POPULATION")


def _soon_row(n: int) -> dict:
    return _base_row(f"MRDSOO{n:03d}", f"CAM-SOO-{n:03d}", "non_safety",
                     do_not_drive=False, match_basis="CONFIRMED")


def _rout_row(n: int) -> dict:
    return _base_row(f"MRDROU{n:03d}", f"CAM-ROU-{n:03d}", "non_safety",
                     do_not_drive=False, match_basis="POPULATION")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCap:
    """Cap 60 respects severity order; equal-severity ties break by (vin, subject_key)."""

    def test_cap_60_drops_one_lowest_severity(self):
        """61 eligible items (20 attention + 20 soon + 21 routine), cap=60:
        60 plan_fn calls; the single dropped item is the lowest-severity one
        with the lexicographically last (vin, subject_key) among routines.
        """
        rows = (
            [_att_row(i) for i in range(20)]
            + [_soon_row(i) for i in range(20)]
            + [_rout_row(i) for i in range(21)]
        )
        assert len(rows) == 61
        fakes = _Fakes()
        stats = _run(rows, fakes, plan_fn=fakes.plan_fn, model_call_cap=60)

        assert stats["model_items"] == 60, (
            f"model_items must be 60; got {stats['model_items']}. "
            "Mutant M1 (cap ignored) would give 61."
        )
        called_vins = {c["vin"] for c in fakes._plan_fn_calls}
        # Dropped item: MRDROU020 — last routine by (vin, subject_key) tie-break
        assert "MRDROU020" not in called_vins, (
            "MRDROU020 must be dropped (lowest severity, last in tie-break). "
            "Mutant M2 (severity reversed) would drop an attention/soon item instead."
        )
        # All higher-severity items must be selected.
        for i in range(20):
            assert f"MRDATT{i:03d}" in called_vins, f"attention MRDATT{i:03d} must be selected"
            assert f"MRDSOO{i:03d}" in called_vins, f"soon MRDSOO{i:03d} must be selected"
        # 20 of 21 routine items selected (MRDROU000..MRDROU019)
        for i in range(20):
            assert f"MRDROU{i:03d}" in called_vins, f"routine MRDROU{i:03d} must be selected"


class TestFixedItemsSkipPlanFn:
    """Ground, parts_expected and exhausted items never call plan_fn (path='fixed')."""

    def test_plan_fn_never_called_for_fixed_item_types(self):
        """ground / parts_expected / book_exhausted → path='fixed' → no plan_fn call."""
        vin = "MRDFIXD000"

        # ground: safety + do_not_drive=True → p0, [ground, book]
        ground_row = _base_row(vin, "CAM-GRND", "safety",
                               do_not_drive=True, match_basis="POPULATION")

        # parts_expected: remedy not yet available
        parts_row = _base_row(vin, "CAM-PEXP", "safety",
                              do_not_drive=False, match_basis="POPULATION")
        parts_row["remedy_available_from"] = "2099-01-01"

        # book_exhausted: attempt 5 with cancelled status
        exhaust_row = _base_row(vin, "CAM-EXST", "safety",
                                do_not_drive=False, match_basis="POPULATION")

        class _ExhaustFakes(_Fakes):
            def history_fn(self, v):
                return {
                    ("CAM-EXST", "book"): {
                        "attempt_number": 5,
                        "queue_status": "cancelled",
                        "decided_at": "2026-10-01T00:00:00Z",
                        "diagnosis_source": "rules",
                        "inputs_hash": "0" * 64,
                        "computed_at": "2026-10-01T05:00:00Z",
                    },
                }, 0

        fakes = _ExhaustFakes()
        _run([ground_row, parts_row, exhaust_row], fakes, plan_fn=fakes.plan_fn)

        fixed_calls = [c for c in fakes._plan_fn_calls
                       if c["item_type"] in ("ground", "parts_expected", "book_exhausted")]
        assert fixed_calls == [], (
            f"plan_fn must NOT be called for ground/parts_expected/book_exhausted; "
            f"got {fixed_calls}."
        )
        # Control: the book item from ground_row IS called (eligible path)
        book_calls = [c for c in fakes._plan_fn_calls if c["item_type"] == "book"]
        assert book_calls, "book item from p0 row must call plan_fn"


class TestClock:
    """remaining_seconds < reserve → deferred to rules, deferred counted."""

    def test_clock_below_reserve_defers_to_rules(self):
        """When remaining_seconds() < MODEL_CALL_RESERVE_S, plan_fn not called, deferred++."""
        fakes = _Fakes()
        row = _att_row(0)
        stats = _run([row], fakes,
                     plan_fn=fakes.plan_fn,
                     remaining_seconds=lambda: MODEL_CALL_RESERVE_S - 1.0)

        assert fakes._plan_fn_calls == [], (
            "plan_fn must NOT be called when clock is below reserve. "
            "Mutant M3 (reserve check removed) would call plan_fn here."
        )
        assert stats["deferred"] == 1, f"deferred must be 1; got {stats['deferred']}"
        assert stats["items_written"] == 1, "rules plan written for deferred item"


class TestRefreshFromLive:
    """same hash + model-written live row → refresh_from_live (no plan_fn call)."""

    def _first_finding(self, row: dict) -> dict:
        """Run once (no live rows) and return the first written finding."""
        f = _Fakes()
        _run([row], f)
        assert f.written, "first run must write a finding"
        return f.written[0][0]

    def _live_model(self, finding: dict) -> dict:
        """Return a live row copy with diagnosis_source=model."""
        live = dict(finding)
        live["detail"] = "live model detail"
        live["confidence"] = "medium"
        live["evidence"] = [
            {"kind": "recall.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "model",
             "computed_at": _COMPUTED_AT, "provenance": "live"},
            {"kind": "recall.plan",
             "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run",
             "value": {"urgency": "this_week", "combine_with": []},
             "computed_at": _COMPUTED_AT, "provenance": "live"},
        ]
        # Copy dealer from recommended_action if present
        return live

    def test_same_hash_model_live_no_plan_fn_call(self):
        """plan_fn never called for refresh_from_live items. Mutant M4."""
        row = _att_row(0)
        first = self._first_finding(row)

        fakes = _Fakes()
        fakes._live[first["finding_key"]] = self._live_model(first)

        stats = _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes._plan_fn_calls == [], (
            f"plan_fn must NOT be called for refresh_from_live item; "
            f"got calls={fakes._plan_fn_calls}. Mutant M4 would call it."
        )
        assert stats["items_refreshed_from_live"] == 1, (
            f"items_refreshed_from_live must be 1; got {stats['items_refreshed_from_live']}"
        )

    def test_same_hash_model_live_writes_live_content(self):
        """Refresh uses live row's detail and refresh_on_noop=True. Mutant M5."""
        row = _att_row(1)
        first = self._first_finding(row)
        live = self._live_model(first)
        live["detail"] = "THIS IS THE LIVE DETAIL"

        fakes = _Fakes()
        fakes._live[first["finding_key"]] = live

        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "refresh_from_live must write a finding"
        written_finding = fakes.written[0][0]
        assert written_finding["detail"] == "THIS IS THE LIVE DETAIL", (
            f"Refreshed finding must carry live detail; got {written_finding['detail']!r}. "
            "Mutant M5 (writes rules plan instead) would give 'Book the recall...'."
        )
        assert fakes._write_kwargs[0].get("refresh_on_noop") is True, (
            f"refresh_from_live must pass refresh_on_noop=True; got {fakes._write_kwargs[0]}"
        )

    def test_refresh_from_live_writes_model_source_standing_change_day3(self):
        """Refresh carries diagnosis_source=model so day 3 is refresh_from_live (no plan_fn call). N1."""
        row = _att_row(6)
        # Day 1: initial write via plan_fn → finding has recall.diagnosis_source=model
        first = self._first_finding(row)
        # Day 2: refresh_from_live path (same hash, model-written live)
        fakes2 = _Fakes()
        fakes2._live[first["finding_key"]] = self._live_model(first)
        _run([row], fakes2, plan_fn=fakes2.plan_fn)
        assert fakes2.written, "refresh must write on day 2"
        day2 = fakes2.written[0][0]
        ev2 = {e["kind"]: e["value"] for e in day2.get("evidence", [])}
        assert ev2.get("recall.diagnosis_source") == "model", (
            f"refresh must write diagnosis_source=model; got "
            f"{ev2.get('recall.diagnosis_source')!r}. N1"
        )
        # Day 3: use day-2 finding as live → must still be refresh_from_live (no plan_fn call)
        fakes3 = _Fakes()
        fakes3._live[day2["finding_key"]] = day2
        _run([row], fakes3, plan_fn=fakes3.plan_fn)
        assert fakes3._plan_fn_calls == [], (
            "after model-source refresh, plan_fn must not be called on day 3. N1"
        )


class TestUpdateOnSameHash:
    """same hash + rules-written live row → update_on_same_hash=True. Mutant M6."""

    def test_same_hash_rules_live_update_on_same_hash(self):
        """plan_fn called and write_finding receives update_on_same_hash=True."""
        row = _att_row(2)
        f0 = _Fakes()
        _run([row], f0)
        first = f0.written[0][0]

        # Seed live with rules-written same-hash finding.
        live = dict(first)
        live["evidence"] = [
            {"kind": "recall.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "rules",
             "computed_at": _COMPUTED_AT, "provenance": "live"},
        ]
        fakes = _Fakes()
        fakes._live[first["finding_key"]] = live

        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes._plan_fn_calls, "plan_fn must be called for rules-written same-hash item"
        assert any(kw.get("update_on_same_hash") for kw in fakes._write_kwargs), (
            "write_finding must receive update_on_same_hash=True (call 59). "
            f"Got kwargs={fakes._write_kwargs}. Mutant M6 omits this flag."
        )


class TestDealerGuard:
    """plan naming a dealer outside rooftops → rules plan. Mutant M7."""

    def test_plan_with_unknown_dealer_uses_rules_plan(self):
        """plans_refused incremented; written finding uses rules-path dealer_id."""
        row = _att_row(3)

        class _BadDealerFakes(_Fakes):
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self._plan_fn_calls.append({"item_type": item_type, "vin": row["vin"],
                                             "campaign_id": row["campaign_id"], "bundle": bundle})
                return {
                    "dealer_id": "NONEXISTENT-DEALER",
                    "combine_with": [], "urgency": band_result.urgency,
                    "detail": "bad dealer plan", "confidence": "medium",
                    "diagnosis_source": "model",
                }

        fakes = _BadDealerFakes()
        stats = _run([row], fakes, plan_fn=fakes.plan_fn)

        assert stats["plans_refused"] == 1, (
            f"plans_refused must be 1; got {stats['plans_refused']}. "
            "Mutant M7 (dealer check removed) would give 0."
        )
        # Written finding must use rules plan's dealer (rooftops[0]["dealer_id"]).
        written_finding = fakes.written[0][0]
        rec_action = written_finding.get("recommended_action", {})
        params = rec_action.get("parameters", {})
        assert params.get("dealer_id") == _DEALER, (
            f"rules plan dealer must be {_DEALER!r}; got {params.get('dealer_id')!r}"
        )
        # confidence comes from rules plan (low), not the bad plan (medium)
        assert written_finding["confidence"] == "low"


class TestPlanExtraKeysIgnored:
    """Extra keys (severity, items, headline, disclosure_txt) in plan are ignored."""

    def test_plan_with_extra_keys_does_not_change_immutable_fields(self):
        row = _att_row(4)  # severity=attention

        class _ExtraKeyFakes(_Fakes):
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self._plan_fn_calls.append({"item_type": item_type, "vin": row["vin"],
                                             "campaign_id": row["campaign_id"], "bundle": bundle})
                return {
                    "dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
                    "combine_with": [], "urgency": band_result.urgency,
                    "detail": "valid detail", "confidence": "medium",
                    "diagnosis_source": "model",
                    # Extra keys that must be ignored:
                    "severity": "routine",
                    "items": ["book_only"],
                    "headline": "WRONG HEADLINE FROM PLAN",
                    "disclosure_txt": "WRONG DISCLOSURE FROM PLAN",
                }

        fakes = _ExtraKeyFakes()
        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "plan_fn was called and a finding was written"
        finding = fakes.written[0][0]
        # Severity comes from band_result (attention), not the plan's "routine"
        assert finding["severity"] == "attention", (
            f"severity must be 'attention' from band_result; got {finding['severity']!r}. "
            "Plan's 'severity' key must be ignored."
        )
        # Headline equals build_headline output exactly — not the plan's literal
        expected_headline = _writer.build_headline(
            "book", row["campaign_id"], _DEALER, row.get("remedy_available_from"),
        )
        assert finding["headline"] == expected_headline, (
            f"headline must equal build_headline(…); got {finding['headline']!r}. "
            "Mutant M1 (plan detail → headline) gives 'valid detail'."
        )
        # disclosure_txt equals build_disclosure_txt(catalog) exactly
        expected_disclosure = _writer.build_disclosure_txt({
            "summary": row["summary"], "consequence": row["consequence"],
            "remedy": row["remedy"],
        })
        assert finding["disclosure_txt"] == expected_disclosure, (
            f"disclosure_txt must equal build_disclosure_txt(catalog); "
            f"got {finding['disclosure_txt']!r}."
        )


class TestBundle:
    """bundle excludes own campaign; vehicle.open_items carries bundle's ids."""

    def test_bundle_excludes_own_campaign(self):
        """open_items_fn provides own + other campaign items; bundle drops the own one."""
        vin = "MRDBNDL000"
        cam = "CAM-BUNDLE-01"
        other_cam = "CAM-OTHER-01"
        own_item = {"finding_id": "FIND-OWN-001", "finding_kind": "fleet.recall.vehicle",
                    "headline": "own recall", "campaign_id": cam}
        other_item = {"finding_id": "FIND-OTH-001", "finding_kind": "fleet.recall.vehicle",
                      "headline": "other recall", "campaign_id": other_cam}

        class _BundleFakes(_Fakes):
            def open_items_fn(self, v):
                return [own_item, other_item]

        fakes = _BundleFakes()
        row = _base_row(vin, cam, "safety", do_not_drive=False, match_basis="POPULATION")
        _run([row], fakes,
             plan_fn=fakes.plan_fn, open_items_fn=fakes.open_items_fn)

        assert fakes._plan_fn_calls, "plan_fn must be called for book item"
        bundle = fakes._plan_fn_calls[0]["bundle"]
        bundle_ids = [it["finding_id"] for it in (bundle or [])]
        assert "FIND-OWN-001" not in bundle_ids, (
            f"bundle must exclude own campaign's item; got bundle_ids={bundle_ids}"
        )
        assert "FIND-OTH-001" in bundle_ids, (
            f"bundle must include other campaigns; got bundle_ids={bundle_ids}"
        )


class TestRecallPlan:
    """recall.plan evidence entry carries plan's urgency and combine_with."""

    def test_recall_plan_evidence_carries_urgency_and_combine_with(self):
        """Model plan's urgency and combine_with are written into recall.plan evidence."""
        combine_id = "FIND-COMBINE-001"
        row = _att_row(5)  # attention → urgency="this_month" from band

        class _CombineFakes(_Fakes):
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self._plan_fn_calls.append({"item_type": item_type, "vin": row["vin"],
                                             "campaign_id": row["campaign_id"], "bundle": bundle})
                return {
                    "dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
                    "combine_with": [combine_id],
                    "urgency": "this_week",  # overrides band's "this_month"
                    "detail": "combine plan", "confidence": "medium",
                    "diagnosis_source": "model",
                }

        fakes = _CombineFakes()
        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "plan_fn was called and finding was written"
        finding = fakes.written[0][0]
        plan_val = _ev_value(finding, "recall.plan")
        assert plan_val is not None, "recall.plan evidence entry must be present"
        assert plan_val.get("urgency") == "this_week", (
            f"recall.plan.urgency must be the plan's 'this_week'; got {plan_val.get('urgency')!r}"
        )
        assert plan_val.get("combine_with") == [combine_id], (
            f"recall.plan.combine_with must carry [{combine_id!r}]; "
            f"got {plan_val.get('combine_with')!r}"
        )



class TestArchitectFixesS2b:
    """Architect fixes after S2b (group4-notes "S2b architect check").

    - A plan with NO dealer (None or "") is refused like an unknown dealer; the guard used
      to pass it, and the book headline then raised, so the item was not written at all.
    - A model call that fell back to rules on a same-hash rules-written row is written with
      refresh_on_noop only, never update_on_same_hash (that would append an audit entry to
      the row every day the model keeps failing).
    """

    def _plan_fakes(self, plan: dict):
        class _F(_Fakes):
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self._plan_fn_calls.append({"item_type": item_type})
                return dict(plan, urgency=band_result.urgency)
        return _F()

    def test_plan_with_no_dealer_is_refused_and_the_rules_item_is_written(self):
        for missing in (None, ""):
            fakes = self._plan_fakes({"dealer_id": missing, "combine_with": [],
                                      "detail": "no dealer", "confidence": "high",
                                      "diagnosis_source": "model"})
            stats = _run([_att_row(5)], fakes, plan_fn=fakes.plan_fn)
            assert stats["plans_refused"] == 1, (missing, stats)
            params = fakes.written[0][0]["recommended_action"]["parameters"]
            assert params["dealer_id"] == _DEALER, (missing, params)

    def test_fallback_plan_on_same_hash_rules_row_is_not_an_update(self):
        row = _att_row(6)
        f0 = _Fakes()
        _run([row], f0)
        first = f0.written[0][0]
        live = dict(first)
        live["evidence"] = [{"kind": "recall.diagnosis_source", "value": "rules"}]
        fakes = self._plan_fakes({"dealer_id": _DEALER, "combine_with": [],
                                  "detail": "Book the recall service at the nearest available dealer.",
                                  "confidence": "low", "diagnosis_source": "rules"})
        fakes._live[first["finding_key"]] = live
        _run([row], fakes, plan_fn=fakes.plan_fn)
        assert fakes._plan_fn_calls, "the item is still offered to the model"
        assert not any(kw.get("update_on_same_hash") for kw in fakes._write_kwargs), (
            f"a rules fallback must not force an update; got {fakes._write_kwargs}")
        assert any(kw.get("refresh_on_noop") for kw in fakes._write_kwargs)
