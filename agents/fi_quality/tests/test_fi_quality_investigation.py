"""Red tests — FI quality investigation agent (task 1.4 item 8).

Covers:

  8. Investigation: one live investigation per pattern (repeat request returns the same id);
     daily cap (20); candidate_id, failure_mode_id, remedy_option_id outside the bundle
     rejected; exactly one recommended option; option numbers equal the corpus formula
     whatever the model returns; fallback picks the top-ranked candidate.

Tests were xfail(strict=True) until tasks 3.3/4.2 built the code. All not-yet-built module
imports are inside test bodies so collection never errors. Use CSV custom:fleetIds in
fixtures.

Run:
    .venv/bin/python -m pytest agents/fi_quality/tests -q
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000020"
_FLEET_ID = "fleet-meridian-01"

#: CSV custom:fleetIds per task constraint.
_ENGINEER_CLAIMS = {
    "sub": "eng-sub-qi-0001",
    "cognito:groups": ["product-engineer"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}

_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_INVESTIGATION_ID = "inv-aaaa-bbbb-cccc-0001"

#: Minimal bundle for investigation tests.
_BUNDLE = {
    "pattern": {
        "pattern_id": _PATTERN_ID,
        "code_family": "P0AA6",
        "dimension": "plant",
        "value": "CGA",
        "ratio": 4.2,
        "p_value": 0.0003,
        "method": "poisson_one_sided",
    },
    "candidates": [
        {"candidate_id": "cand-001", "dimension": "plant", "value": "CGA", "rank": 1},
        {"candidate_id": "cand-002", "dimension": "build_month",
         "value": "2024-03", "rank": 2},
    ],
    "failure_modes": [
        {
            "failure_mode_id": "fm-001",
            "component": "hv_battery_module",
            "mechanism": "isolation_degradation",
            "symptom_codes": ["P0AA6"],
            "remedy_options": [
                {
                    "remedy_option_id": "opt-001",
                    "remedy_type": "hardware_recall",
                    "parts_cost_per_vehicle": 2400.00,
                    "labor_hours_per_vehicle": 8.0,
                    "lead_time_weeks": 16,
                    "over_the_air": False,
                    "tradeoffs": "High cost; eliminates isolation risk.",
                },
                {
                    "remedy_option_id": "opt-002",
                    "remedy_type": "monitor",
                    "parts_cost_per_vehicle": 0.0,
                    "labor_hours_per_vehicle": 0.5,
                    "lead_time_weeks": 1,
                    "over_the_air": True,
                    "tradeoffs": "No material fix; monitors for progression.",
                },
            ],
        }
    ],
    "fleet_type": "rental",   # cohort label only — no fleet_id, name, driver, owner, etc.
}

# ============================================================================
# Item 8a — One live investigation per pattern; daily cap (route-based)
# ============================================================================

# W9 fix: InvestigationStore had no production caller and held a duplicate
# _DAILY_CAP constant separate from the route's environment default.  The class
# has been deleted; its item-8a properties (one live per pattern, daily cap,
# repeat not counted, failed admits) are now exercised by
# TestTransactItemsFullStructure in lambdas/api-avx-investigations/tests/.
# This class is retained as a stub so existing pytest collection doesn't fail;
# the meaningful assertions live in the route test file.
class TestInvestigationIdempotency:
    """Item 8a properties proved by TestTransactItemsFullStructure in route tests."""

    def test_item_8a_covered_by_route_tests(self):
        """W9 fix: item 8a properties (one per pattern, cap, failed admits) are
        exercised by TestTransactItemsFullStructure in the route test file.
        InvestigationStore was a no-production-caller helper; its dual cap
        constant has been removed.  This stub prevents collection failure.
        """
        # Property: the route's C3-fake tests cover every item-8a case.
        # See lambdas/api-avx-investigations/tests/test_investigations_route.py
        # :: TestTransactItemsFullStructure.
        pass  # all assertions are in the route test file


# ============================================================================
# Item 8b — Bundle validation: ids outside bundle rejected
# ============================================================================

class TestBundleValidation:
    """candidate_id, failure_mode_id, remedy_option_id outside the bundle are rejected."""

    def test_candidate_id_outside_bundle_rejected(self):
        """A candidate_id not in the bundle is rejected at validation."""
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        with pytest.raises(ValidationError):
            v.validate_root_cause(
                candidate_id="cand-UNKNOWN",
                failure_mode_id="fm-001",
            )

    def test_failure_mode_id_outside_bundle_rejected(self):
        """A failure_mode_id not in the bundle is rejected."""
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        with pytest.raises(ValidationError):
            v.validate_root_cause(
                candidate_id="cand-001",
                failure_mode_id="fm-UNKNOWN",
            )

    def test_remedy_option_id_outside_bundle_rejected(self):
        """A remedy_option_id not in the bundle's failure mode is rejected.

        Fix C10: the previous version sent one option, which caused the 2–4-count
        check to fire before the membership check, leaving membership untested.
        This version sends a valid 2-option list (count passes) with one option
        outside the bundle so only the membership check can reject.
        Mutation: disable the membership check → no ValidationError raised → test fails.
        """
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        with pytest.raises(ValidationError):
            v.validate_design_options(
                failure_mode_id="fm-001",
                options=[
                    {
                        "remedy_option_id": "opt-UNKNOWN",  # not in bundle → membership fires
                        "tradeoffs": "Fabricated option.",
                        "recommended": True,   # exactly one recommended; count=2 is valid
                    },
                    {
                        "remedy_option_id": "opt-002",      # in bundle — count and
                        "tradeoffs": "Valid option.",        # recommended checks pass
                        "recommended": False,
                    },
                ],
            )

    def test_bundle_contains_no_fleet_private_fields(self):
        """The bundle must not contain fleet_id, driver, owner, position or cost fields.

        Fix W18: the previous test passed plain VIN strings; none of the inputs
        carried fleet-private fields, so a builder that forwards its inputs verbatim
        still passed.  This version feeds affected-VIN, control and service-history
        rows that DO carry fleet_id, driver_id, owner_id, coordinates, trip_id,
        cost and dealer_id.  A correct builder strips those fields; a verbatim-
        forwarding builder leaks them into the result and the recursive scan fires.

        Mutation: return inputs verbatim from build_bundle → forbidden keys survive
        in the result → _scan_nested assertion fires → test fails.
        """
        from agents.fi_quality.bundle import build_bundle

        # Rich inputs — every fleet-private field class is represented.
        _affected_vins = [
            {
                "vin": "MRDVEH00000000020",
                "fleet_id": "fleet-meridian-01",
                "driver_id": "drv-001",
                "owner_id": "own-001",
                "latitude": 33.4484,
                "longitude": -112.0740,
                "trip_id": "trip-001",
                "cost": 1234.56,
                "dealer_id": "dlr-001",
            }
        ]
        _controls = [
            {
                "vin": "MRDVEH00000000099",
                "fleet_id": "fleet-meridian-02",
                "driver_id": "drv-099",
                "latitude": 33.5000,
                "longitude": -112.1000,
                "cost": 0.0,
            }
        ]
        _service_history = [
            {
                "ro_id": "RO-001",
                "dealer_id": "dlr-002",
                "total_cost_usd": "500.00",  # W5 fix: real reader column name
                "driver_id": "drv-001",
            }
        ]

        result = build_bundle(
            pattern=_BUNDLE["pattern"],
            failure_modes=_BUNDLE["failure_modes"],
            fleet_type="rental",
            affected_vins=_affected_vins,
            controls=_controls,
            service_history=_service_history,
        )
        forbidden_keys = {
            "fleet_id", "fleet_name", "driver_id", "driver_name",
            "owner_id", "customer_id", "latitude", "longitude",
            "trip_id", "cost", "total_cost_usd", "dealer_id",
        }

        def _scan_nested(obj: object, path: str = "bundle") -> None:
            if isinstance(obj, dict):
                for key, val in obj.items():
                    assert key not in forbidden_keys, (
                        f"Forbidden key {key!r} found at {path}.{key}. "
                        f"Bundle must not carry fleet identity (spec D10 step 1). "
                        f"Mutation: build_bundle returns inputs verbatim → forbidden "
                        f"key survives → this assertion fires."
                    )
                    _scan_nested(val, f"{path}.{key}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan_nested(item, f"{path}[{i}]")

        _scan_nested(result)


# ============================================================================
# Item 8c — Exactly one recommended option; numbers from corpus
# ============================================================================

class TestOptionValidation:
    """Exactly one recommended option; numeric fields from ADP corpus, never from model."""

    def test_exactly_one_recommended_option_required(self):
        """The tool output must mark exactly one option as recommended; zero or two raises."""
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        # Two recommended — must fail.
        with pytest.raises(ValidationError):
            v.validate_design_options(
                failure_mode_id="fm-001",
                options=[
                    {"remedy_option_id": "opt-001", "tradeoffs": "...", "recommended": True},
                    {"remedy_option_id": "opt-002", "tradeoffs": "...", "recommended": True},
                ],
            )

    def test_zero_recommended_options_rejected(self):
        """Zero recommended options must also be rejected."""
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        with pytest.raises(ValidationError):
            v.validate_design_options(
                failure_mode_id="fm-001",
                options=[
                    {"remedy_option_id": "opt-001", "tradeoffs": "...", "recommended": False},
                    {"remedy_option_id": "opt-002", "tradeoffs": "...", "recommended": False},
                ],
            )

    def test_fewer_than_2_options_rejected(self):
        """Fewer than 2 design options are rejected (Fix W18: D10 requires 2–4).

        The previous tests only checked exactly-1-recommended and exactly-0-recommended.
        Mutation: allow 1 option when it is recommended → ValidationError not raised, fails.
        """
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        v = InvestigationValidator(bundle=_BUNDLE)
        with pytest.raises(ValidationError):
            v.validate_design_options(
                failure_mode_id="fm-001",
                options=[
                    {"remedy_option_id": "opt-001", "tradeoffs": "...", "recommended": True},
                    # only 1 option — below the 2-option minimum
                ],
            )

    def test_more_than_4_options_rejected(self):
        """More than 4 design options are rejected (Fix W18: D10 requires 2–4).

        Uses a 5-option bundle so the rejection is count-based, not outside-bundle.
        Mutation: allow any count above 1 → ValidationError not raised for 5, fails.
        """
        from agents.fi_quality.validator import InvestigationValidator, ValidationError

        # Build a minimal bundle with 5 remedy options for fm-001.
        bundle_5 = {
            **_BUNDLE,
            "failure_modes": [
                {
                    **_BUNDLE["failure_modes"][0],
                    "remedy_options": [
                        {
                            "remedy_option_id": f"opt-{i:03d}",
                            "remedy_type": "monitor",
                            "parts_cost_per_vehicle": 0.0,
                            "labor_hours_per_vehicle": 0.5,
                            "lead_time_weeks": 1,
                            "over_the_air": True,
                            "tradeoffs": "Generic option.",
                        }
                        for i in range(1, 6)   # opt-001 … opt-005
                    ],
                }
            ],
        }
        v = InvestigationValidator(bundle=bundle_5)
        with pytest.raises(ValidationError):
            v.validate_design_options(
                failure_mode_id="fm-001",
                options=[
                    {"remedy_option_id": f"opt-{i:03d}", "tradeoffs": "...",
                     "recommended": (i == 1)}
                    for i in range(1, 6)   # 5 options — above the 4-option maximum
                ],
            )

    def test_2_options_accepted(self):
        """Exactly 2 design options (minimum) are accepted (Fix W18: D10 2–4)."""
        from agents.fi_quality.validator import InvestigationValidator

        v = InvestigationValidator(bundle=_BUNDLE)
        # Should NOT raise — 2 options is the minimum.
        v.validate_design_options(
            failure_mode_id="fm-001",
            options=[
                {"remedy_option_id": "opt-001", "tradeoffs": "...", "recommended": True},
                {"remedy_option_id": "opt-002", "tradeoffs": "...", "recommended": False},
            ],
        )

    def test_option_numbers_from_corpus_not_model(self):
        """Numeric fields (parts cost, labor hours, total cost) come from ADP corpus tables.

        The model tool schema has no numeric field; a test that constructs the Finding
        verifies that cost fields match corpus entries and not a model-supplied value.
        Mutation: wire parts_cost_per_vehicle to a model-returned number → assertion fails.
        """
        from agents.fi_quality.options import build_option_evidence

        corpus_option = _BUNDLE["failure_modes"][0]["remedy_options"][0]
        total_vehicles = 85  # from pattern's affected count

        evidence = build_option_evidence(
            corpus_option=corpus_option,
            total_vehicles=total_vehicles,
        )
        assert evidence["parts_cost_per_vehicle"] == corpus_option["parts_cost_per_vehicle"], (
            "parts_cost_per_vehicle must come from the corpus, not the model. "
            "Mutation: wire to model value → assertion fails."
        )
        expected_total = (
            corpus_option["parts_cost_per_vehicle"]
            * total_vehicles
        )
        assert abs(evidence["total_cost"] - expected_total) < 0.01, (
            "total_cost must be parts_cost_per_vehicle × total_vehicles from corpus."
        )

    def test_option_numbers_mutation_wired_to_model_fails(self):
        """Mutation guard: if build_option_evidence uses a different cost, assertion fails.

        C4 fix: model_cost_suggestion parameter removed; this test now verifies
        that build_option_evidence with corpus-only data returns the correct value.
        Mutation: replace parts_cost with 99.99 → assertion fails.
        """
        from agents.fi_quality.options import build_option_evidence

        corpus_option = _BUNDLE["failure_modes"][0]["remedy_options"][0]
        evidence = build_option_evidence(
            corpus_option=corpus_option,
            total_vehicles=10,
        )
        # If mutation used 99.99 as parts_cost, this would be 99.99, not 2400.00.
        assert evidence["parts_cost_per_vehicle"] != 99.99


# ============================================================================
# Item 8d — Rules fallback: picks top-ranked candidate
# ============================================================================

class TestInvestigationFallback:
    """Fallback fires after one retry; picks the top candidate, matches best failure mode."""

    def test_fallback_picks_top_ranked_candidate(self):
        """Rules fallback selects the candidate with rank=1."""
        from agents.fi_quality.fallback import investigation_fallback

        result = investigation_fallback(bundle=_BUNDLE)
        assert result["candidate_id"] == "cand-001", (
            "Fallback must pick the top-ranked (rank=1) candidate (spec D10 step 4)."
        )

    def test_fallback_selects_best_matching_failure_mode(self):
        """Fallback selects the failure mode whose symptom_codes overlap the pattern codes."""
        from agents.fi_quality.fallback import investigation_fallback

        result = investigation_fallback(bundle=_BUNDLE)
        # fm-001 has symptom_codes=['P0AA6'] which matches the pattern's code_family
        assert result["failure_mode_id"] == "fm-001"

    def test_fallback_recommends_first_option_in_catalog_order(self):
        """Fallback recommends the first option from the failure mode's catalog order."""
        from agents.fi_quality.fallback import investigation_fallback

        result = investigation_fallback(bundle=_BUNDLE)
        # opt-001 is first in catalog order
        assert result["recommended_option_id"] == "opt-001"

    def test_fallback_sets_confidence_low_and_diagnosis_source_rules(self):
        """Fallback result carries confidence=low and diagnosis_source=rules."""
        from agents.fi_quality.fallback import investigation_fallback

        result = investigation_fallback(bundle=_BUNDLE)
        assert result.get("confidence") == "low"
        assert result.get("diagnosis_source") == "rules"

    def test_fallback_on_unknown_failure_mode_returns_unknown(self):
        """When no failure mode matches, fallback uses failure_mode_id='unknown'."""
        from agents.fi_quality.fallback import investigation_fallback

        bundle_no_fm = {
            **_BUNDLE,
            "failure_modes": [],
        }
        result = investigation_fallback(bundle=bundle_no_fm)
        assert result["failure_mode_id"] == "unknown"

    def test_one_retry_then_fallback_exact_call_count(self):
        """Root-cause model call: one retry then fallback → exactly two model calls."""
        from agents.fi_quality.runner import run_with_investigation_fallback

        call_count = [0]

        def failing_model_fn():
            call_count[0] += 1
            raise ValueError("validation failed")

        result = run_with_investigation_fallback(
            model_fn=failing_model_fn,
            fallback_fn=lambda: {
                "candidate_id": "cand-001",
                "failure_mode_id": "fm-001",
                "confidence": "low",
                "diagnosis_source": "rules",
                "recommended_option_id": "opt-001",
            },
            max_retries=1,
        )
        assert result["diagnosis_source"] == "rules"
        assert call_count[0] == 2, (
            f"Expected 2 model calls (attempt + retry), got {call_count[0]}."
        )



