"""Health agent — judgment layer for the Tier 2 vehicle health platform.

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D3, D3a, D3b, D8
Tier: Tier 2 — autonomous, scheduled, nobody waiting.

What this module does
---------------------
This is the **judgment layer**.  ADP has already performed all statistical
inference (tire wear rates, battery state-of-health, brake service history).
This agent decides:

1. Does this finding matter for this owner now?
2. How urgent is it?  (``severity_hint`` ∈ ``{routine, soon, attention}``).
3. Should the findings for multiple components be raised together or separately?
   This is the bundling decision — exercisable now that all three components
   exist.
4. What should the owner actually be told?

What this module does NOT do
-----------------------------
- Compute thresholds, fit models, or extrapolate trends (spec § D3/D3a).
- Call any notification channel (spec § D7/M13).
- Modify the triage classifier or its output (spec § D8/M9).
- Set ``severity_hint`` to a P-level (P0/P1/P2/P3) — those belong to the
  deterministic triage classifier (spec § D8).

The P0 verbatim rule (spec § D8)
---------------------------------
ADP's knowledge base defines DTC ``C0161`` as severity P0 with the remedy
"Critical brake pressure loss detected. Pull over immediately."  Any finding
that involves ``C0161`` MUST contain that phrase verbatim.  Softening it
("exercise caution") is a boundary violation even when the verdict is
preserved.  This module enforces the phrase programmatically via
``prompt.inject_p0_verbatim_phrases`` — it is not left to the LLM alone.

Battery reassurance (spec § D3a)
---------------------------------
Nothing in the current ADP data crosses a warranty threshold (~70% SoH floor).
The honest output for a healthy battery is reassurance: "at 92%, degrading
normally for its age."  The prompt explicitly steers toward reassurance and
away from manufactured urgency.

Brakes are service history (spec § D3b)
-----------------------------------------
~95% of vehicles have exactly one brake record.  No trend language; no wear
extrapolation.  The highest-value signal is ``outcome ∈ {parts_pending,
follow_up_required}`` — an open, unresolved brake job.

DTC coverage
------------
Only ``C0161`` and ``C0040`` have deployed ADP KB guides.  The agent may
note other codes were recorded but must not invent explanations for them.

Public interface
----------------
``assess_vehicle_health(vin, *, tire_data, battery_data, brake_data,
                        strands_model, agent_version)``
    Full multi-component judgment for one VIN.  Returns a dict keyed by
    component (``"tires"``, ``"battery"``, ``"brakes"``) each containing
    ``finding``, ``severity_hint``, ``confidence``, ``evidence``.
    Also returns ``bundle`` and ``bundle_rationale``.

``run_health_agent(vin, rows)``
    Adapter for the runner.py ``agent_fn(vin, rows)`` interface.  ``rows``
    may be a list of single-component rows (legacy per-component call) or a
    dict with ``{"component": ..., "data": ...}`` wrapper.  Returns the
    standard ``{finding, severity_hint, confidence, evidence}`` dict that
    ``runner._build_artifact`` expects.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Optional

from agents.health.prompt import (
    C0161_VERBATIM_REMEDY,
    BRAKE_DTC_KB_GUIDED,
    SEVERITY_HINTS,
    SYSTEM_PROMPT,
    WARRANTY_VERDICT_PROMPT_CLAUSE,
    assemble_brake_dtc_note,
    inject_p0_verbatim_phrases,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal errors
# ---------------------------------------------------------------------------


class _LlmCallCircuitBreakerError(RuntimeError):
    """Internal signal: the per-batch LLM-call budget is exhausted.

    Raised by the Strands ``_llm_caller`` wrapper once the per-batch LLM
    call count reaches ``TIER2_MAX_LLM_CALLS_PER_BATCH`` (default 100).
    Caught by ``_enrich_with_llm`` as part of the fail-safe fallback,
    which returns the deterministic finding for the remaining VINs in
    the batch.  C5 fix, review cycle 1, 2026-10-04.
    """

# ---------------------------------------------------------------------------
# Coverage-verdict verbatim guard (agent-agnostic)
# ---------------------------------------------------------------------------


class CoverageVerdictVerbatimError(ValueError):
    """Raised when a warranty verdict phrase is not present verbatim in a Finding.

    Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md
    § Group 2 "Author + mutation-verify coverage-verdict-verbatim guard"

    This guard is **agent-agnostic**: it knows nothing about batteries, tires,
    brakes, or Agent 2.  It takes a verdict phrase and two rendered strings
    (``headline`` and ``detail``), and asserts the phrase appears verbatim in
    BOTH.  Softening "denied" to "under review" is the canonical boundary
    violation.

    Consumed by:
      - Agent 2's post-service-visit narration path (this module).
      - Spec 1b's Agent 3 (inherited; will import this error class directly).
      - Group 6.1 ``warranty_agent`` persona activation test
        (``test_dms_personas.py::TestVerbatimWarrantyVerdict``).

    Design notes (spec learned-expensively rules):
      1. Never read prose in a guard — compare structured data against exact
         expectations.  The verdict phrase is passed as a parameter; the guard
         does not infer it from the rendered text.
      2. Mutation values come from OUTSIDE the assertion's vocabulary —
         "under review" (with a space) is not in DMS's verdict enum, which is
         ``{approved, denied, requires_review}`` per
         ``agents/health/dms_verdicts.py`` (mirrored from the DMS repo's
         ``source/_lib/warranty_rules.py:79``).  The enum belongs to the
         producer, never to the tests: an earlier version of this note said
         "the test's DMS verdict enum", and that phrasing was the fingerprint
         of the fabricated-vocabulary defect closed by Fix Group 19 —
         ``declined`` and ``under_review`` were pinned here for three review
         cycles without DMS ever emitting either.  Do not reword it back.
      3. "Covered by construction" is a claim, not a measurement.  The guard is
         measured by a mutation that FAILS, recorded in decisions.md.
      4. Restore from a ``cp`` copy, never ``git checkout --``.
    """


def assert_coverage_verdict_verbatim(
    verdict_phrase: str,
    headline: str,
    detail: str,
) -> None:
    """Assert that *verdict_phrase* appears verbatim in BOTH *headline* and *detail*.

    This is the agent-agnostic half of the coverage-verdict-verbatim guard.
    The guard does NOT inspect prose for implicit meaning — it performs exact
    substring tests on structured data.

    Parameters
    ----------
    verdict_phrase:
        The exact DMS warranty verdict string that must appear verbatim,
        e.g. ``"denied"``, ``"approved"``, ``"requires_review"``.  Must be
        non-empty.
    headline:
        The rendered ``headline`` field of the AVX Finding, derived by
        ``agents.tier2.card_text.derive_headline`` from the finding text.
    detail:
        The rendered ``detail`` field of the AVX Finding (full finding text).

    Raises
    ------
    CoverageVerdictVerbatimError
        If ``verdict_phrase`` is absent from ``headline`` OR from ``detail``.
        The error message names which field(s) failed.

    Notes
    -----
    - Both fields are checked independently so the error is actionable.
    - The check is case-sensitive: DMS verdicts are lowercase strings
      (``"denied"``, not ``"Denied"``), and softening to title-case
      must also be caught.
    - This function knows nothing about Agent 2 specifics (batteries, tires,
      brakes).  It is imported and called identically by Agent 3 (Spec 1b).
    """
    if not verdict_phrase:
        raise ValueError("verdict_phrase must be a non-empty string")

    missing_in: list[str] = []
    if verdict_phrase not in headline:
        missing_in.append("headline")
    if verdict_phrase not in detail:
        missing_in.append("detail")

    if missing_in:
        raise CoverageVerdictVerbatimError(
            f"Coverage verdict phrase {verdict_phrase!r} is missing from "
            f"{' and '.join(missing_in)}. "
            "The DMS warranty verdict MUST be narrated verbatim — softening "
            f"(e.g. 'under review' instead of {verdict_phrase!r}) is a "
            "deterministic-seam boundary violation "
            "(spec § Group 2 coverage-verdict-verbatim guard)."
        )


# ---------------------------------------------------------------------------
# Column name references — built programmatically so the string literals
# do not appear in this module and trigger the isolation scanner.
# The actual column names live exclusively in agents/health/adp_*_source.py.
# These constants are only used for .get() key lookups on normalised rows
# returned by those source modules.
# ---------------------------------------------------------------------------

# Tire column keys (from adp_tire_source row contract)
# Agent-facing vocabulary emitted by agents/health/adp_tire_source.py.  These are
# deliberately NOT ADP column names: each source module translates at its own
# boundary so spec § D3's interface is genuinely swappable — an ADP rename is a
# one-file change and never reaches this module.  An earlier revision defined
# these by string concatenation to slip past the isolation scan, which hid a real
# bug: the agent was reading keys the source never emits, so a tire ADP had
# flagged for replacement was narrated as "normal wear".
_KEY_REPLACEMENT_FLAGGED = "replacement_flagged"
_KEY_WEAR_BAND = "wear_band"
# Bands ADP reports for a tire needing no action.  Anything else is elevated.
_HEALTHY_WEAR_BANDS = frozenset({"ok", "normal", "good"})
# wear_rate_mm_per_1k_km is accessed via source rows but not referenced here

# Brake column keys (from adp_brake_source row contract)
_COL_OUTCOME = "outcome"
_COL_SERVICE_DATE = "service_date"
_COL_DTC_CODES = "dtc_codes"
# _COL_COMPLAINT ("complaint_text") was removed in T4.10 (a): the unused
# `complaint` local in _build_brake_finding was the only consumer, and it
# was deleted there.  The key is retained in adp_brake_source's output dict
# but the deterministic agent does not use the free text — it surfaces the
# structured outcome signal instead.
_COL_WARRANTY = "warranty_covered"
_COL_COST = "total_cost_usd"
_COL_IS_OPEN = "is_open"

# Battery column keys (from adp_battery_source row contract)
_KEY_HEALTH_PCT = "health_pct"
_KEY_AGE_DAYS = "age_days"

# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------

#: Valid ``severity_hint`` values — mirrors contract.py for local use.
_VALID_SEVERITY_HINTS: frozenset[str] = SEVERITY_HINTS

#: P-level strings that must NEVER appear in ``severity_hint``.
_FORBIDDEN_P_LEVELS: frozenset[str] = frozenset({"P0", "P1", "P2", "P3"})

#: Brake outcomes that represent an **active / unresolved** service job.
#: The P0 verbatim phrase (C0161) is only injected when the outcome is in
#: this set or ``is_open`` is True — historical DTC codes on completed work
#: must not trigger the stop-driving narration.  Diluting the phrase by
#: applying it to resolved records erodes trust just as much as softening it.
#:
#: Spec § D3b: ``outcome ∈ {parts_pending, follow_up_required}`` means an open,
#: unresolved brake job.  All other outcomes (including ``resolved``,
#: ``completed``, ``warranty_closed``, etc.) are considered resolved.
#:
#: Security review Cycle 7 § W5 named this defect.  Fix Group 4b T4.9 closes it.
_ACTIVE_BRAKE_OUTCOMES: frozenset[str] = frozenset({"parts_pending", "follow_up_required"})


# ---------------------------------------------------------------------------
# Deterministic component finding builders
#
# These functions produce findings WITHOUT an LLM call when the data is
# sufficient for a deterministic answer.  They are also the fallback when
# the Strands model is unavailable or times out.
#
# The LLM path calls these as well, using their output as the guaranteed
# floor.  The P0 verbatim rule is enforced here rather than relying on
# the LLM — a boundary that exists only in a prompt will be crossed.
# ---------------------------------------------------------------------------


def _build_battery_finding(rows: list[dict]) -> dict[str, Any]:
    """Produce a battery finding dict from ADP energy_usage rows.

    Returns the canonical reassurance finding for the common case.
    No thresholds, no degradation model — ADP already inferred; CVX judges.

    Parameters
    ----------
    rows:
        Normalised rows from ``adp_battery_source.fetch_findings()``.
        Each row has ``state_of_health_pct``, ``battery_age_days``.
        May be empty (absent data scenario).

    Returns
    -------
    dict
        ``{finding, severity_hint, confidence, evidence}``
    """
    if not rows:
        return {
            "finding": (
                "No battery health data is available for this vehicle. "
                "Battery status could not be assessed."
            ),
            "severity_hint": "routine",
            "confidence": "low",
            "evidence": [
                {"source": "adp.battery." + _KEY_HEALTH_PCT, "value": None, "provenance": "absent"},
                {"source": "adp.battery." + _KEY_AGE_DAYS, "value": None, "provenance": "absent"},
            ],
        }

    row = rows[0]
    soh_raw = row.get(_KEY_HEALTH_PCT)
    age_raw = row.get(_KEY_AGE_DAYS)

    try:
        soh = float(soh_raw) if soh_raw is not None else None
    except (TypeError, ValueError):
        soh = None
    try:
        age_days = int(float(age_raw)) if age_raw is not None else None
    except (TypeError, ValueError):
        age_days = None

    age_years = round(age_days / 365.25, 1) if age_days is not None else None

    # Severity judgment: spec § D3a — nothing on current data crosses a
    # warranty threshold (~70%), so the honest output is reassurance.
    if soh is None:
        severity = "routine"
        finding = (
            "Battery data is present but state of health could not be determined. "
            "No service action required at this time."
        )
    elif soh >= 80:
        severity = "routine"
        age_phrase = f" for a {age_years}-year-old battery" if age_years else ""
        finding = (
            f"Battery state of health is {soh:.0f}%, "
            f"degrading normally{age_phrase}. "
            "No service action required."
        )
    elif soh >= 70:
        severity = "soon"
        age_phrase = f" (age: {age_years} years)" if age_years else ""
        finding = (
            f"Battery state of health is {soh:.0f}%{age_phrase}. "
            "Degradation is progressing — worth mentioning at the next service visit."
        )
    else:
        severity = "attention"
        age_phrase = f" (age: {age_years} years)" if age_years else ""
        finding = (
            f"Battery state of health is {soh:.0f}%{age_phrase}. "
            "Degradation is significant. Consider scheduling a battery inspection."
        )

    evidence = [
        {
            "source": "adp.battery." + _KEY_HEALTH_PCT,
            "value": soh_raw,
            "provenance": "live",
        },
        {
            "source": "adp.battery." + _KEY_AGE_DAYS,
            "value": age_raw,
            "provenance": "live",
        },
    ]

    return {
        "finding": finding,
        "severity_hint": severity,
        "confidence": "high" if soh is not None else "low",
        "evidence": evidence,
    }


def _build_tire_finding(rows: list[dict]) -> dict[str, Any]:
    """Produce a tire finding dict from ADP tire_health rows.

    No thresholds computed — ADP's own replacement label is the signal.

    Parameters
    ----------
    rows:
        Normalised rows from ``adp_tire_source.fetch_findings()``.
        Each row uses the **agent-facing vocabulary** emitted by
        ``adp_tire_source`` — ``vin``, ``position``, ``event_date``,
        ``replacement_flagged``, ``wear_band``, ``wear_rate_per_1k_km`` — not ADP
        column names.  The source translates at its own boundary so an ADP rename
        never reaches this module (spec § D3).

    Returns
    -------
    dict
        ``{finding, severity_hint, confidence, evidence}``
    """
    if not rows:
        return {
            "finding": (
                "No tire health data is available for this vehicle. "
                "Tire status could not be assessed."
            ),
            "severity_hint": "routine",
            "confidence": "low",
            "evidence": [
                {"source": "adp.tires." + _KEY_REPLACEMENT_FLAGGED, "value": None, "provenance": "absent"},
            ],
        }

    # Determine the worst-case across all four tire positions.
    replacement_flags = [r.get(_KEY_REPLACEMENT_FLAGGED) for r in rows]
    any_replacement = any(f is True for f in replacement_flags)
    wear_categories = [r.get(_KEY_WEAR_BAND) for r in rows if r.get(_KEY_WEAR_BAND)]
    # Deduplicate position names while preserving the order in which each
    # position was first flagged — the ADP ``tire_health`` grain is
    # ``(vin, tire_position, event_date)``, so one VIN with 60 flagged events
    # on "FR" produces 60 copies of "FR" without this dedup.  Live 2026-10-04
    # headline: "One or more tires (FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR,
    # FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR,"  — truncated at
    # 120 chars still mid-list.  ``dict.fromkeys`` keeps insertion order (3.7+)
    # which is stable within a single build; ``set()`` is not — order is
    # process-level randomised and the test would then be flaky across runs.
    positions_needing = list(dict.fromkeys(
        r.get("position")
        for r in rows
        if r.get(_KEY_REPLACEMENT_FLAGGED) is True and r.get("position")
    ))

    if any_replacement:
        pos_str = ", ".join(positions_needing)
        finding = (
            f"One or more tires ({pos_str}) show a replacement signal from ADP. "
            "Schedule a tire inspection."
        )
        severity = "attention"
        confidence = "high"
    elif any(c not in _HEALTHY_WEAR_BANDS for c in wear_categories):
        # Elevated wear without a replacement flag — worth mentioning, not urgent.
        worst_category = next(c for c in wear_categories if c not in _HEALTHY_WEAR_BANDS)
        finding = (
            f"Tire wear is in the '{worst_category}' band. "
            "No replacement needed yet; worth a look at the next service visit."
        )
        severity = "soon"
        confidence = "medium"
    elif wear_categories:
        # Reported band is a healthy one.  Saying "soon" here would manufacture
        # urgency the data does not support — the same error § D3a warns against
        # for battery, where the honest output is reassurance.
        finding = (
            f"Tire wear is in the '{wear_categories[0]}' band — normal for this vehicle. "
            "No action needed."
        )
        severity = "routine"
        confidence = "high"
    else:
        finding = "Tire health data shows normal wear. No replacement signal."
        severity = "routine"
        confidence = "high"

    evidence = [
        {
            "source": "adp.tires." + _KEY_REPLACEMENT_FLAGGED,
            "value": str(r.get(_KEY_REPLACEMENT_FLAGGED)),
            "provenance": "live",
        }
        for r in rows
    ]

    return {
        "finding": finding,
        "severity_hint": severity,
        "confidence": confidence,
        "evidence": evidence,
    }


def _build_brake_finding(brake_data: dict | None) -> dict[str, Any]:
    """Produce a brake finding dict from ADP brake service data.

    Spec § D3b: brakes are service history, not prediction.  No trend
    analysis — ~95% of vehicles have a single record.

    The P0 verbatim rule is enforced here: if C0161 is in dtc_codes,
    the returned ``finding`` MUST contain ``C0161_VERBATIM_REMEDY`` verbatim.
    This is test-enforced in test_agent.py.

    Parameters
    ----------
    brake_data:
        Dict from ``adp_brake_source.fetch_findings()`` or ``None`` (no record).
        Keys: ``service_date``, ``dtc_codes``, ``complaint_text``,
        ``warranty_covered``, ``total_cost_usd``, ``outcome``, ``is_open``.

    Returns
    -------
    dict
        ``{finding, severity_hint, confidence, evidence}``
    """
    if brake_data is None:
        return {
            "finding": (
                "No brake service history is available for this vehicle. "
                "Brake status could not be assessed from service records."
            ),
            "severity_hint": "routine",
            "confidence": "low",
            "evidence": [
                {"source": "adp.brakes.outcome", "value": None, "provenance": "absent"},
            ],
        }

    outcome = brake_data.get("outcome", "")
    is_open = brake_data.get("is_open", False)
    service_date = brake_data.get("service_date", "unknown date")
    dtc_codes: list[str] = brake_data.get("dtc_codes") or []
    # complaint_text is available in the brake record but not used in deterministic
    # finding construction — removed in T4.10 (a) as an unused local.  The field
    # remains in the source's output contract (adp_brake_source emits it).
    # If per-record context is ever needed (e.g. LLM enrichment), read it then.
    warranty = brake_data.get("warranty_covered", "")
    cost = brake_data.get("total_cost_usd")
    # model_year is supplied by AdpBrakeSource via a vehicle_identity JOIN
    # (Review Cycle 1 W6; spec T4.3).  Present as int or string from ADP.
    # Absent (None) means the JOIN found no identity row — treated as unknown.
    model_year_raw = brake_data.get("model_year")

    # W5 fix: determine whether this brake job is still active BEFORE building
    # the DTC note.  The P0 verbatim phrase ("Pull over immediately") must only
    # appear for genuinely unresolved brake safety issues — service-history
    # tables retain historical DTC codes on completed work by design.
    # Security review Cycle 7 § W5; Fix Group 4b T4.9.
    _active_brake_job = is_open or outcome in _ACTIVE_BRAKE_OUTCOMES

    # Build DTC note — only for KB-guided codes; no invented explanations.
    # For resolved jobs, assemble_brake_dtc_note would emit the verbatim phrase
    # for C0161 — that must not happen.  Instead, acknowledge the DTC was
    # recorded and addressed without narrating the stop-driving phrase.
    # For active jobs, pass all dtc_codes so the P0 phrase is included.
    if _active_brake_job:
        dtc_note = assemble_brake_dtc_note(dtc_codes)
    else:
        # Resolved job: generate notes for non-C0161 codes via the standard helper,
        # then add a plain historical acknowledgement for C0161 (if present) that
        # records the DTC without the stop-driving phrase.
        non_p0_codes = [c for c in dtc_codes if c.upper() != "C0161"]
        non_p0_note = assemble_brake_dtc_note(non_p0_codes) if non_p0_codes else ""
        c0161_ack = (
            "DTC C0161 (brake pressure) was recorded and addressed during this service."
            if any(c.upper() == "C0161" for c in dtc_codes)
            else ""
        )
        dtc_note = " ".join(part for part in [c0161_ack, non_p0_note] if part)

    # Severity judgment
    if is_open:
        severity = "attention"
        if outcome == "parts_pending":
            status_phrase = "is awaiting parts"
        elif outcome == "follow_up_required":
            status_phrase = "requires a follow-up visit"
        else:
            status_phrase = f"has an open status ({outcome!r})"
        finding_parts = [
            f"Brake service from {service_date} {status_phrase}.",
        ]
        if dtc_note:
            finding_parts.append(dtc_note)
        finding = " ".join(finding_parts)
        confidence = "high"
    elif dtc_codes:
        severity = "soon"
        finding_parts = [
            f"Brake service on {service_date} was completed (outcome: {outcome!r}).",
        ]
        if dtc_note:
            finding_parts.append(dtc_note)
        finding = " ".join(finding_parts)
        confidence = "high"
    else:
        severity = "routine"
        finding = (
            f"Most recent brake service ({service_date}) completed successfully "
            f"with outcome '{outcome}'. No open issues."
        )
        confidence = "high"

    # Review Cycle 1 W6 / T4.3: if the service date predates the vehicle's
    # model year, the record is suspect (may be a data-entry error or a wrong-VIN
    # match).  Lower confidence and name BOTH years so the owner can judge.
    # model_year is int (ADP partition key); service_date is ISO-8601 string —
    # we compare only the four-digit year prefix.
    try:
        service_year = int(str(service_date)[:4]) if service_date and service_date != "unknown date" else None
    except (ValueError, TypeError):
        service_year = None
    try:
        model_year = int(model_year_raw) if model_year_raw is not None else None
    except (ValueError, TypeError):
        model_year = None

    if service_year is not None and model_year is not None and service_year < model_year - 1:
        # The service date predates the vehicle's model year by more than one calendar
        # year.  (Model years go on sale the prior calendar year, so a service record
        # dated the same year as the model year — or the year before — is plausible.
        # Only flag when service_year < model_year - 1.)  Lower confidence because
        # the data may be mismatched; name both years so the owner can judge.
        #
        # W3 / S3 (Review Cycle 1): the caveat is placed as its own trailing paragraph,
        # separated by "\n\n", so it is never adjacent to (or inside the 200-char window
        # of) the C0161 directive on an active brake job.  inject_p0_verbatim_phrases
        # prepends the directive as a standalone paragraph, so the final structure is:
        #   "[directive]\n\n[finding_text]\n\n[date_caveat]"
        # The caveat is therefore always well outside the directive's deny-list window.
        # It is also never appended to an active C0161 job's dtc_note content so it
        # cannot soften the stop-driving instruction.
        confidence = "medium"
        if _active_brake_job and any(c.upper() == "C0161" for c in dtc_codes):
            # On an active P0 brake job, skip the date caveat entirely — we must not
            # place any hedging text near the directive, and lowering the confidence of
            # a genuine stop-driving finding is a safety seam decision.  The caveat
            # records a data-quality concern; that concern is secondary to the safety
            # directive.  (Recorded in decisions.md per W3 / Review Cycle 1.)
            confidence = "high"
        else:
            # Defect 5 fix (2026-10-04): the caveat is a *trailing* paragraph in
            # ``detail``, never a prefix.  ``derive_headline`` takes the first
            # sentence as the headline; prepending the caveat made the caveat
            # the headline on every suspect-date brake record and buried the
            # actual brake finding.  Live 2026-10-04 example (FIND-34adace5):
            # "This service record is dated 2020, but the vehicle is a 2022
            # model, so the date may be inaccurate."  The user's constraint for
            # the fix: *keep the finding as the headline; the caveat goes in
            # the detail.*  Active C0161 jobs never reach this branch (above),
            # so this change does not affect the P0 verbatim path.
            date_note = (
                f"Note: the service record is dated {service_year}, but the "
                f"vehicle is a {model_year} model, so the date may be inaccurate."
            )
            finding = finding + "\n\n" + date_note

    # CRITICAL: enforce P0 verbatim phrase for C0161 — but ONLY for active,
    # unresolved brake jobs.
    #
    # Service-history tables retain historical DTC codes on completed work;
    # that is what they are for.  ~475k VINs / ~500k rows of ADP service records
    # have exactly this shape: C0161 was recorded, the work was done, outcome is
    # 'resolved'.  Injecting the stop-driving phrase on a completed job creates
    # a severity / narrative contradiction (severity_hint='soon' vs "Pull over
    # immediately") and — more critically — dilutes the phrase: an owner who
    # sees it on every historical record stops treating it as actionable when it
    # genuinely is.  Over-narration of a safety phrase erodes trust the same
    # way softening it does.
    #
    # Gate: the phrase fires only when the brake job is ACTIVE, i.e. when
    # ``is_open`` is True OR ``outcome`` is an unresolved state.  Resolved
    # jobs (outcome='resolved', 'completed', etc.) pass an empty dtc list so
    # inject_p0_verbatim_phrases is a no-op.
    #
    # Security review Cycle 7 § W5; Fix Group 4b T4.9.
    _active_brake_job = is_open or outcome in _ACTIVE_BRAKE_OUTCOMES
    finding = inject_p0_verbatim_phrases(
        finding,
        dtc_codes if _active_brake_job else [],
    )

    evidence = [
        {"source": "adp.brakes.outcome", "value": outcome, "provenance": "live"},
        {"source": "adp.brakes.service_date", "value": service_date, "provenance": "live"},
        {"source": "adp.brakes.dtc_codes", "value": str(dtc_codes), "provenance": "live"},
    ]

    return {
        "finding": finding,
        "severity_hint": severity,
        "confidence": confidence,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# W2 cycle-2 (2026-10-04): date caveat is kept out of the LLM path entirely
# ---------------------------------------------------------------------------
#
# ``_build_brake_finding`` appends the data-date caveat as a trailing paragraph
# separated by ``\n\n``.  On the deterministic path that is sufficient for
# ``derive_headline`` to land the brake finding (not the caveat) as the headline.
#
# On the LLM path it is not sufficient.  The caveat was sent to the model as
# part of the deterministic_finding text; a rule-following model may rearrange
# the sentences, and ``_parse_llm_response`` takes the model's ``finding`` as-is,
# so the headline-selecting sentence (``derive_headline``) can end up on the
# caveat.  Every brakes batch now sends up to 100 records to the model (C5
# circuit breaker), and brakes selection only picks active jobs on this fleet,
# so most of those 100 can carry the caveat.
#
# Fix: strip the trailing caveat from the brakes finding BEFORE sending it to
# the LLM, and re-attach it AFTER enrichment.  The model therefore cannot
# promote the caveat text, because it never sees it.  The deterministic
# fallback path (fresh ``component_results`` dict passed into
# ``_enrich_with_llm``) is unaffected: it keeps the original, caveat-bearing
# finding, which the deterministic trailing-paragraph structure already makes
# safe for ``derive_headline``.
#
# The marker string is the deterministic prefix produced by
# ``_build_brake_finding``; the full caveat is appended with ``\n\n``.  A single
# constant pin makes the two sides of the split/merge agree.

_BRAKE_DATE_CAVEAT_SEPARATOR: str = "\n\nNote: the service record is dated "


def _split_brake_date_caveat(finding_text: str) -> tuple[str, Optional[str]]:
    """Return ``(body, caveat_or_None)`` for a brake finding.

    If ``finding_text`` carries the deterministic data-date caveat (produced by
    ``_build_brake_finding`` as a trailing paragraph), return the body and the
    caveat separately.  Otherwise return ``(finding_text, None)``.

    The caveat marker is pinned by ``_BRAKE_DATE_CAVEAT_SEPARATOR``; a change to
    ``_build_brake_finding``'s caveat phrasing must update that constant so the
    split/merge agree.
    """
    sep = _BRAKE_DATE_CAVEAT_SEPARATOR
    idx = finding_text.rfind(sep)
    if idx < 0:
        return finding_text, None
    body = finding_text[:idx]
    # Keep the full caveat text (not just the suffix) so the re-attach is
    # byte-identical to the original string.  idx + 2 skips the two leading
    # newlines so ``caveat`` is the sentence proper; we store it with its own
    # ``\n\n`` prefix when re-attaching below.
    caveat = finding_text[idx + 2:]
    return body, caveat


# ---------------------------------------------------------------------------
# Severity guard — enforces no P-level reaches severity_hint
# ---------------------------------------------------------------------------


def _sanitise_severity_hint(value: Any) -> str:
    """Return a safe ``severity_hint`` value, rejecting P-levels.

    If *value* is a valid scheduling-urgency hint, return it unchanged.
    If it is a P-level (P0–P3), log a warning and return ``"attention"``
    (the highest valid urgency) to preserve the spirit of the alert without
    leaking the triage vocabulary.  Any other unknown value defaults to
    ``"routine"``.

    This is the last-resort guard; the prompt and deterministic builders
    should never produce a P-level.  The guard is test-enforced in
    ``test_agent.py::test_severity_hint_never_carries_p_level``.
    """
    if value in _VALID_SEVERITY_HINTS:
        return value
    if value in _FORBIDDEN_P_LEVELS:
        log.warning(
            "health agent: severity_hint received a P-level %r — "
            "P-levels belong to the triage classifier; coercing to 'attention'.",
            value,
        )
        return "attention"
    log.warning(
        "health agent: unknown severity_hint %r — defaulting to 'routine'.", value
    )
    return "routine"


def _sanitise_component_result(result: dict[str, Any]) -> dict[str, Any]:
    """Sanitise a single component result dict.

    Ensures:
    - ``severity_hint`` is from the allowed vocabulary (never a P-level).
    - ``finding`` is a non-empty string.
    - ``confidence`` is one of high/medium/low.
    - ``evidence`` is a list.
    """
    result["severity_hint"] = _sanitise_severity_hint(result.get("severity_hint"))
    if not isinstance(result.get("finding"), str) or not result["finding"].strip():
        result["finding"] = "Finding unavailable."
    if result.get("confidence") not in {"high", "medium", "low"}:
        result["confidence"] = "low"
    if not isinstance(result.get("evidence"), list):
        result["evidence"] = []
    return result


# ---------------------------------------------------------------------------
# Bundling decision
# ---------------------------------------------------------------------------


def _decide_bundling(component_results: dict[str, dict]) -> tuple[bool, str]:
    """Decide whether to present multiple findings bundled or separately.

    Bundling rules (spec § D8 / T4.3):
    - Separate when any single component is ``attention`` AND others are
      ``routine`` — an urgent finding must not be buried.
    - Separate when a C0161 (P0) brake finding is present — it must stand alone.
    - Bundle when multiple components are ``attention`` (holistic picture).
    - Bundle when a brake open-outcome and a tire finding together imply a
      combined service visit.

    Parameters
    ----------
    component_results:
        Dict mapping component name → sanitised result dict.

    Returns
    -------
    tuple[bool, str]
        ``(bundle_flag, rationale_string)``
    """
    if len(component_results) <= 1:
        return False, "Only one component has findings — no bundling decision needed."

    severities = {comp: res["severity_hint"] for comp, res in component_results.items()}
    attention_comps = [c for c, s in severities.items() if s == "attention"]
    routine_comps = [c for c, s in severities.items() if s == "routine"]

    # Check if any brake finding has a P0 verbatim phrase (C0161).
    brake_result = component_results.get("brakes")
    has_p0_brake = (
        brake_result is not None
        and C0161_VERBATIM_REMEDY in brake_result.get("finding", "")
    )

    if has_p0_brake:
        return False, (
            "A P0 brake safety alert (C0161) is present. "
            "It must stand alone to ensure it is not buried in a combined message."
        )

    if len(attention_comps) >= 2:
        return True, (
            f"Multiple components require attention "
            f"({', '.join(sorted(attention_comps))}). "
            "Presenting a combined picture helps the owner prioritise one service visit."
        )

    if len(attention_comps) == 1 and routine_comps:
        return False, (
            f"Only {attention_comps[0]} requires attention; "
            f"{', '.join(sorted(routine_comps))} are routine. "
            "Separating ensures the attention-level finding is not diluted."
        )

    # All 'soon' or all 'routine' — bundle for efficiency.
    return True, (
        "All components share similar urgency. "
        "Bundling provides an efficient, holistic service summary."
    )


# ---------------------------------------------------------------------------
# Main public interface
# ---------------------------------------------------------------------------


def assess_vehicle_health(
    vin: str,
    *,
    tire_rows: Optional[list[dict]] = None,
    battery_rows: Optional[list[dict]] = None,
    brake_data: Optional[dict] = None,
    warranty_verdict_phrase: Optional[str] = None,
    _llm_caller: Optional[Callable] = None,
) -> dict[str, Any]:
    """Perform full multi-component health judgment for one VIN.

    This is the primary judgment function.  It:
    1. Builds deterministic findings for each available component.
    2. Applies the P0 verbatim rule programmatically (not only via prompt).
    3. Makes the bundling decision across all available components.
    4. Optionally invokes an LLM to enrich the findings if ``_llm_caller``
       is provided (tests inject a ``None`` or a callable double).
    5. When ``warranty_verdict_phrase`` is provided, applies the
       coverage-verdict-verbatim guard (agent-agnostic) as a post-generation
       assertion on every component finding.  On violation, the finding is
       written with ``narration_degraded=True`` and the offending text is
       replaced with a deterministic fallback that embeds the phrase verbatim.
       The caller should treat ``narration_degraded=True`` as a signal to log
       and surface a degraded-narration flag on the AVX Finding.

    Parameters
    ----------
    vin:
        Vehicle Identification Number.
    tire_rows:
        Rows from ``adp_tire_source`` or ``None`` if not available.
    battery_rows:
        Rows from ``adp_battery_source`` or ``None`` if not available.
    brake_data:
        Single finding dict from ``adp_brake_source`` or ``None``.
    warranty_verdict_phrase:
        Optional DMS warranty verdict phrase that must appear verbatim in
        BOTH ``headline`` and ``detail`` of every component finding that
        cites a warranty verdict.  When provided, ``assert_coverage_verdict_verbatim``
        is applied after narration.  On violation the finding is rewritten
        with the phrase prepended and ``narration_degraded=True`` is set.
        Pass ``None`` (the default) when no warranty verdict is involved.
        This parameter is agent-agnostic — it is also consumed by Agent 3
        (Spec 1b) and the ``warranty_agent`` persona (Group 6.1).
    _llm_caller:
        Optional callable ``(prompt: str) -> str`` that invokes the Strands
        LLM.  If ``None``, uses deterministic findings only (for tests and
        for cases where Strands is not available).  In production, the runner
        supplies the Strands Agent; in unit tests, inject a simple lambda.

    Returns
    -------
    dict
        ``{
            "battery": {finding, severity_hint, confidence, evidence, narration_degraded?} | absent,
            "tires":   {finding, severity_hint, confidence, evidence, narration_degraded?} | absent,
            "brakes":  {finding, severity_hint, confidence, evidence, narration_degraded?} | absent,
            "bundle":  bool,
            "bundle_rationale": str,
        }``

        Each component dict carries ``narration_degraded=True`` only when the
        coverage-verdict-verbatim guard fired and the finding text was rewritten.
    """
    # Step 1: Build deterministic findings for each available component.
    component_results: dict[str, dict] = {}

    if battery_rows is not None:
        battery_result = _build_battery_finding(battery_rows)
        component_results["battery"] = _sanitise_component_result(battery_result)

    if tire_rows is not None:
        tire_result = _build_tire_finding(tire_rows)
        component_results["tires"] = _sanitise_component_result(tire_result)

    if brake_data is not None or (
        brake_data is None and tire_rows is None and battery_rows is None
    ):
        # Always include brake component if data was passed; also include
        # absent-data finding if no data at all (so caller knows it was checked).
        brake_result = _build_brake_finding(brake_data)
        component_results["brakes"] = _sanitise_component_result(brake_result)
    elif brake_data is None and (tire_rows is not None or battery_rows is not None):
        # brake_data explicitly absent but other data present — include absent finding.
        brake_result = _build_brake_finding(None)
        component_results["brakes"] = _sanitise_component_result(brake_result)

    # Step 2: LLM enrichment (optional; deterministic output is the fallback).
    if _llm_caller is not None and component_results:
        component_results = _enrich_with_llm(
            vin, component_results, brake_data, _llm_caller,
            warranty_verdict_phrase=warranty_verdict_phrase,
        )

    # Step 2b: Coverage-verdict-verbatim guard (post-generation assertion).
    # Applied when a DMS warranty verdict phrase was passed in.  The guard is
    # agent-agnostic — it knows nothing about batteries, tires, or brakes; it
    # only checks that the phrase appears verbatim in headline + detail.
    #
    # The AVX Finding's ``headline`` is derived by ``card_text.derive_headline``
    # and ``detail`` is the full ``finding`` text (per runner.py _build_artifact).
    # IMPORTANT: we use the SAME derive_headline function here as runner.py uses,
    # so the guard checks exactly the headline the runner will write.  Using a
    # different derivation (e.g. ``finding_text[:120]``) is a seam break — the
    # guard would check a different string than the one that reaches the Finding.
    #
    # On violation: the finding text is rewritten to prepend the verdict phrase
    # (making it unambiguously present in both headline and detail), and
    # ``narration_degraded=True`` is set on the component result.  We log the
    # violation; we do NOT raise to avoid dropping the entire Finding.
    if warranty_verdict_phrase is not None:
        from agents.tier2.card_text import derive_headline as _derive_headline
        for comp in list(component_results.keys()):
            comp_result = component_results[comp]
            if not isinstance(comp_result, dict):
                continue
            finding_text = comp_result.get("finding", "")
            # Build the Finding-level headline and detail the runner will produce.
            # The headline travels to the runner as ``headline`` (runner uses it
            # verbatim), so the guard checks the exact string that is written.
            headline = (
                _derive_headline(finding_text, (warranty_verdict_phrase,))
                if finding_text else ""
            )
            detail = finding_text
            try:
                assert_coverage_verdict_verbatim(warranty_verdict_phrase, headline, detail)
                component_results[comp] = {**comp_result, "headline": headline}
            except CoverageVerdictVerbatimError:
                log.warning(
                    "health agent: coverage-verdict-verbatim guard fired for "
                    "component %r — verdict phrase %r absent from narration. "
                    "Rewriting with verbatim phrase prepended; "
                    "narration_degraded=True.",
                    comp,
                    warranty_verdict_phrase,
                )
                rewritten = f"{warranty_verdict_phrase}\n\n{finding_text}"
                component_results[comp] = {
                    **comp_result,
                    "finding": rewritten,
                    "headline": _derive_headline(rewritten, (warranty_verdict_phrase,)),
                    "narration_degraded": True,
                }

    # Step 3: Bundling decision.
    bundle, bundle_rationale = _decide_bundling(component_results)

    return {
        **component_results,
        "bundle": bundle,
        "bundle_rationale": bundle_rationale,
    }


def _enrich_with_llm(
    vin: str,
    component_results: dict[str, dict],
    brake_data: Optional[dict],
    llm_caller: Callable,
    *,
    warranty_verdict_phrase: Optional[str] = None,
) -> dict[str, dict]:
    """Optionally enrich deterministic findings with LLM judgment.

    The LLM may improve phrasing and add nuance, but:

    **T4.14 — Structural P0 bypass (primary control)**
    When the brakes component carries ``C0161`` (case-insensitive), that
    component is excluded from the LLM call entirely — active OR resolved —
    and its deterministic finding is returned byte-identical.  Review cycle 1
    C4 widened this from active-only to any-C0161; the deterministic path
    places the verbatim phrase for active jobs and omits it for resolved jobs,
    while the LLM would do neither reliably.  There is no LLM text to detect
    or repair.

    Other components still enrich normally — the bypass is per-component, not a
    whole-call abort.

    **C5 needs-judgment filter (budget safety)**
    When ``warranty_verdict_phrase`` is None, components with
    ``severity_hint == 'routine'`` are also kept off the LLM path — their
    deterministic sentence is already owner-ready, and sending every nightly
    VIN through the LLM to receive a rephrased routine sentence blew the
    2026-10-04 run on both tokens and wall-clock.  See the longer comment at
    the filter site.

    **W2 cycle-2 (2026-10-04): date caveat is kept off the LLM path**
    For the brakes component, the trailing date caveat produced by
    ``_build_brake_finding`` is stripped from the finding before the LLM call
    (``_split_brake_date_caveat``) and re-attached after enrichment.  The model
    therefore cannot promote the caveat to the headline, which is the
    2026-10-04 Defect 5 failure on the LLM path.

    **Remaining guards (all still apply)**
    - All P0 verbatim phrases are re-injected after the LLM call (defence-in-depth).
    - ``severity_hint`` is sanitised after the call to reject P-levels AND
      downward overrides (T4.10 b2).
    - The P0 re-inject fires only for active brake jobs (T4.10 b3).
    - A resolved C0161 that somehow carries the stop-driving phrase after
      enrichment is scrubbed back to the deterministic finding (defence-in-
      depth; unreachable on the primary path because of the C4 bypass above).
    - If the LLM response cannot be parsed, ``_parse_llm_response`` returns
      the fallback (``llm_components`` after the strip step), so the W2
      re-attach still runs and the caveat is restored.
    - The C5 breaker (``_LlmCallCircuitBreakerError``, both call-count and
      wall-time) short-circuits remaining VINs; this function's own
      ``except _LlmCallCircuitBreakerError`` branch handles the fallback.

    Parameters
    ----------
    vin:
        VIN — used only for logging (not included in prompt).
    component_results:
        Deterministic findings (safe fallback).
    brake_data:
        Raw brake data for DTC list extraction.
    llm_caller:
        Callable ``(system_prompt: str, user_prompt: str) -> str``.

    Returns
    -------
    dict
        Enriched (or unchanged) component results.  Active-P0-brake component
        is always byte-identical to the deterministic finding.
    """
    dtc_codes: list[str] = []
    if brake_data:
        dtc_codes = brake_data.get("dtc_codes") or []

    # T4.10 (b3): apply the same resolved-job gate as T4.9.
    # The P0 re-inject block below must only fire for active brake jobs.
    # Using the same _ACTIVE_BRAKE_OUTCOMES set that governs the deterministic
    # path — if the outcome is resolved, the dtc list passed to the injector is
    # empty, making the call a no-op.
    _enrichment_active_brake = bool(
        brake_data
        and (
            brake_data.get("is_open")
            or brake_data.get("outcome") in _ACTIVE_BRAKE_OUTCOMES
        )
    )

    # T4.14 — structural bypass extended (C4, 2026-10-04 review cycle 1):
    # determine whether the brakes component mentions C0161 in ANY form
    # (active OR resolved).  Any such record is excluded from the LLM call
    # and its deterministic finding is returned unchanged.
    #
    # Why the bypass extends to resolved jobs too (C4):
    # D2 (2026-10-04 fix) started sending the deterministic finding text —
    # which mentions "DTC C0161" on the resolved-job path — into the user
    # prompt.  SYSTEM_PROMPT instructs the model to add the verbatim stop-
    # driving phrase whenever C0161 appears in dtc_codes.  Nothing between
    # the model's reply and the Finding scrubs the phrase when the job is
    # resolved, so a rule-following model would place "Pull over immediately"
    # on a ~475k-row set of resolved brake jobs.  Over-narration of a safety
    # phrase erodes trust the same way softening it does.
    #
    # The structural move:  if C0161 is in dtc_codes, no LLM narrates the
    # brakes component.  The textual guard (inject_p0_verbatim_phrases) and
    # the resolved-vs-active deterministic branching in _build_brake_finding
    # continue to operate on the deterministic path — defence-in-depth,
    # not load-bearing.
    _brakes_has_c0161 = (
        brake_data is not None
        and "C0161" in {code.upper() for code in dtc_codes}
    )

    # Separate out the C0161-brakes component so the LLM never sees it.
    # All other components proceed through enrichment normally.
    deterministic_p0_components: dict[str, dict] = {}
    llm_components: dict[str, dict] = {}
    for comp, res in component_results.items():
        if comp == "brakes" and _brakes_has_c0161:
            # Brake record with C0161 — bypass LLM entirely for this component
            # whether the job is active or resolved.  The deterministic path
            # has already placed the verbatim phrase for active jobs and
            # omitted it for resolved jobs; the LLM would do neither reliably.
            deterministic_p0_components[comp] = res
        else:
            llm_components[comp] = res

    # If there are no components left for the LLM, return deterministic results.
    if not llm_components:
        return component_results

    # C5 (2026-10-04 review cycle 1): needs-judgment filter.
    #
    # The deterministic path already produces a well-formed, owner-ready sentence
    # for routine findings — "Battery is at 92%, degrading normally for a 3.0-
    # year-old battery", "Tire wear is in the 'normal' band — no action needed",
    # "Most recent brake service (…) completed successfully".  Sending those
    # through the LLM only to receive a rephrased version of the same sentence
    # inflates every nightly run (156/375/2000 VINs) with calls that cannot
    # improve the finding, which blew both the token cap and the wall-clock
    # budget on 2026-10-04 (reviewer: brakes at ~3-5s/call × 2000 VINs =
    # ~6,800s against TIER2_MAX_WALL_SECONDS=600).
    #
    # Needs-judgment = ``severity_hint`` != 'routine'.
    # A component that is routine (whether high-confidence "degrading normally"
    # or low-confidence "data not available") has nothing the LLM can
    # legitimately improve; it is skipped from the enrichment call entirely
    # and the deterministic finding reaches the owner-facing card unchanged.
    # The small residual judgement cost ("improve the phrasing of a
    # reassurance / absent-data sentence") is deliberately traded against
    # being able to finish a batch at all.
    #
    # Exception — post-service-visit warranty verdict path: when
    # ``warranty_verdict_phrase`` is non-None, the LLM has a REAL task that
    # the deterministic path has not performed — narrate the DMS verdict
    # phrase verbatim.  Skipping routine+high components in that case would
    # produce a Finding without the required phrase and trigger
    # ``narration_degraded=True`` on every routine resolved brake record
    # with a warranty verdict.  The filter is therefore disabled when a
    # verdict phrase is present.
    #
    # This fix is upstream of the structural C0161 bypass above — but the
    # C0161 bypass still runs first because its safety rationale (never let
    # the model add the stop-driving phrase to a resolved job) is independent
    # of the budget rationale.  A resolved C0161 record has severity "soon"
    # (``_build_brake_finding``'s ``elif dtc_codes`` branch), so it would
    # NOT be filtered out by the "routine" needs-judgment filter — the C0161
    # bypass is the only reason resolved C0161 records skip the LLM.
    needs_judgment_components: dict[str, dict] = {}
    routine_components: dict[str, dict] = {}
    if warranty_verdict_phrase is not None:
        # Warranty verdict path: every component with a Finding is routed to
        # the LLM so the verdict phrase can be narrated.  No routine filter.
        needs_judgment_components = dict(llm_components)
    else:
        for comp, res in llm_components.items():
            severity = res.get("severity_hint", "routine")
            if severity == "routine":
                # Deterministic finding is already a well-formed owner-ready
                # sentence (reassurance or absent-data); no LLM call needed.
                routine_components[comp] = res
            else:
                needs_judgment_components[comp] = res

    # If every remaining component is routine/high-confidence, there is
    # nothing for the LLM to do: return the deterministic result set
    # (including the bypassed C0161 component).
    if not needs_judgment_components:
        return component_results

    # The LLM only sees components that genuinely need judgment.  Routine
    # components rejoin the result below so the returned dict is complete.
    llm_components = needs_judgment_components

    # W2 cycle-2 (2026-10-04): strip the data-date caveat from the brakes
    # finding before the LLM call.  The caveat is a trailing paragraph on the
    # deterministic finding; a rule-following model may reorder sentences and
    # ``_parse_llm_response`` takes the model's ``finding`` as-is, so without
    # stripping, ``derive_headline`` can land the caveat as the owner-facing
    # headline on the ~100 LLM-called brake records per batch.  We stash the
    # caveat here and re-attach it to the enriched (or fallback) finding below
    # — the model therefore never sees the caveat and cannot promote it.
    #
    # The deterministic fallback path (routine_components, deterministic_p0_components)
    # keeps the caveat on its original finding — that path's structural
    # ``\n\n``-separation already lands the brake finding (not the caveat) as the
    # headline, so no action is needed there.
    _stashed_brake_caveat: Optional[str] = None
    if "brakes" in llm_components:
        _brake_body, _stashed_brake_caveat = _split_brake_date_caveat(
            llm_components["brakes"].get("finding", "") or ""
        )
        if _stashed_brake_caveat is not None:
            # Replace the brakes entry with a copy whose finding is body-only.
            # We copy the dict so the pre-enrichment ``component_results`` dict is
            # unaffected — the fallback path still has the full caveat-bearing
            # finding available.
            _brakes_copy = dict(llm_components["brakes"])
            _brakes_copy["finding"] = _brake_body
            llm_components = dict(llm_components)
            llm_components["brakes"] = _brakes_copy

    summary_parts: list[str] = []
    for comp, res in llm_components.items():
        # The prompt must carry what the LLM is being asked to improve.
        # Spec § D2b and the 2026-10-04 recovery: a prompt built from only
        # `severity_hint` + `confidence` + `evidence_count` is not an
        # improvement task; the model has nothing to work with and either
        # refuses ("When you have the data, I'm here.", logged against
        # the 2026-10-04 tires run) or stalls on previous content.
        #
        # The prior rev stripped the deterministic finding text because
        # a substring of it had reached the prompt directly from a raw ADP
        # column (security-review Cycle 8 W-C8-2).  The remedy there was
        # correct and incomplete: ADP string columns reaching the prompt is
        # the hazard, not finding text in general.  The deterministic
        # finding is now assembled downstream of column-level validators
        # (`validate_dtc_codes` in `adp_brake_source` constrains dtc_codes
        # to a strict regex; the tire / battery builders only template
        # categorical bands and numeric floats into their sentences).  The
        # text we send here is therefore from the validated vocabulary, not
        # from raw ADP strings — see test_complaint_text_not_in_prompt.
        #
        # Finding text is capped at 500 chars as a belt-and-braces size
        # bound; a brake record carrying an unexpectedly large structured
        # field cannot inflate a single component into the entire prompt.
        finding_text = (res.get("finding") or "").strip()
        if len(finding_text) > 500:
            finding_text = finding_text[:500] + "…"
        evidence_count = len(res.get("evidence") or [])
        summary_parts.append(
            f"[{comp.upper()}]\n"
            f"  severity_hint: {res['severity_hint']!r}\n"
            f"  confidence: {res['confidence']!r}\n"
            f"  evidence_count: {evidence_count}\n"
            f"  deterministic_finding: {finding_text!r}"
        )
    component_summary = "\n".join(summary_parts)

    user_prompt = (
        "Deterministic health findings for VIN (redacted):\n"
        f"{component_summary}\n\n"
        "Each ``deterministic_finding`` was built from validated ADP columns "
        "(categorical bands, format-checked DTC codes, numeric metrics). "
        "Review and if needed improve the plain-language text for each "
        "component.  Keep the same severity_hint values unless you have a "
        "strong reason to change them.  Return a JSON object with one key "
        "per component."
    )

    # Post-service-visit: when a DMS warranty verdict phrase is present, append
    # the verbatim-narration clause so the LLM is instructed before generation.
    # The post-generation guard (Step 2b) remains the primary enforcement; this
    # clause is defence-in-depth per the WARRANTY_VERDICT_PROMPT_CLAUSE docstring.
    if warranty_verdict_phrase is not None:
        user_prompt = (
            user_prompt
            + f"\n\nThe DMS warranty verdict for this vehicle is: "
            f"{warranty_verdict_phrase!r}."
            + WARRANTY_VERDICT_PROMPT_CLAUSE
        )

    try:
        llm_response = llm_caller(SYSTEM_PROMPT, user_prompt)
        enriched = _parse_llm_response(llm_response, llm_components)
    except _LlmCallCircuitBreakerError as exc:
        # C5 circuit breaker — expected and benign.  ``_trip_breaker`` in
        # ``make_strands_agent_fn`` already logs the first trip at INFO; the
        # per-VIN entry here was producing ~1,900 lines on a brakes run
        # (reviewer Cycle-3 Suggestion 1).  DEBUG so operators can still
        # read the per-VIN detail when they are explicitly looking for it.
        log.debug(
            "health agent: %s — this VIN returns the deterministic finding.",
            exc,
        )
        enriched = llm_components
    except Exception:
        log.warning(
            "health agent: LLM enrichment failed for VIN (redacted); "
            "using deterministic findings."
        )
        enriched = llm_components

    # Merge back the deterministic P0-brake component (byte-identical to input).
    enriched.update(deterministic_p0_components)

    # Merge back the routine/high-confidence components that bypassed the LLM
    # (C5 needs-judgment filter).  These are byte-identical to the deterministic
    # finding so the returned dict is complete even when the LLM only enriched
    # a subset of components.
    for comp, res in routine_components.items():
        if comp not in enriched:
            enriched[comp] = res

    # Re-apply P0 verbatim guarantee after LLM enrichment.
    # T4.10 (b3): gate on active brake jobs only — resolved jobs must not re-acquire
    # the stop-driving phrase via this path.  The _enrichment_active_brake flag uses
    # the same _ACTIVE_BRAKE_OUTCOMES check as _build_brake_finding (T4.9), ensuring
    # the two paths agree on when the phrase is warranted.
    if "brakes" in enriched and dtc_codes and _enrichment_active_brake:
        brakes = enriched["brakes"]
        brakes["finding"] = inject_p0_verbatim_phrases(
            brakes.get("finding", ""), dtc_codes
        )

    # C4 defence-in-depth (2026-10-04 review cycle 1): if the brakes record
    # mentions C0161 and the job is RESOLVED, scrub the verbatim stop-driving
    # phrase from the finding.  The _brakes_has_c0161 branch above already
    # bypasses the LLM for every C0161 record, so this scrub is unreachable
    # on the primary path — but a future change that re-routes a resolved
    # C0161 record through the LLM (or an LLM that writes the phrase from
    # independent knowledge rather than echoing the prompt) must not land
    # "Pull over immediately" on a completed job.  If the phrase is present
    # after scrubbing would be a no-op, we fall back to the deterministic
    # finding entirely so the result cannot carry a half-scrubbed sentence.
    if (
        "brakes" in enriched
        and brake_data is not None
        and "C0161" in {code.upper() for code in dtc_codes}
        and not _enrichment_active_brake
    ):
        brake_result = enriched["brakes"]
        if isinstance(brake_result, dict) and C0161_VERBATIM_REMEDY in (
            brake_result.get("finding") or ""
        ):
            log.warning(
                "health agent: resolved C0161 brake finding carried the "
                "stop-driving phrase after enrichment; falling back to the "
                "deterministic finding (defence-in-depth scrub)."
            )
            # Replace with the deterministic finding verbatim.  component_results
            # is the pre-enrichment dict passed in, so this restores exactly the
            # text the C4 bypass would have returned.
            enriched["brakes"] = component_results["brakes"]

    # Re-sanitise severity_hint after LLM enrichment.
    # T4.10 (b2): enforce a severity FLOOR — the LLM may not downgrade below what
    # the deterministic builder determined.  The deterministic floor is the severity
    # in `component_results` (the fallback passed into _parse_llm_response).
    # Rationale: the deterministic builder has direct access to the ADP data; the
    # LLM only sees a text summary.  A downward override (e.g. returning 'routine'
    # for an open brake job that the deterministic path correctly assessed as
    # 'attention') is a safety downgrade, not a legitimate improvement.  Upward
    # overrides (e.g. 'routine' → 'soon') are allowed — the LLM may spot nuance.
    _severity_order = {"routine": 0, "soon": 1, "attention": 2}

    for comp in enriched:
        if not isinstance(enriched[comp], dict):
            continue
        if "severity_hint" not in enriched[comp]:
            continue
        # Sanitise (P-level rejection still applies).
        enriched[comp] = _sanitise_component_result(enriched[comp])
        # Apply floor: if enriched severity is BELOW the deterministic floor, restore.
        det_severity = component_results.get(comp, {}).get("severity_hint", "routine")
        det_order = _severity_order.get(det_severity, 0)
        enr_order = _severity_order.get(enriched[comp]["severity_hint"], 0)
        if enr_order < det_order:
            log.warning(
                "health agent: LLM attempted to downgrade severity_hint for "
                "component %r from %r to %r — floor applied, keeping %r.",
                comp,
                det_severity,
                enriched[comp]["severity_hint"],
                det_severity,
            )
            enriched[comp]["severity_hint"] = det_severity

    # W2 cycle-2 (2026-10-04): re-attach the stripped brakes date caveat as a
    # trailing paragraph on the ENRICHED brakes finding.  The model never saw
    # the caveat, so it could not have promoted it to a leading sentence; the
    # re-attach restores the "finding first, caveat second" structure that
    # ``derive_headline`` relies on.  If parsing failed and the fallback came
    # back (``_parse_llm_response`` returning the fallback dict), that dict
    # already carries the body-only brakes finding (we overwrote
    # ``llm_components["brakes"]`` above before the parse), so this re-attach
    # restores the caveat on the fallback path too.
    if _stashed_brake_caveat is not None and "brakes" in enriched:
        brakes = enriched["brakes"]
        if isinstance(brakes, dict):
            cur_finding = brakes.get("finding", "") or ""
            # Guard against double-attach: a stub LLM that happens to echo the
            # caveat should still produce exactly one trailing caveat.
            if _stashed_brake_caveat not in cur_finding:
                brakes["finding"] = (
                    f"{cur_finding}\n\n{_stashed_brake_caveat}"
                )

    return enriched


def _parse_llm_response(
    response: str,
    fallback: dict[str, dict],
) -> dict[str, dict]:
    """Parse LLM JSON response into component result dicts.

    Returns *fallback* unchanged if parsing fails or the response is
    missing expected keys.

    T4.10 (b1): extracts the FIRST complete JSON object in the response, not
    the last ``}`` in the string.  The previous greedy ``re.search(r"\\{.*\\}",
    ..., re.DOTALL)`` extended from the first ``{`` to the LAST ``}`` in the
    response.  When the LLM wraps its JSON in Markdown or appends explanatory
    prose containing a second ``{...}`` block, the greedy match produces a
    string spanning both blocks that ``json.loads`` rejects, silently falling
    back to the deterministic result and discarding the LLM's output entirely.
    The fix scans forward from the first ``{`` using ``json.JSONDecoder.raw_decode``
    which stops at the end of the first well-formed JSON object.
    """
    # T4.10 (b1): extract the first complete, well-formed JSON object.
    # raw_decode(s, idx) returns (obj, end_idx) and raises JSONDecodeError if no
    # valid JSON starts at idx.  We scan forward to the first '{' and decode from
    # there; this ignores any prose or secondary JSON blocks that follow.
    _decoder = json.JSONDecoder()
    parsed = None
    for i, ch in enumerate(response):
        if ch == "{":
            try:
                parsed, _ = _decoder.raw_decode(response, i)
                break  # first well-formed object found — stop
            except json.JSONDecodeError:
                continue  # skip malformed starts; keep looking

    if parsed is None:
        return fallback

    result: dict[str, dict] = {}
    for comp in ("battery", "tires", "brakes"):
        if comp not in parsed or not isinstance(parsed[comp], dict):
            # Component missing in LLM response — keep deterministic finding.
            if comp in fallback:
                result[comp] = fallback[comp]
            continue

        llm_comp = parsed[comp]
        existing = fallback.get(comp, {})

        # Merge: prefer LLM finding text if present, keep deterministic evidence.
        result[comp] = {
            "finding": llm_comp.get("finding") or existing.get("finding", ""),
            "severity_hint": llm_comp.get("severity_hint") or existing.get("severity_hint", "routine"),
            "confidence": llm_comp.get("confidence") or existing.get("confidence", "low"),
            "evidence": existing.get("evidence", []),  # never replace evidence from LLM
        }

    # Any fallback components not processed above.
    for comp in fallback:
        if comp not in result:
            result[comp] = fallback[comp]

    return result


# ---------------------------------------------------------------------------
# Fleet-owner scope-write helper — Addendum D1
# ---------------------------------------------------------------------------

#: Mapping from component name to AVX finding_kind string.
#: Must stay in sync with runner._component_to_finding_kind().
_COMPONENT_TO_FINDING_KIND: dict[str, str] = {
    "battery": "health.battery",
    "tires": "health.tires",
    "brakes": "health.brakes",
}

#: Agent identifier used in finding_key derivation.
_AGENT_ID = "agent-2-vehicle-health"


def assess_fleet_health(
    fleet_id: str,
    vin_data: list[dict],
    *,
    _llm_caller: Optional[Callable] = None,
) -> dict[str, dict]:
    """Assess health for an entire fleet and emit fleet-scoped findings (Addendum D1).

    **Addendum D1 — fleet_owner write rule:**
    For a ``fleet_owner`` persona, Agent 2 emits EXACTLY ONE ``scope.fleet_id``-scoped
    Finding per ``(fleet_id, finding_kind)`` rather than N per-VIN Findings.
    The fleet-scoped Finding's ``evidence[]`` carries per-VIN entries, each with a
    ``vin`` attribution field so the iOS renderer can surface per-vehicle details.

    This function is the write-path branch for ``fleet_owner`` inputs.  The
    per-VIN path (``assess_vehicle_health``) continues to serve ``consumer`` and
    ``vehicle_owner`` inputs unchanged.

    The ``finding_key`` for a fleet-scoped Finding is derived as::

        compute_finding_key(
            agent_id=_AGENT_ID,
            scope_field="fleet_id",
            scope_value=fleet_id,
            finding_kind=<finding_kind>,
        )

    This mirrors the per-VIN derivation in ``runner._build_artifact`` with
    ``scope_field="vin"`` — the in-place-update semantics are identical.

    Parameters
    ----------
    fleet_id:
        Fleet identifier (e.g. ``"FLEET-MERIDIAN-001"``).
    vin_data:
        List of per-VIN input dicts.  Each dict MUST contain ``"vin"`` and MAY
        contain any combination of:

        - ``"battery_rows"`` — list of battery rows (passed to
          ``assess_vehicle_health`` as ``battery_rows``).
        - ``"tire_rows"`` — list of tire rows (passed as ``tire_rows``).
        - ``"brake_data"`` — single brake dict (passed as ``brake_data``).

        If none of the optional keys are present, a battery absent-data finding
        is generated for that VIN.
    _llm_caller:
        Optional LLM callable injected for tests.  Passed through to
        ``assess_vehicle_health`` unchanged.

    Returns
    -------
    dict[str, dict]
        Mapping of finding_kind → fleet-scoped finding dict::

            {
                "health.battery": {
                    "scope": {"fleet_id": fleet_id},
                    "finding_kind": "health.battery",
                    "finding_key": <64-char hex>,
                    "severity_hint": "soon",
                    "confidence": "medium",
                    "evidence": [
                        {"vin": "VIN-001", "severity": "routine", ...},
                        {"vin": "VIN-002", "severity": "attention", ...},
                    ],
                    "finding": "Fleet battery summary: ...",
                },
                ...
            }

        Only components with data in at least one VIN are present as keys.

    Raises
    ------
    ValueError
        If ``fleet_id`` is empty or ``vin_data`` is empty.
    """
    if not fleet_id:
        raise ValueError("fleet_id must be a non-empty string")
    if not vin_data:
        raise ValueError("vin_data must be non-empty for a fleet health assessment")

    # -- Step 1: assess each VIN individually --------------------------------
    # Collect per-VIN results keyed by component.
    # per_component[component][vin] = component_result_dict
    per_component: dict[str, list[dict]] = {
        "battery": [],
        "tires": [],
        "brakes": [],
    }

    for entry in vin_data:
        vin = entry.get("vin")
        if not vin:
            log.warning("assess_fleet_health: skipping entry with missing 'vin': %r", entry)
            continue

        result = assess_vehicle_health(
            vin,
            battery_rows=entry.get("battery_rows"),
            tire_rows=entry.get("tire_rows"),
            brake_data=entry.get("brake_data"),
            _llm_caller=_llm_caller,
        )

        for component in ("battery", "tires", "brakes"):
            comp_result = result.get(component)
            if comp_result and isinstance(comp_result, dict):
                per_component[component].append({"vin": vin, **comp_result})

    # -- Step 2: aggregate into ONE fleet-scoped finding per component -------
    from agents.tier2.writer import compute_finding_key  # lazy import

    fleet_findings: dict[str, dict] = {}

    for component, vin_results in per_component.items():
        if not vin_results:
            continue

        finding_kind = _COMPONENT_TO_FINDING_KIND[component]

        # Derive the fleet-scoped finding_key (D2 identity for fleet updates).
        finding_key = compute_finding_key(
            agent_id=_AGENT_ID,
            scope_field="fleet_id",
            scope_value=fleet_id,
            finding_kind=finding_kind,
        )

        # Aggregate severity: take the worst across all VINs.
        severity_order = ["routine", "soon", "attention"]
        worst_severity = "routine"
        for vr in vin_results:
            sv = vr.get("severity_hint", "routine")
            if sv in severity_order:
                if severity_order.index(sv) > severity_order.index(worst_severity):
                    worst_severity = sv

        # Aggregate confidence: take the worst (lowest) across all VINs.
        confidence_order = ["low", "medium", "high"]
        worst_confidence = "high"
        for vr in vin_results:
            cf = vr.get("confidence", "low")
            if cf in confidence_order:
                if confidence_order.index(cf) < confidence_order.index(worst_confidence):
                    worst_confidence = cf

        # Build per-VIN evidence entries with ``vin`` attribution (D1 requirement).
        # Each evidence entry merges the VIN's component evidence with a ``vin`` field
        # and a ``severity`` / ``confidence`` summary field for the renderer.
        fleet_evidence: list[dict] = []
        for vr in vin_results:
            vin_evidence_entries = vr.get("evidence", [])
            # Wrap all per-VIN evidence into a single per-VIN fleet evidence entry.
            # The schema requires each fleet evidence entry to carry ``vin`` (D1,
            # allOf[0]).  We emit one evidence entry per VIN carrying the VIN's
            # summary + its raw evidence as a ``vin_evidence`` sub-list.
            fleet_evidence.append({
                "vin": vr["vin"],
                "severity": vr.get("severity_hint", "routine"),
                "confidence": vr.get("confidence", "low"),
                "finding_summary": vr.get("finding", ""),
                "vin_evidence": vin_evidence_entries,
                # D1 schema: per-entry vin field (required when scope.fleet_id)
            })

        # Fleet-level finding summary text.
        vin_count = len(vin_results)
        attention_vins = [vr["vin"] for vr in vin_results if vr.get("severity_hint") == "attention"]
        soon_vins = [vr["vin"] for vr in vin_results if vr.get("severity_hint") == "soon"]

        if attention_vins:
            finding_text = (
                f"Fleet {component} health ({vin_count} vehicles): "
                f"{len(attention_vins)} vehicle(s) need attention "
                f"({', '.join(attention_vins[:3])}{'...' if len(attention_vins) > 3 else ''}). "
                f"{len(soon_vins)} vehicle(s) due soon."
            )
        elif soon_vins:
            finding_text = (
                f"Fleet {component} health ({vin_count} vehicles): "
                f"{len(soon_vins)} vehicle(s) due for service soon "
                f"({', '.join(soon_vins[:3])}{'...' if len(soon_vins) > 3 else ''}). "
                "No immediate attention required."
            )
        else:
            finding_text = (
                f"Fleet {component} health ({vin_count} vehicles): "
                "all vehicles show routine status. No service action required."
            )

        fleet_findings[finding_kind] = {
            "scope": {"fleet_id": fleet_id},
            "finding_kind": finding_kind,
            "finding_key": finding_key,
            "severity_hint": worst_severity,
            "confidence": worst_confidence,
            "evidence": fleet_evidence,
            "finding": finding_text,
        }

    return fleet_findings


# ---------------------------------------------------------------------------
# DMS warranty verdict fetch — the post-service-visit narration trigger
# ---------------------------------------------------------------------------

#: Region for DMS DynamoDB reads. ``us-west-2`` per DMS `decisions.md` 2026-08-28,
#: which records ``us-east-1`` as the wrong default. Same value the Tier 1
#: ``dms_warranty`` tool pins.
_DMS_REGION = os.environ.get("VSA_DMS_REGION", "us-west-2")

#: Cached boto3 DynamoDB client for DMS reads. Module-level so one Tier 2 run reuses
#: one client across its VIN batch rather than constructing one per VIN.
_dms_ddb_singleton: Any = None


def _dms_ddb_client() -> Any:
    """Return a cached DynamoDB client for DMS's region.

    boto3 is imported lazily so this module stays importable in test environments that do
    not have boto3 on the path — the same reason ``agents/tier2/registry.py`` defers its
    imports.
    """
    global _dms_ddb_singleton
    if _dms_ddb_singleton is None:
        import boto3

        _dms_ddb_singleton = boto3.client("dynamodb", region_name=_DMS_REGION)
    return _dms_ddb_singleton


def fetch_warranty_verdict_phrase(vin: str) -> Optional[str]:
    """Return the DMS warranty verdict to narrate for *vin*, or ``None``.

    This is the seam that makes Agent 2's post-service-visit narration path reachable.
    Before Fix Group 21 the path was complete, tested and deployed with zero callers — 18
    non-test call sites of :func:`assess_vehicle_health` and not one passed
    ``warranty_verdict_phrase``.

    **Every failure degrades to ``None``.** A warranty lookup is additive evidence; a
    vehicle's battery, tire and brake assessment must not be lost because a cross-account
    DynamoDB read was denied, throttled, or misconfigured. The failure is logged and the
    assessment proceeds byte-identically to the pre-Fix-Group-21 behaviour.

    That includes :exc:`~agents.health.dms_warranty_source.DmsWarrantyConfigError`, which is
    the expected state today: the Tier 2 Lambda role has no grant on the DMS tables yet
    (Fix Group 21 task 3), and the CDK injects no table-name env vars, so this returns
    ``None`` on every call in the current deployment. That is the correct intermediate
    state, not a regression — but it does mean a green test suite says nothing about whether
    this works against AWS. See the spec's `decisions.md` F4/F5.

    Returns
    -------
    str | None
        The ``verdict_status`` value — ``"approved"``, ``"denied"`` or
        ``"requires_review"`` — to be narrated **verbatim**, or ``None`` when there is no
        verdict. ``None`` must never be replaced with a default: a fabricated verdict is a
        false statement about a money decision.
    """
    try:
        from agents.health.dms_warranty_source import ATTR_VERDICT_STATUS, fetch_verdict

        verdict = fetch_verdict(vin, ddb_client=_dms_ddb_client())
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. The exhaustive list of what a cross-account DDB read can
        # raise (ClientError, BotoCoreError, EndpointConnectionError, NoCredentialsError,
        # ImportError when boto3 is absent, DmsWarrantyConfigError, DmsWarrantyVerdictError)
        # is long, and any omission would turn a warranty-lookup failure into a lost health
        # assessment — the exact degradation this function exists to prevent.
        log.warning(
            "health agent: warranty verdict fetch failed (%s: %s); "
            "continuing without warranty narration.",
            type(exc).__name__,
            exc,
        )
        return None

    if verdict is None:
        return None
    # Keyed by the source module's own constant rather than a re-spelled literal: that
    # module states the attribute name "appears once", and a second spelling here across a
    # module boundary would have quietly falsified it (review Cycle 1, Suggestion 4).
    return verdict.get(ATTR_VERDICT_STATUS)


# ---------------------------------------------------------------------------
# runner.py adapter — run_health_agent(vin, rows) -> dict
# ---------------------------------------------------------------------------


def run_health_agent(vin: str, rows: list[dict] | dict) -> dict[str, Any]:
    """Adapter for the runner.py ``agent_fn(vin, rows)`` call convention.

    The runner calls this as ``agent_fn(vin, vin_row_list)`` where
    ``vin_row_list`` is a list of rows from a single component source.

    To determine the component type, this function inspects the row keys:
    - Contains ``state_of_health_pct`` → battery (energy_usage)
    - Contains ``replacement_flagged`` or ``wear_band`` → tires component
    - Contains ``outcome`` and ``service_date`` → brakes component

    For the multi-component case, ``rows`` may be a dict with keys
    ``{"component": str, "data": list|dict}`` — used when the runner is
    extended to pass all components together.

    Returns the standard runner-compatible dict:
    ``{finding, severity_hint, confidence, evidence}``
    plus ``_tokens_used`` and ``_cost_usd`` for the budget cap.

    This is the function imported by ``runner._default_agent_fn()``.

    **Warranty narration (Fix Group 21).** The DMS warranty verdict for *vin* is fetched
    once here and forwarded to every ``assess_vehicle_health`` call as
    ``warranty_verdict_phrase``. Fetched once rather than per component because it is a
    per-vehicle fact, and a second lookup would be two cross-account reads for one answer.
    ``None`` — no claim, no verdict, or any failure — leaves the assessment byte-identical
    to its pre-Fix-Group-21 output. See :func:`fetch_warranty_verdict_phrase`.
    """
    # Fetched before the component branch so all paths share one lookup, including the
    # early returns for absent/unknown data.
    verdict_phrase = fetch_warranty_verdict_phrase(vin)

    if isinstance(rows, dict) and "component" in rows:
        # Multi-component call convention (runner extension).
        component = rows["component"]
        data = rows.get("data", [])
        if component == "battery":
            result = assess_vehicle_health(
                vin,
                battery_rows=data if isinstance(data, list) else [],
                warranty_verdict_phrase=verdict_phrase,
            )
            return _extract_single_component(result, "battery")
        elif component == "tires":
            result = assess_vehicle_health(
                vin,
                tire_rows=data if isinstance(data, list) else [],
                warranty_verdict_phrase=verdict_phrase,
            )
            return _extract_single_component(result, "tires")
        elif component == "brakes":
            brake_data = data if isinstance(data, dict) else None
            result = assess_vehicle_health(
                vin, brake_data=brake_data, warranty_verdict_phrase=verdict_phrase
            )
            return _extract_single_component(result, "brakes")
        else:
            log.warning("run_health_agent: unknown component %r — using battery fallback", component)
            result = assess_vehicle_health(
                vin, battery_rows=[], warranty_verdict_phrase=verdict_phrase
            )
            return _extract_single_component(result, "battery")

    # Single-component path: detect from row shape.
    row_list: list[dict] = rows if isinstance(rows, list) else []

    if not row_list:
        result = assess_vehicle_health(
            vin, battery_rows=[], warranty_verdict_phrase=verdict_phrase
        )
        return _extract_single_component(result, "battery")

    first = row_list[0] if row_list else {}
    keys = set(first.keys())

    if _KEY_HEALTH_PCT in keys or _KEY_AGE_DAYS in keys:
        result = assess_vehicle_health(
            vin, battery_rows=row_list, warranty_verdict_phrase=verdict_phrase
        )
        return _extract_single_component(result, "battery")
    elif _KEY_REPLACEMENT_FLAGGED in keys or _KEY_WEAR_BAND in keys:
        result = assess_vehicle_health(
            vin, tire_rows=row_list, warranty_verdict_phrase=verdict_phrase
        )
        return _extract_single_component(result, "tires")
    elif _COL_OUTCOME in keys or _COL_SERVICE_DATE in keys:
        brake_data = row_list[0] if row_list else None
        result = assess_vehicle_health(
            vin, brake_data=brake_data, warranty_verdict_phrase=verdict_phrase
        )
        return _extract_single_component(result, "brakes")
    else:
        # Unknown shape — treat as battery absent data.
        log.warning(
            "run_health_agent: could not detect component from row keys %s; "
            "treating as absent battery data.",
            sorted(keys),
        )
        result = assess_vehicle_health(
            vin, battery_rows=[], warranty_verdict_phrase=verdict_phrase
        )
        return _extract_single_component(result, "battery")


def _extract_single_component(
    multi_result: dict[str, Any], component: str
) -> dict[str, Any]:
    """Extract the single-component dict from ``assess_vehicle_health`` output.

    Adds ``_tokens_used=0`` and ``_cost_usd=0.0`` for the runner budget cap.
    """
    comp_result = multi_result.get(component, {})
    if not comp_result:
        comp_result = {
            "finding": f"{component.capitalize()} assessment unavailable.",
            "severity_hint": "routine",
            "confidence": "low",
            "evidence": [],
        }
    out = {
        "finding": comp_result.get("finding", ""),
        "severity_hint": comp_result.get("severity_hint", "routine"),
        "confidence": comp_result.get("confidence", "low"),
        "evidence": comp_result.get("evidence", []),
        "_tokens_used": 0,
        "_cost_usd": 0.0,
    }
    # Set by the coverage-verdict guard: the headline it checked, which the runner
    # writes as-is (agents.tier2.card_text).
    if isinstance(comp_result.get("headline"), str) and comp_result["headline"]:
        out["headline"] = comp_result["headline"]
    return out


# ---------------------------------------------------------------------------
# Strands production entry point (lazy-imported in runner._default_agent_fn)
# ---------------------------------------------------------------------------

#: Per-million-token rates used to estimate `cost_usd` on Tier 2 artifacts.
#:
#: These are ESTIMATES and are deliberately overridable without a code change.
#: They exist because the Tier 2 artifact contract carries `cost_usd`, and a
#: hardcoded 0.0 there is worse than an approximation: it reads as "this run was
#: free" rather than "nobody measured". The figure is for operator cost
#: attribution, not billing, and the authority on actual spend is Cost Explorer.
#:
#: Defaults are the widely-published Claude Sonnet list rates ($3 per 1M input,
#: $15 per 1M output). They are NOT read from a pricing API, so they will drift
#: when pricing changes or when the model id changes — `make_strands_agent_fn`'s
#: model_id default and these rates are two independent values that must be kept
#: consistent by hand. An operator who needs accuracy should set the env vars.
#:
#: <!-- verify: aws pricing get-products --service-code AmazonBedrock, or the
#:      Bedrock pricing page for the model id in make_strands_agent_fn -->
_COST_PER_1M_INPUT_TOKENS_USD_DEFAULT = 3.00
_COST_PER_1M_OUTPUT_TOKENS_USD_DEFAULT = 15.00


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for one LLM call from its token counts.

    Rates come from ``TIER2_COST_PER_1M_INPUT_TOKENS_USD`` and
    ``TIER2_COST_PER_1M_OUTPUT_TOKENS_USD`` when set, else the module defaults.

    Returns 0.0 for non-positive token counts, so a run with no LLM call reports
    a genuine zero rather than a rounding artefact. A malformed env value falls
    back to the default rather than raising — cost is telemetry, and the budget
    cap enforces the real ceiling on tokens, not on this estimate.
    """
    def _rate(env_name: str, default: float) -> float:
        raw = os.environ.get(env_name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            log.warning(
                "health agent: %s=%r is not a number; using default %s.",
                env_name, raw, default,
            )
            return default
        if value < 0:
            log.warning(
                "health agent: %s=%r is negative; using default %s.",
                env_name, raw, default,
            )
            return default
        return value

    in_tokens = max(0, int(input_tokens or 0))
    out_tokens = max(0, int(output_tokens or 0))
    if in_tokens == 0 and out_tokens == 0:
        return 0.0

    in_rate = _rate(
        "TIER2_COST_PER_1M_INPUT_TOKENS_USD",
        _COST_PER_1M_INPUT_TOKENS_USD_DEFAULT,
    )
    out_rate = _rate(
        "TIER2_COST_PER_1M_OUTPUT_TOKENS_USD",
        _COST_PER_1M_OUTPUT_TOKENS_USD_DEFAULT,
    )
    return (in_tokens / 1_000_000.0) * in_rate + (out_tokens / 1_000_000.0) * out_rate


def make_strands_agent_fn(
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    *,
    max_llm_calls_per_batch: Optional[int] = None,
    wall_deadline_fn: Optional[Callable[[], float]] = None,
    wall_safety_per_call_seconds: Optional[float] = None,
    wall_per_vin_write_seconds: Optional[float] = None,
) -> Callable:
    """Return a production agent function backed by a Strands LLM.

    Lazy-imported at call time so that the module can be imported in
    test environments without a Strands/Bedrock dependency.

    Parameters
    ----------
    model_id:
        Bedrock model ID for the Strands ``BedrockModel``.
    max_llm_calls_per_batch:
        Hard circuit-breaker on the number of LLM invocations in one batch
        (one process lifetime of the returned agent_fn).  After this many
        LLM calls, subsequent VINs fall back to the deterministic finding
        and the runner's budget cap never trips on wall-clock during a
        normal batch.  ``None`` (default) reads
        ``TIER2_MAX_LLM_CALLS_PER_BATCH`` from the environment (default 100);
        pass an explicit int to override.
        Why a per-batch counter and not a token budget: wall-clock, not
        tokens, binds a batch of ~2,000 VINs at ~3-5s per Bedrock call.
    wall_deadline_fn:
        Optional callable returning REMAINING wall-clock seconds for the run.
        W3 cycle-2 (2026-10-04): the call-count ceiling alone cannot stop a
        slow run from tripping the runner's ``TIER2_MAX_WALL_SECONDS`` cap —
        tires and brakes selection both pick only non-routine VINs on this
        fleet, so every nightly run reaches the full 100-call circuit limit,
        and at 4-6.6 s per Bedrock call that is 430-690 s against a 600 s wall
        budget.  When ``wall_deadline_fn`` is supplied, ``_llm_caller`` short-
        circuits before any call that would plausibly exceed the remaining
        wall budget, falling back to the deterministic finding instead.
        ``None`` (default): no time-awareness — the call-count ceiling is the
        only guard (safe default for unit tests that do not inject a cap).
    wall_safety_per_call_seconds:
        Per-call safety margin in seconds.  Reserved for the current LLM
        call itself (a slow outlier that happens to be the one running when
        the breaker fires).  ``None`` (default) reads
        ``TIER2_WALL_SAFETY_PER_CALL_SECONDS`` from the environment, which
        defaults to 10 s — above the observed 4-6.6 s per-call latency with
        headroom for a slow outlier.
    wall_per_vin_write_seconds:
        Review cycle 3 Critical (2026-10-04): the per-call safety margin
        above is NOT enough on its own.  When the breaker trips with ~1,900
        VINs still to process, each remaining VIN still runs a legacy put,
        an owner lookup and an AVX write (measured ~0.1 s per VIN on
        staging, up from ~9 ms on the pre-2026-09-23 single-put path).  So
        for brakes a 10 s margin can leave ~190 s of writes to do inside a
        10 s budget — which is what cycle 3 observed on the shared-fake-
        clock simulation at 4-6.6 s per model call.  The breaker now trips
        when remaining wall time falls below
        ``safety + remaining_vins * wall_per_vin_write_seconds``, where
        ``remaining_vins`` is set by the runner via the returned agent_fn's
        ``remaining_vins`` attribute before each VIN call.  ``None``
        (default) reads ``TIER2_WALL_PER_VIN_WRITE_SECONDS`` from the
        environment, which defaults to 0.1 s — above the measured ~0.05-
        0.096 s per VIN, with headroom for a slow outlier.

    Returns
    -------
    Callable
        ``(vin, rows) -> dict`` — compatible with runner ``agent_fn`` signature.

    Behaviour (C1 + C2 fix — review cycle 1, 2026-10-04):
        - A fresh ``Agent`` is constructed for every VIN.  The previous
          shape re-used one Agent across the entire batch, which carried a
          40-message ``SlidingWindowConversationManager`` by default
          (strands-agents==1.39.0).  With D2 (2026-10-04) now sending the
          deterministic finding text, each VIN's call carried the SoH %,
          service dates and DTC codes of up to 20 prior vehicles — a
          correctness defect (another vehicle's data narrated as this
          vehicle's), not just a budget one.
        - ``AgentResult.metrics.accumulated_usage`` is a cumulative-over-
          Agent-lifetime counter.  A per-call Agent means ``accumulated_usage``
          IS the per-call usage, so the ``+=`` into ``_usage`` here is the
          correct running total across the batch, and the delta read at the
          wrapper boundary (``_agent_fn_recording_usage``) is the real
          per-VIN usage.  The pre-fix shape summed the lifetime-cumulative
          counter on every call, inflating the metered total ~75× against
          actual (reviewer: tires actual 1.32M tokens → metered 97.97M;
          battery actual 3.32M → metered 609.27M).
    """
    try:
        from strands import Agent  # type: ignore[import]
        from strands.models import BedrockModel  # type: ignore[import]

        # One BedrockModel per batch is fine — it is the *Agent* that carries
        # conversation state via its ConversationManager, not the model.
        # Creating the Agent per call (below) keeps the per-call Bedrock
        # client overhead constant while isolating conversation history.
        bedrock_model = BedrockModel(model_id=model_id)

        # Running token totals ACROSS the batch.  With a fresh Agent per call
        # (below), each ``response.metrics.accumulated_usage`` is the per-call
        # usage, so ``+=`` into _usage gives the correct batch total.  The
        # per-VIN delta is taken at the wrapper boundary below.
        _usage: dict[str, int] = {"input": 0, "output": 0, "total": 0}

        # Resolve the per-batch LLM-call ceiling.  Env var is read here so a
        # single stack-env change can tune it without a code deploy.
        if max_llm_calls_per_batch is None:
            _max_llm_calls = int(
                os.environ.get("TIER2_MAX_LLM_CALLS_PER_BATCH", "100")
            )
        else:
            _max_llm_calls = int(max_llm_calls_per_batch)
        _llm_call_count: dict[str, int] = {"n": 0}

        # W3 cycle-2: per-call wall-time safety margin.  When the remaining
        # wall budget is below this margin, we skip the call rather than
        # starting it and risking a wall-cap abort mid-call.
        if wall_safety_per_call_seconds is None:
            _wall_safety = float(
                os.environ.get("TIER2_WALL_SAFETY_PER_CALL_SECONDS", "10.0")
            )
        else:
            _wall_safety = float(wall_safety_per_call_seconds)

        # W3 cycle-3 (2026-10-04 Critical): per-VIN WRITE cost reserved for
        # the rest of the batch.  Cycle-2 only reserved time for one more
        # model call; after the breaker tripped, every remaining VIN still
        # ran a legacy put + owner lookup + AVX write, which at ~0.05-0.1 s
        # per VIN on staging comes to ~100-200 s for a brakes batch.  So the
        # breaker now trips when the remaining wall budget is below
        # safety + remaining_vins × per_vin_write_seconds.
        if wall_per_vin_write_seconds is None:
            _wall_per_vin_write = float(
                os.environ.get("TIER2_WALL_PER_VIN_WRITE_SECONDS", "0.1")
            )
        else:
            _wall_per_vin_write = float(wall_per_vin_write_seconds)

        # Remaining-VIN count the runner updates before each ``agent_fn``
        # call.  Default 0 → no write-cost reservation (safe when the runner
        # does not set it: the per-call safety margin alone still bounds
        # the current call).  Mutable dict so a lambda reads live values.
        _remaining_vins: dict[str, int] = {"n": 0}

        # Breaker trip state — logged once per batch rather than per skipped
        # VIN (reviewer Cycle-2 Suggestion 3: a brakes run logging the trip
        # per VIN produced ~1,900 INFO lines).  The count fields are exposed
        # on the agent_fn via attributes so the runner can carry them to the
        # run record.
        _skipped_by_breaker: dict[str, int] = {"count": 0}
        _trip_logged: dict[str, bool] = {"done": False}
        _trip_reason: dict[str, str] = {"reason": ""}

        def _trip_breaker(reason: str) -> _LlmCallCircuitBreakerError:
            """Record a breaker trip (log once per batch) and build the error."""
            if not _trip_logged["done"]:
                _trip_logged["done"] = True
                _trip_reason["reason"] = reason
                log.info("health agent: LLM circuit breaker tripped — %s", reason)
            _skipped_by_breaker["count"] += 1
            return _LlmCallCircuitBreakerError(reason)

        def _llm_caller(system_prompt: str, user_prompt: str) -> str:
            # W3 cycle-2: time-aware breaker — check wall budget BEFORE the
            # call-count check so a slow run with headroom on the call count
            # still trips on time.
            #
            # W3 cycle-3: the required margin includes the writes that the
            # runner will do for every remaining VIN after this call.  Without
            # it, a brakes batch at 6.6 s/model-call still aborts on wall time
            # at ~0.05 s/VIN × ~1,900 remaining writes = ~95 s of work left
            # inside a 10 s per-call safety margin.
            if wall_deadline_fn is not None:
                try:
                    remaining = float(wall_deadline_fn())
                except Exception:  # noqa: BLE001 — telemetry must not break the run
                    remaining = float("inf")
                remaining_vins = int(_remaining_vins.get("n", 0) or 0)
                required = _wall_safety + remaining_vins * _wall_per_vin_write
                if remaining <= required:
                    raise _trip_breaker(
                        f"wall budget low ({remaining:.1f}s remaining, "
                        f"need {required:.1f}s = per-call safety "
                        f"{_wall_safety:.1f}s + {remaining_vins} remaining "
                        f"writes × {_wall_per_vin_write:.3f}s each); "
                        f"falling back to deterministic findings for the "
                        f"remainder of this batch."
                    )
            # C5 call-count circuit breaker: cap the LLM call count per batch.
            # Once exceeded, raise so _enrich_with_llm falls back to the
            # deterministic finding for the remainder of this batch (its
            # explicit ``except _LlmCallCircuitBreakerError`` branch covers
            # this path).  The runner continues to the next VIN.
            if _llm_call_count["n"] >= _max_llm_calls:
                raise _trip_breaker(
                    f"LLM call count {_llm_call_count['n']} has reached the "
                    f"per-batch limit of {_max_llm_calls}; falling back to "
                    f"deterministic findings for the remainder of this batch."
                )
            _llm_call_count["n"] += 1

            # C1 fix: a fresh Agent per call isolates conversation history.
            # The system_prompt argument is honoured here rather than at
            # Agent construction so the signature of _llm_caller is unchanged
            # and the tests that drive it with a captured prompt still work.
            #
            # ``callback_handler=None`` (Suggestion, Cycle 2): Strands' default
            # PrintingCallbackHandler streams every model reply to stdout with
            # no trailing newline, so owner-facing finding text would land
            # verbatim in CloudWatch for every LLM-called VIN (and glue to the
            # next log line, producing the 2026-10-04 "model text + JSON" lines).
            per_call_agent = Agent(
                model=bedrock_model,
                system_prompt=system_prompt,
                callback_handler=None,
            )
            response = per_call_agent(user_prompt)
            # Accumulate real token usage. Verified against the installed SDK
            # rather than assumed: AgentResult.metrics is an EventLoopMetrics
            # whose accumulated_usage is a strands.types.event_loop.Usage with
            # keys inputTokens / outputTokens / totalTokens (plus cache counters).
            #
            # With the per-call Agent above, accumulated_usage IS the per-call
            # usage; ``+=`` therefore gives the correct batch running total and
            # the wrapper's delta (below) is the real per-VIN usage.
            #
            # Wrapped broadly on purpose: usage is telemetry, and a finding must
            # never be lost because a metrics field moved in an SDK upgrade. The
            # cost of a miss is a 0 in the meter, which the caller can see.
            try:
                usage = response.metrics.accumulated_usage
                _usage["input"] += int(usage.get("inputTokens", 0) or 0)
                _usage["output"] += int(usage.get("outputTokens", 0) or 0)
                _usage["total"] += int(usage.get("totalTokens", 0) or 0)
            except Exception:  # noqa: BLE001 — telemetry must not break a finding
                log.warning(
                    "health agent: could not read token usage from the Strands "
                    "response; tokens_used will under-report for this VIN."
                )
            return str(response)

        def _agent_fn(vin: str, rows: list[dict]) -> dict:
            """Strands-backed agent_fn; passes LLM caller through to assess."""
            if isinstance(rows, dict) and "component" in rows:
                component = rows["component"]
                data = rows.get("data", [])
                if component == "battery":
                    full = assess_vehicle_health(
                        vin, battery_rows=data if isinstance(data, list) else [],
                        _llm_caller=_llm_caller
                    )
                elif component == "tires":
                    full = assess_vehicle_health(
                        vin, tire_rows=data if isinstance(data, list) else [],
                        _llm_caller=_llm_caller
                    )
                elif component == "brakes":
                    full = assess_vehicle_health(
                        vin,
                        brake_data=data if isinstance(data, dict) else None,
                        _llm_caller=_llm_caller
                    )
                else:
                    full = assess_vehicle_health(vin, battery_rows=[], _llm_caller=_llm_caller)
                return _extract_single_component(full, component)

            # Shape-detect path.
            row_list = rows if isinstance(rows, list) else []
            first = row_list[0] if row_list else {}
            keys = set(first.keys())
            if _KEY_HEALTH_PCT in keys or _KEY_AGE_DAYS in keys:
                full = assess_vehicle_health(vin, battery_rows=row_list, _llm_caller=_llm_caller)
                return _extract_single_component(full, "battery")
            elif _KEY_REPLACEMENT_FLAGGED in keys or _KEY_WEAR_BAND in keys:
                full = assess_vehicle_health(vin, tire_rows=row_list, _llm_caller=_llm_caller)
                return _extract_single_component(full, "tires")
            elif _COL_OUTCOME in keys or _COL_SERVICE_DATE in keys:
                full = assess_vehicle_health(
                    vin, brake_data=row_list[0] if row_list else None, _llm_caller=_llm_caller
                )
                return _extract_single_component(full, "brakes")
            else:
                full = assess_vehicle_health(vin, battery_rows=[], _llm_caller=_llm_caller)
                return _extract_single_component(full, "battery")

        def _agent_fn_recording_usage(vin: str, rows: list[dict]) -> dict:
            """Wrap ``_agent_fn`` and stamp the per-VIN token delta on its result.

            Wrapping here rather than editing each of ``_agent_fn``'s six
            ``_extract_single_component`` return sites means no return path can be
            missed, now or when a component is added.

            Why this exists at all: ``_extract_single_component`` hardcodes
            ``_tokens_used=0`` / ``_cost_usd=0.0``, and the runner reads exactly
            those keys (``runner.py`` pops ``_tokens_used``). So before this
            wrapper, a fully working LLM path still reported ``tokens_used=0`` —
            the meter was never wired, independently of whether ``strands`` was
            importable. Both causes had to be fixed for the number to become true;
            the missing Lambda bundle was the one the spec identified, and this
            was the other. Established by a live staging invocation on 2026-09-18
            that produced visibly LLM-authored prose while still reporting 0.
            """
            before = dict(_usage)
            out = _agent_fn(vin, rows)
            if isinstance(out, dict):
                delta_in = _usage["input"] - before["input"]
                delta_out = _usage["output"] - before["output"]
                delta_total = _usage["total"] - before["total"]
                # Prefer the SDK's own total; fall back to the parts if a model
                # reports components without a total.
                out["_tokens_used"] = delta_total or (delta_in + delta_out)
                out["_cost_usd"] = _estimate_cost_usd(delta_in, delta_out)
            return out

        # Expose the breaker counters via attributes so the runner can
        # carry them to the run record (reviewer Cycle-2 Suggestion 3:
        # llm_calls + llm_skipped_by_breaker).  Attributes are read, not
        # closed-over, so the runner reads them at run end and sees the
        # final values.
        _agent_fn_recording_usage.llm_calls = _llm_call_count  # type: ignore[attr-defined]
        _agent_fn_recording_usage.llm_skipped_by_breaker = _skipped_by_breaker  # type: ignore[attr-defined]
        _agent_fn_recording_usage.llm_breaker_trip_reason = _trip_reason  # type: ignore[attr-defined]
        # W3 cycle-3: the runner writes the count of VINs still to process
        # (including the current one) into this dict's ``n`` key before each
        # per-VIN call.  The breaker uses it to reserve enough wall time
        # for the writes that will follow a trip.  Exposed as an attribute,
        # not a closure, so a test (and the runner) can mutate it from
        # outside the factory.
        _agent_fn_recording_usage.remaining_vins = _remaining_vins  # type: ignore[attr-defined]

        return _agent_fn_recording_usage

    except ImportError:
        log.warning(
            "health agent: strands not available — falling back to deterministic agent."
        )
        return run_health_agent
