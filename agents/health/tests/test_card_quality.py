"""Red tests for Finding card content (spec 2026-09-25-avx-own-vehicle-findings, T1.3 / T4.3).

Seen on the iOS Alerts card 2026-09-25: a routine brakes Finding showed the same sentence as
headline and detail, and stated a 2018 service date on a 2022 vehicle as fact.

The two red tests are xfail(strict=True) until T4.3 lands; strict mode makes T4.3 delete
the marker. The control test is not marked: it must pass before and after.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_RED = pytest.mark.xfail(strict=True, raises=AssertionError,
                         reason="red until T4.3 fixes card content")

_ROUTINE = ("Most recent brake service (2025-01-01) completed successfully with outcome "
            "'completed'. No open issues.")


def _fields(text):
    return {"finding": text, "severity_hint": "routine", "confidence": "high",
            "evidence": [{"source": "adp.brakes.outcome", "value": "completed",
                          "provenance": "live"}]}


def test_headline_is_not_a_copy_of_the_detail():
    from agents.tier2.runner import _build_artifact
    artifact, _ = _build_artifact("MRDN0000000000013", "brakes", [{"service_date": "2025-01-01"}],
                                  _fields(_ROUTINE))
    assert artifact["headline"] and artifact["detail"]
    assert artifact["headline"] != artifact["detail"]
    assert _ROUTINE in artifact["detail"]  # nothing the agent said is dropped


def test_service_date_before_model_year_is_flagged_not_stated_as_fact():
    from agents.health.agent import _build_brake_finding
    out = _build_brake_finding({
        "service_date": "2018-03-01", "model_year": 2022, "outcome": "completed",
        "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
        "total_cost_usd": None,
    })
    assert out["confidence"] != "high"
    assert "2018" in out["finding"] and "2022" in out["finding"]


def test_service_date_after_model_year_is_unchanged():
    from agents.health.agent import _build_brake_finding
    out = _build_brake_finding({
        "service_date": "2025-01-01", "model_year": 2022, "outcome": "completed",
        "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
        "total_cost_usd": None,
    })
    assert out["confidence"] == "high"



# ── The headline rule (agents/tier2/card_text.py), architect fix to the first T4.3 cut ──
#
# The first cut forced headline != detail on every text by stripping a single sentence's
# period or dropping its last word ("Brake pads at 3 mm" -> "Brake pads at 3"), and it
# split a two-sentence text even when the second sentence carried the warranty verdict or
# the C0161 directive. These pin the corrected rule.


def test_single_short_sentence_is_kept_whole():
    from agents.tier2.card_text import derive_headline
    assert derive_headline("Brake pads at 3 mm") == "Brake pads at 3 mm"
    assert derive_headline("Your battery is healthy.") == "Your battery is healthy."


def test_long_text_keeps_the_legacy_slice():
    from agents.tier2.card_text import derive_headline
    text = "A" * 130
    assert derive_headline(text) == text[:120]


def test_safety_directive_text_is_never_split():
    from agents.health.prompt import C0161_VERBATIM_REMEDY
    from agents.tier2.card_text import derive_headline
    text = f"{C0161_VERBATIM_REMEDY} Brake job open."
    assert len(text) <= 120
    assert C0161_VERBATIM_REMEDY in derive_headline(text)


def test_required_phrase_picks_the_sentence_that_carries_it():
    from agents.tier2.card_text import derive_headline
    text = "Brake service is complete. The warranty claim was denied by the dealer."
    assert derive_headline(text) == "Brake service is complete."
    assert derive_headline(text, ("denied",)) == "The warranty claim was denied by the dealer."


def test_runner_writes_the_agent_supplied_headline():
    from agents.tier2.runner import _build_artifact
    fields = {**_fields(_ROUTINE), "headline": "Brakes checked"}
    artifact, _ = _build_artifact("MRDN0000000000013", "brakes", [{"service_date": "2025-01-01"}], fields)
    assert artifact["headline"] == "Brakes checked"
    assert artifact["detail"] == _ROUTINE


def test_verdict_in_second_sentence_reaches_the_written_headline():
    """End to end: agent guard -> _extract_single_component -> runner._build_artifact."""
    import json as _json

    from agents.health.agent import _extract_single_component, assess_vehicle_health
    from agents.tier2.runner import _build_artifact

    def _llm(system_prompt, user_prompt):
        return _json.dumps({"brakes": {
            "finding": "Brake service is complete. The warranty claim was denied by the dealer.",
            "severity_hint": "routine", "confidence": "high"}})

    brake = {"service_date": "2025-01-01", "outcome": "resolved", "is_open": False,
             "dtc_codes": [], "complaint_text": "", "warranty_covered": "true",
             "total_cost_usd": None}
    result = assess_vehicle_health("VIN-T43", brake_data=brake, warranty_verdict_phrase="denied",
                                   _llm_caller=_llm)
    assert not result["brakes"].get("narration_degraded", False)
    fields = _extract_single_component(result, "brakes")
    artifact, _ = _build_artifact("VIN-T43", "brakes", [brake], fields)
    assert "denied" in artifact["headline"]
    assert "denied" in artifact["detail"]
    assert artifact["headline"] != artifact["detail"]



# ── C1: deterministic path (no LLM) with verdict — headline/detail must carry it ──
#
# Production calls assess_vehicle_health with no _llm_caller, so narration is
# deterministic and never contains the verdict phrase. The guard ALWAYS fires on
# the rewrite branch. After c377f7a the runner writes the agent-supplied headline
# as-is. Without a test on this path, M10 (replace headline with derive_headline
# sans required_phrases) survives the full suite.
#
# Test coverage: short verdict text (≤120) and long verdict text (>120).
# Both go through assess_vehicle_health → _extract_single_component → _build_artifact.
# Both assert verdict in artifact["headline"] AND artifact["detail"].


def _brake_resolved(service_date="2025-01-01"):
    return {
        "service_date": service_date, "outcome": "resolved", "is_open": False,
        "dtc_codes": [], "complaint_text": "", "warranty_covered": "true",
        "total_cost_usd": None,
    }


def test_c1_verdict_in_headline_short_text_deterministic_path():
    """C1: deterministic (no LLM) path, short text ≤120 — verdict in written headline.

    The routine brake text is ~84 characters. With verdict prepended the rewritten
    text is short enough for the headline to take a sentence-splitting path. The key
    property: artifact["headline"] MUST contain the verdict phrase.

    Mutation M10 (replace rewrite headline with derive_headline(finding_text)):
    drops verdict from headline on this path — caught by this test.
    """
    from agents.health.agent import _extract_single_component, assess_vehicle_health
    from agents.tier2.runner import _build_artifact

    brake = _brake_resolved()
    result = assess_vehicle_health(
        "VIN-C1-SHORT", brake_data=brake, warranty_verdict_phrase="denied",
        _llm_caller=None,
    )
    # Guard must fire on deterministic path (verdict absent from deterministic text).
    assert result["brakes"].get("narration_degraded") is True
    fields = _extract_single_component(result, "brakes")
    artifact, _ = _build_artifact("VIN-C1-SHORT", "brakes", [brake], fields)
    assert "denied" in artifact["headline"], (
        "Verdict 'denied' must be in the written headline on the deterministic "
        "(no-LLM) rewrite path. M10 mutation survival = this assertion fails."
    )
    assert "denied" in artifact["detail"]


def test_c1_verdict_in_headline_long_text_deterministic_path():
    """C1: deterministic (no LLM) path, text >120 chars after verdict prepend.

    When the rewritten text exceeds 120 characters the headline follows a
    truncation rule. The verdict phrase is in the first sentence ("denied"),
    but plain truncation of the bare verdict token (W1 defect) would produce
    the single word "denied" as the headline. The W1 fix joins it with the
    next substantive sentence. In all cases "denied" must appear in the headline.

    Mutation M10 (replace rewrite headline with derive_headline(finding_text)):
    drops verdict from headline — caught by this test.
    """
    from agents.health.agent import _extract_single_component, assess_vehicle_health
    from agents.tier2.runner import _build_artifact

    # dtc_codes triggers the DTC note path which produces a long finding text
    # (outcome section + DTC note exceeds 120 chars after verdict prepend).
    brake = {
        "service_date": "2025-01-01", "outcome": "resolved", "is_open": False,
        "dtc_codes": ["C0040"], "complaint_text": "Brake noise",
        "warranty_covered": "true", "total_cost_usd": "350.00",
    }
    result = assess_vehicle_health(
        "VIN-C1-LONG", brake_data=brake, warranty_verdict_phrase="denied",
        _llm_caller=None,
    )
    # Guard fires: deterministic text does not contain "denied".
    assert result["brakes"].get("narration_degraded") is True
    fields = _extract_single_component(result, "brakes")
    artifact, _ = _build_artifact("VIN-C1-LONG", "brakes", [brake], fields)
    assert "denied" in artifact["headline"], (
        "Verdict 'denied' must be in the written headline even when the rewritten "
        "text exceeds 120 characters."
    )
    assert "denied" in artifact["detail"]


# ── C2: production-shaped active C0161 finding — 'Pull over immediately' in headline ──
#
# Every production active-C0161 finding is longer than 120 characters (shortest ~173):
# inject_p0_verbatim_phrases prepends the remedy phrase + "\n\n" to a text that already
# carries the phrase in its DTC note, and T4.14 keeps active P0 brakes away from the LLM.
# So production always takes the rule-2 (long-text) slice.
#
# M1 mutation: rule 2 → first sentence only → drops "Pull over immediately" from headline.
# This test catches M1 before touching rule 2.


_BRAKE_P0_ACTIVE = {
    "service_date": "2025-06-01", "outcome": "parts_pending", "is_open": True,
    "dtc_codes": ["C0161"], "complaint_text": "Brake warning",
    "warranty_covered": "false", "total_cost_usd": None,
}


def test_c2_pull_over_immediately_in_headline_production_shaped():
    """C2: production-shaped active C0161 (>120 chars) — directive in written headline.

    This goes through _build_brake_finding (which produces the DTC note that pushes
    the text over 120 chars) → assess_vehicle_health → _build_artifact.

    Mutation M1 (rule 2 → first sentence only): drops "Pull over immediately" from
    the headline — caught by this test.
    """
    from agents.health.prompt import C0161_VERBATIM_REMEDY
    from agents.health.agent import _extract_single_component, assess_vehicle_health
    from agents.tier2.runner import _build_artifact

    result = assess_vehicle_health(
        "VIN-C2-P0", brake_data=_BRAKE_P0_ACTIVE, _llm_caller=None,
    )
    # T4.14 structural bypass: no LLM narration, deterministic text preserved.
    assert not result["brakes"].get("narration_degraded", False)
    finding_text = result["brakes"]["finding"]
    # The full finding text must be longer than 120 chars (production shape).
    assert len(finding_text) > 120, (
        f"Production-shaped C0161 finding must exceed 120 chars; got {len(finding_text)}"
    )
    fields = _extract_single_component(result, "brakes")
    artifact, _ = _build_artifact("VIN-C2-P0", "brakes", [_BRAKE_P0_ACTIVE], fields)
    assert C0161_VERBATIM_REMEDY in artifact["headline"], (
        "C0161_VERBATIM_REMEDY must appear in the written headline of an active "
        "C0161 Finding. M1 mutation (first sentence only) drops 'Pull over "
        "immediately' from the headline — this test catches it."
    )


def test_c2_pull_over_immediately_in_headline_with_verdict():
    """C2 + verdict combination: active C0161 + warranty verdict — directive still in headline."""
    from agents.health.prompt import C0161_VERBATIM_REMEDY
    from agents.health.agent import _extract_single_component, assess_vehicle_health
    from agents.tier2.runner import _build_artifact

    result = assess_vehicle_health(
        "VIN-C2-P0-VERDICT", brake_data=_BRAKE_P0_ACTIVE,
        warranty_verdict_phrase="denied", _llm_caller=None,
    )
    fields = _extract_single_component(result, "brakes")
    artifact, _ = _build_artifact("VIN-C2-P0-VERDICT", "brakes", [_BRAKE_P0_ACTIVE], fields)
    assert C0161_VERBATIM_REMEDY in artifact["headline"], (
        "C0161_VERBATIM_REMEDY must be in the headline even when a warranty verdict "
        "is also present."
    )


# ── W1: verdict-rewrite branch — bare verdict token is not the sole headline ──

def test_w1_verdict_rewrite_headline_is_not_bare_phrase():
    """W1: on the rewrite branch, the headline must not be the bare verdict token.

    Before the W1 fix, a short text ≤120 chars with verdict prepended yielded
    headline='denied' because _SENTENCE_END splits on newlines and the verdict
    token is its own 'sentence'. The fix joins the bare phrase with the next
    substantive sentence.

    Mutation: comment out the W1 join clause → headline becomes bare "denied" →
    this test fails.
    """
    from agents.tier2.card_text import derive_headline

    # Simulate the rewrite branch: verdict prepended, short enough text.
    rewritten = "denied\n\nBrake service from 2025-01-01 is awaiting parts."
    headline = derive_headline(rewritten, ("denied",))
    assert headline != "denied", (
        "Headline must not be the bare verdict token. "
        "Expected the verdict joined with the next substantive sentence."
    )
    assert "denied" in headline
    # The substantive sentence must also be present.
    assert "Brake service" in headline or "awaiting parts" in headline


# ── W2: long text headline truncates at sentence/word boundary, not mid-word ──

def test_w2_long_text_truncates_at_word_boundary():
    """W2: headline for long text must not end mid-word or mid-sentence.

    The date-note case produces a text >120 chars. Before W2 fix the headline
    ended with a partial word fragment. The fix truncates at a sentence or word
    boundary.

    Mutation: revert to text[:120] → headline ends mid-word → this test fails
    (last char is not a space and is not the end of a word).
    """
    from agents.tier2.card_text import derive_headline

    # Simulate routine brakes + date note (total >120 chars):
    text = (
        "Note: this service record is dated 2018, but the vehicle is a 2022 model "
        "— the date may be inaccurate. "
        "Most recent brake service (2018-03-01) completed successfully with outcome "
        "'completed'. No open issues."
    )
    assert len(text) > 120
    headline = derive_headline(text)
    assert len(headline) <= 120, f"Headline must be ≤120 chars; got {len(headline)}"
    # Must not end mid-word (last char should be a word char only if the whole word fits).
    # The simplest guard: the character after the headline in the original text is
    # either a sentence boundary, a space, or does not exist.
    end_idx = len(headline)
    if end_idx < len(text):
        assert text[end_idx] in (" ", ".", "!", "?", "\n"), (
            f"Headline ends mid-word at index {end_idx}: "
            f"'…{text[end_idx - 5:end_idx + 5]}'"
        )


# ── W3: date note on active C0161 — caveat sentence comes first ──

def test_w3_date_note_on_active_c0161_caveat_comes_first():
    """W3: on an active C0161 job with a suspect date, the date caveat must NOT
    appear within the C0161 directive's 200-character deny-list window.

    Before the W3 fix, the date note was appended inline AFTER the finding text,
    placing it immediately after 'Pull over immediately.' in the detail.
    The fix places the note as a trailing standalone paragraph ("\n\n"-separated)
    or skips it on an active P0 job (safety seam decision).

    The test verifies the decision recorded in decisions.md: on an active C0161
    job, the confidence must remain 'high' (safety seam takes precedence over the
    date data-quality concern) and the note must NOT appear adjacent to the directive.

    Mutation: revert to inline trailing note → confidence becomes 'medium' and the
    note appears inside the directive window → this test fails.
    """
    from agents.health.agent import _build_brake_finding
    from agents.health.prompt import C0161_VERBATIM_REMEDY

    brake = {
        "service_date": "2018-06-01", "model_year": 2022, "outcome": "parts_pending",
        "is_open": True, "dtc_codes": ["C0161"], "complaint_text": "",
        "warranty_covered": "false", "total_cost_usd": None,
    }
    out = _build_brake_finding(brake)
    finding = out["finding"]

    # C0161_VERBATIM_REMEDY must be present (active brake job).
    directive_idx = finding.find("Pull over immediately")
    assert directive_idx != -1, "C0161_VERBATIM_REMEDY must be present for active brake job"

    # On an active P0 brake job the safety seam takes precedence:
    # confidence stays 'high' (no caveat lowering) and the note does not appear.
    assert out["confidence"] == "high", (
        "Active P0 brake job: date caveat must NOT lower confidence to 'medium'. "
        "Safety seam decision: the stop-driving directive takes precedence over "
        "data-quality concerns about the service date."
    )
    assert "inaccurate" not in finding, (
        "Active P0 brake job: date caveat must NOT appear in the finding text. "
        "Placing a hedge near 'Pull over immediately' softens the directive."
    )


# ── S3: model_year - 1 boundary test ──

def test_s3_service_year_equal_to_model_year_not_flagged():
    """S3: service_year == model_year must NOT trigger the date caveat.

    A model-year 2022 vehicle serviced in 2022 is the exact boundary case.
    Only service_year < model_year - 1 should flag.

    Mutation M8 (< → <=): flags model_year == service_year → this test fails.
    """
    from agents.health.agent import _build_brake_finding

    out = _build_brake_finding({
        "service_date": "2022-11-15", "model_year": 2022, "outcome": "completed",
        "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
        "total_cost_usd": None,
    })
    assert out["confidence"] == "high", (
        "service_year == model_year must NOT trigger the date caveat "
        "(boundary: only service_year < model_year - 1 flags)."
    )
    assert "inaccurate" not in out["finding"]


def test_s3_service_year_is_model_year_minus_one_not_flagged():
    """S3: service_year == model_year - 1 must NOT trigger the caveat.

    Model years go on sale the prior calendar year; servicing a 2022 vehicle
    in late 2021 is a normal pre-delivery or PDI scenario.

    Mutation M8 (< → <=): flags model_year - 1 → this test fails.
    """
    from agents.health.agent import _build_brake_finding

    out = _build_brake_finding({
        "service_date": "2021-11-01", "model_year": 2022, "outcome": "completed",
        "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
        "total_cost_usd": None,
    })
    assert out["confidence"] == "high", (
        "service_year == model_year - 1 must NOT trigger the date caveat."
    )
    assert "inaccurate" not in out["finding"]


def test_s3_service_year_two_below_model_year_is_flagged():
    """S3: service_year == model_year - 2 MUST trigger the caveat.

    This is the true-positive side of the boundary: two years before the model year
    is not a plausible scenario and should be flagged.
    """
    from agents.health.agent import _build_brake_finding

    out = _build_brake_finding({
        "service_date": "2020-06-01", "model_year": 2022, "outcome": "completed",
        "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
        "total_cost_usd": None,
    })
    assert out["confidence"] == "medium", (
        "service_year == model_year - 2 must trigger the date caveat "
        "and lower confidence to medium."
    )
    assert "inaccurate" in out["finding"]


def test_s3_date_note_never_on_active_brake_job():
    """S3/W3: date note must NEVER lower confidence of an active P0 brake job alone.

    The confidence for an active brake job is driven by the DTC severity path,
    and the date caveat does lower confidence (medium). But the check ensures that
    the caveat — if present — appears BEFORE the directive (tested in W3 above).
    This test pins the confidence value for an active P0 job WITHOUT a date issue.
    """
    from agents.health.agent import _build_brake_finding

    # Active P0 brake, service_date matches model_year (no date issue).
    out = _build_brake_finding({
        "service_date": "2025-06-01", "model_year": 2022, "outcome": "parts_pending",
        "is_open": True, "dtc_codes": ["C0161"], "complaint_text": "",
        "warranty_covered": "false", "total_cost_usd": None,
    })
    # service_year=2025 > model_year-1=2021 → no date caveat.
    assert out["confidence"] == "high", (
        "Active P0 brake with a recent service date must not have its confidence "
        "lowered by the date-note check."
    )
    assert "inaccurate" not in out["finding"]



# ── Review g4-card cycle 2 (W1): production date-note shape ───────────────────

def test_date_note_card_headline_carries_the_caveat_and_keeps_its_punctuation():
    """The real routine brake text with the date note, through the runner."""
    from agents.health.agent import _build_brake_finding
    from agents.tier2.runner import _build_artifact

    brake = {"service_date": "2018-03-01", "model_year": 2022, "outcome": "completed",
             "is_open": False, "dtc_codes": [], "complaint_text": "", "warranty_covered": "",
             "total_cost_usd": None}
    out = _build_brake_finding(brake)
    artifact, _ = _build_artifact("VIN-DATE", "brakes", [brake], out)
    headline = artifact["headline"]
    assert "2018" in headline and "2022" in headline and "inaccurate" in headline
    assert ".." not in headline
    assert len(headline) <= 120
    assert artifact["detail"] == out["finding"]


def test_long_text_headline_keeps_original_separators():
    from agents.tier2.card_text import derive_headline
    text = ("First sentence is here. Second one too! " + "Third sentence goes on " * 6).strip()
    assert len(text) > 120
    h = derive_headline(text)
    assert h == "First sentence is here. Second one too!"
