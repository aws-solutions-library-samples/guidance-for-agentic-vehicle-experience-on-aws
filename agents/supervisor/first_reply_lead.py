"""First-reply lead: the fault in plain words and the safe-to-drive verdict, as fixed text.

Built on ``demo/linkedin-video`` (CMS spec ``.kiro/specs/2026-10-29-avx-linkedin-demo-video/``) and
ported to main without that branch's demo hooks; see ``issues/2026-10-30-safe-to-drive-narration/``.

The problem
-----------
A driver asks "A warning light just came on. What's wrong, and is it safe to drive?".
The model has the codes (``get_active_dtcs``) and the priority (``triage``), and in the
recorded takes it did not reliably say either thing in plain words. It said the code's
meaning "isn't in the knowledge base", read out "reported via UDS 0x19 on signal_901",
skipped both, or turned triage's "limited" into "you should not continue driving".

Why code and not a prompt rule
------------------------------
Same reasoning as ``severity_narration``:
a boundary that exists only in a prompt will be crossed, and it was. The words come from
this module, not from the model. ``bidi_app`` puts them, quoted, in the platform note it
already injects for the service-center answer, tagged with the session's platform tag and
introduced by ``FIRST_REPLY_LEAD_NOTE``; the tool's offer follows as "Then tell the driver:
...". The tests in ``tests/test_first_reply_lead.py`` are the enforcement.

Where the words come from
-------------------------
* Verdict: ``lambdas/shared/dtc_severity.yaml`` ``safe_to_drive``, the table the triage
  classifier grades from, for the codes triage used (``ctx.triage_active_dtcs``). A code
  the table does not list gets the classifier's own default (``moderate/unknown/yes``).
  With several codes the strictest verdict leads (no > limited > yes), then severity.
* Agreement with the grade the driver is shown: triage returns only P0-P3, and its grade
  can also come from telemetry, recalls or predictive signals. The lead is built only when
  the table implies exactly the classification (most severe level over the codes), so
  "fine to keep using the car" can never sit beside a P1. This means "no other input is
  more severe", not "no other input contributed": a telemetry reading at the same level as
  the code still gets the lead.
* Fault description: never the dtc-history row's text, which for a live UDS fault is
  protocol text ("reported via UDS 0x19 on signal_901"). ``PLAIN_DESCRIPTIONS`` holds
  curated sentences taken from the CMS event catalog; any other code gets a sentence built
  from the table's ``system`` field, which ``SYSTEM_LABELS`` must cover (test-enforced).

What this does and does not guarantee
-------------------------------------
* It guarantees what the model is TOLD to say, word for word. It cannot make Nova say it:
  the model renders the note sentence by sentence and has reworded it before, and the screen shows Nova's
  transcript. (A platform-authored screen bubble was tried and dropped: iOS suppresses a
  transcript flagged ``_synthetic_from_auto_book`` as its own echo, and an unflagged one
  renders twice beside the model's sentences.) Whether Nova said them is judged from the
  recording, not asserted here.
* It rides on ``find_service_center``: the lead is quoted ahead of that tool's offer in its
  note, once per session, so it needs the model to call the tool (it did in take 3, the one recorded
  take of this prompt that ran its tools). It is not tied to the first user turn; a session whose first service-center answer comes
  later opens that answer with the fault.
* P0 is not handled here, in any sense: ``bidi_app._first_reply_lead`` stands down for a
  classifier P0 and whenever the platform's own view differs from the grade (a surfaced tier
  that is not the classifier's, a reported hazard, a confirmed P0, a waiting safety
  question). The directive (``hazard_directive``, ``_show_p0_directive``) owns those replies.
* If the model contradicts the verdict in its own words, ``severity_narration`` still takes
  that back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

log = logging.getLogger(__name__)

# The table the triage classifier grades from. It ships in the runtime bundle (the whole
# working tree is zipped); nothing checks that at deploy time, so ``load_severity_table``
# logs the row count it loaded and the first live turn is where that is confirmed.
SEVERITY_TABLE_PATH = Path(__file__).resolve().parents[2] / "lambdas" / "shared" / "dtc_severity.yaml"

# Mirrors ``lambdas/triage-classifier/classifier.py`` ``_DEFAULT_DTC``; a test pins them equal.
DEFAULT_ENTRY: Mapping[str, str] = {"severity": "moderate", "system": "unknown", "safe_to_drive": "yes"}

# One fixed phrase per ``safe_to_drive`` value. The wording is claim-neutral for
# ``severity_narration``: "yes" and "limited" must make no stop / urgent / routine claim, so
# the platform's own line can never trigger its own correction (test-enforced, every
# classification). In particular neither says "safe to drive" or "keep driving", which that
# check reads as a routine claim and would flag against a P1.
VERDICT_PHRASES: Mapping[str, str] = {
    "yes": "It's fine to keep using the car as normal for now.",
    "limited": (
        "You can still drive it, but only with care: keep trips short and avoid "
        "highway speeds until it has been checked."
    ),
    # Never delivered through ``build_lead`` (every "no" in the table grades P0, which the
    # directive owns); present so every verdict value has a phrase, test-enforced.
    "no": "Please don't drive it. Pull over somewhere safe and stop.",
}

# (severity, safe_to_drive) -> level; mirrors ``classifier._DTC_TO_LEVEL`` (a test pins them equal).
TABLE_LEVELS: Mapping[tuple[str, str], str] = {
    ("critical", "no"): "P0", ("critical", "limited"): "P1", ("critical", "yes"): "P1",
    ("high", "no"): "P0", ("high", "limited"): "P1", ("high", "yes"): "P1",
    ("moderate", "no"): "P1", ("moderate", "limited"): "P2", ("moderate", "yes"): "P2",
    ("low", "no"): "P2", ("low", "limited"): "P3", ("low", "yes"): "P3",
}

# How the injected note introduces the quoted fixed words when a lead is present; the tool's offer
# then follows "Then tell the driver:". Same form as the P0 directive's "word for word, exactly as written".
FIRST_REPLY_LEAD_NOTE = "Say this to the driver word for word, exactly as written, adding and changing nothing"

# Strictest first.
_VERDICT_STRICTNESS = {"no": 2, "limited": 1, "yes": 0}
_SEVERITY_RANK = {"critical": 3, "high": 2, "moderate": 1, "low": 0}

# Curated plain-language sentences, from the CMS event catalog (``event-catalog.json``).
PLAIN_DESCRIPTIONS: Mapping[str, str] = {
    # safety.service_steering: "Electric power steering fault — increased steering effort required"
    "C0460": "Your power steering has a fault, so the steering may feel heavier than usual.",
}

# Every ``system`` value in the table needs a label, so the fallback never speaks a raw token.
SYSTEM_LABELS: Mapping[str, str] = {
    "abs": "anti-lock brake",
    "body": "body",
    "brakes": "brake",
    "communication": "vehicle communication",
    "cooling": "cooling",
    "electrical": "electrical",
    "emissions": "emissions",
    "fuel": "fuel",
    "hv_battery": "high-voltage battery",
    "powertrain": "powertrain",
    "restraints": "airbag and seatbelt",
    "safety": "safety",
    "steering": "steering",
    "transmission": "transmission",
}

_GENERIC_FAULT_SENTENCE = "Your car has reported a fault."

# P0 is the directive's. A classification outside this set has no verdict to narrate.
_LEAD_CLASSIFICATIONS = frozenset({"P1", "P2", "P3"})


@dataclass(frozen=True)
class FirstReplyLead:
    """The fixed opening of the first reply about an active fault."""

    code: str
    verdict: str
    fault_sentence: str
    verdict_phrase: str

    @property
    def text(self) -> str:
        return f"{self.fault_sentence} {self.verdict_phrase}"


_TABLE_CACHE: dict[str, Mapping[str, Mapping[str, str]]] = {}


def load_severity_table() -> Mapping[str, Mapping[str, str]]:
    """The classifier's severity table, or an empty mapping when it cannot be read.

    Empty means no lead is built (the reply is the model's, as before) and an ERROR is
    logged. It never guesses a verdict. A failed read is not cached, so a table that
    becomes readable is used on the next call.
    """
    cached = _TABLE_CACHE.get("table")
    if cached is not None:
        return cached
    try:
        import yaml

        table = yaml.safe_load(SEVERITY_TABLE_PATH.read_text()) or {}
        if not isinstance(table, dict) or not table:
            raise ValueError("severity table is empty or not a mapping")
    except Exception:  # noqa: BLE001
        log.exception("first-reply-lead: could not read %s; no lead will be built", SEVERITY_TABLE_PATH)
        return {}
    _TABLE_CACHE["table"] = table
    log.info("first-reply-lead: severity table loaded rows=%d", len(table))
    return table


def _entry_for(code: str, table: Mapping[str, Mapping[str, str]]) -> Mapping[str, str]:
    entry = table.get(code)
    if not isinstance(entry, Mapping) or entry.get("safe_to_drive") not in VERDICT_PHRASES:
        return DEFAULT_ENTRY
    return entry


def pick_primary_code(
    codes: Iterable[str], table: Mapping[str, Mapping[str, str]]
) -> Optional[tuple[str, Mapping[str, str]]]:
    """The code that drives the verdict: strictest verdict, then severity, then input order."""
    best: Optional[tuple[tuple[int, int], str, Mapping[str, str]]] = None
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            continue
        code = code.strip()
        entry = _entry_for(code, table)
        rank = (
            _VERDICT_STRICTNESS.get(entry.get("safe_to_drive", ""), 0),
            _SEVERITY_RANK.get(entry.get("severity", ""), 0),
        )
        if best is None or rank > best[0]:
            best = (rank, code, entry)
    return None if best is None else (best[1], best[2])


def implied_level(codes: Iterable[str], table: Mapping[str, Mapping[str, str]]) -> Optional[str]:
    """The most severe level the table gives any of ``codes`` (what triage grades from codes alone)."""
    levels = []
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            continue
        entry = _entry_for(code.strip(), table)
        level = TABLE_LEVELS.get((entry.get("severity", ""), entry.get("safe_to_drive", "")))
        if level is None:
            return None
        levels.append(level)
    return min(levels) if levels else None  # "P0" < "P1" < "P2" < "P3"


def fault_sentence(code: str, entry: Mapping[str, str]) -> str:
    """The plain-words sentence for a code: curated, else from its system, else generic."""
    curated = PLAIN_DESCRIPTIONS.get(code)
    if curated:
        return curated
    label = SYSTEM_LABELS.get(entry.get("system", ""))
    if label:
        return f"Your car has reported a fault in the {label} system."
    return _GENERIC_FAULT_SENTENCE


def build_lead(
    codes: Optional[Iterable[str]],
    classification: Optional[str],
    table: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Optional[FirstReplyLead]:
    """The lead for a session whose triage used ``codes`` and graded ``classification``.

    ``None`` when there is nothing to say: no active code, a P0 (the directive's), no
    classification yet, a table that could not be read, or a grade the table does not imply.
    """
    if classification not in _LEAD_CLASSIFICATIONS or not codes:
        return None
    table = load_severity_table() if table is None else table
    if not table:
        return None
    picked = pick_primary_code(codes, table)
    if picked is None:
        return None
    if implied_level(codes, table) != classification:
        # Another input (telemetry, a recall, a tenant rule) set the grade, so the table's
        # verdict for these codes may not describe it. Say nothing rather than reassure.
        log.info(
            "first-reply-lead: no lead, table implies %s but triage graded %s",
            implied_level(codes, table), classification,
        )
        return None
    code, entry = picked
    verdict = entry["safe_to_drive"]
    return FirstReplyLead(
        code=code,
        verdict=verdict,
        fault_sentence=fault_sentence(code, entry),
        verdict_phrase=VERDICT_PHRASES[verdict],
    )
