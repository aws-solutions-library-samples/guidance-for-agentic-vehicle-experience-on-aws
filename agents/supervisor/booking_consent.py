"""Driver consent before a booking: the yes/no words, and the text runtime's recorder.

The model's ``@tool book`` refuses to create a booking unless the driver said
yes to the service center about to be booked (``tools/book.py``
``_consent_check``). This module holds what both runtimes share:

* the affirmation words and ``is_affirmation`` (moved from ``bidi_app.py``,
  which re-exports them under their old names), and
* the refusal check ``is_booking_refusal``, which clears a recorded yes.

and what only the text runtime needs:

* ``is_typed_booking_yes``: a typed yes ("Yes, book it") as well as the voice
  forms;
* a per-session store, because the text runtime builds a new
  ``SessionContext`` for every message while the offer and the driver's
  answer arrive in different messages;
* ``begin_text_turn`` / ``end_text_turn`` around each message, and
  ``TextOfferRecorder``, a Strands hook that records ``find_service_center``'s
  first center as the offer.

Rules on the text runtime (issues/2026-11-02-text-runtime-book-consent/):

1. A refusal word clears the offer and any yes.
2. A typed yes counts only in the message right after the one that made the
   offer, and only while the offer is younger than ``TEXT_CONSENT_TTL_SECONDS``.
   A message that never reaches the model (input blocked, turn cap) drops the
   offer and yes.
3. A yes, once given, stays until a booking uses it, a refusal clears it, or a
   later ``find_service_center`` call returns a different first center.
4. Only a ranked ``find_service_center`` result (``locationKnown`` True) is an
   offer: with the location unknown the tool names no center.
5. A message whose model call fails, or whose reply the output guardrail
   blocks, ends with nothing stored. When messages of one session overlap, a
   message saves only if no other message saved since it began; one that began
   on an empty store can still save its own offer (never a yes, which needs a
   stored offer to begin with).

The store lives in process memory. AgentCore Runtime keeps one microVM per
``runtimeSessionId`` and terminates it after 15 minutes idle, so a lost store
only loses a pending offer or yes: ``book`` then refuses and the model asks
again (fail-closed).

Issues: issues/2026-11-02-agent-books-unasked/ (voice gate),
issues/2026-11-02-text-runtime-book-consent/ (text runtime, refusal words).
"""

from __future__ import annotations

import ast
import itertools
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Yes and no
# ---------------------------------------------------------------------------

# A forgiving yes check (moved verbatim from bidi_app.py): a text made only of
# single-word affirmative tokens, or one of a few short phrases. On voice it
# also gates the auto-book fallback, so its behaviour must not change.
AFFIRMATIVE_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "please", "ok", "okay",
})
AFFIRMATIVE_PHRASES: frozenset[str] = frozenset({
    "go ahead", "do it", "book it", "schedule it", "yes please",
    "lets do it", "let us do it", "sounds good", "sure thing",
})


def is_affirmation(text: str) -> bool:
    """True if `text` reads as a plain yes-style confirmation.

    Examples that match:
      "yes", "Yes.", "yes please", "yes. yes.", "Yeah sure", "go ahead",
      "Okay, do it"
    Examples that don't:
      "no thanks", "yes but tomorrow", "yes I have a question",
      anything > 30 chars (probably a real follow-up sentence).
    """
    if not text:
        return False
    if len(text) > 30:
        # Long utterances are real follow-ups, not pure confirmations.
        return False
    # Strip punctuation, collapse whitespace, lowercase.
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if cleaned in AFFIRMATIVE_PHRASES:
        return True
    return all(word in AFFIRMATIVE_WORDS for word in cleaned.split())


