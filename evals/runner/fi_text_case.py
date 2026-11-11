"""Load and evaluate FI text-runtime eval cases (decisions.md call 60).

Task 5.5 runs cases from evals/cases/fi/ via /assistant/chat as fleet_manager.
This module is unit-tested by test_fi_text_case.py; it is not imported by
any Tier 3 bidi runner.

Public API
----------
load_case(path)                    -> EvalCase
evaluate_response(case, reply_text, tool_calls) -> tuple[bool, list[str]]
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from evals.runner.schema import EvalCase


def load_case(path: str | Path) -> EvalCase:
    """Load and schema-validate a YAML case file.

    Parameters
    ----------
    path:
        Absolute or repo-relative path to the YAML file.

    Returns
    -------
    EvalCase
        Validated case object.
    """
    raw = yaml.safe_load(Path(path).read_text())
    return EvalCase.model_validate(raw)


_KNOWN_RESPONSE_KEYS: frozenset[str] = frozenset(
    {"must_contain_all", "must_not_contain", "tool_evidence"}
)


def evaluate_response(
    case: EvalCase,
    reply_text: str,
    tool_calls: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate an agent reply against the case's expected assertions.

    Checks (all case-insensitive):
    * Every phrase in ``expected.response.must_contain_all`` is present.
    * No phrase in ``expected.response.must_not_contain`` is present.
    * When *tool_calls* is None: every phrase in ``expected.response.tool_evidence``
      must appear in the reply (the phrase is a marker carried only by the tool's
      output, so its presence proves the tool was called and read).
    * When *tool_calls* is provided: every tool name in
      ``expected.tool_calls[*].name`` appears in *tool_calls*.

    Raises
    ------
    ValueError
        If ``expected.response`` contains an unknown key (catches misspellings
        or unsupported assertion types that would otherwise pass vacuously).
    ValueError
        If the case has no phrase assertion (neither ``must_contain_all`` nor
        ``tool_evidence``) — such a case cannot distinguish a correct from an
        incorrect reply.

    Parameters
    ----------
    case:
        Loaded eval case.
    reply_text:
        The agent's text reply.
    tool_calls:
        List of tool names called during the turn (order ignored).  Pass
        ``None`` when tool calls are not observable (e.g. text runtime); the
        evaluator then checks ``tool_evidence`` phrases instead.

    Returns
    -------
    (passed, missing)
        *passed* is True iff nothing is missing.  *missing* names each
        failing assertion: ``'phrase:<phrase>'``, ``'forbidden:<phrase>'``,
        or ``'evidence:<phrase>'``.
    """
    resp: dict[str, Any] = case.expected.response or {}

    unknown = set(resp) - _KNOWN_RESPONSE_KEYS
    if unknown:
        raise ValueError(f"unknown response keys: {sorted(unknown)}")

    has_phrase_assertion = bool(resp.get("must_contain_all") or resp.get("tool_evidence"))
    if not has_phrase_assertion:
        raise ValueError(
            "case has no phrase assertion: add must_contain_all or tool_evidence"
        )

    low = reply_text.lower()
    missing: list[str] = []

    for phrase in resp.get("must_contain_all") or []:
        if phrase.lower() not in low:
            missing.append(f"phrase:{phrase}")

    for phrase in resp.get("must_not_contain") or []:
        if phrase.lower() in low:
            missing.append(f"forbidden:{phrase}")

    # tool_evidence is checked whenever the case declares it (infra cycle 3 S2: skipping it
    # when a tool list is passed let a tool_evidence-only case pass on any reply).
    for phrase in resp.get("tool_evidence") or []:
        if phrase.lower() not in low:
            missing.append(f"evidence:{phrase}")
    if tool_calls is not None:
        called_lower = {t.lower() for t in tool_calls}
        for entry in case.expected.tool_calls or []:
            name = (entry if isinstance(entry, str) else entry.get("name", ""))
            if name.lower() not in called_lower:
                missing.append(f"tool:{name}")

    return (len(missing) == 0, missing)