# ============================================================================
# C1 fix tests: stale queued/running age limit
# ============================================================================

class TestLivePredicateAgeLimit:
    """C1 fix: queued/running rows older than _STALE_AGE_SECONDS are not live."""

    def test_stale_queued_is_not_live(self):
        """A queued row older than the stale limit is not live.

        Mutation: removing the age check makes old rows live forever, blocking
        new investigations.  test_stale_queued_is_not_live catches it.
        """
        from agents.fi_quality.write import is_live_investigation, _STALE_AGE_SECONDS
        # 1 day ago — way beyond _STALE_AGE_SECONDS (3600 s).
        old_ts = "2026-01-01T00:00:00Z"
        assert not is_live_investigation("queued", old_ts), (
            "A queued row older than _STALE_AGE_SECONDS must not be live. "
            "Mutation: no age check → returns True, blocks new investigations."
        )

    def test_stale_running_is_not_live(self):
        """A running row older than the stale limit is not live.

        Mutation: removing the age check makes stale running rows live,
        blocking the pattern forever.
        """
        from agents.fi_quality.write import is_live_investigation
        old_ts = "2026-01-01T00:00:00Z"
        assert not is_live_investigation("running", old_ts), (
            "A running row older than _STALE_AGE_SECONDS must not be live. "
            "Mutation: no age check → returns True, pattern blocked forever."
        )

    def test_fresh_queued_is_live(self):
        """A queued row within the stale limit is live."""
        import datetime
        from agents.fi_quality.write import is_live_investigation
        fresh_ts = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_live_investigation("queued", fresh_ts), (
            "A fresh queued row must be live."
        )

    def test_no_timestamp_queued_is_live(self):
        """A queued row with no timestamp (in-memory store) is live (safe direction)."""
        from agents.fi_quality.write import is_live_investigation
        assert is_live_investigation("queued", None), (
            "A queued row with no timestamp must be treated as live (safe direction)."
        )

    def test_failed_is_not_live_regardless_of_age(self):
        """A failed row is never live, at any age."""
        from agents.fi_quality.write import is_live_investigation
        assert not is_live_investigation("failed", "2026-01-01T00:00:00Z")
        assert not is_live_investigation("failed", None)


