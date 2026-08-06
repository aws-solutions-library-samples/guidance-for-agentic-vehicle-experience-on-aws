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
            "I'm connecting you to a live fleet-support agent through "
            "the in-app chat.",
            "Roadside assistance has been dispatched to your location.",
        ],
    },
    "pullover_safe_location": {
        "name": "pull over at next safe location",
        "first_sentence": (
            "this is critical — pull over at the next safe location, "
            "turn on your hazards, and shut off the vehicle."
        ),
        "follow_sentences": [
            "I'm connecting you to a live fleet-support agent through "
            "the in-app chat.",
            "Roadside assistance has been dispatched to your location.",
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
            "I'm connecting you to a live fleet-support agent right now.",
        ],
    },
    "exit_vehicle": {
        "name": "exit vehicle immediately",
        "first_sentence": (
            "this is an emergency — stop the vehicle, turn it off, get "
            "out, and move at least fifty feet away."
        ),
        "follow_sentences": [
            "Do not open the hood.",
            "Emergency services are being called and an agent is on "
            "the line to help.",
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
            "A fleet-support agent is joining the chat to help.",
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
    # Hybrid battery / powertrain electric
    "P0A80": {
        "template": "pullover_safe_location",
        "keywords": ["battery", "hybrid", "power", "losing power", "battery warning"],
        "diagnosis": (
            "Your high-voltage battery is overheating, which is why you're "
            "losing power."
        ),
        "chat_diagnosis": (
            "VSA Diagnosis — P0A80 (EV high-voltage battery thermal event). "
            "The battery has hit a thermal safety limit and the vehicle is "
            "derating power to protect itself. A fleet support agent is "
            "joining this chat now with your location, the fault code, and "
            "the diagnostic history."
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
            "Emergency services and roadside have your location and are "
            "en route."
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
            "Impact sensors recorded a collision. Emergency services have "
            "your exact location and are en route. Please stay on this "
            "chat so fleet support can relay anything the emergency "
            "responders need."
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
