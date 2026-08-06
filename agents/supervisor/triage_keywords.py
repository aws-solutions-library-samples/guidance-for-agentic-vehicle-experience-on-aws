"""Emergency keyword triggers for driver-confirmed triage escalation.

The deterministic triage classifier (vsa-prod-triage-classifier Lambda)
intentionally never reads the driver's words — it trusts sensor data only.
That's the right safety design, but it creates a gap: when the driver
reports something urgent that the sensors don't corroborate (lag,
sensor-blind failure modes, things the vehicle doesn't instrument), the
classifier returns a low tier and the assistant narrates as if nothing
is wrong.

This module fills that gap without changing the classifier. It holds a
vetted list of emergency keyword categories and exposes a matcher that
bidi_app.py uses to decide whether to ASK the driver a yes/no
confirmation question when the classifier and the utterance seem to
disagree. If the driver confirms, bidi_app.py escalates via a
driver-confirmed audit path (see VSA_TRIAGE_CLASSIFIER.md §8 — driver-
confirmed escalation is distinct from sensor-backed triage).

Matching is INTENTIONALLY conservative:
  - Phrases are substrings, compared case-insensitively, on normalized
    whitespace.
  - Only strong emergency phrasing triggers — "my brakes are spongy" does
    NOT match; "my brakes failed" / "brakes aren't stopping" / "lost
    brakes" do.
  - No probabilistic or LLM-backed matching. If an exact phrase isn't in
    the list and a real emergency goes un-flagged, that's a prompt to add
    the phrase; we never silently guess.

See also:
  - bidi_app._auto_triage_and_inject — uses match_emergency_keywords()
    to decide whether to route through the confirmation path.
  - bidi_app's user-transcript handler — interprets the driver's
    confirmation response (yes/no/unclear).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmergencyCategory:
    """One class of driver-reported emergencies requiring confirmation.

    Fields:
      name: Short machine-readable identifier. Used in audit records and
        logs. e.g. "brake_failure".
      phrases: Lowercase substrings to match in the driver's utterance.
        Each phrase is matched with substring-in semantics on whitespace-
        normalized text. See _matches_phrase below.
      confirmation_question: The yes/no question the assistant should
        ask the driver. Must be unambiguous and answerable with a single
        word. Use a TEMPLATE that bidi_app formats with the driver's
        first name when available.
      target_tier_on_confirm: Classification to apply if the driver
        confirms. P0 for all current categories; future additions might
        use P1 for less-severe-but-still-urgent reports.
      rationale: Short human-readable note explaining why a YES answer
        warrants this tier. Gets recorded on the audit trail so fleet
        ops can see the reasoning behind a driver-confirmed escalation.
    """

    name: str
    phrases: tuple[str, ...]
    confirmation_question: str
    target_tier_on_confirm: str
    rationale: str


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
# Order matters only for tie-breaking in match_emergency_keywords: the FIRST
# category with a matching phrase wins. Categories are ordered roughly by
# immediacy of physical risk so the most-critical wins on overlap.
# Phrases within a category are lowercase, trimmed of punctuation.

_BRAKE_FAILURE = EmergencyCategory(
    name="brake_failure",
    phrases=(
        "brakes failed",
        "brakes have failed",
        "brakes aren't working",
        "brakes are not working",
        "brakes aren't responding",
        "brakes are not responding",
        "lost brakes",
        "lost my brakes",
        "can't stop",
        "cannot stop",
        "brakes gone",
        "brakes are gone",
        "brake failure",
        "no brakes",
        "brake pedal to the floor",
        "pedal goes to the floor",
    ),
    confirmation_question=(
        "{driver_name}I need to confirm something critical. Are your "
        "brakes not stopping the vehicle at all? Please say yes or no."
    ),
    target_tier_on_confirm="P0",
    rationale=(
        "Driver verbally confirmed total brake failure. Sensor data did "
        "not corroborate, but driver-confirmed brake loss is always P0 "
        "because it removes the vehicle's primary means of stopping."
    ),
)

_STEERING_FAILURE = EmergencyCategory(
    name="steering_failure",
    phrases=(
        "can't steer",
        "cannot steer",
        "lost steering",
        "steering locked",
        "steering is locked",
        "steering wheel locked",
        "steering failed",
        "steering is gone",
        "wheel won't turn",
        "wheel doesn't turn",
    ),
    confirmation_question=(
        "{driver_name}I need to confirm something critical. Is the "
        "steering wheel unresponsive or locked? Please say yes or no."
    ),
    target_tier_on_confirm="P0",
    rationale=(
        "Driver verbally confirmed loss of steering control. Always P0: "
        "without steering the vehicle cannot be kept on the road."
    ),
)

_FIRE_SMOKE = EmergencyCategory(
    name="fire_or_smoke",
    phrases=(
        "engine fire",
        "engine is on fire",
        "the engine is on fire",
        "car is on fire",
        "there's a fire",
        "there is a fire",
        "smoke coming from",
        "smoke is coming",
        "smoke from the hood",
        "flames",
        "burning smell",
        "smells like burning",
        "smells like smoke",
        "something is burning",
    ),
    confirmation_question=(
        "{driver_name}I need to confirm something critical. Do you see "
        "flames or heavy smoke coming from your vehicle right now? "
        "Please say yes or no."
    ),
    target_tier_on_confirm="P0",
    rationale=(
        "Driver verbally confirmed active fire or heavy smoke. Always P0: "
        "vehicle must be evacuated, not driven."
    ),
)

_CRASH = EmergencyCategory(
    name="crash",
    phrases=(
        "i crashed",
        "we crashed",
        "i hit",
        "i've hit",
        "i have hit",
        "hit another car",
        "hit a car",
        "hit a tree",
        "hit a pole",
        "hit something",
        "been in an accident",
        "in an accident",
        "rolled the vehicle",
        "rolled the car",
        "flipped",
        "the car flipped",
    ),
    confirmation_question=(
        "{driver_name}I need to confirm something critical. Have you "
        "been in a collision just now? Please say yes or no."
    ),
    target_tier_on_confirm="P0",
    rationale=(
        "Driver verbally confirmed collision. Always P0: dispatch "
        "emergency response and preserve the scene."
    ),
)

_VISIBILITY = EmergencyCategory(
    name="visibility_loss",
    phrases=(
        "can't see",
        "cannot see",
        "windshield shattered",
        "windshield is shattered",
        "windshield broke",
        "windshield cracked through",
        "no visibility",
        "lost visibility",
    ),
    confirmation_question=(
        "{driver_name}I need to confirm something critical. Is your "
        "windshield broken or your view completely blocked right now? "
        "Please say yes or no."
    ),
    target_tier_on_confirm="P0",
    rationale=(
        "Driver verbally confirmed loss of forward visibility. Always "
        "P0: cannot safely continue driving without sight of the road."
    ),
)


EMERGENCY_CATEGORIES: tuple[EmergencyCategory, ...] = (
    _BRAKE_FAILURE,
    _STEERING_FAILURE,
    _FIRE_SMOKE,
    _CRASH,
    _VISIBILITY,
)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace. Cheap, deterministic, no regex."""
    return " ".join(text.lower().split())


