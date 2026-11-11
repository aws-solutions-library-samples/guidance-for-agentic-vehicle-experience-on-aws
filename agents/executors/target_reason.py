"""The target's own refusal words, passed through word for word (contract § 10b).

When a deterministic target refuses an Action, the operator should read the target's
own words: CMS's "Vehicle <vin> is grounded and can't be moved." rather than AVX's
"CMS refused the request (HTTP 409).", or DMS's warranty verdict reason code
(``miles_term_expired``). A deterministic seam's refusal is narrated verbatim
(agentic-tiers), so AVX never rewords either. The text goes in the audit entry's
``target_reason``.
``failure_reason`` keeps its fixed § 10a vocabulary and is still written beside it.

The text comes from another system, so it is untrusted:

* An executor only *reads* it, from one named field of a 4xx JSON body
  (``read_reason_field``). It never cleans or judges it.
* ``registry.dispatch()`` is the one place that keeps it (``kept_target_reason``):
  only on ``target_refused`` with a status this module allows for that target, and
  only after ``clean_target_reason``. Everything else is dropped, so a 5xx body, an
  exception message or a terminal error can never reach the record.
* ``clean_target_reason`` returns plain text or ``None``. It never raises and never
  cuts a sentence: text it can't pass unchanged in meaning is dropped whole, and the
  screen falls back to ``failure_reason``.

CMS's queue route re-checks the stored value with the same rules before showing it
(``modules/cms_ui/source/handlers/main_api/fi_action_queue.py``).
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any, Optional

# Module-level on purpose: an import failure here fails at cold start (the bundle
# cold-start test imports this module), not silently inside kept_target_reason.
from agents.executors.base import classify_status

#: Longest sentence kept. Longer text is dropped, not cut.
MAX_LEN = 200

#: Largest response body read for the field. A refusal sentence is a few hundred
#: bytes; anything bigger is not one, and is not parsed.
MAX_BODY_BYTES = 4096

#: Targets whose refusal sentence is kept, and the 4xx statuses it is kept for
#: (``None`` = every status ``classify_status`` calls a refusal).
#: * ``cms``: the FI move, grounding and cost-action routes answer ``{"error": ...}``.
#: * ``dms``: only a 422. The executor reads ``verdict_reason`` only on a warranty
#:   claim's 422 (coverage denied): DMS's deterministic verdict, a reason code such as
#:   ``miles_term_expired`` (DMS ``source/_lib/warranty_rules.py``).
ALLOWED_STATUSES: dict[str, Optional[frozenset[int]]] = {
    "cms": None,
    "dms": frozenset({422}),
}

#: Unicode categories removed: controls, format characters (bidi overrides,
#: zero-width characters, BOM), surrogates, private use and unassigned.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

#: Markup characters. Text containing either is dropped whole: a refusal sentence
#: never needs them, and dropping keeps the text plain wherever it is shown.
_MARKUP = frozenset("<>")


def clean_target_reason(raw: object) -> Optional[str]:
    """Plain-text form of ``raw``, or ``None`` when it can't be passed through.

    * Not a ``str``, or longer than ``MAX_BODY_BYTES`` characters → ``None``.
    * Every whitespace character (newline, tab, no-break space, line separator …)
      becomes one space; characters in ``_DROPPED_CATEGORIES`` are removed; runs of
      spaces collapse; ends are stripped.
    * Empty after that, longer than ``MAX_LEN``, or containing ``<`` or ``>`` →
      ``None``.

    An ordinary sentence comes back unchanged. Never raises.
    """
    try:
        if not isinstance(raw, str):
            return None
        # Text can only come from a body of at most MAX_BODY_BYTES, so anything longer
        # is not a refusal sentence. Checked first to bound the per-character work.
        if len(raw) > MAX_BODY_BYTES:
            return None
        out: list[str] = []
        for ch in raw:
            if ch.isspace():
                out.append(" ")
            elif unicodedata.category(ch) in _DROPPED_CATEGORIES:
                continue
            else:
                out.append(ch)
        text = " ".join(part for part in "".join(out).split(" ") if part)
        if not text or len(text) > MAX_LEN:
            return None
        if any(ch in _MARKUP for ch in text):
            return None
        return text
    except Exception:  # noqa: BLE001 — a reason is optional; never fail the dispatch
        return None


def kept_target_reason(
    *, target_system: object, failure_kind: object, error_status: object, raw: object,
) -> Optional[str]:
    """The ``target_reason`` ``registry.dispatch()`` records, or ``None``.

    Kept only when the target is in ``ALLOWED_STATUSES``, the result is
    ``target_refused``, ``error_status`` is a plain int in 400–499 that the target's
    entry allows (for ``None``: one ``classify_status`` calls ``"refused"``), and
    ``clean_target_reason(raw)`` passes. Never raises.
    """
    try:
        if failure_kind != "target_refused":
            return None
        if not isinstance(target_system, str) or target_system not in ALLOWED_STATUSES:
            return None
        if type(error_status) is not int or not 400 <= error_status <= 499:
            return None
        allowed = ALLOWED_STATUSES[target_system]
        if allowed is None:
            # Only a 4xx the executors treat as a refusal: not 401/403/404/405/415
            # (our plumbing) and not 408/425/429 (retryable).
            if classify_status(error_status) != "refused":
                return None
        elif error_status not in allowed:
            return None
        return clean_target_reason(raw)
    except Exception:  # noqa: BLE001
        return None


def read_reason_field(resp: Any, field: str) -> Optional[str]:
    """The string at ``field`` in a small JSON-object response body, else ``None``.

    For executors, on a refusal only. Reads ``resp.content`` (bytes) and parses it
    only when it is at most ``MAX_BODY_BYTES``. No cleaning here; that is
    ``registry.dispatch()``'s job. Never raises.
    """
    try:
        content = resp.content
        if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_BODY_BYTES:
            return None
        body = json.loads(content.decode("utf-8"))
        if not isinstance(body, dict):
            return None
        value = body.get(field)
        return value if isinstance(value, str) else None
    except Exception:  # noqa: BLE001
        return None
