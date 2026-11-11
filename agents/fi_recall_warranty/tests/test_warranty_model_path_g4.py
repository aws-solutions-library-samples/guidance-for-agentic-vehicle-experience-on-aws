"""Tests for run_warranty_loop model path — task 4.1 S3a group 4.

Mutation anchors (note.md):
  M1: cap ignored            → test_cap_30_drops_one_routine
  M2: reserve check removed  → test_clock_below_reserve_defers_to_rules
  M3: refresh path calls plan_fn → test_same_hash_model_live_no_plan_fn_call
  M4: refresh path writes rules notes → test_same_hash_model_live_writes_live_notes
  M5: model estimated_value_usd used → test_plan_extra_keys_ignored
  M6: proceed ignored        → test_proceed_insufficient_evidence_gives_informational
"""
from __future__ import annotations

from typing import Any, Optional

from agents.fi_recall_warranty.warranty import MODEL_CALL_RESERVE_S, run_warranty_loop

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_COMPUTED_AT = "2026-10-09T12:00:00Z"
_VIN_BASE = "MRDWAR"

_DEFAULT_TERMS = {
    "basic": {
        "terms_version": "1.0.0", "coverage_group": "basic",
        "effective_from": "2024-01-01", "term_years": 4, "term_miles": 50_000,
        "labor_rate_usd": 150.0,
    }
}


def _row(n: int, *, vin: str | None = None, verdict: str = "approved",
         recoverable_usd: str = "500.00", coverage_group: str = "basic",
         soon: bool = False) -> dict:
    """Build a minimal warranty candidate row with a unique ro_id/line_no."""
    return {
        "vehicle_vin": vin or f"{_VIN_BASE}{n:05d}",
        "ro_id": f"RO-{n:04d}",
        "line_no": str(n),
        "verdict_status": verdict,
        "recoverable_usd": str(2_000.0) if soon else recoverable_usd,
        "labor_op": f"LO-{n:04d}",
        "dealer_id": "dealer-dms-01",
        "coverage_group": coverage_group,
        "in_service_date": "2023-01-15",
        "odometer_km": "45000",
        "repair_date": "2026-09-01",
        "verdict_reason": "Within mileage term",
        "matched_rule_id": "RULE-101",
        "terms_version": "1.0.0",
    }


def _soon_row(n: int) -> dict:
    """Row with recoverable_usd >= SOON_RECOVERABLE_USD → severity 'soon'."""
    return _row(n, soon=True)


def _routine_row(n: int) -> dict:
    """Row with low recoverable_usd → severity 'routine'."""
    return _row(n, recoverable_usd="50.00")


class _Fakes:
    def __init__(self):
        self.written: list[dict] = []
        self._live: dict[str, dict] = {}
        self._write_kwargs: list[dict] = []
        self._plan_fn_calls: list[dict] = []

    def standing_fn(self, vin: str):
        return None

    def lookup_live(self, key: str) -> Optional[dict]:
        return self._live.get(key)

    def write_finding(self, finding: dict, standing: Any, **kw) -> None:
        self.written.append(finding)
        self._write_kwargs.append(kw)

    def plan_fn(self, *, row, evidence) -> dict:
        self._plan_fn_calls.append({"vin": row.get("vehicle_vin"), "ro_id": row.get("ro_id")})
        return {
            "narrative": f"model notes for {row.get('ro_id')}",
            "proceed": "file",
            "detail": "model detail",
            "confidence": "medium",
            "diagnosis_source": "model",
        }


