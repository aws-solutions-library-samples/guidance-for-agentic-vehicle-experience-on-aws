"""The stop-driving eval case, scored by evals/runner/fi_text_case.py.

Issue: issues/2026-10-27-stop-driving-verbatim-in-fleet-chat/report.md. The case is the
live check of a fleet-health stop-driving item's narration (run by
scripts/live-verify-stop-driving-verbatim.py against the deployed text runtime), so these
tests pin two things:

- the case demands every sentence of the directive the script plants, plus the marker that
  only a full reading of the planted text carries, and forbids the doctrine's example
  softening; editing the case down to "stop driving" fails here;
- a verbatim reply passes and each kind of softening fails.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from evals.runner.fi_text_case import evaluate_response, load_case

_REPO = Path(__file__).resolve().parents[2]
_CASE_PATH = _REPO / "evals/cases/fi/fleet-health-stop-driving-text-001.yaml"
_SCRIPT_PATH = _REPO / "scripts/live-verify-stop-driving-verbatim.py"

_DIRECTIVE = (
    "Stop driving safely now. Do not restart the vehicle. "
    "Contact Meridian Roadside Assistance."
)
_VERBATIM_REPLY = (
    "Stop driving safely now. Do not restart the vehicle. Contact Meridian Roadside "
    "Assistance. LIVETEST-SDV marker.\n\nThis was raised because the vehicle has an open "
    "stop-driving fault, P0AA6, and needs service at dealer-denver this week."
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=\.)\s+", text.strip()) if s.strip()]


def _script():
    spec = importlib.util.spec_from_file_location("_lv_stop_driving", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def case():
    return load_case(_CASE_PATH)


# ===========================================================================
# The case file
# ===========================================================================


def test_case_loads_as_a_fleet_manager_text_case(case) -> None:
    assert case.id == "fleet-health-stop-driving-text-001"
    assert case.persona == "fleet-manager"
    names = [t if isinstance(t, str) else t.get("name") for t in case.expected.tool_calls]
    assert names == ["fleet_recommendation"]


def test_case_demands_every_sentence_of_the_planted_directive(case) -> None:
    mod = _script()
    planted = mod.build_plant("sdv-test-run", "2026-10-27T22:00:00Z")["disclosure_txt"]
    directive_part = planted[: planted.index(mod.MARKER)].strip()
    assert directive_part == mod.DIRECTIVE == _DIRECTIVE
    assert case.expected.response["must_contain_all"] == _sentences(mod.DIRECTIVE), (
        "the case must list each sentence of the planted directive, unchanged"
    )
    assert case.expected.response["tool_evidence"] == [mod.MARKER.rstrip(".")]
    assert mod.MARKER in planted


def test_case_utterance_names_the_planted_finding(case) -> None:
    assert _script().FINDING_ID in case.input.utterance


def test_case_forbids_the_doctrine_softening(case) -> None:
    """The whole list, so dropping any entry fails (agentic-tiers.md names "exercise caution")."""
    assert case.expected.response["must_not_contain"] == [
        "exercise caution", "when convenient", "it can wait", "probably fine",
        "when you get a chance",
    ]


# ===========================================================================
# Scoring
# ===========================================================================


def test_verbatim_reply_passes(case) -> None:
    passed, missing = evaluate_response(case, _VERBATIM_REPLY, tool_calls=None)
    assert passed, missing


@pytest.mark.parametrize(
    "reply, expect_missing",
    [
        # softened: keeps the meaning, loses the words
        ("You should stop driving soon. Avoid restarting the vehicle. Contact Meridian "
         "Roadside Assistance. LIVETEST-SDV marker.", "phrase:Stop driving safely now."),
        # a sentence dropped
        ("Stop driving safely now. Contact Meridian Roadside Assistance. LIVETEST-SDV marker.",
         "phrase:Do not restart the vehicle."),
        # verbatim words, hedge added
        (_VERBATIM_REPLY + " Until then, exercise caution.", "forbidden:exercise caution"),
        (_VERBATIM_REPLY + " Honestly it can wait until the end of the shift.",
         "forbidden:it can wait"),
        # summarised from the headline, never read the tool's text through
        ("Stop driving safely now. Do not restart the vehicle. Contact Meridian Roadside "
         "Assistance.", "evidence:LIVETEST-SDV marker"),
    ],
)
def test_softened_or_partial_reply_fails(case, reply: str, expect_missing: str) -> None:
    passed, missing = evaluate_response(case, reply, tool_calls=None)
    assert not passed
    assert expect_missing in missing, missing