def _matches_phrase(normalized_utterance: str, phrase: str) -> bool:
    """True if `phrase` appears as a substring of `normalized_utterance`.

    Substring rather than word-boundary matching is intentional: phrases
    are already whitespace-normalized and long enough that false positives
    are rare. "brake failure" matching inside "my brake failure started"
    is exactly what we want.

    `phrase` MUST already be lowercased and whitespace-normalized at
    module load time — caller does not re-normalize per call.
    """
    return phrase in normalized_utterance


def match_emergency_keywords(utterance: str) -> EmergencyCategory | None:
    """Return the first matching emergency category, or None.

    This function is the ONLY language-based decision the server makes
    about triage. It does NOT return a priority. It returns a category
    plus the canonical follow-up question for bidi_app to ask the driver.
    The actual escalation (if any) depends on the driver's yes/no
    confirmation to that question.

    Returns None when:
      - the utterance is empty,
      - or no phrase from any category matched.

    When multiple categories would match (e.g. a driver saying "I crashed
    and my brakes failed"), the first one in EMERGENCY_CATEGORIES wins.
    The order is hand-tuned so immediate-collision wins over
    brakes-failed, which wins over steering, etc. In practice this
    matters very rarely; a confirming driver on any of these paths ends
    up at P0.
    """
    if not utterance:
        return None
    normalized = _normalize(utterance)
    for category in EMERGENCY_CATEGORIES:
        for phrase in category.phrases:
            if _matches_phrase(normalized, phrase):
                return category
    return None