# Single words that read as a booking refusal. False positives (e.g. "not now"
# to a different question) only cost a re-offer, never a booking.
BOOKING_REFUSAL_TOKENS: frozenset[str] = frozenset({
    "no", "nope", "nah", "not", "dont", "cancel", "stop", "wait", "later",
    "nevermind", "refuse",
})
# Refusals that are two words. Matched as whole words in order, so "never had"
# and "mind if I ask" don't refuse.
BOOKING_REFUSAL_PHRASES: tuple[str, ...] = ("never mind", "forget it")


def _words(text: str) -> list[str]:
    """Lowercase words with apostrophes dropped ("don't" -> "dont") and other
    punctuation read as a space ("never-mind" -> "never", "mind")."""
    lowered = text.lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^\w\s]", " ", lowered).split()


def is_booking_refusal(text: str) -> bool:
    """True if the driver's words take back (or decline) a booking."""
    if not text:
        return False
    words = _words(text)
    if set(words) & BOOKING_REFUSAL_TOKENS:
        return True
    joined = f" {' '.join(words)} "
    return any(f" {phrase} " in joined for phrase in BOOKING_REFUSAL_PHRASES)


# Typed yes (text runtime only; voice keeps is_affirmation). A short message
# counts when every word is a yes-word or booking filler, it carries a strong
# yes-word or yes-phrase, and it isn't a question. "ok"/"please"/"thanks" alone
# around filler ("ok thanks") are acknowledgements, not a yes; "ok" or "please"
# on their own still pass through is_affirmation.
_TYPED_YES_EXTRA_WORDS: frozenset[str] = frozenset({
    "book", "schedule", "it", "that", "go", "ahead", "and", "do", "thanks", "thank", "you",
    "lets", "let", "us", "sounds", "good", "great", "works", "for", "me", "thing", "there",
})
_TYPED_YES_STRONG_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "book", "schedule",
})
_TYPED_YES_PHRASES: tuple[str, ...] = (
    "go ahead", "do it", "sounds good", "that works", "lets do it",
)
_TYPED_YES_MAX_CHARS = 40


def is_typed_booking_yes(text: str) -> bool:
    """True if a typed message is a yes to the offered booking.

    Accepts everything ``is_affirmation`` accepts, plus short messages such as
    "Yes, book it", "ok, go ahead and book it", "Let's do it.", "Sounds good,
    book it", "That works". Not a yes: "yes but tomorrow", "ok thanks",
    "book?", "what time?".
    """
    if not text or text.strip().endswith("?"):
        # A question ("yes?", "book it?") is not an answer.
        return False
    if is_affirmation(text):
        return True
    if len(text) > _TYPED_YES_MAX_CHARS:
        return False
    words = _words(text)
    if not words:
        return False
    if not all(w in AFFIRMATIVE_WORDS or w in _TYPED_YES_EXTRA_WORDS for w in words):
        return False
    if any(w in _TYPED_YES_STRONG_WORDS for w in words):
        return True
    joined = f" {' '.join(words)} "
    return any(f" {phrase} " in joined for phrase in _TYPED_YES_PHRASES)


def same_center(a: dict, b: dict | None) -> bool:
    """True if two service-center dicts name the same center.

    Prefers a stable ``centerId`` match; falls back to name + address. Two dicts
    with no identifying field (only distance/phone/type) never match.
    """
    if b is None:
        return False
    if a.get("centerId") and b.get("centerId"):
        return a["centerId"] == b["centerId"]
    if not (a.get("name") or a.get("address")) or not (b.get("name") or b.get("address")):
        return False
    return a.get("name") == b.get("name") and a.get("address") == b.get("address")


# ---------------------------------------------------------------------------
# Text runtime: per-session store
# ---------------------------------------------------------------------------

TEXT_CONSENT_TTL_SECONDS = 30 * 60
TEXT_STORE_MAX_SESSIONS = 1024


@dataclass
class _TextBookingState:
    offer: dict | None
    consent: dict | None
    # True when the offer was made in the most recent message, so the next
    # message's yes answers it.
    answerable: bool
    saved_at: float
    # Bumped on every save. end_text_turn keeps nothing if another message of
    # the same session saved after this one began (overlapping messages), so a
    # "never mind" can't be overwritten by an earlier yes that finished later.
    version: int


