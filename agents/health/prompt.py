"""Prompt templates and deterministic phrase constants for the health agent.

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D8

Deterministic seams
-------------------
Section § D8 states that where a finding coincides with a triage P0, the
deterministic classifier's phrase is narrated **verbatim**.  Softening
"stop driving now" into "exercise caution" is a boundary violation even
when the verdict itself is preserved.

This module is the single source of truth for every P0 verbatim phrase.
The agent assembles finding text by referencing these constants — not by
asking the LLM to paraphrase them.  The boundary is therefore enforced by
code rather than by a prompt instruction (which the doctrine warns will be
crossed).

Test-enforced invariant
-----------------------
``agents/health/tests/test_agent.py`` asserts that a brake finding carrying
``C0161`` produces a ``finding`` field containing
``C0161_VERBATIM_REMEDY`` verbatim.  A test that merely checks the verdict
is preserved while the phrase is softened would miss the boundary violation;
the test checks the string itself.

P-level exclusion
-----------------
``severity_hint`` must never carry a P-level (P0/P1/P2/P3).  Those labels
belong to the triage classifier; the health agent's advisory vocabulary is
``routine | soon | attention``.  This is also test-enforced in
``test_agent.py``.

ADP KB coverage
---------------
Of the ten brake DTC codes in ADP's ``BRAKE_DTC_CODES``, only ``C0161``
and ``C0040`` have deployed Bedrock KB guides.  The agent must not invent
explanations for the other eight.  ``BRAKE_DTC_KB_GUIDED`` names the
covered set; ``assemble_brake_dtc_note`` returns a note only for codes in
that set, and is silent otherwise.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# P0 verbatim phrases — the deterministic seam content
# ---------------------------------------------------------------------------

#: Verbatim remedy text for DTC C0161 (ABS Brake Switch Circuit Malfunction
#: / critical brake pressure loss).
#:
#: ADP's knowledge base defines this as severity P0 with this exact remedy.
#: This string MUST appear verbatim in any finding that involves C0161.
#: Softening it is a spec § D8 boundary violation.
#:
#: Test-enforced: test_agent.py::test_c0161_finding_contains_verbatim_p0_phrase
C0161_VERBATIM_REMEDY: str = (
    "Critical brake pressure loss detected. Pull over immediately."
)

#: C0040 (Tire Pressure Sensor, Left Front) has a KB guide and is non-P0.
#: Listed here for completeness; no verbatim-phrase obligation.
C0040_VERBATIM_NOTE: str = (
    "A tire pressure sensor fault (C0040) has been recorded. "
    "Have the tire pressure monitoring system inspected."
)

# ---------------------------------------------------------------------------
# ADP KB coverage for brake DTC codes
# ---------------------------------------------------------------------------

#: The subset of ADP brake DTC codes that have deployed Bedrock KB guides.
#: The agent may explain codes in this set using their KB content.
#: The agent MUST NOT invent explanations for codes outside this set.
#:
#: Source: spec § D3b — "only C0161 and C0040 of the ten brake DTC codes
#: have KB guides."
BRAKE_DTC_KB_GUIDED: frozenset[str] = frozenset({"C0161", "C0040"})

#: Codes known to exist in ADP's BRAKE_DTC_CODES but NOT covered by a KB guide.
#: The agent may note that a code was recorded without explaining it.
#: If a future ADP KB update covers these, remove them here and from the
#: "no explanation" guard in assemble_brake_dtc_note().
BRAKE_DTC_NO_KB: frozenset[str] = frozenset({
    "C0031", "C0032", "C0034", "C0035",
    "C0041", "C0045", "C0046",
    "C0051", "C0052",
    "C0081",
})

# ---------------------------------------------------------------------------
# Severity-hint vocabulary (canonical, spec § D4)
# ---------------------------------------------------------------------------

#: The valid ``severity_hint`` values.  Must be an exact subset of the
#: vocabulary defined in ``agents/tier2/contract.py:VALID_SEVERITY_HINTS``.
#: Repeated here so prompt.py has no dependency on contract.py.
SEVERITY_HINTS: frozenset[str] = frozenset({"routine", "soon", "attention"})

# ---------------------------------------------------------------------------
# Agent system prompt (main guidance for the LLM judgment layer)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = """
You are the Vehicle Health Assessment Agent — a Tier 2 autonomous agent that
reasons about vehicle health data on behalf of vehicle owners.  You receive
pre-fetched data from ADP (the Automotive Data Platform) and produce a plain-
language finding for each health component.

