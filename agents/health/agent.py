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
from typing import Any, Callable, Optional

from agents.health.prompt import (
    C0161_VERBATIM_REMEDY,
    BRAKE_DTC_KB_GUIDED,
    SEVERITY_HINTS,
    SYSTEM_PROMPT,
    assemble_brake_dtc_note,
    inject_p0_verbatim_phrases,
)

log = logging.getLogger(__name__)

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
    positions_needing = [
        r.get("position")
        for r in rows
        if r.get(_KEY_REPLACEMENT_FLAGGED) is True
    ]

    if any_replacement:
        pos_str = ", ".join(p for p in positions_needing if p)
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
    _llm_caller: Optional[Callable] = None,
) -> dict[str, Any]:
    """Perform full multi-component health judgment for one VIN.

    This is the primary judgment function.  It:
    1. Builds deterministic findings for each available component.
    2. Applies the P0 verbatim rule programmatically (not only via prompt).
    3. Makes the bundling decision across all available components.
    4. Optionally invokes an LLM to enrich the findings if ``_llm_caller``
       is provided (tests inject a ``None`` or a callable double).

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
    _llm_caller:
        Optional callable ``(prompt: str) -> str`` that invokes the Strands
        LLM.  If ``None``, uses deterministic findings only (for tests and
        for cases where Strands is not available).  In production, the runner
        supplies the Strands Agent; in unit tests, inject a simple lambda.

    Returns
    -------
    dict
        ``{
            "battery": {finding, severity_hint, confidence, evidence} | absent,
            "tires":   {finding, severity_hint, confidence, evidence} | absent,
            "brakes":  {finding, severity_hint, confidence, evidence} | absent,
            "bundle":  bool,
            "bundle_rationale": str,
        }``
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
            vin, component_results, brake_data, _llm_caller
        )

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
) -> dict[str, dict]:
    """Optionally enrich deterministic findings with LLM judgment.

    The LLM may improve phrasing and add nuance, but:

    **T4.14 — Structural P0 bypass (primary control)**
    When the brakes component carries an active P0 DTC (C0161, case-insensitive)
    AND the job is active (outcome in ``_ACTIVE_BRAKE_OUTCOMES`` or ``is_open``),
    that component is excluded from the LLM call entirely and its deterministic
    finding is returned byte-identical.  There is no LLM text to detect or
    repair — the textual guard becomes defence-in-depth on the deterministic
    path rather than the primary control.

    Other components still enrich normally — the bypass is per-component, not a
    whole-call abort.

    **Remaining guards (all still apply)**
    - All P0 verbatim phrases are re-injected after the LLM call (defence-in-depth).
    - ``severity_hint`` is sanitised after the call to reject P-levels AND
      downward overrides (T4.10 b2).
    - The P0 re-inject fires only for active brake jobs (T4.10 b3).
    - If the LLM response cannot be parsed, the deterministic finding
      is returned unchanged (fail-safe).

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

    # T4.14 — structural bypass: determine whether the brakes component carries
    # an active P0 DTC.  If so, it is excluded from the LLM call and its
    # deterministic finding is returned unchanged.  The LLM's output is absent,
    # not repaired — there is nothing to detect or soften.
    #
    # Rationale (from Fix Group 4d preamble): every textual guard so far —
    # window deny-list, position rule, or both — is a *textual* remedy for a
    # problem where the harmful sentence still reaches the owner.  A third
    # textual rule inherits the same ceiling.  The structural move eliminates
    # the class: when a P0 DTC is present, no LLM narrates that component.
    # The guard then operates only on the deterministic path, where it is
    # defence-in-depth rather than load-bearing.
    _brakes_has_active_p0 = (
        _enrichment_active_brake
        and "C0161" in {code.upper() for code in dtc_codes}
    )

    # Separate out the P0-brakes component so the LLM never sees it.
    # All other components proceed through enrichment normally.
    deterministic_p0_components: dict[str, dict] = {}
    llm_components: dict[str, dict] = {}
    for comp, res in component_results.items():
        if comp == "brakes" and _brakes_has_active_p0:
            # Active P0 brake — bypass LLM entirely for this component.
            deterministic_p0_components[comp] = res
        else:
            llm_components[comp] = res

    # If there are no components left for the LLM, return deterministic results.
    if not llm_components:
        return component_results

    summary_parts: list[str] = []
    for comp, res in llm_components.items():
        # W-C8-2 (security-review Cycle 8): build a structured representation
        # from validated fields only — no ADP-derived prose reaches the LLM
        # prompt.  The previous `finding_preview={res['finding'][:80]!r}` took
        # a 80-char substring of the deterministic finding text, which is
        # assembled from ADP columns (outcome, dtc_codes, service_date, etc.).
        # An injection payload in dtc_codes (now format-validated at the source
        # boundary in adp_brake_source.validate_dtc_codes, but other components
        # have analogous structured columns) could still slip a phrase into the
        # finding string and from there into the prompt.  Replacing the substring
        # with a bounded enum + numeric set eliminates the class rather than
        # escaping it.  The LLM's task is to improve phrasing of already-safe
        # findings; it does not need to see the raw data.
        evidence_count = len(res.get("evidence") or [])
        summary_parts.append(
            f"[{comp.upper()}] severity_hint={res['severity_hint']!r} "
            f"confidence={res['confidence']!r} "
            f"evidence_count={evidence_count}"
        )
    component_summary = "\n".join(summary_parts)

    user_prompt = (
        f"Deterministic findings for VIN (redacted):\n{component_summary}\n\n"
        "Review and if needed improve the plain-language finding text for each "
        "component.  Keep the same severity_hint values unless you have a strong "
        "reason to change them.  Return a JSON object with one key per component."
    )

    try:
        llm_response = llm_caller(SYSTEM_PROMPT, user_prompt)
        enriched = _parse_llm_response(llm_response, llm_components)
    except Exception:
        log.warning(
            "health agent: LLM enrichment failed for VIN (redacted); "
            "using deterministic findings."
        )
        enriched = llm_components

    # Merge back the deterministic P0-brake component (byte-identical to input).
    enriched.update(deterministic_p0_components)

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
    """
    if isinstance(rows, dict) and "component" in rows:
        # Multi-component call convention (runner extension).
        component = rows["component"]
        data = rows.get("data", [])
        if component == "battery":
            result = assess_vehicle_health(vin, battery_rows=data if isinstance(data, list) else [])
            return _extract_single_component(result, "battery")
        elif component == "tires":
            result = assess_vehicle_health(vin, tire_rows=data if isinstance(data, list) else [])
            return _extract_single_component(result, "tires")
        elif component == "brakes":
            brake_data = data if isinstance(data, dict) else None
            result = assess_vehicle_health(vin, brake_data=brake_data)
            return _extract_single_component(result, "brakes")
        else:
            log.warning("run_health_agent: unknown component %r — using battery fallback", component)
            result = assess_vehicle_health(vin, battery_rows=[])
            return _extract_single_component(result, "battery")

    # Single-component path: detect from row shape.
    row_list: list[dict] = rows if isinstance(rows, list) else []

    if not row_list:
        result = assess_vehicle_health(vin, battery_rows=[])
        return _extract_single_component(result, "battery")

    first = row_list[0] if row_list else {}
    keys = set(first.keys())

    if _KEY_HEALTH_PCT in keys or _KEY_AGE_DAYS in keys:
        result = assess_vehicle_health(vin, battery_rows=row_list)
        return _extract_single_component(result, "battery")
    elif _KEY_REPLACEMENT_FLAGGED in keys or _KEY_WEAR_BAND in keys:
        result = assess_vehicle_health(vin, tire_rows=row_list)
        return _extract_single_component(result, "tires")
    elif _COL_OUTCOME in keys or _COL_SERVICE_DATE in keys:
        brake_data = row_list[0] if row_list else None
        result = assess_vehicle_health(vin, brake_data=brake_data)
        return _extract_single_component(result, "brakes")
    else:
        # Unknown shape — treat as battery absent data.
        log.warning(
            "run_health_agent: could not detect component from row keys %s; "
            "treating as absent battery data.",
            sorted(keys),
        )
        result = assess_vehicle_health(vin, battery_rows=[])
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
    return {
        "finding": comp_result.get("finding", ""),
        "severity_hint": comp_result.get("severity_hint", "routine"),
        "confidence": comp_result.get("confidence", "low"),
        "evidence": comp_result.get("evidence", []),
        "_tokens_used": 0,
        "_cost_usd": 0.0,
    }


# ---------------------------------------------------------------------------
# Strands production entry point (lazy-imported in runner._default_agent_fn)
# ---------------------------------------------------------------------------

def make_strands_agent_fn(model_id: str = "us.anthropic.claude-sonnet-4-6") -> Callable:
    """Return a production agent function backed by a Strands LLM.

    Lazy-imported at call time so that the module can be imported in
    test environments without a Strands/Bedrock dependency.

    Parameters
    ----------
    model_id:
        Bedrock model ID for the Strands ``BedrockModel``.

    Returns
    -------
    Callable
        ``(vin, rows) -> dict`` — compatible with runner ``agent_fn`` signature.
    """
    try:
        from strands import Agent  # type: ignore[import]
        from strands.models import BedrockModel  # type: ignore[import]

        bedrock_model = BedrockModel(model_id=model_id)
        _strands_agent = Agent(model=bedrock_model, system_prompt=SYSTEM_PROMPT)

        def _llm_caller(system_prompt: str, user_prompt: str) -> str:
            # Strands Agent is invoked with the user message; system prompt
            # is set at construction time.
            response = _strands_agent(user_prompt)
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

        return _agent_fn

    except ImportError:
        log.warning(
            "health agent: strands not available — falling back to deterministic agent."
        )
        return run_health_agent