# ---------------------------------------------------------------------------
# Confirmation-response parsing
# ---------------------------------------------------------------------------
# Separate from match_emergency_keywords because it runs on a different
# turn (the driver's answer to the confirmation question). Kept in this
# module so all language-based triage logic is in one place.


# Affirmative phrases that count as a clear "yes" confirmation. Conservative
# — ambiguous responses get treated as "unclear" and routed to P1.
_AFFIRMATIVE = frozenset(
    [
        "yes",
        "yeah",
        "yep",
        "yup",
        "yes it is",
        "yes they are",
        "yes they're not",
        "yes confirmed",
        "confirmed",
        "affirmative",
        "correct",
        "that's correct",
        "that is correct",
        "true",
        "right",
        "exactly",
    ]
)

# Negative phrases that count as a clear "no" stand-down. Equally
# conservative — we'd rather re-ask than silently under-triage.
_NEGATIVE = frozenset(
    [
        "no",
        "nope",
        "not really",
        "no it's not",
        "no it is not",
        "no they're fine",
        "no they are fine",
        "no they're not",
        "no not at all",
        "negative",
        "false",
        "incorrect",
        "that's wrong",
        "that is wrong",
        "i was wrong",
        "never mind",
        "nevermind",
    ]
)


def parse_confirmation_response(utterance: str) -> str:
    """Classify the driver's answer to a confirmation question.

    Returns one of:
      "yes"      — clear affirmative; bidi_app should escalate to the
                   category's target tier.
      "no"       — clear negative; bidi_app should stand down and fall
                   back to the classifier's original result.
      "unclear"  — anything ambiguous, including silence, off-topic
                   answers, or mixed signals. bidi_app should escalate
                   to P1 defensively and record the unclear response.

    The matcher is intentionally strict. We'd rather classify a
    borderline response as "unclear" (which is the safer default per
    the confirmation design) than guess wrong on yes vs no.
    """
    if not utterance:
        return "unclear"
    normalized = _normalize(utterance)

    # Exact-match common responses first (cheap, covers 90%+ of cases).
    if normalized in _AFFIRMATIVE:
        return "yes"
    if normalized in _NEGATIVE:
        return "no"

    # Substring fallback with a one-shot "leading word" rule: accept
    # "yes, I'm pressing the pedal and nothing happens" as yes, but
    # NOT "I don't know if that's a yes or no" as yes.
    # Strip leading punctuation from the first token so "yes," and "yes."
    # both reduce to "yes".
    first_token = normalized.split(" ", 1)[0] if normalized else ""
    first_word = first_token.rstrip(",.!?;:")
    if first_word in {"yes", "yeah", "yep", "yup", "correct", "affirmative"}:
        return "yes"
    if first_word in {"no", "nope", "negative", "never"}:
        return "no"

    # Explicit "I don't know" / uncertainty → unclear.
    if any(
        indicator in normalized
        for indicator in (
            "i don't know",
            "i dont know",
            "not sure",
            "i'm not sure",
            "im not sure",
            "can't tell",
            "cant tell",
            "maybe",
            "i think so",
            "kind of",
            "sort of",
        )
    ):
        return "unclear"

    return "unclear"