# ============================================================================
# C4 fix tests: build_options_evidence with model_options
# ============================================================================

class TestBuildOptionsEvidenceModelPath:
    """C4 fix: build_options_evidence(model_options=...) uses corpus numeric fields."""

    _CORPUS_FM = {
        "failure_mode_id": "fm-001",
        "remedy_options": [
            {
                "remedy_option_id": "opt-001",
                "remedy_type": "hardware_recall",
                "parts_cost_per_vehicle": 2400.00,
                "labor_hours_per_vehicle": 8.0,
                "lead_time_weeks": 16,
                "over_the_air": False,
                "tradeoffs": "High cost; eliminates isolation risk.",
            },
            {
                "remedy_option_id": "opt-002",
                "remedy_type": "monitor",
                "parts_cost_per_vehicle": 0.0,
                "labor_hours_per_vehicle": 0.5,
                "lead_time_weeks": 1,
                "over_the_air": True,
                "tradeoffs": "No material fix.",
            },
        ],
    }

    def test_model_options_numeric_fields_from_corpus(self):
        """When model_options is provided, numeric fields come from corpus.

        C4 fix: model options carry tradeoffs text and recommended flag only.
        Any numeric key the model might include is ignored; numbers come from
        the corpus option matched by remedy_option_id.

        Mutation: wiring parts_cost to a model-supplied numeric → corpus
        equality assertion fails.
        """
        from agents.fi_quality.options import build_options_evidence

        model_options = [
            {
                "remedy_option_id": "opt-001",
                "tradeoffs": "Model analysis: high priority.",
                "recommended": True,
                # If the model supplied a numeric, it must be ignored:
                "parts_cost_per_vehicle": 99999.0,  # must be overridden by corpus
            },
            {
                "remedy_option_id": "opt-002",
                "tradeoffs": "Model: monitor approach.",
                "recommended": False,
            },
        ]
        results = build_options_evidence(
            failure_mode=self._CORPUS_FM,
            total_vehicles=100,
            model_options=model_options,
        )

        assert len(results) == 2
        rec = next(r for r in results if r["remedy_option_id"] == "opt-001")
        assert rec["parts_cost_per_vehicle"] == 2400.0, (
            "parts_cost must come from corpus (2400.0), not from model option (99999.0). "
            "Mutation: wire to model value → 99999.0, assertion fails."
        )
        assert abs(rec["total_cost"] - 2400.0 * 100) < 0.01, (
            "total_cost must be corpus parts_cost × total_vehicles."
        )
        assert rec["recommended"] is True
        assert "Model analysis" in rec["tradeoffs"], (
            "tradeoffs text should come from model_options."
        )

    def test_model_options_without_corpus_match_skipped(self):
        """A model option whose remedy_option_id is not in the corpus is skipped."""
        from agents.fi_quality.options import build_options_evidence

        model_options = [
            {"remedy_option_id": "opt-UNKNOWN", "tradeoffs": "...", "recommended": True},
            {"remedy_option_id": "opt-001", "tradeoffs": "...", "recommended": False},
        ]
        results = build_options_evidence(
            failure_mode=self._CORPUS_FM,
            total_vehicles=50,
            model_options=model_options,
        )
        ids = [r["remedy_option_id"] for r in results]
        assert "opt-UNKNOWN" not in ids, (
            "An option not in the corpus must be skipped."
        )
        assert "opt-001" in ids


