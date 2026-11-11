"""Unit tests for the KB audit runner's top-hit matcher.

WHY THIS EXISTS
---------------
`_match_top_hit` compares a live Bedrock KB retrieval result against an audit
set's `expected_doc`, which is a LOCAL repo path. Two bugs are possible and both
have real consequences:

1. **Too strict** — comparing the S3 URI against the local path directly can never
   match, because the KB layout has a different parent prefix and hyphenated
   category directories where the local tree uses underscores. That is what
   shipped: the gate reported 31/31 FAIL on a correctly-ingested corpus.
2. **Too loose** — matching on basename alone would match across categories.
   Filenames were NOT unique: during pre-Meridian staging, all ten manufacturing
   stage filenames existed in both `bike-manufacturing/` and `auto-manufacturing/`,
   so a two-wheeler document could have satisfied a four-wheeler prompt. This
   strict category+basename matching exists to prevent that class of false match.

The matcher has to thread between the two. These tests pin both edges. No AWS
calls. See issues/2026-08-03-kb-audit-runner-unusable/.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "smoke-test-kb-retrieve.py"

_spec = importlib.util.spec_from_file_location("smoke_test_kb_retrieve", SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_match_top_hit = _mod._match_top_hit

S3_BASE = (
    "s3://adp-staging-foundation-lake-123456789012-us-east-1"
    "/knowledge/vehicle_knowledge_base/sources"
)


def _result(uri: str, score: float = 0.72) -> list[dict]:
    return [{"score": score, "location": {"s3Location": {"uri": uri}}}]


class TestMatchTopHit:
    def test_matches_hyphenated_s3_prefix_against_underscored_local_path(self):
        """The regression: local `corpora/<under_scored>/` vs S3 `<hyphen-ated>/`."""
        matched, score, uri = _match_top_hit(
            _result(f"{S3_BASE}/meridian-lineup-narrative/01-windrose.md"),
            "corpora/meridian_lineup_narrative/01-windrose.md",
        )
        assert matched, (
            f"Expected a match across the underscore/hyphen convention gap. "
            f"This failing is the exact bug that made the gate report 31/31 FAIL "
            f"on a correct corpus. uri={uri}"
        )
        assert score == pytest.approx(0.72)

    def test_auto_expectation_is_not_satisfied_by_the_bike_document(self):
        """The false-positive edge. Same filename, different category."""
        matched, _, _ = _match_top_hit(
            _result(f"{S3_BASE}/meridian-lineup-narrative/04-sub-assembly.md"),
            "corpora/vehicle_manufacturing/04-sub-assembly.md",
        )
        assert not matched, (
            "A lineup-narrative document must NOT satisfy a manufacturing prompt. "
            "Matching on basename alone would let this pass and the gate would "
            "green-light a confidently wrong answer."
        )

    def test_bike_expectation_is_not_satisfied_by_the_auto_document(self):
        """A vehicle_manufacturing document must NOT satisfy a shopping-guide prompt."""
        matched, _, _ = _match_top_hit(
            _result(f"{S3_BASE}/vehicle-manufacturing/04-sub-assembly.md"),
            "corpora/vehicle_shopping_guide/04-sub-assembly.md",
        )
        assert not matched

    def test_wrong_document_in_the_right_category_does_not_match(self):
        matched, _, _ = _match_top_hit(
            _result(f"{S3_BASE}/vehicle-manufacturing/08-shipped.md"),
            "corpora/vehicle_manufacturing/07-quality-control.md",
        )
        assert not matched

    def test_all_four_new_meridian_categories_match(self):
        for local_dir, s3_prefix, doc in [
            ("vehicle_manufacturing", "vehicle-manufacturing", "05-paint.md"),
            ("meridian_lineup_narrative", "meridian-lineup-narrative", "02-trailwind.md"),
            ("meridian_owner_manual", "meridian-owner-manual", "01-safety-systems.md"),
            ("meridian_ive_package", "meridian-ive-package", "01-family.md"),
        ]:
            matched, _, _ = _match_top_hit(
                _result(f"{S3_BASE}/{s3_prefix}/{doc}"),
                f"corpora/{local_dir}/{doc}",
            )
            assert matched, f"{local_dir}/{doc} should match {s3_prefix}/{doc}"

    def test_empty_results_do_not_match(self):
        matched, score, uri = _match_top_hit([], "corpora/vehicle_manufacturing/05-paint.md")
        assert not matched and score == 0.0 and uri == ""

    def test_missing_location_does_not_raise(self):
        """A malformed result must fail the assertion, not crash the whole run and
        lose the other 30 prompts' results."""
        matched, _, uri = _match_top_hit(
            [{"score": 0.9}], "corpora/vehicle_manufacturing/05-paint.md"
        )
        assert not matched and uri == ""

    def test_substring_filename_does_not_false_match(self):
        """`5-paint.md` must not satisfy an expectation of `05-paint.md`."""
        matched, _, _ = _match_top_hit(
            _result(f"{S3_BASE}/vehicle-manufacturing/x05-paint.md"),
            "corpora/vehicle_manufacturing/05-paint.md",
        )
        assert not matched, (
            "Matching must be anchored on a full path segment, not a bare suffix."
        )