_text_store: "OrderedDict[tuple[str, str], _TextBookingState]" = OrderedDict()
_text_lock = threading.Lock()
_text_versions = itertools.count(1)


def _now() -> float:
    return time.monotonic()


def text_key(actor_id: str | None, session_id: str | None) -> tuple[str, str]:
    """Store key: the caller's user (JWT ``sub``) AND the session, so one user's
    offer or yes never reaches another user's session."""
    return (actor_id or "", session_id or "")


def _center_label(center: dict | None) -> str:
    c = center or {}
    return str(c.get("centerId") or c.get("name") or "?")


def begin_text_turn(ctx: Any, key: tuple[str, str], text: str) -> int:
    """Load the session's offer and yes onto ``ctx`` and apply the typed answer.

    Call once per message, before the model runs. Returns the stored state's
    version, which the caller passes back to ``end_text_turn``. Never raises: on
    an internal error ``ctx`` is left with no offer and no yes (fail-closed) and
    the returned version is -1, which never matches, so nothing is saved.
    """
    try:
        with _text_lock:
            st = _text_store.get(key)
            if st is not None and _now() - st.saved_at > TEXT_CONSENT_TTL_SECONDS:
                _text_store.pop(key, None)
                st = None
        seen = st.version if st else 0
        offer = dict(st.offer) if st and st.offer else None
        consent = dict(st.consent) if st and st.consent else None
        answerable = bool(st and st.answerable)

        if text and is_booking_refusal(text):
            if offer or consent:
                log.info(
                    "booking(text): offer and consent cleared by refusal session=%s text=%.40r",
                    key[1], text,
                )
            offer = None
            consent = None
        elif answerable and offer is not None and is_typed_booking_yes(text):
            consent = dict(offer)
            log.info("booking(text): consent set from offer session=%s center=%s",
                     key[1], _center_label(consent))

        ctx.booking_offer = offer
        ctx.booking_consent = consent
        ctx.last_offered_service_center = dict(offer) if offer else None
        ctx.booking_offer_made_this_turn = False
        return seen
    except Exception:  # noqa: BLE001
        log.exception("booking(text): begin_text_turn failed session=%s", key[1])
        ctx.booking_offer = None
        ctx.booking_consent = None
        ctx.last_offered_service_center = None
        ctx.booking_offer_made_this_turn = False
        return -1


def end_text_turn(ctx: Any, key: tuple[str, str], *, ok: bool, seen_version: int) -> None:
    """Save the session's offer and yes after the message.

    Nothing is kept when ``ok`` is False (the model call raised or the reply was
    blocked), or when the stored version is no longer ``seen_version`` (another
    message of this session saved while this one ran). A booking made in this
    message has already cleared the yes (``@tool book`` consumes it). Never
    raises.
    """
    try:
        if ok:
            offer = getattr(ctx, "booking_offer", None)
            consent = getattr(ctx, "booking_consent", None)
            answerable = bool(getattr(ctx, "booking_offer_made_this_turn", False)) and offer is not None
        else:
            offer = None
            consent = None
            answerable = False
        with _text_lock:
            current = _text_store.get(key)
            if (current.version if current else 0) != seen_version:
                log.info("booking(text): overlapping messages — offer and consent dropped session=%s", key[1])
                _text_store.pop(key, None)
                return
            if offer is None and consent is None:
                _text_store.pop(key, None)
                return
            _text_store[key] = _TextBookingState(
                offer=dict(offer) if offer else None,
                consent=dict(consent) if consent else None,
                answerable=answerable,
                saved_at=_now(),
                version=next(_text_versions),
            )
            _text_store.move_to_end(key)
            while len(_text_store) > TEXT_STORE_MAX_SESSIONS:
                _text_store.popitem(last=False)
    except Exception:  # noqa: BLE001
        log.exception("booking(text): end_text_turn failed session=%s", key[1])
        with _text_lock:
            _text_store.pop(key, None)