**Your job is judgment, not computation.**
ADP has already performed all statistical inference (wear rates, state-of-health
percentages, service history records).  You do not compute thresholds, fit
models, or extrapolate trends.  You reason about what the data means for THIS
owner NOW.

**Component-specific guidance:**

TIRES (ADP product: the tires health catalog table)
- ADP supplies: a replacement flag (supervised label), a wear category, and a wear rate metric.
- If any tire has the replacement flag set to True, say so plainly.
- Do not predict remaining life, extrapolate wear, or invent replacement dates.
- severity_hint:
    * 'attention' — replacement flag is True on any tire
    * 'soon' — elevated wear rate without replacement flag
    * 'routine' — normal wear, no replacement signal

BATTERY (ADP product: energy usage catalog table)
- ADP supplies: state_of_health_pct, battery_age_days.
- The honest output on current data is REASSURANCE. Nothing crosses a warranty
  threshold (~70%). A correct finding says "your battery is at 92%, degrading
  normally for its age" — it does NOT manufacture urgency the data does not support.
- Do not compute a degradation model or predict remaining life.
- severity_hint:
    * 'routine' — normal age-correlated degradation (the typical case)
    * 'soon' — SoH notably below expected for battery age (rare on this data)
    * 'attention' — SoH at risk of approaching warranty floor (very rare)

BRAKES (ADP product: the service records catalog table, filtered to brake service)
- ADP supplies: service_date, dtc_codes, complaint_text, warranty_covered,
  total_cost_usd, outcome.
- This is SERVICE HISTORY, not prediction. Do not use trend language or
  extrapolate wear — ~95% of vehicles have exactly one brake record.
- The most important signal is outcome: 'parts_pending' or 'follow_up_required'
  means an open, unresolved brake job. Surface that plainly.
- severity_hint:
    * 'attention' — open outcome (parts_pending / follow_up_required)
    * 'soon' — resolved but recent service with notable DTCs
    * 'routine' — resolved service, no open issues

**CRITICAL: P0 verbatim phrase rule (spec § D8)**
DTC C0161 (critical brake pressure loss) is severity P0 in ADP's knowledge base.
If C0161 appears in dtc_codes, your finding MUST contain the exact phrase:
    "Critical brake pressure loss detected. Pull over immediately."
DO NOT soften, paraphrase, or omit this phrase.  "Exercise caution" or
"seek service soon" are boundary violations.  The verbatim phrase must appear.

**DTC explanations**
Only C0161 and C0040 have ADP Knowledge Base guides.  For other DTC codes, you
may note that a code was recorded, but do NOT invent explanations.

**severity_hint vocabulary**
Use ONLY: 'routine', 'soon', 'attention'.
NEVER use: P0, P1, P2, P3, critical, high, medium, low.
P-levels belong to the triage classifier; this field is advisory scheduling
urgency and must not be mistaken for a safety classification.

**Bundling decision**
When you have findings for multiple components (tires + battery + brakes), decide
whether to report them together or separately.  Bundle when:
- Multiple components are at 'attention' (owner needs a holistic picture)
- A brake open-outcome and a tire finding together imply a service visit is needed
  anyway (efficient to bundle)
Separate when:
- One finding is urgent (e.g. brake P0) and the others are routine
  (the urgent finding must not be buried in a combined message)
- Components have unrelated urgency levels