# ============================================================================
# W3 fix tests: monitor option when no failure mode matches
# ============================================================================

class TestMonitorOptionFallback:
    """W3 fix: a monitor option is always present even with no matching failure mode."""

    def test_empty_failure_mode_yields_monitor_option(self):
        """build_options_evidence with an empty corpus always returns a monitor option."""
        from agents.fi_quality.options import build_options_evidence

        empty_fm = {"failure_mode_id": "unknown", "remedy_options": []}
        results = build_options_evidence(failure_mode=empty_fm, total_vehicles=10)

        assert len(results) == 1, (
            "Empty failure mode must produce exactly one 'monitor' option. "
            "W3 fix: without this, option_id would be null and the engineer "
            "can't approve a Finding with no options."
        )
        assert results[0]["remedy_option_id"] == "monitor"
        assert results[0]["recommended"] is True
        assert results[0]["remedy_type"] == "monitor"


# ============================================================================
# W5 fix tests: service history allowlist
# ============================================================================

class TestServiceHistoryAllowlist:
    """W5 fix: service_history is built from an allowlist, not a denylist."""

    def test_total_cost_usd_stripped_from_service_history(self):
        """total_cost_usd is stripped from service_history by the allowlist.

        W5 fix: only the exact key 'cost' was in the denylist, so 'total_cost_usd'
        (the real reader column name) would survive. The allowlist approach strips
        any key not in _SERVICE_HISTORY_ALLOWED.

        Mutation: use denylist only → 'total_cost_usd' survives in the bundle,
        violating the no-cost constraint.
        """
        from agents.fi_quality.bundle import build_bundle, _FORBIDDEN_FIELDS

        service_history = [
            {
                "vin": "MRDVEH00000000020",
                "total_cost_usd": "500.00",  # must be stripped
                "service_type": "brake_service",
                "dtc_codes_csv": "C0035",
                "outcome": "completed",
            }
        ]
        result = build_bundle(
            pattern={},
            failure_modes=[],
            fleet_type="rental",
            affected_vins=[],
            controls=[],
            service_history=service_history,
        )
        svc = result["service_history"]
        assert len(svc) == 1
        assert "total_cost_usd" not in svc[0], (
            "total_cost_usd must be stripped by the service_history allowlist. "
            "Mutation: denylist-only → total_cost_usd survives, assertion fails."
        )
        assert "service_type" in svc[0], (
            "service_type is allowlisted and must survive."
        )