def _run(rows, fakes=None, **kwargs) -> dict:
    if fakes is None:
        fakes = _Fakes()
    kwargs.setdefault("warranty_terms", _DEFAULT_TERMS)
    return run_warranty_loop(
        candidate_rows=rows,
        computed_at=_COMPUTED_AT,
        standing_fn=fakes.standing_fn,
        lookup_live=fakes.lookup_live,
        write_finding=fakes.write_finding,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------

class TestCap:
    """31 eligible items (mixed soon/routine), cap=30 → 30 plan_fn calls; dropped is routine."""

    def test_cap_30_drops_one_routine(self):
        """31 items: 1 soon + 30 routine; cap=30 → soon selected + 29 routine selected;
        the dropped item is the routine with the lexicographically last (vin, subject_key).
        Mutant M1: cap ignored → model_items == 31.
        """
        rows = [_soon_row(0)] + [_routine_row(i) for i in range(1, 31)]
        assert len(rows) == 31
        fakes = _Fakes()
        stats = _run(rows, fakes, plan_fn=fakes.plan_fn, model_call_cap=30)

        assert stats["model_items"] == 30, (
            f"model_items must be 30; got {stats['model_items']}. "
            "Mutant M1 (cap ignored) would give 31."
        )
        called_vins = {c["vin"] for c in fakes._plan_fn_calls}
        # soon row must be selected
        assert f"{_VIN_BASE}{0:05d}" in called_vins, "soon row must be selected over routine"
        # dropped item: MRDWAR00030 — last routine by (vin, subject_key)
        assert f"{_VIN_BASE}{30:05d}" not in called_vins, (
            "last routine by tie-break must be dropped"
        )
        # all other routine items selected
        for i in range(1, 30):
            assert f"{_VIN_BASE}{i:05d}" in called_vins, f"routine row {i} must be selected"

    def test_ties_broken_by_vin_subject_key(self):
        """Equal-severity items are stable-sorted by (vin, subject_key) before cap."""
        rows = [_routine_row(i) for i in range(5)]
        fakes = _Fakes()
        _run(rows, fakes, plan_fn=fakes.plan_fn, model_call_cap=3)
        called_vins = {c["vin"] for c in fakes._plan_fn_calls}
        # First 3 by (vin, subject_key) must be called
        for i in range(3):
            assert f"{_VIN_BASE}{i:05d}" in called_vins, f"row {i} must be in first 3"
        # Last 2 must be dropped
        for i in range(3, 5):
            assert f"{_VIN_BASE}{i:05d}" not in called_vins, f"row {i} must be dropped"

    def test_default_cap_is_warranty_cap(self):
        """When model_call_cap is None, runner.WARRANTY_CAP (30) is used."""
        from agents.fi_recall_warranty.runner import WARRANTY_CAP
        rows = [_routine_row(i) for i in range(WARRANTY_CAP + 1)]
        fakes = _Fakes()
        stats = _run(rows, fakes, plan_fn=fakes.plan_fn)
        assert stats["model_items"] == WARRANTY_CAP, (
            f"default cap must be WARRANTY_CAP={WARRANTY_CAP}; got {stats['model_items']}"
        )


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

class TestClock:
    """remaining_seconds < reserve → deferred to rules; deferred counted."""

    def test_clock_below_reserve_defers_to_rules(self):
        """When remaining_seconds() < MODEL_CALL_RESERVE_S, plan_fn not called, deferred++.
        Mutant M2: reserve check removed → plan_fn would be called here.
        """
        fakes = _Fakes()
        rows = [_routine_row(0)]
        stats = _run(rows, fakes,
                     plan_fn=fakes.plan_fn,
                     remaining_seconds=lambda: MODEL_CALL_RESERVE_S - 1.0)

        assert fakes._plan_fn_calls == [], (
            "plan_fn must NOT be called when clock is below reserve. "
            "Mutant M2 (reserve check removed) would call it."
        )
        assert stats["deferred"] == 1, f"deferred must be 1; got {stats['deferred']}"
        assert stats["items_written"] == 1, "rules plan must still write the deferred item"


# ---------------------------------------------------------------------------
# Refresh from live
# ---------------------------------------------------------------------------

class TestRefreshFromLive:
    """same hash + model-written live row → refresh_from_live; no plan_fn call."""

    def _first_finding(self, row: dict) -> dict:
        fakes = _Fakes()
        _run([row], fakes)
        assert fakes.written, "first run must write a finding"
        return fakes.written[0]

    def _live_model(self, finding: dict, *, notes: str = "live model notes",
                    detail: str = "live model detail", confidence: str = "medium") -> dict:
        """Return a live row copy with diagnosis_source=model in evidence."""
        live = dict(finding)
        live["detail"] = detail
        live["confidence"] = confidence
        live["evidence"] = [
            {"kind": "warranty.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "model",
             "computed_at": _COMPUTED_AT, "provenance": "live"},
        ]
        if "recommended_action" in finding:
            action = dict(finding["recommended_action"])
            action["parameters"] = dict(action.get("parameters", {}))
            action["parameters"]["notes"] = notes
            live["recommended_action"] = action
        return live

    def test_same_hash_model_live_no_plan_fn_call(self):
        """plan_fn never called for refresh_from_live items. Mutant M3."""
        row = _routine_row(0)
        first = self._first_finding(row)
        fakes = _Fakes()
        fakes._live[first["finding_key"]] = self._live_model(first)

        stats = _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes._plan_fn_calls == [], (
            "plan_fn must NOT be called for refresh_from_live. Mutant M3 would call it."
        )
        assert stats["items_refreshed_from_live"] == 1
        assert stats["items_skipped_unchanged"] == 1

    def test_same_hash_model_live_writes_live_notes(self):
        """Refresh carries live notes (from recommended_action.parameters.notes). Mutant M4."""
        row = _routine_row(1)
        first = self._first_finding(row)
        live = self._live_model(first, notes="LIVE MODEL NOTES", detail="LIVE DETAIL",
                                confidence="high")
        fakes = _Fakes()
        fakes._live[first["finding_key"]] = live

        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "refresh_from_live must write a finding"
        written = fakes.written[0]
        action_notes = (written.get("recommended_action") or {}).get("parameters", {}).get("notes")
        assert action_notes == "LIVE MODEL NOTES", (
            f"refresh must carry live notes; got {action_notes!r}. "
            "Mutant M4 (rules notes instead) would give _rules_narrative output."
        )
        assert written.get("detail") == "LIVE DETAIL"
        assert written.get("confidence") == "high"
        assert fakes._write_kwargs[0].get("refresh_on_noop") is True, (
            "refresh_from_live must pass refresh_on_noop=True"
        )

    def test_refresh_from_live_writes_model_source_standing_change_day3(self):
        """Refresh carries diagnosis_source=model so a standing-change write preserves
        model provenance and the next run skips without a plan_fn call. W1."""
        row = _routine_row(5)
        # Day 1: initial write via plan_fn → evidence has diagnosis_source=model
        first = self._first_finding(row)
        # Day 2: refresh from live (simulates a standing-change day in real writer)
        fakes2 = _Fakes()
        fakes2._live[first["finding_key"]] = self._live_model(first)
        _run([row], fakes2, plan_fn=fakes2.plan_fn)
        assert fakes2.written, "refresh must write"
        day2 = fakes2.written[0]
        ev2 = {e["kind"]: e["value"] for e in day2.get("evidence", [])}
        assert ev2.get("warranty.diagnosis_source") == "model", (
            f"refresh must write diagnosis_source=model; got "
            f"{ev2.get('warranty.diagnosis_source')!r}. W1"
        )
        # Day 3: use day-2 finding as live → no plan_fn call
        fakes3 = _Fakes()
        fakes3._live[day2["finding_key"]] = day2
        _run([row], fakes3, plan_fn=fakes3.plan_fn)
        assert fakes3._plan_fn_calls == [], (
            "after model-source refresh, plan_fn must not be called on day 3. W1"
        )


# ---------------------------------------------------------------------------
# update_on_same_hash
# ---------------------------------------------------------------------------

class TestUpdateOnSameHash:
    """same hash + rules-written live row → model plan gets update_on_same_hash=True."""

    def _first_finding(self, row: dict) -> dict:
        fakes = _Fakes()
        _run([row], fakes)
        assert fakes.written
        return fakes.written[0]

    def _live_rules(self, finding: dict) -> dict:
        live = dict(finding)
        live["evidence"] = [
            {"kind": "warranty.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "rules",
             "computed_at": _COMPUTED_AT, "provenance": "live"},
        ]
        return live

    def test_same_hash_rules_live_update_on_same_hash(self):
        """model plan on a rules-written same-hash row → update_on_same_hash=True (call 59)."""
        row = _routine_row(2)
        first = self._first_finding(row)
        fakes = _Fakes()
        fakes._live[first["finding_key"]] = self._live_rules(first)

        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes._plan_fn_calls, "plan_fn must be called for rules-written same-hash item"
        assert any(kw.get("update_on_same_hash") for kw in fakes._write_kwargs), (
            "write_finding must receive update_on_same_hash=True for model plan on "
            f"rules-written same-hash row. Got kwargs={fakes._write_kwargs}"
        )

    def test_rules_fallback_on_same_hash_rules_no_update_flag(self):
        """A rules-fallback plan (diagnosis_source=rules) on a same-hash rules row must NOT
        pass update_on_same_hash=True — only a model plan does (call 59)."""
        row = _routine_row(3)
        first = self._first_finding(row)

        class _RulesFakes(_Fakes):
            def plan_fn(self, *, row, evidence) -> dict:
                self._plan_fn_calls.append({"ro_id": row.get("ro_id")})
                return {
                    "narrative": "rules fallback",
                    "proceed": "file",
                    "detail": "",
                    "confidence": "low",
                    "diagnosis_source": "rules",   # ← rules, not model
                }

        fakes = _RulesFakes()
        fakes._live[first["finding_key"]] = {
            **first,
            "evidence": [{"kind": "warranty.diagnosis_source", "value": "rules",
                          "source": "agent-fi-recall-warranty",
                          "source_ref": "fi-recall-warranty-run",
                          "computed_at": _COMPUTED_AT, "provenance": "live"}],
        }
        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert not any(kw.get("update_on_same_hash") for kw in fakes._write_kwargs), (
            "rules fallback plan must NOT set update_on_same_hash. "
            f"Got kwargs={fakes._write_kwargs}"
        )


# ---------------------------------------------------------------------------
# proceed
# ---------------------------------------------------------------------------

class TestProceed:
    """proceed=insufficient_evidence → informational item with no recommended_action."""

    def test_proceed_insufficient_evidence_gives_informational(self):
        """plan proceed=insufficient_evidence → informational Finding (no recommended_action).
        Mutant M6: proceed ignored → recommended_action present.
        """
        class _InsufFakes(_Fakes):
            def plan_fn(self, *, row, evidence) -> dict:
                self._plan_fn_calls.append({"ro_id": row.get("ro_id")})
                return {
                    "narrative": "insufficient evidence",
                    "proceed": "insufficient_evidence",
                    "detail": "not enough info",
                    "confidence": "low",
                    "diagnosis_source": "model",
                }

        fakes = _InsufFakes()
        stats = _run([_routine_row(4)], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "informational item must still be written"
        finding = fakes.written[0]
        assert "recommended_action" not in finding, (
            "proceed=insufficient_evidence must produce no recommended_action. "
            "Mutant M6 (proceed ignored) would leave recommended_action present."
        )
        assert stats["items_written"] == 1


# ---------------------------------------------------------------------------
# Plan extra keys ignored (mutation guard b)
# ---------------------------------------------------------------------------

class TestPlanExtraKeysIgnored:
    """Plan with extra keys estimated_value_usd, verdict_status, severity, disclosure_txt
    must not change any of those fields on the Finding."""

    def test_plan_extra_keys_ignored(self):
        """estimated_value_usd always equals the DMS row's recoverable_usd (guard b).
        Mutant M5: using model estimated_value_usd would set 9_999.99 instead of 500.00.
        """
        class _ExtraKeyFakes(_Fakes):
            def plan_fn(self, *, row, evidence) -> dict:
                self._plan_fn_calls.append({"ro_id": row.get("ro_id")})
                return {
                    "narrative": "plan with extras",
                    "proceed": "file",
                    "detail": "detail",
                    "confidence": "medium",
                    "diagnosis_source": "model",
                    # Extra keys the plan must NOT propagate to the Finding
                    "estimated_value_usd": 9_999.99,
                    "verdict_status": "PLAN_VERDICT",
                    "severity": "p0",
                    "disclosure_txt": "PLAN_DISCLOSURE",
                }

        row = _row(5, recoverable_usd="500.00")
        fakes = _ExtraKeyFakes()
        _run([row], fakes, plan_fn=fakes.plan_fn)

        assert fakes.written, "a finding must be written"
        finding = fakes.written[0]
        action = finding.get("recommended_action", {})
        usd = action.get("estimated_value_usd")
        assert usd == 500.0, (
            f"estimated_value_usd must equal DMS recoverable_usd=500.0; got {usd!r}. "
            "Mutant M5 (uses model value) would give 9999.99."
        )
        # severity from _compute_severity(500.0) → "routine", not plan's "p0"
        assert finding.get("severity") == "routine", (
            f"severity must be 'routine' from compute_severity; got {finding.get('severity')!r}. "
            "Mutant M2 (plan verdict_status → headline) would fail here."
        )
        # headline built from verdict+labor_op, not plan's verdict_status
        assert finding.get("headline") == "Warranty claim candidate: approved — LO-0005", (
            f"headline must be fixed by verdict+labor_op; got {finding.get('headline')!r}. "
            "Mutant M2 (plan verdict_status → headline) gives 'PLAN_VERDICT' in headline."
        )
        # disclosure_txt from term_statement + suffix, not plan's narrative
        expected_disc = (
            "Basic warranty: 4 years / 50,000 miles "
            "DMS decides coverage when the claim is filed."
        )
        assert finding.get("disclosure_txt") == expected_disc, (
            f"disclosure_txt must equal term_statement+suffix; got {finding.get('disclosure_txt')!r}. "
            "Mutant M3 (plan narrative → disclosure_txt) would fail here."
        )


# ---------------------------------------------------------------------------
# Denied rows never reach plan_fn
# ---------------------------------------------------------------------------

class TestDeniedSkipsPlanFn:
    def test_denied_rows_never_reach_plan_fn(self):
        """Denied rows are filtered in the decide phase; plan_fn is never called for them."""
        fakes = _Fakes()
        rows = [_row(6, verdict="denied"), _routine_row(7)]
        stats = _run(rows, fakes, plan_fn=fakes.plan_fn)

        denied_vins = {c["vin"] for c in fakes._plan_fn_calls
                       if c["ro_id"] == "RO-0006"}
        assert not denied_vins, "plan_fn must not be called for denied rows"
        assert stats["rows_refused"] >= 1, "denied row must increment rows_refused"
        # non-denied row is processed
        assert stats["model_items"] == 1, "non-denied row must call plan_fn"
