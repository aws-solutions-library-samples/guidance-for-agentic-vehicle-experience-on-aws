"""DTC response catalog — per-fault narration scripts for Nova Sonic's
critical-path voice response.

Rationale (2026-05-08, revised 2026-05-11)
------------------------------------------
Early prompt-priming used a single generic "pull over, hazards on,
connecting agent, roadside dispatched" script for every P0 DTC. That's
defensible for most powertrain/brake emergencies but wrong for a few
edge cases — notably a fire (should be "get out of the vehicle") or a
crash (should be "stay in the vehicle, call 911"). This module lets
each DTC map to the right response template without bloating prompt
logic with conditionals.

Two tables:

* ``RESPONSE_TEMPLATES`` maps a template ID (a short stable string) to
  the Nova-narration sentences we want the model to say, plus a
  human-readable name for logs and the reasoning drawer.
* ``DTC_TO_TEMPLATE`` maps each known DTC code to the right template
  plus:

    - a list of symptom keywords the driver might use,
    - a one-sentence *diagnosis* (inserted as Nova's second sentence
      in the voice response so the driver hears WHY they're being
      told to stop), and
    - a multi-sentence *chat_diagnosis* shown as a system bubble in
      the in-app chat once the Connect handoff starts — designed to
      orient the driver while they wait for the live agent and to
      give the agent a compact context block to land on.

The previous *post_handoff* voice script was removed on 2026-05-11
because the iOS client mutes Nova Sonic the moment an agent joins the
chat (see VoiceSessionViewModel.participantJoined). That meant the
SECOND REPLY directive in the prompt could never fire. Moving the
deeper-diagnosis content into chat preserves the intent (driver gets
mechanism + context while waiting) without fighting the mute.

Lookup via :func:`lookup_dtc_response` returns all pieces so the
caller can embed everything into a single prompt block and include the
chat diagnosis in the escalation wire event.

Extension path
--------------
This module is hardcoded today. For production the intent is to move
``DTC_TO_TEMPLATE`` into ``cms-prod-event-catalog`` as four new fields
on each DTC row (``response_template``, ``symptom_keywords``,
``diagnosis_sentence``, ``chat_diagnosis``) and have this module's
``lookup_dtc_response`` read from DynamoDB instead of the dict. The
response-template scripts themselves — i.e. the actual first-sentence
+ follow-sentence wording Nova says — should probably stay in code
(tenant-independent, reviewed as part of the safety-relevant surface)
rather than moving to editable config.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict


class ResponseTemplate(TypedDict):
    """One critical-path narration script.

    ``sentences`` is a list of short sentences Nova Sonic should read in
    order. The first sentence always starts with the driver's first
    name (prepended by the prompt renderer); subsequent sentences are
    the template's verbatim text.
    """

    name: str  # Human-readable identifier, e.g. "pull-over-stop-engine"
    first_sentence: str  # After "<name>, " — Nova leads with this
    follow_sentences: list[str]  # Zero or more additional short sentences


# -----------------------------------------------------------------------------
# Response templates
# -----------------------------------------------------------------------------
#
# Keep sentences SHORT and spoken-friendly. No jargon ("coolant" is ok;
# "catalyst efficiency threshold exceeded" is not). No priority codes
# (P0/P1/P2/P3) — the prompt forbids those from being spoken.
#
# First sentence always implicitly starts with "<name>, " (handled by
# the prompt renderer). So "this is critical — pull over..." becomes
# "Samantha, this is critical — pull over..." at runtime.
RESPONSE_TEMPLATES: dict[str, ResponseTemplate] = {
    "pullover_stop_engine": {
        "name": "pull-over, shut off engine",
        "first_sentence": (
            "this is critical — pull over safely right now, turn on your "
            "hazard lights, and shut off the engine."
        ),
        "follow_sentences": [
            # Recommend, never assert (spec 2026-09-23-cvx-escalation-
            # recommend-not-act): nothing auto-opens a chat by default, and
            # nothing dispatches a truck. `render_response` has no callers
            # today; this keeps a future caller from reviving the false claim.
            "You can tap to talk with a live fleet-support agent in the app.",
            "You can request roadside assistance from your screen if the "
            "vehicle can't be driven safely.",
        ],
    },
    "pullover_safe_location": {
        "name": "pull over at next safe location",
        "first_sentence": (
            "this is critical — pull over at the next safe location, "
            "turn on your hazards, and shut off the vehicle."
        ),
        "follow_sentences": [
            # Recommend, never assert (spec 2026-09-23-cvx-escalation-
            # recommend-not-act): nothing auto-opens a chat by default, and
            # nothing dispatches a truck. `render_response` has no callers
            # today; this keeps a future caller from reviving the false claim.
            "You can tap to talk with a live fleet-support agent in the app.",
            "You can request roadside assistance from your screen if the "
            "vehicle can't be driven safely.",
        ],
    },
    "brake_emergency": {
        "name": "brake emergency",
        "first_sentence": (
            "this is critical — pump the brakes, downshift to slow the "
            "vehicle, and pull to the shoulder with your hazards on."
        ),
        "follow_sentences": [
            "If the brakes don't respond, use your emergency brake gently.",
            "You can tap to talk with a live fleet-support agent in the app.",
        ],
    },
    "exit_vehicle": {
        "name": "exit vehicle immediately",
        # 2026-09-28: the 911 clause is now inside the first sentence and
        # UNCONDITIONAL. Before this, `first_sentence` said only
        # "this is an emergency — stop the vehicle, turn it off, get out,
        # and move at least fifty feet away." and the 911 instruction lived
        # in `follow_sentences[1]` conditioned on "if anyone is hurt or in
        # danger". `follow_sentences` is not consumed by `p0_directive`
        # (the P0 directive path emits `first_sentence` alone; the pre-2026-
        # 09-28 note above `render_response` still applies), so the 911
        # instruction never reached the driver on the platform's own P0
        # channel and the driver was told to call only after — if at all —
        # they had already left the vehicle and Connect handoff had opened.
        # See issues/2026-09-28-b0001-fire-p0-directive-omits-911/.
        #
        # Wording approved 2026-09-28 for every code that uses this
        # template (B0001_FIRE, P0A80, the driver-confirmed fire path).
        "first_sentence": (
            "this is an emergency — stop the vehicle, turn it off, get "
            "out, move at least fifty feet away, and call nine-one-one now."
        ),
        "follow_sentences": [
            "Do not open the hood.",
            # Dropped 2026-09-28: "Call nine-one-one if anyone is hurt or in
            # danger." The 911 instruction now lives unconditionally in
            # `first_sentence` above, so keeping a conditional version here
            # would (a) contradict the unconditional line the platform just
            # emitted and (b) reintroduce the exact defect
            # `issues/2026-09-28-b0001-fire-p0-directive-omits-911/`
            # describes — a driver who feels fine can read a condition as
            # permission not to call. `follow_sentences` still has no live
            # caller, but the guards in `test_dtc_catalog_narration_safety`
            # scan every catalog string, so a resurrected conditional here
            # would fail the fire/crash-directive test.
        ],
    },
    "stay_call_911": {
        "name": "stay in vehicle, call 911",
        "first_sentence": (
            "are you okay? Stay in the vehicle if it's safe and call "
            "nine-one-one right now."
        ),
        "follow_sentences": [
            "If the vehicle is in traffic and you can move safely, pull "
            "to the shoulder.",
            "You can tap to talk with a fleet-support agent in the app.",
        ],
    },
}


# -----------------------------------------------------------------------------
# DTC → response template mapping
# -----------------------------------------------------------------------------
#
# Each DTC code binds to exactly one response template. The ``keywords``
# list gives the voice prompt phrasing it can hand to Nova Sonic so the
# model can tell which template to use based on the driver's own words.
#
# Hypothetical codes (fire, crash) are included for demo range — the
# real CMS DTC catalog doesn't currently emit these, but the simulator
# could.
DTC_TO_TEMPLATE: dict[str, dict] = {
    # Powertrain / cooling
    "P0217": {
        "template": "pullover_stop_engine",
        "keywords": ["temperature", "coolant", "overheat", "overheating",
                     "hot engine", "temperature gauge"],
        "diagnosis": (
            "Your coolant temperature is over the safe limit — continuing "
            "would damage the engine and could cause a fire."
        ),
        # Shown as a system bubble in the Connect chat once the handoff
        # completes. Keep factual, tenant-neutral, and short enough to
        # scan in 5 seconds — this is meant to orient the driver while
        # they wait for the agent, not to be another full narration.
        "chat_diagnosis": (
            "VSA Diagnosis — P0217 (Engine coolant critically overheated). "
            "Your engine hit its thermal shutdown threshold, which is why "
            "power cut out. A fleet support agent is joining this chat now "
            "with your location, the fault code, and the diagnostic history."
        ),
    },
    # Hybrid battery / powertrain electric. A high-voltage battery thermal event
    # (CMS: "stop driving immediately, evacuate") gets the fire line, not "pull
    # over at the next safe location": user decision 2026-09-27, backlog row
    # "P0A80 safety line" (cvx/issues/2026-09-26-dtc-grading-and-copy-follow-ons/ item 1).
    "P0A80": {
        "template": "exit_vehicle",
        "keywords": ["battery", "hybrid", "power", "losing power", "battery warning"],
        "diagnosis": (
            "Your high-voltage battery is overheating, which is why you're "
            "losing power."
        ),
        # The bubble must not say less than the spoken line (user rule
        # 2026-09-24). After the header it is the `exit_vehicle` template's copy word
        # for word (the spoken first sentence, then the follow sentences), which
        # since 2026-09-28 carries an UNCONDITIONAL "call nine-one-one now" —
        # see the note above the template's `first_sentence`. Composed by the
        # architect 2026-09-27 for the user to confirm; the user approved the
        # template, not this bubble. It used to say the vehicle "is derating power
        # to protect itself", which reads as the car handling it while the voice
        # says get out (review 2026-09-27 W2).
        #
        # The prior version of this bubble carried a trailing "Call nine-one-one
        # if anyone is hurt or in danger." — the same conditional 911 clause the
        # 2026-09-28 fix removed from `exit_vehicle.follow_sentences` for the
        # same reason (issues/2026-09-28-b0001-fire-p0-directive-omits-911/).
        "chat_diagnosis": (
            "VSA Diagnosis — P0A80 (EV high-voltage battery thermal event). "
            "This is an emergency. Stop the vehicle, turn it off, get out, "
            "move at least fifty feet away, and call nine-one-one now. Do "
            "not open the hood. Fleet support can reach you on this chat."
        ),
    },
    # Braking
    "C1234": {
        "template": "brake_emergency",
        "keywords": ["brakes", "braking", "brake pedal", "pedal is soft",
                     "brakes aren't working"],
        "diagnosis": (
            "Your brake system has a fault and may not respond normally — "
            "that's why it feels different."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — C1234 (Brake system fault). "
            "Brake pressure and sensor telemetry indicate a hydraulic or "
            "ABS fault, which is why the pedal feels soft or less "
            "responsive. A fleet support agent is joining this chat now "
            "with your location and the diagnostic history."
        ),
    },
    # Turbo / boost
    "P0299": {
        "template": "pullover_safe_location",
        "keywords": ["turbo", "boost", "sluggish", "low power", "won't accelerate"],
        "diagnosis": (
            "Your turbocharger has lost boost pressure, which is why the "
            "vehicle feels sluggish."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — P0299 (Turbo/supercharger underboost). "
            "The turbo isn't making enough boost — likely an air leak or "
            "wastegate issue. Continuing under load will worsen the fault. "
            "A fleet support agent is joining this chat now with your "
            "location and the diagnostic history."
        ),
    },
    # Steering (hypothetical — example of exit/emergency template)
    "C1201": {
        "template": "pullover_safe_location",
        "keywords": ["steering", "can't steer", "wheel is stiff", "pulling to one side"],
        "diagnosis": (
            "Your power steering has a fault and the wheel may feel "
            "heavier than usual."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — C1201 (Power steering fault). "
            "The steering assist system detected a fault, so expect "
            "heavier-than-normal steering effort until repair. A fleet "
            "support agent is joining this chat now with your location "
            "and the diagnostic history."
        ),
    },
    # Fire / thermal runaway (hypothetical demo code — real emission
    # would come from a temperature/thermal-imaging sensor, not OBD-II)
    "B0001_FIRE": {
        "template": "exit_vehicle",
        "keywords": ["fire", "smoke", "flames", "burning", "something's on fire"],
        "diagnosis": (
            "Sensors show a thermal event in the engine compartment — "
            "there's a fire risk."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — B0001 (Thermal runaway / engine compartment). "
            "Temperature sensors in the engine bay crossed the "
            "thermal-runaway threshold — there's an active fire risk. "
            # Was "Emergency services and roadside have your location and are
            # en route." Nothing in the platform calls emergency services, and
            # F1.1 removed the roadside dispatch — so both halves were false
            # (spec 2026-09-23-cvx-escalation-recommend-not-act, security
            # Cycle 2 W4). Unlike the `follow_sentences` e6d0bed corrected,
            # this field IS consumed: bidi_app.py emits it as
            # `escalation.chatDiagnosis` and iOS renders it as a SYSTEM bubble
            # in the Connect chat. It also contradicted this template's own
            # spoken line ("call nine-one-one if anyone is hurt or in danger"),
            # which is the dangerous part: a driver told help is already coming
            # may wait instead of calling.
            "Get clear of the vehicle and call nine-one-one now. Fleet "
            "support can reach you on this chat."
        ),
    },
    # Crash / impact (hypothetical — airbag or crash-detection systems
    # would emit codes here; the demo will use a simulator trigger)
    "U3000_CRASH": {
        "template": "stay_call_911",
        "keywords": ["crash", "accident", "collision", "hit", "impact"],
        "diagnosis": (
            "Sensors detected a collision."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — U3000 (Collision detected). "
            "Impact sensors recorded a collision. "
            # Was "Emergency services have your exact location and are en
            # route." Nothing in the platform calls emergency services (security
            # Cycle 2 W4). This field is emitted to iOS as
            # `escalation.chatDiagnosis` and rendered as a SYSTEM bubble, and it
            # contradicted this template's own spoken line ("call nine-one-one
            # right now") — telling a driver in a collision that responders are
            # already coming is the one error that could stop them calling.
            #
            # The replacement first read "If anyone is hurt, call nine-one-one
            # now." User-rejected 2026-09-24 (HANDOFF-fire-crash-wording.md):
            # the spoken line above is unconditional, and "if anyone is hurt"
            # lets a driver who feels fine read it as permission not to call.
            # The chat bubble must never say less than the spoken line.
            "Stay in the vehicle if it's safe and call nine-one-one now. "
            "Stay on this chat so fleet support can relay anything the "
            "emergency responders need."
        ),
    },
}


def lookup_dtc_response(code: str) -> Optional[dict]:
    """Return the response template + symptom keywords + voice
    diagnosis + chat diagnosis for a DTC code, or None if the code
    isn't in the catalog.

    Returned dict keys:

    * ``template_id`` — key into :data:`RESPONSE_TEMPLATES`
    * ``template`` — the :class:`ResponseTemplate` itself
    * ``keywords`` — driver-phrasing list (str)
    * ``diagnosis`` — one-sentence explanation of the fault, spoken by
      Nova as sentence 2 of the critical-path voice response (str, may
      be empty if the catalog entry doesn't supply one)
    * ``chat_diagnosis`` — longer plain-text diagnosis shown as a
      SYSTEM bubble in the Connect chat once the handoff starts (str,
      may be empty)
    """
    if not code:
        return None
    mapping = DTC_TO_TEMPLATE.get(code)
    if not mapping:
        return None
    template_id = mapping.get("template")
    template = RESPONSE_TEMPLATES.get(template_id) if template_id else None
    if not template:
        return None
    return {
        "template_id": template_id,
        "template": template,
        "keywords": list(mapping.get("keywords") or []),
        "diagnosis": (mapping.get("diagnosis") or "").strip(),
        "chat_diagnosis": (mapping.get("chat_diagnosis") or "").strip(),
    }


def render_response(
    name: str,
    template_id: str,
    diagnosis: str | None = None,
) -> list[str]:
    """Build the full first-reply narration for ``template_id``.

    The returned sentence list is, in order:

    1. ``"{name}, {first_sentence}"`` — the immediate safety directive,
       personalised with the driver's name.
    2. The ``diagnosis`` sentence, if one was supplied. Skipped when
       ``diagnosis`` is falsy — the first reply then falls back to the
       pre-diagnosis behaviour (directive + follow sentences only).
    3. Each of the template's ``follow_sentences`` (handoff
       announcement, roadside status, etc.).

    If ``template_id`` isn't known the fallback is the conservative
    pull-over script.
    """
    tpl = RESPONSE_TEMPLATES.get(template_id) or RESPONSE_TEMPLATES["pullover_stop_engine"]
    first = f"{name}, {tpl['first_sentence']}"
    sentences = [first]
    if diagnosis:
        sentences.append(diagnosis)
    sentences.extend(tpl["follow_sentences"])
    return sentences



# -----------------------------------------------------------------------------
# The P0 safety directive the platform itself shows the driver
# -----------------------------------------------------------------------------
#
# Escalation spec Fix Group 14 (issues/2026-09-25-p0-pull-over-never-reaches-the-driver/).
# On a P0 the bidi server sends this sentence to the client as an assistant transcript
# line (flag `_platform_safety_directive`, see bidi_app._show_p0_directive) BEFORE
# anything else on the auto-triage path, and asks the model to say it word for word.
# The line reaches the driver's screen whatever the model says; before this, the
# model's speech was the only channel and on 2026-09-25 it refused.
#
# When several active faults map to different templates, the most protective directive
# wins. Order (confirmed by the user 2026-09-27): leave a burning vehicle, then the crash
# directive, then the brake emergency, then stop the engine, then pull over at the next
# safe place. An unknown or unmapped fault falls back to the conservative stop-engine
# directive, the same fallback `render_response` uses.
P0_TEMPLATE_PRECEDENCE: tuple[str, ...] = (
    "exit_vehicle",
    "stay_call_911",
    "brake_emergency",
    "pullover_stop_engine",
    "pullover_safe_location",
)
P0_FALLBACK_TEMPLATE = "pullover_stop_engine"


def faults_for_codes(codes) -> list[dict]:
    """``{"code", "template_id"}`` for each code the catalog maps, skipping the rest
    and anything that is not a string."""
    faults: list[dict] = []
    for code in codes or []:
        if not isinstance(code, str):
            continue
        resolved = lookup_dtc_response(code)
        if resolved:
            faults.append({"code": code, "template_id": resolved["template_id"]})
    return faults


def p0_directive_template(active_dtcs: list[dict] | None) -> str:
    """The template id whose first sentence is the P0 directive for these faults.

    ``active_dtcs`` are the session's primed CRITICAL/HIGH faults, each optionally
    carrying the ``template_id`` that ``lookup_dtc_response`` resolved. Entries with no
    or an unknown ``template_id`` are ignored.
    """
    present = {
        (d or {}).get("template_id")
        for d in (active_dtcs or [])
        if (d or {}).get("template_id") in RESPONSE_TEMPLATES
    }
    for template_id in P0_TEMPLATE_PRECEDENCE:
        if template_id in present:
            return template_id
    return P0_FALLBACK_TEMPLATE


# Security Cycle 7 W2: the driver's first name comes from a CMS table and is
# spliced into the platform's own safety line and into quoted "exactly as written"
# notes to the model. Only a plain name is used; anything else (quotes, control
# characters, instructions, more than 40 characters) falls back to no name.
# Security Cycle 9 S4: at most two words, since the name is now spoken verbatim
# before the directive and a sentence-length "name" could read as an instruction.
_SAFE_NAME = re.compile(r"(?=.{1,40}\Z)[A-Za-z][A-Za-z'.-]*(?: [A-Za-z][A-Za-z'.-]*)?")


def safe_first_name(first_name: object) -> str:
    """The name if it is a plain name, else ``""``."""
    name = first_name.strip() if isinstance(first_name, str) else ""
    return name if _SAFE_NAME.fullmatch(name) else ""


def is_p0_directive(text: object) -> bool:
    """True if ``text`` is a directive ``p0_directive`` can produce: a template's first
    sentence, capitalised on its own or after ``"{name}, "`` with a plain name.

    Used where platform text is added after the output guardrail (security Cycle 9
    S1), so only the catalog's own sentences can take that path.
    """
    if not isinstance(text, str):
        return False
    for template in RESPONSE_TEMPLATES.values():
        first = template["first_sentence"]
        if text == first[:1].upper() + first[1:]:
            return True
        suffix = ", " + first
        if text.endswith(suffix):
            name = text[: -len(suffix)]
            if name and safe_first_name(name) == name:
                return True
    return False


def p0_directive(active_dtcs: list[dict] | None, first_name: str | None) -> str:
    """The exact P0 safety sentence: the chosen template's first sentence, verbatim.

    Personalised as ``"{first_name}, {first_sentence}"`` when the name is a plain
    name (``safe_first_name``); otherwise the first sentence stands alone with its
    first letter capitalised.
    """
    first = RESPONSE_TEMPLATES[p0_directive_template(active_dtcs)]["first_sentence"]
    name = safe_first_name(first_name)
    if name:
        return f"{name}, {first}"
    return first[:1].upper() + first[1:]