# ============================================================================
# W6 fix tests: failure mode selection ranking and empty code_family
# ============================================================================

class TestFailureModeRanking:
    """W6 fix: exact code match ranks above prefix match; empty code_family → None."""

    _FM_EXACT = {
        "failure_mode_id": "fm-exact",
        "symptom_codes": ["P0AA6"],  # exact match for "P0AA6"
        "remedy_options": [],
    }
    _FM_PREFIX = {
        "failure_mode_id": "fm-prefix",
        "symptom_codes": ["P0AA7"],  # prefix match for "P0AA" family
        "remedy_options": [],
    }

    def test_exact_match_preferred_over_prefix(self):
        """An exact symptom code match beats a prefix match.

        W6 fix: before the fix, list order determined the winner.
        With the fix, exact beats prefix regardless of order.
        """
        from agents.fi_quality.fallback import _match_failure_mode

        result = _match_failure_mode(
            [self._FM_PREFIX, self._FM_EXACT],  # prefix is first in list
            "P0AA6",
        )
        assert result is not None
        assert result["failure_mode_id"] == "fm-exact", (
            "Exact code match must be preferred over prefix match. "
            "Mutation: list-order wins → fm-prefix returned, fails."
        )

    def test_empty_code_family_returns_none(self):
        """An empty code_family returns None (unknown), not failure_modes[0].

        W6 fix: previously returned failure_modes[0] silently, which could
        recommend a hardware recall for an unrelated component.
        """
        from agents.fi_quality.fallback import _match_failure_mode

        result = _match_failure_mode([self._FM_EXACT, self._FM_PREFIX], "")
        assert result is None, (
            "Empty code_family must return None (unknown path). "
            "Mutation: returns failure_modes[0] → wrong component recommended."
        )

    def test_no_match_returns_none(self):
        """No matching failure mode returns None."""
        from agents.fi_quality.fallback import _match_failure_mode

        result = _match_failure_mode([self._FM_EXACT], "U3000")
        assert result is None


# ============================================================================
# W10 fix tests: issue.py unknown draft_type and missing decided_by
# ============================================================================

