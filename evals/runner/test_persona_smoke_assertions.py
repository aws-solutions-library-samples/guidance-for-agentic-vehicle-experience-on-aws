"""Offline unit tests for the Tier-3 KB persona-smoke citation hardening.

These tests are fully offline — they exercise the pure helpers
(`_coerce_score`, `_collect_kb_citations`, `_evaluate_kb_smoke`) against
synthetic traces, with no network/AgentCore connection. They run as part of
the default suite (no --staging, no RUNTIME_ID); the live
`test_persona_smoke_*` tests remain double-gated and skip here.

Issue: 2026-06-22-cvx-tier3-kb-citation-assertion.
"""
from __future__ import annotations

from evals.runner.tier3_e2e import (
    _SMOKE_CASES,
    _coerce_score,
    _collect_kb_citations,
    _evaluate_kb_smoke,
)


def _retrieve_result(passages: list[dict]) -> dict:
    """A recv tool.result event for the bare `retrieve` tool (flat list output)."""
    return {"dir": "recv", "type": "tool.result", "name": "retrieve", "output": passages}


# ---------------------------------------------------------------------------
# _coerce_score
# ---------------------------------------------------------------------------


def test_coerce_score_numeric():
    assert _coerce_score(0.5666) == 0.5666
    assert _coerce_score(1) == 1.0


def test_coerce_score_stringified():
    assert _coerce_score("0.5666") == 0.5666


def test_coerce_score_absent_or_bad():
    assert _coerce_score(None) is None
    assert _coerce_score("n/a") is None
    # bool is an int subclass — must NOT be treated as a score
    assert _coerce_score(True) is None


# ---------------------------------------------------------------------------
# _collect_kb_citations
# ---------------------------------------------------------------------------


def test_collect_citations_flat_retrieve_shape():
    trace = [
        _retrieve_result([
            {"snippet": "...", "source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.5666},
            {"snippet": "...", "source": "s3://lake/sources/dtc-guides/dtc-P0700.md", "score": 0.486},
        ])
    ]
    cites = _collect_kb_citations(trace)
    assert len(cites) == 2
    assert cites[0] == {"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.5666}
    assert cites[1]["score"] == 0.486


def test_collect_citations_nested_wrapper_shape():
    # lookup_knowledge-style: output["passages"][].source
    trace = [
        {
            "dir": "recv",
            "type": "tool.result",
            "name": "lookup_knowledge",
            "output": {"passages": [
                {"text": "...", "source": "s3://lake/sources/service-policy/warranty-coverage-matrix.md", "score": 0.61},
            ]},
        }
    ]
    cites = _collect_kb_citations(trace)
    assert cites == [{"source": "s3://lake/sources/service-policy/warranty-coverage-matrix.md", "score": 0.61}]


def test_collect_citations_excludes_local_fallback():
    # Local-corpora fallback returns a relative (non-s3://) source.
    trace = [_retrieve_result([{"snippet": "...", "source": "technical-reference/p0420.md", "score": 3}])]
    assert _collect_kb_citations(trace) == []


def test_collect_citations_score_absent():
    trace = [_retrieve_result([{"snippet": "...", "source": "s3://lake/x.md"}])]
    cites = _collect_kb_citations(trace)
    assert cites == [{"source": "s3://lake/x.md", "score": None}]


def test_collect_citations_ignores_non_tool_result_events():
    trace = [
        {"dir": "send", "type": "tool.result", "output": [{"source": "s3://should/not/count.md"}]},
        {"dir": "recv", "type": "transcript", "text": "s3://not-here.md"},
    ]
    assert _collect_kb_citations(trace) == []


# ---------------------------------------------------------------------------
# _evaluate_kb_smoke
# ---------------------------------------------------------------------------

_SA = {"persona_id": "service_advisor", "check": "kb",
       "expected_source_substr": "dtc-P0420.md", "min_citation_score": 0.55}


def test_eval_pass_on_topic_with_score():
    ok, detail = _evaluate_kb_smoke(
        _SA, ["retrieve"],
        [{"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.5666}],
    )
    assert ok, detail
    assert "0.5666" in detail


def test_eval_fail_retrieve_not_called():
    ok, detail = _evaluate_kb_smoke(
        _SA, ["lookup_vehicle_identity"],
        [{"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.9}],
    )
    assert not ok
    assert "retrieve" in detail


def test_eval_fail_no_citation():
    ok, detail = _evaluate_kb_smoke(_SA, ["retrieve"], [])
    assert not ok
    assert "no s3://" in detail


def test_eval_fail_wrong_doc_cited():
    # The regression catch: P0420 dropped → neighbour cited → substring missing.
    ok, detail = _evaluate_kb_smoke(
        _SA, ["retrieve"],
        [{"source": "s3://lake/sources/dtc-guides/dtc-P0700.md", "score": 0.486}],
    )
    assert not ok
    assert "dtc-P0420.md" in detail


def test_eval_fail_score_below_floor():
    ok, detail = _evaluate_kb_smoke(
        _SA, ["retrieve"],
        [{"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.40}],
    )
    assert not ok
    assert "< floor 0.55" in detail


def test_eval_pass_floor_skipped_when_score_absent():
    # Score floor configured, but no numeric score on the wire → substring-only pass.
    ok, detail = _evaluate_kb_smoke(
        _SA, ["retrieve"],
        [{"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": None}],
    )
    assert ok, detail
    assert "floor skipped" in detail


def test_eval_pass_best_matching_score_clears_floor():
    # Multiple P0420 chunks; the best matching score must clear the floor.
    ok, detail = _evaluate_kb_smoke(
        _SA, ["retrieve"],
        [
            {"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.51},
            {"source": "s3://lake/sources/dtc-guides/dtc-P0420.md", "score": 0.58},
        ],
    )
    assert ok, detail


def test_eval_technician_substring_only_no_floor():
    tech = {"persona_id": "technician", "check": "kb",
            "expected_source_substr": "warranty-coverage-matrix.md"}
    ok, detail = _evaluate_kb_smoke(
        tech, ["retrieve"],
        [{"source": "s3://lake/sources/service-policy/warranty-coverage-matrix.md", "score": 0.12}],
    )
    # No min_citation_score → low score does NOT fail; substring is the guard.
    assert ok, detail


# ---------------------------------------------------------------------------
# _SMOKE_CASES data invariants
# ---------------------------------------------------------------------------


def test_smoke_cases_kb_have_expected_substr():
    kb_cases = [c for c in _SMOKE_CASES if c.get("check") == "kb"]
    assert len(kb_cases) == 2
    for c in kb_cases:
        assert c.get("expected_source_substr"), c


def test_smoke_cases_service_advisor_maps_p0420():
    sa = [c for c in _SMOKE_CASES if c["persona_id"] == "service_advisor"][0]
    assert sa["expected_source_substr"] == "dtc-P0420.md"
    assert sa["min_citation_score"] == 0.55


def test_smoke_cases_technician_maps_warranty():
    tech = [c for c in _SMOKE_CASES if c["persona_id"] == "technician"][0]
    assert tech["expected_source_substr"] == "warranty-coverage-matrix.md"
    # No score floor on the un-probed warranty doc.
    assert "min_citation_score" not in tech