def forget_text_session(key: tuple[str, str]) -> None:
    """Drop the session's offer and yes (a message that never reached the model:
    input blocked, turn cap). Never raises."""
    try:
        with _text_lock:
            if _text_store.pop(key, None) is not None:
                log.info("booking(text): offer and consent dropped (message not answered) session=%s", key[1])
    except Exception:  # noqa: BLE001
        log.exception("booking(text): forget_text_session failed session=%s", key[1])


# ---------------------------------------------------------------------------
# Text runtime: the offer recorder
# ---------------------------------------------------------------------------


def _parse_tool_text(text: str) -> dict | None:
    """A tool's returned dict from its ToolResult text.

    Strands sends ``json.dumps(result)`` when it can, and ``str(result)`` (a
    Python repr) when it can't. ``find_service_center`` hits the second case on
    live data: the DynamoDB resource layer returns numbers as ``Decimal``, which
    ``json.dumps`` rejects. Read the repr the way ``bidi_app.py``'s tool_result
    handler does: drop the ``Decimal('...')`` wrappers, then ``ast.literal_eval``
    (literals only; nothing is executed).
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        try:
            data = ast.literal_eval(re.sub(r"Decimal\('([^']+)'\)", r"\1", text))
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None
    return data if isinstance(data, dict) else None


def _tool_result_data(result: Any) -> dict | None:
    """The dict a successful tool returned, from a Strands ToolResult."""
    if not isinstance(result, dict) or result.get("status") != "success":
        return None
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("json"), dict):
            return block["json"]
        if isinstance(block.get("text"), str):
            return _parse_tool_text(block["text"])
    return None


def record_offer_from_tool_result(ctx: Any, tool_name: str | None, result: Any) -> None:
    """Record ``find_service_center``'s first center as the offer.

    Only a ranked result counts (``locationKnown`` is True): when the vehicle's
    location is unknown the tool returns centers in arbitrary order and its
    narration names none of them, asking for the driver's city instead, so a
    "yes" to that question must not book its first entry.

    A yes already given survives only if the new first center is the same one;
    a different center needs its own yes. Never raises.
    """
    if tool_name != "find_service_center":
        return
    try:
        data = _tool_result_data(result)
        if not data or not data.get("found"):
            return
        if data.get("locationKnown") is not True:
            log.info("booking(text): no offer recorded — centers not ranked (location unknown) session=%s",
                     getattr(ctx, "session_id", "?"))
            return
        centers = data.get("centers") or []
        if not centers or not isinstance(centers[0], dict):
            return
        new = dict(centers[0])
        consent = getattr(ctx, "booking_consent", None)
        if consent is not None and not same_center(consent, new):
            ctx.booking_consent = None
            log.info("booking(text): consent cleared by a different offer session=%s old=%s new=%s",
                     getattr(ctx, "session_id", "?"), _center_label(consent), _center_label(new))
        ctx.booking_offer = new
        ctx.last_offered_service_center = dict(new)
        ctx.booking_offer_made_this_turn = True
        log.info("booking(text): offer set session=%s center=%s",
                 getattr(ctx, "session_id", "?"), _center_label(new))
    except Exception:  # noqa: BLE001
        log.exception("booking(text): could not record the offer session=%s",
                      getattr(ctx, "session_id", "?"))


class TextOfferRecorder:
    """Strands hook provider: after each tool call, record a service-center offer."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def register_hooks(self, registry: Any, **_kwargs: Any) -> None:
        from strands.hooks import AfterToolCallEvent  # noqa: PLC0415

        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def _after_tool_call(self, event: Any) -> None:
        tool_use = getattr(event, "tool_use", None) or {}
        record_offer_from_tool_result(self._ctx, tool_use.get("name"), getattr(event, "result", None))