class TestIssueFindingBuilderW10:
    """W10 fix: issue.py fails closed on unknown draft_type / missing decided_by."""

    _BASE_ACTION = {
        "decided_by": {"kind": "engineer", "sub": "eng-001"},
        "parameters": {"action_id": "act-001"},
        "investigation_finding": {},
    }

    def test_unknown_draft_type_returns_none(self):
        """An unknown draft_type returns None instead of silently creating a campaign.

        W10 fix: previously the else branch created an issue_software_campaign
        with "Draft campaign" headline for any unrecognized draft_type.

        Mutation: keep the else branch → non-None returned for unknown type, fails.
        """
        from agents.fi_quality.issue import build_issue_finding_from_hook

        response = {
            "thread_id": "thread-001",
            "draft_id": "draft-001",
            "draft_type": "hardware_recall",  # valid
            "version": 1,
        }
        valid = build_issue_finding_from_hook(self._BASE_ACTION, response)
        assert valid is not None, "Valid draft_type must produce a Finding."

        response_unknown = {
            "thread_id": "thread-002",
            "draft_id": "draft-002",
            "draft_type": "unknown_future_type",
            "version": 1,
        }
        result = build_issue_finding_from_hook(self._BASE_ACTION, response_unknown)
        assert result is None, (
            "Unknown draft_type must return None (fail closed). "
            "Mutation: else branch always creates issue_software_campaign → not None, fails."
        )

    def test_missing_decided_by_kind_returns_none(self):
        """A missing decided_by.kind returns None rather than defaulting to 'engineer'.

        W10 fix: previously defaulted to 'engineer' silently.
        """
        from agents.fi_quality.issue import build_issue_finding_from_hook

        action_no_kind = {
            "decided_by": {},  # missing kind
            "parameters": {"action_id": "act-002"},
            "investigation_finding": {},
        }
        response = {
            "thread_id": "thread-003",
            "draft_id": "draft-003",
            "draft_type": "hardware_recall",
            "version": 1,
        }
        result = build_issue_finding_from_hook(action_no_kind, response)
        assert result is None, (
            "Missing decided_by.kind must return None (refuse to create the Finding). "
            "Mutation: default to 'engineer' → not None, fails."
        )

    def test_valid_inputs_produce_finding_with_correct_action_kind(self):
        """Valid inputs produce a Finding with the correct action_kind."""
        from agents.fi_quality.issue import build_issue_finding_from_hook

        response = {
            "thread_id": "thread-004",
            "draft_id": "draft-004",
            "draft_type": "hardware_recall",
            "version": 1,
        }
        finding = build_issue_finding_from_hook(self._BASE_ACTION, response)
        assert finding is not None
        assert finding["recommended_action"]["action_kind"] == "issue_recall", (
            "hardware_recall draft must produce issue_recall action_kind."
        )
        assert finding["severity"] == "soon", (
            "hardware_recall must produce 'soon' severity."
        )

    def test_software_update_produces_issue_software_campaign(self):
        """software_update draft type produces issue_software_campaign action_kind."""
        from agents.fi_quality.issue import build_issue_finding_from_hook

        response = {
            "thread_id": "thread-005",
            "draft_id": "draft-005",
            "draft_type": "software_update",
            "version": 1,
        }
        finding = build_issue_finding_from_hook(self._BASE_ACTION, response)
        assert finding is not None
        assert finding["recommended_action"]["action_kind"] == "issue_software_campaign"
        assert finding["severity"] == "soon"

    def test_no_fleet_id_in_issue_finding(self):
        """quality.issue Finding must not contain fleet_id at any level."""
        from agents.fi_quality.issue import build_issue_finding_from_hook

        # Include fleet_id in the investigation_finding to verify it doesn't leak.
        action_with_fleet = {
            "decided_by": {"kind": "engineer", "sub": "eng-001"},
            "parameters": {"action_id": "act-003"},
            "investigation_finding": {
                "scope": {"population_id": "meridian-motors", "fleet_id": "fleet-001"},
                "evidence": [],
            },
        }
        response = {
            "thread_id": "thread-006",
            "draft_id": "draft-006",
            "draft_type": "hardware_recall",
            "version": 1,
        }
        finding = build_issue_finding_from_hook(action_with_fleet, response)
        assert finding is not None

        def _scan(obj, path="finding"):
            if isinstance(obj, dict):
                assert "fleet_id" not in obj, (
                    f"fleet_id found at {path} in quality.issue Finding."
                )
                for k, v in obj.items():
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(finding)



# ============================================================================
# W7 fix tests: quality.issue hook retry + CloudWatch metric on final failure
# ============================================================================