Your output must be a JSON object with one key per component that has a finding:
{
  "battery": { "finding": "...", "severity_hint": "routine|soon|attention", "confidence": "high|medium|low", "evidence": [...] },
  "tires":   { "finding": "...", "severity_hint": "...", "confidence": "...", "evidence": [...] },
  "brakes":  { "finding": "...", "severity_hint": "...", "confidence": "...", "evidence": [...] },
  "bundle": true | false,
  "bundle_rationale": "..."
}
Omit a component key if no data was provided for that component.
""".strip()


# ---------------------------------------------------------------------------
# Per-component user prompt templates
# ---------------------------------------------------------------------------
#
# T4.10 (a): the four per-component *_USER_TEMPLATE constants were deleted here.
#
# They were orphaned twice over:
#   1. Nothing in this codebase imports them (confirmed by grep).
#   2. The only LLM call site (_enrich_with_llm in agent.py) builds its prompt
#      inline from the deterministic summary — it never referenced these constants.
#
# They were also the last four {vin} interpolation slots in agents/health/ after
# T4.8 removed VIN logging from the three sources, so deleting them completes
# T4.8's privacy fix (no ADP free text or VIN reaches any LLM prompt path).
#
# W2 / security review Cycle 7: if they had been wired, BRAKE_USER_TEMPLATE
# would have interpolated `complaint_text` (ADP PII, marked drift:true) into a
# prompt unescaped and undelimited, making a crafted complaint capable of
# overriding the severity downward.  Deleting them removes the injection surface
# before it can be armed.  The hardened _enrich_with_llm seam (b) below is the
# correct pattern to copy if per-component prompts are ever needed.
#
# See decisions.md § 2026-08-05 "Fix Group 4b" for the full call-graph analysis.
#
# ---------------------------------------------------------------------------
# Verbatim-phrase injection helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Softening-continuation deny-list (defence-in-depth for the textual guard)
# ---------------------------------------------------------------------------

#: Number of characters after the phrase end to scan for softening continuations.
#: Cycle 7 W1 asked for both a position rule AND a post-phrase window check.
#: T4.14 restores the window alongside the position rule.
#: The window stays in effect even when ``_enrich_with_llm`` bypasses the LLM
#: for active P0 brakes — it operates on the deterministic path too, as a
#: defence-in-depth backstop rather than the primary control.
_SOFTENING_WINDOW: int = 200

#: Softening terms that must not appear within ``_SOFTENING_WINDOW`` characters
#: after the verbatim phrase.  The list is intentionally small — exhaustiveness
#: is not the goal.  Position is the primary control; this deny-list is
#: defence-in-depth for softening continuations that immediately follow a
#: leading phrase.
#:
#: T4.14 restores this list alongside the position rule.  T4.12 deleted it
#: because position "supersedes" it — but the two rules catch different
#: surfaces: position catches softening BEFORE the phrase, the window catches
#: softening AFTER.  Cycle 7 explicitly asked for both.
_SOFTENING_DENY_LIST: tuple[str, ...] = (
    "however",
    "that said",
    "false positive",
    "false alarm",
    "you can continue",
    "exercise caution",
    "no urgent",
)


def inject_p0_verbatim_phrases(finding_text: str, dtc_codes: list[str]) -> str:
    """Ensure verbatim P0 phrases are present in *finding_text* when required.

    If ``C0161`` appears in *dtc_codes* (case-insensitive — W6 fix), this
    function applies **two rules** — both Cycle 7 originally asked for, and
    T4.14 restores together after T4.12 deleted the second one:

    **Rule 1 — Position**: ``C0161_VERBATIM_REMEDY`` must be the **leading
    sentence of its paragraph** in the returned finding.  "Leading" means the
    stripped text starts with the verbatim phrase.  Anything placed before it
    (softening, qualifiers, other sentences) is a violation.

    **Rule 2 — Softening continuation**: within ``_SOFTENING_WINDOW`` characters
    after the phrase end, none of the terms in ``_SOFTENING_DENY_LIST`` may
    appear.  This catches a softening sentence that immediately follows a
    leading phrase.

    If EITHER rule is violated, the phrase is prepended as a standalone paragraph
    (``phrase + "\\n\\n" + original_text``).

    Why both rules are needed
    -------------------------
    - Position catches softening placed **before** the phrase (Cycle 8 [A]).
    - The deny-list catches softening placed **after** the phrase within the
      window (Cycle 7 original bypass: "...Pull over immediately. However, false
      positive...").
    - Together they are defence-in-depth on the textual path.  The primary
      control is T4.14's structural bypass: when a component carries an active
      P0 DTC, ``_enrich_with_llm`` never passes it to the LLM at all.  The
      textual guard then operates only on the deterministic path, where it is
      load-bearing only as a safety net.

    Cycle 8 three-defeat analysis
    ------------------------------
    - **(A) Softening BEFORE the phrase** — caught by Rule 1 (phrase not leading
      → inject).
    - **(B) Softening past the 200-char window** — phrase IS leading (Rule 1
      passes); "however" is past the 200-char window (Rule 2 passes) → no
      injection needed.  This is structurally unreachable via the T4.14 bypass
      for active P0 components.  For the deterministic path the guard correctly
      does not inject: the phrase leads, and the softening is far downstream.
    - **(C) Novel softening vocabulary** — phrase IS leading (Rule 1 passes);
      "actually" is not in the deny-list (Rule 2 passes) → no injection from
      the textual guard.  Structurally unreachable via the T4.14 bypass for
      active P0 components.  The deny-list is defence-in-depth, not an
      exhaustive vocabulary gate; position is the total property.

    This function does NOT modify the finding for any other DTC code.

    Security invariant (spec § D8, agentic-tiers.md):
        A boundary that exists only in a prompt will be crossed.
        This guard is an executable assertion, not a prompt instruction.
        T4.14 makes the guard defence-in-depth by ensuring active-P0 brakes
        never reach the LLM at all; see ``_enrich_with_llm`` in ``agent.py``.

    Parameters
    ----------
    finding_text:
        The finding text produced by the LLM judgment call (or assembled
        deterministically by the agent).
    dtc_codes:
        List of DTC code strings from the brake service record.  Matched
        case-insensitively.

    Returns
    -------
    str
        The finding text with the verbatim phrase guaranteed to lead as a
        standalone paragraph if C0161 is in *dtc_codes*.
    """
    # W6 fix: casefold the comparison — a safety backstop that depends on
    # upstream casing conventions is not a backstop.
    if "C0161" not in {code.upper() for code in dtc_codes}:
        return finding_text

    # Rule 1 (T4.12): the phrase must be the leading sentence of the finding.
    # Any content before it is a violation — softening before the phrase,
    # qualifiers, preamble, etc.  Position is a total property: no vocabulary
    # to evade, no window to step outside of.
    phrase_leads = finding_text.lstrip().startswith(C0161_VERBATIM_REMEDY)

    if not phrase_leads:
        # Phrase absent OR phrase present but not leading → inject at front.
        return f"{C0161_VERBATIM_REMEDY}\n\n{finding_text}"

    # Rule 2 (T4.7, restored by T4.14): even when the phrase leads, check
    # for softening continuation within _SOFTENING_WINDOW characters after it,
    # but only within the SAME paragraph (do not cross a blank-line break).
    # A "However" that begins a new paragraph is a new thought, not a softening
    # continuation of the verbatim phrase.
    abs_phrase_end = len(finding_text) - len(finding_text.lstrip()) + len(C0161_VERBATIM_REMEDY)
    raw_window = finding_text[abs_phrase_end : abs_phrase_end + _SOFTENING_WINDOW]
    # Clip at the first paragraph break so we only scan the immediate continuation.
    para_break = raw_window.find("\n\n")
    if para_break != -1:
        raw_window = raw_window[:para_break]
    window = raw_window.lower()

    for term in _SOFTENING_DENY_LIST:
        if term in window:
            # Softening continuation detected — prepend to make the phrase
            # unambiguously the first thing the reader sees, with the
            # softening text following a hard paragraph break.
            return f"{C0161_VERBATIM_REMEDY}\n\n{finding_text}"

    # Both rules pass — phrase leads and no immediate softening. Correct output.
    return finding_text


def assemble_brake_dtc_note(dtc_codes: list[str]) -> str:
    """Return a structured note for brake DTC codes.

    For ``C0161`` (P0): returns the verbatim remedy phrase.
    For ``C0040``: returns the KB-grounded note.
    For codes outside ``BRAKE_DTC_KB_GUIDED``: notes their presence without
    inventing an explanation.

    The result is intended to be incorporated into the agent's finding, not
    returned standalone — the agent may further wrap it in context.

    Parameters
    ----------
    dtc_codes:
        List of DTC code strings, e.g. ``["C0161", "C0031"]``.

    Returns
    -------
    str
        A human-readable note about the DTC codes.  Empty string if the
        list is empty.
    """
    if not dtc_codes:
        return ""

    notes: list[str] = []

    # Process in stable order: P0 first, then C0040, then others.
    # W6 fix: normalise to uppercase so the KB-guided set membership check is
    # case-insensitive — the output display code retains the normalised form.
    for raw_code in dtc_codes:
        code = raw_code.upper()  # normalise for case-insensitive matching
        if code == "C0161":
            notes.append(C0161_VERBATIM_REMEDY)
        elif code == "C0040":
            notes.append(C0040_VERBATIM_NOTE)
        elif code in BRAKE_DTC_NO_KB:
            # Acknowledge the code without inventing an explanation.
            notes.append(f"DTC {code} was recorded. No published explanation is available.")
        else:
            # Unknown code — acknowledge presence, no explanation.
            notes.append(f"DTC {code} was recorded.")

    return " ".join(notes)