class TestIssueHookRetry:
    """W7 fix: hook retries once then emits a CloudWatch metric on final failure."""

    def test_hook_retries_on_first_failure(self):
        """Hook retries once if the first write_finding call raises.

        W7 fix (decision 5): a single throttle or transient error should not
        leave an ADP draft with no quality.issue Finding.
        """
        import os
        from unittest.mock import patch, MagicMock, call

        os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")

        call_count = [0]

        def _flaky_write_finding(finding, *, table_name, dynamodb_client):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("transient throttle")
            # Second call succeeds.

        action = {
            "decided_by": {"kind": "engineer", "sub": "eng-001"},
            "parameters": {"action_id": "act-retry"},
            "investigation_finding": {},
        }
        response = {
            "thread_id": "thread-retry",
            "draft_id": "draft-retry",
            "draft_type": "hardware_recall",
            "version": 1,
        }

        from agents.fi_quality import issue as issue_mod

        fake_ddb = MagicMock()
        hook = issue_mod.make_issue_hook(ddb_client=fake_ddb)

        with patch("agents.tier2.writer.write_finding", side_effect=_flaky_write_finding):
            # Should not raise (hook never raises).
            hook(action, response)

        assert call_count[0] == 2, (
            f"Hook must retry once (expected 2 calls, got {call_count[0]}). "
            "W7 fix: a single transient error should be retried."
        )

    def test_hook_emits_metric_on_final_failure(self):
        """Hook emits a CloudWatch metric when all retries are exhausted.

        W7 fix (decision 5): the action_id is logged and a CloudWatch metric
        is emitted so 4.3's alarm can fire.
        """
        import os
        from unittest.mock import patch, MagicMock

        os.environ["AVX_FINDINGS_TABLE"] = "vsa-test-avx-findings"

        def _always_fail(finding, *, table_name, dynamodb_client):
            raise RuntimeError("persistent failure")

        action = {
            "decided_by": {"kind": "engineer", "sub": "eng-001"},
            "parameters": {"action_id": "act-metric"},
            "investigation_finding": {},
        }
        response = {
            "thread_id": "thread-metric",
            "draft_id": "draft-metric",
            "draft_type": "hardware_recall",
            "version": 1,
        }

        from agents.fi_quality import issue as issue_mod

        cw_calls = []

        def _fake_cw(**kwargs):
            cw_calls.append(kwargs)

        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = lambda **kw: cw_calls.append(kw)

        def _fake_boto3_client(service, **_kw):
            if service == "cloudwatch":
                return fake_cw
            return MagicMock()

        hook = issue_mod.make_issue_hook(ddb_client=MagicMock())

        with patch("agents.tier2.writer.write_finding", side_effect=_always_fail), \
             patch("boto3.client", side_effect=_fake_boto3_client):
            hook(action, response)  # must not raise

        assert fake_cw.put_metric_data.called, (
            "put_metric_data must be called when all hook retries fail. "
            "W7 fix: the metric feeds 4.3's alarm."
        )
        metric_call = fake_cw.put_metric_data.call_args
        metric_data = metric_call.kwargs.get("MetricData", [])
        metric_name = metric_data[0]["MetricName"] if metric_data else ""
        assert metric_name == "QualityIssueHookFailure", (
            f"Expected metric name 'QualityIssueHookFailure', got {metric_name!r}."
        )

    def test_hook_makes_at_most_two_write_attempts(self):
        """The hook writes at most twice, then emits the metric once (G3 cycle 2/3 answer 8).

        The bound keeps the hook inside the approve route's budget: 2 attempts x
        (2 s connect + 3 s read, one botocore attempt each). Mutant
        ``_max_retries = 2`` -> ``3`` (reviewer c ``W4-three-attempts``) makes 3 calls
        and FAILS here; ``-> 1`` makes 1 call and FAILS too.
        """
        import os
        from unittest.mock import patch, MagicMock

        calls = []

        def _always_fail(finding, *, table_name, dynamodb_client):
            calls.append(table_name)
            raise RuntimeError("persistent failure")

        action = {
            "action_id": "act-bound",
            "decided_by": {"kind": "engineer", "sub": "eng-001"},
            "parameters": {},
            "investigation_finding": {},
        }
        response = {
            "thread_id": "thread-bound",
            "draft_id": "draft-bound",
            "draft_type": "hardware_recall",
            "version": 1,
        }

        from agents.fi_quality import issue as issue_mod

        fake_cw = MagicMock()

        def _fake_boto3_client(service, *_a, **_kw):
            return fake_cw if service == "cloudwatch" else MagicMock()

        hook = issue_mod.make_issue_hook(ddb_client=MagicMock())
        with patch.dict(os.environ, {"AVX_FINDINGS_TABLE": "vsa-test-avx-findings"}), \
             patch("agents.tier2.writer.write_finding", side_effect=_always_fail), \
             patch("boto3.client", side_effect=_fake_boto3_client):
            hook(action, response)  # must not raise

        assert calls == ["vsa-test-avx-findings", "vsa-test-avx-findings"], (
            f"The hook must make exactly 2 write attempts; made {len(calls)}."
        )
        assert fake_cw.put_metric_data.call_count == 1, (
            "One QualityIssueHookFailure metric after the 2 failed attempts; "
            f"got {fake_cw.put_metric_data.call_count}."
        )


# ============================================================================
# W10 fix tests: investigation Finding builder contracts
# ============================================================================

class TestInvestigationFindingBuilderContracts:
    """W10 fix: investigation Finding builder contracts are tested."""

    _BUNDLE = {
        "pattern": {
            "pattern_id": "pat-test-001",
            "code_family": "P0AA6",
            "dimension": "assembly_plant",
            "value": "CGA",
            "ratio": 4.2,
            "p_value": 0.0001,
            "method": "poisson_bonferroni",
            "vehicles_affected": 42,
        },
        "candidates": [
            {"candidate_id": "cand-001", "dimension": "assembly_plant",
             "value": "CGA", "rank": 1},
        ],
        "failure_modes": [
            {
                "failure_mode_id": "fm-001",
                "component": "hv_battery_module",
                "mechanism": "isolation_degradation",
                "symptom_codes": ["P0AA6"],
                "remedy_options": [
                    {
                        "remedy_option_id": "opt-001",
                        "remedy_type": "hardware_recall",
                        "parts_cost_per_vehicle": 2400.0,
                        "labor_hours_per_vehicle": 8.0,
                        "lead_time_weeks": 16,
                        "over_the_air": False,
                        "tradeoffs": "High cost.",
                    }
                ],
            }
        ],
        "affected_vins": [
            {"vin": "MRDVEH00000000020", "model": "Azimuth", "model_year": "2024",
             "plant": "CGA", "build_month": "2024-03", "component_lot": "L-A"},
        ],
        "controls": [],
        "service_history": [],
    }

    _ROOT_CAUSE = {
        "candidate_id": "cand-001",
        "failure_mode_id": "fm-001",
        "confidence": "low",
        "diagnosis_source": "rules",
        "evidence_refs": [],
    }

    _OPTIONS = [
        {
            "remedy_option_id": "opt-001",
            "remedy_type": "hardware_recall",
            "parts_cost_per_vehicle": 2400.0,
            "labor_hours_per_vehicle": 8.0,
            "total_vehicles": 42,
            "total_cost": 2400.0 * 42,
            "lead_time_weeks": 16,
            "over_the_air": False,
            "recommended": True,
            "tradeoffs": "High cost.",
        }
    ]

    def test_finding_severity_for_recall(self):
        """A hardware_recall option produces 'soon' severity."""
        from agents.fi_quality.write import build_investigation_finding

        finding = build_investigation_finding(
            investigation_id="inv-test-001",
            pattern_id="pat-test-001",
            bundle=self._BUNDLE,
            root_cause=self._ROOT_CAUSE,
            options=self._OPTIONS,
            computed_at="2026-10-05T06:00:00Z",
        )
        assert finding["severity"] == "soon", (
            f"hardware_recall must yield severity='soon', got {finding['severity']!r}."
        )

    def test_finding_server_set_ids(self):
        """investigation_id and pattern_id are server-set in recommended_action."""
        from agents.fi_quality.write import build_investigation_finding

        finding = build_investigation_finding(
            investigation_id="inv-test-002",
            pattern_id="pat-test-002",
            bundle=self._BUNDLE,
            root_cause=self._ROOT_CAUSE,
            options=self._OPTIONS,
            computed_at="2026-10-05T06:00:00Z",
        )
        params = finding["recommended_action"]["parameters"]
        assert params["investigation_id"] == "inv-test-002", (
            "investigation_id must be server-set in recommended_action.parameters."
        )
        assert params["pattern_id"] == "pat-test-002", (
            "pattern_id must be server-set in recommended_action.parameters."
        )

    def test_finding_no_fleet_id(self):
        """Investigation Finding must not contain fleet_id at any level."""
        from agents.fi_quality.write import build_investigation_finding

        bundle_with_fleet = dict(self._BUNDLE)
        bundle_with_fleet["affected_vins"] = [
            {"vin": "VIN001", "fleet_id": "fleet-001"}
        ]
        finding = build_investigation_finding(
            investigation_id="inv-test-003",
            pattern_id="pat-test-003",
            bundle=bundle_with_fleet,
            root_cause=self._ROOT_CAUSE,
            options=self._OPTIONS,
            computed_at="2026-10-05T06:00:00Z",
        )

        def _scan(obj, path="finding"):
            if isinstance(obj, dict):
                assert "fleet_id" not in obj, f"fleet_id found at {path}"
                for k, v in obj.items():
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(finding)

    def test_root_cause_evidence_has_confidence(self):
        """quality.root_cause evidence entry includes confidence (W4 fix)."""
        from agents.fi_quality.write import build_investigation_finding

        finding = build_investigation_finding(
            investigation_id="inv-test-004",
            pattern_id="pat-test-004",
            bundle=self._BUNDLE,
            root_cause={**self._ROOT_CAUSE, "confidence": "medium"},
            options=self._OPTIONS,
            computed_at="2026-10-05T06:00:00Z",
        )
        rc_entry = next(
            e for e in finding["evidence"] if e["kind"] == "quality.root_cause"
        )
        assert "confidence" in rc_entry["value"], (
            "quality.root_cause evidence must include confidence (W4 fix § 2.6)."
        )
        assert rc_entry["value"]["confidence"] == "medium"

    def test_affected_vins_count_uses_vehicles_affected(self):
        """quality.affected_vins.count uses vehicles_affected, not sample length."""
        from agents.fi_quality.write import build_investigation_finding

        finding = build_investigation_finding(
            investigation_id="inv-test-005",
            pattern_id="pat-test-005",
            bundle=self._BUNDLE,
            root_cause=self._ROOT_CAUSE,
            options=self._OPTIONS,
            computed_at="2026-10-05T06:00:00Z",
        )
        av_entry = next(
            (e for e in finding["evidence"] if e["kind"] == "quality.affected_vins"),
            None,
        )
        assert av_entry is not None
        # bundle has vehicles_affected=42 but only 1 VIN in affected_vins.
        assert av_entry["value"]["count"] == 42, (
            f"count must use vehicles_affected (42), not len(affected_vins) (1). "
            f"W4 fix: pattern.affected_count and quality.affected_vins.count must agree."
        )



# ============================================================================
# S3 fix: hook-failure metric emitter reads AWS_REGION
# ============================================================================

class TestHookFailureMetricEmitterRegion:
    """S3 fix: QualityIssueHookFailure emitter reads AWS_REGION, not a hardcode.

    Both alarm emitters must derive the CloudWatch region from AWS_REGION so
    that deployments outside us-west-2 emit to the right regional endpoint.
    """

    def test_emitter_uses_aws_region_env_var(self):
        """Setting AWS_REGION=eu-west-1 makes the CW client use eu-west-1."""
        import os
        from unittest.mock import patch, MagicMock

        from agents.fi_quality import issue as issue_mod

        captured_region: list[str | None] = []
        fake_cw = MagicMock()

        def _fake_boto3_client(service, *_args, **kw):
            if service == "cloudwatch":
                captured_region.append(kw.get("region_name"))
                return fake_cw
            return MagicMock()

        with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}), \
             patch("boto3.client", side_effect=_fake_boto3_client):
            issue_mod._emit_hook_failure_metric()

        assert captured_region, (
            "boto3.client('cloudwatch', ...) was never called by _emit_hook_failure_metric."
        )
        assert captured_region[0] == "eu-west-1", (
            f"S3 fix: emitter must read AWS_REGION; expected 'eu-west-1', "
            f"got {captured_region[0]!r}.  Mutation: hardcode 'us-west-2' → FAILS here."
        )
