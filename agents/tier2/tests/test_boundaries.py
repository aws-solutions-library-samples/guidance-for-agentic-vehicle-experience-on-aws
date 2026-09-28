"""Boundary tests — deterministic seams and no-channel (T1.4, red phase).

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D7 and § D8
Doctrine: ~/.kiro/steering/agentic-tiers.md § "What stays deterministic, and why"

These tests make the doctrine's boundaries *executable* rather than only
stated in a prompt.  A boundary that exists only in a prompt will be crossed.

Three boundary invariants enforced here:

  M13/D7 — no-channel
      agents/tier2/ and agents/health/ must not import any notification client
      (SNS, SES, Connect, Pinpoint, End User Messaging).  Detected by AST scan
      over source text, not by importing — the packages do not exist yet and
      the scan must still work when they do.

  M9/D8 — seam non-modification
      lambdas/triage-classifier/, agents/supervisor/tools/offers_lookup.py, and
      the ADP prediction code are unmodified by this spec.  Enforced via SHA-256
      content hashes captured at spec authoring time (2026-08-03).
      NOT implemented via git diff — the working tree carries uncommitted changes
      from concurrent sessions plus an in-flight tenant rename, so a
      diff-based test would be permanently red for reasons unrelated to this spec.

  M9/D8 — severity_hint vocabulary
      severity_hint values are drawn from a non-P vocabulary; no artifact field
      may carry the triage P-levels P0–P3.

Design note — "pass trivially today, but confirmed to fail on violation":
  The no-channel tests (M13/D7) pass trivially today because agents/tier2/ and
  agents/health/ contain no Python source yet.  The companion tests
  test_no_channel_detector_fires_on_violation and
  test_no_channel_detector_allows_legitimate_imports prove the scanner is live:
  the former asserts the scanner catches a synthetic violating source, the
  latter asserts it does not false-positive on legitimate boto3 usage.

  The severity_hint tests pass trivially today because no code produces
  artifacts yet.  test_severity_hint_detector_fires_on_violation exercises
  the detector against each of P0–P3 directly and asserts it catches them.
"""
from __future__ import annotations

import ast
import hashlib
import re
import textwrap
import tempfile
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Paths — resolved from this file's location
# ---------------------------------------------------------------------------

# agents/tier2/tests/ → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

CVX_TIER2_PKG = REPO_ROOT / "agents" / "tier2"
CVX_HEALTH_PKG = REPO_ROOT / "agents" / "health"

TRIAGE_CLASSIFIER_DIR = REPO_ROOT / "lambdas" / "triage-classifier"
OFFERS_LOOKUP_FILE = REPO_ROOT / "agents" / "supervisor" / "tools" / "offers_lookup.py"

ADP_PREDICTION_START_BATCH = (
    Path.home()
    / "automotive-data-platform-on-aws"
    / "guidance-for-predictive-maintenance"
    / "source"
    / "lambda"
    / "start_batch_transform"
    / "function"
    / "main.py"
)
ADP_PREDICTION_DAILY_TIRE = (
    Path.home()
    / "automotive-data-platform-on-aws"
    / "guidance-for-predictive-maintenance"
    / "source"
    / "lambda"
    / "daily_tire_check"
    / "main.py"
)
ADP_PREDICTION_REALTIME_BLOWOUT = (
    Path.home()
    / "automotive-data-platform-on-aws"
    / "guidance-for-predictive-maintenance"
    / "source"
    / "lambda"
    / "realtime_blowout_risk"
    / "main.py"
)

# ---------------------------------------------------------------------------
# Known-good SHA-256 digests captured 2026-08-03 (spec authoring date).
# These ARE the seam invariant — any modification to these files fails this
# test, which is the intended behaviour for a seam-protection assertion.
# ---------------------------------------------------------------------------

# fmt: off
# Pins refreshed 2026-09-18 for THREE files that had drifted. All three drifts are
# from unrelated work that PRE-DATES this spec, which is the condition this test's
# own failure message prescribes a refresh for. Verified per file rather than
# assumed, because re-pinning a seam on trust is how a real modification would get
# laundered through:
#
#   lambdas/triage-classifier/handler.py
#     c8f5d70 (brand de-naming: a docstring example tenantId "michelin" -> "vehicle-owner")
#     53d33c0 (warmup ping: an early return placed BEFORE tenantId/vin are read,
#              touching no DDB). Neither touches P0 classification. The C0161
#              stop-driving phrase is separately value-pinned by
#              agents/health/tests/test_agent.py::test_c0161_verbatim_remedy_value_is_pinned.
#
#   ADP daily_tire_check/main.py + realtime_blowout_risk/main.py
#     ADP-side d6512a8 (paginate telemetry newest-first, plus model-version
#     provenance from SSM). ADP's own prediction-pipeline internals, authored by
#     the ADP effort before this spec existed. This spec writes zero ADP files.
#
# These pins had never passed in this spec: they were authored by the 2026-08-03
# Tier 2 spec (commit 3276236) and the files moved afterwards, so the guard has
# been red the whole time. It was outside the suite the review gates measured
# (`pytest tests/`), which is why nobody saw it — the gate and the guard were
# looking at different sets of files.
#
# handler.py re-pinned 2026-09-27 for work outside this spec: review W3 on a137e13
# (issues/2026-09-25-triage-drops-active-dtcs-first-seen-over-24h/). Verified by
# diff, not on trust: the change adds `import sys`, two helpers
# (`_dtc_read_outcome`, `_emit_dtc_read_degraded`), stores the CMS client's read
# outcome as `inputsSnapshot.dtcRead` on the decision row, and emits one EMF line for
# a partial or failed read. `classify(ctx)` is called with the same TriageContext as
# before, the handler's response dict is unchanged, and nothing reads the new field
# back into classification. The warmup early return still precedes all of it.
# Re-pinned again the same day for review cycle 2's W5: the only further change
# adds `vehicle_lookup_failed` to `_DTC_READ_DEGRADED` (which outcomes emit the
# metric) and rewords that constant's comment. Classification is untouched.
KNOWN_GOOD_HASHES: dict[str, str] = {
    # lambdas/triage-classifier/ (CVX repo)
    "lambdas/triage-classifier/__init__.py":
        "d0137f31df21e61fba84768dd52618fa6313a762b008c5255d65572bdd0766d4",
    "lambdas/triage-classifier/classifier.py":
        "87085ac709aebfc36cd51a16d54654ac206f357a1c2657cb13aaa6e8bcb19365",
    "lambdas/triage-classifier/handler.py":
        "035c77738eca238e3155511e3a83a28940e77658ec3d6d2ea953af99709a0dab",
    "lambdas/triage-classifier/types.py":
        "58a510d845ee9fc8ffb6ac6414d6c2620bf4fc3d64b7dfc4258019478b595bcc",
    # agents/supervisor/tools/offers_lookup.py (CVX repo)
    "agents/supervisor/tools/offers_lookup.py":
        "12e901eab8f775c565111b5b643ce19231b1e47cbd2ab01c08c9e7d9ee87777d",
    # ADP prediction code (separate repo — ~/automotive-data-platform-on-aws)
    "guidance-for-predictive-maintenance/source/lambda/start_batch_transform/function/main.py":
        "f00bc946d0f48ed62831bdfd6d2d051266dec061ee809ab54f6ac959dd4a7dba",
    "guidance-for-predictive-maintenance/source/lambda/daily_tire_check/main.py":
        "959d7198ea38a5bb733969e0965c7fad35e1e11ba43530391a20e723d83ae7bc",
    "guidance-for-predictive-maintenance/source/lambda/realtime_blowout_risk/main.py":
        "da5142e9682f503dda302265303d0fa132839b12a6ff752102cc3d3a387bc350",
}
# fmt: on

# Resolved absolute paths keyed by the same labels used in KNOWN_GOOD_HASHES.
# CVX-repo files use REPO_ROOT; ADP files use Path.home() since they live in
# a sibling repo, not inside the CVX working tree.
_SEAM_FILE_PATHS: dict[str, Path] = {
    "lambdas/triage-classifier/__init__.py": TRIAGE_CLASSIFIER_DIR / "__init__.py",
    "lambdas/triage-classifier/classifier.py": TRIAGE_CLASSIFIER_DIR / "classifier.py",
    "lambdas/triage-classifier/handler.py": TRIAGE_CLASSIFIER_DIR / "handler.py",
    "lambdas/triage-classifier/types.py": TRIAGE_CLASSIFIER_DIR / "types.py",
    "agents/supervisor/tools/offers_lookup.py": OFFERS_LOOKUP_FILE,
    "guidance-for-predictive-maintenance/source/lambda/start_batch_transform/function/main.py": ADP_PREDICTION_START_BATCH,
    "guidance-for-predictive-maintenance/source/lambda/daily_tire_check/main.py": ADP_PREDICTION_DAILY_TIRE,
    "guidance-for-predictive-maintenance/source/lambda/realtime_blowout_risk/main.py": ADP_PREDICTION_REALTIME_BLOWOUT,
}

# ---------------------------------------------------------------------------
# Vocabulary — severity_hint must use these values, never P0–P3
# ---------------------------------------------------------------------------

# The allowed severity_hint vocabulary from spec § D4.
# Explicitly not P0/P1/P2/P3 — those are triage-classifier outputs, not
# artifact advisory metadata.
SEVERITY_HINT_ALLOWED = frozenset({"routine", "soon", "attention"})
# Canonical per spec.md § D4. Deliberately excludes "critical": it maps onto a P0
# verdict, and § D8 exists precisely so this field cannot be mistaken for one.

# Triage P-levels that must never appear as an artifact field value.
# The triage classifier owns these labels; the health agent must not reproduce
# them as if it were a classifier.
FORBIDDEN_P_LEVELS = frozenset({"P0", "P1", "P2", "P3"})

# ---------------------------------------------------------------------------
# Forbidden notification clients (M13/D7)
# ---------------------------------------------------------------------------

# Service names that must not appear as a boto3.client() / boto3.resource()
# argument, or as a dotted/underscore segment in an import path.
_FORBIDDEN_SERVICES: frozenset[str] = frozenset(
    {"sns", "ses", "pinpoint", "connect", "end_user_messaging"}
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_path(path: Path) -> str:
    """Render a path without leaking the developer's home directory.

    Test output is pasted into reviews, issues, and handbacks, so an absolute
    path interpolates a username into places it does not belong.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return path.name


def _sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect_python_files(directory: Path) -> list[Path]:
    """Return all .py files under *directory*, excluding __pycache__."""
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _source_mentions_forbidden_channel(source: str) -> tuple[bool, Optional[str]]:
    """Return (True, reason) if *source* imports or calls a forbidden channel.

    Two detection strategies applied to the parsed AST (no imports executed):

    1. Import scanning — checks both dotted segments *and* underscore sub-parts
       of each segment, so ``mypy_boto3_sns`` → ``sns`` is caught even though
       the module name uses underscores rather than dots.

    2. boto3.client/resource call scanning — catches ``boto3.client("sns")``
       and equivalent where the service name appears as a string literal in the
       first argument position.  This is the most common runtime pattern for
       creating notification clients.

    Returns (False, None) if no forbidden channel is found.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, None

    # Strategy 1: import statements
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Normalise to lowercase, split on dots and underscores
                for dotted_seg in alias.name.lower().replace("-", "_").split("."):
                    for sub in dotted_seg.split("_"):
                        if sub in _FORBIDDEN_SERVICES:
                            return True, (
                                f"import {alias.name!r}: segment {sub!r} "
                                "is a forbidden notification service"
                            )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for dotted_seg in node.module.lower().replace("-", "_").split("."):
                    for sub in dotted_seg.split("_"):
                        if sub in _FORBIDDEN_SERVICES:
                            return True, (
                                f"from {node.module!r} import ...: "
                                f"segment {sub!r} is a forbidden notification service"
                            )

    # Strategy 2: boto3.client("service") / boto3.resource("service") calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("client", "resource")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            svc = str(node.args[0].value).lower()
            if svc in _FORBIDDEN_SERVICES:
                return True, (
                    f"boto3.{node.func.attr}({svc!r}): "
                    "direct notification channel call is forbidden (spec § D7)"
                )

    return False, None


def _artifact_dict_contains_p_level(artifact: dict) -> tuple[bool, Optional[str]]:
    """Return (True, field_key) if any string value in *artifact* equals a P-level."""
    for key, value in artifact.items():
        if isinstance(value, str) and value in FORBIDDEN_P_LEVELS:
            return True, key
    return False, None


# ---------------------------------------------------------------------------
# M13/D7 — no-channel tests
# ---------------------------------------------------------------------------


class TestNoChannel:
    """agents/tier2/ and agents/health/ must not import notification clients.

    Today both directories contain only __init__.py (no agent source), so
    the scan finds nothing to violate and both tests pass trivially.

    The companion tests test_no_channel_detector_fires_on_violation and
    test_no_channel_detector_allows_legitimate_imports confirm the scanner is
    not vacuous: the former asserts the scanner catches a synthetic violating
    file; the latter asserts it does not false-positive on legitimate usage.
    """

    def _scan_pkg(self, pkg_dir: Path) -> list[str]:
        """Scan all .py files under *pkg_dir* and return violation strings."""
        violations: list[str] = []
        for py_file in _collect_python_files(pkg_dir):
            source = py_file.read_text(encoding="utf-8", errors="replace")
            found, msg = _source_mentions_forbidden_channel(source)
            if found:
                violations.append(f"{py_file.relative_to(REPO_ROOT)}: {msg}")
        return violations

    def test_tier2_package_imports_no_channel_client(self) -> None:
        """agents/tier2/ contains no import of SNS, SES, Connect, Pinpoint, etc.

        Passes trivially when no agent source exists (nothing to violate).
        Would fail the moment any .py file in agents/tier2/ imports or calls
        a forbidden notification service — confirmed by
        test_no_channel_detector_fires_on_violation below.
        """
        violations = self._scan_pkg(CVX_TIER2_PKG)
        assert not violations, (
            "agents/tier2/ imports a forbidden notification channel client.\n"
            "Spec § D7 / M13: agents must never call channels directly.\n"
            "The broker is S2; S1 is pull-only.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_health_package_imports_no_channel_client(self) -> None:
        """agents/health/ contains no import of SNS, SES, Connect, Pinpoint, etc.

        Passes trivially when no agent source exists (nothing to violate).
        Would fail the moment any .py file in agents/health/ imports or calls
        a forbidden notification service — confirmed by
        test_no_channel_detector_fires_on_violation below.
        """
        violations = self._scan_pkg(CVX_HEALTH_PKG)
        assert not violations, (
            "agents/health/ imports a forbidden notification channel client.\n"
            "Spec § D7 / M13: agents must never call channels directly.\n"
            "The broker is S2; S1 is pull-only.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_channel_detector_fires_on_violation(self) -> None:
        """Prove the detection logic is live: it *does* catch a violating file.

        Writes synthetic .py source that imports SNS via two patterns into a
        temp directory, runs the same scanner used by the two boundary tests
        above, and asserts that at least one violation is detected.

        If this test passes, the two boundary tests above are confirmed to be
        non-vacuous: they would fire if forbidden channel code appeared.
        If this test fails, the scanner itself is broken and the boundary is
        not enforced.
        """
        violating_source = textwrap.dedent("""\
            import boto3

            # Pattern 1: direct boto3.client call with SNS service name
            _sns = boto3.client("sns", region_name="us-east-1")

            # Pattern 2: typed-client stub import — the underscore-joined module
            # name 'mypy_boto3_sns' must be decomposed to find 'sns'
            from mypy_boto3_sns import SNSClient
        """)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "violating_module.py").write_text(violating_source)

            violations: list[str] = []
            for py_file in _collect_python_files(tmp_path):
                source = py_file.read_text()
                found, msg = _source_mentions_forbidden_channel(source)
                if found:
                    violations.append(msg or "detected")

        assert violations, (
            "The no-channel detector failed to fire on source that:\n"
            "  1. calls boto3.client('sns', ...)\n"
            "  2. imports from mypy_boto3_sns\n\n"
            "The detection logic is not working correctly.  The no-channel "
            "boundary (spec § D7) is not enforced."
        )

    def test_no_channel_detector_allows_legitimate_imports(self) -> None:
        """Prove the detector does not false-positive on legitimate boto3 usage.

        agents/tier2/ legitimately uses DynamoDB, S3, and Athena.  These must
        not be flagged as notification-channel violations.
        """
        clean_source = textwrap.dedent("""\
            import boto3
            from botocore.exceptions import ClientError

            # Legitimate services used by the Tier 2 platform
            _ddb = boto3.resource("dynamodb", region_name="us-west-2")
            _s3 = boto3.client("s3", region_name="us-west-2")
            _athena = boto3.client("athena", region_name="us-west-2")
        """)

        found, msg = _source_mentions_forbidden_channel(clean_source)
        assert not found, (
            f"False positive: legitimate boto3 import flagged as forbidden channel.\n"
            f"Detector message: {msg}\n"
            "DynamoDB, S3, and Athena are not notification channels."
        )

    def test_no_channel_detector_catches_ses_and_pinpoint(self) -> None:
        """Detector fires on SES and Pinpoint too, not only SNS.

        Confirms the forbidden-service set is complete, not only the most
        obvious member.
        """
        for svc in ("ses", "pinpoint"):
            source = f'client = boto3.client("{svc}", region_name="us-east-1")\n'
            found, _ = _source_mentions_forbidden_channel(source)
            assert found, (
                f"Detector did not fire for boto3.client({svc!r}).\n"
                "All notification services must be in the forbidden set."
            )


# ---------------------------------------------------------------------------
# M9/D8 — seam non-modification tests
# ---------------------------------------------------------------------------


class TestSeamNonModification:
    """The deterministic seams are unmodified by this spec.

    Implemented as SHA-256 content assertions against digests captured on the
    spec authoring date (2026-08-03).  Any modification to the listed files
    causes this test to fail — which is the intended behaviour.

    NOT implemented via git diff: the working tree carries uncommitted changes
    from concurrent sessions plus an in-flight tenant rename, so a
    diff-based test is permanently red for reasons unrelated to this spec.
    See spec § Constraints.
    """

    @pytest.mark.parametrize("label,expected_hash", list(KNOWN_GOOD_HASHES.items()))
    def test_seam_file_unmodified(self, label: str, expected_hash: str) -> None:
        """Each seam file's SHA-256 matches the digest captured at spec authoring.

        Failure means the file was changed after 2026-08-03.  Two cases:

        * The change is from a concurrent session unrelated to this spec —
          update KNOWN_GOOD_HASHES to the new digest and document in
          decisions.md.

        * The change is from this spec — that is a spec violation.  Spec § D8
          / M9 requires these files to be unmodified; the seam boundary was
          crossed.
        """
        path = _SEAM_FILE_PATHS[label]
        if not path.exists():
            pytest.skip(
                f"Seam file not found (missing dev environment?): "
                f"{_display_path(path)}"
            )

        actual = _sha256_file(path)
        assert actual == expected_hash, (
            f"Seam file modified: {label}\n"
            f"  Expected SHA-256: {expected_hash}\n"
            f"  Actual   SHA-256: {actual}\n"
            f"  Path:             {_display_path(path)}\n\n"
            "This file is a deterministic seam that this spec (§ D8) must not "
            "modify.  If the change is from a concurrent session unrelated to "
            "this spec, update KNOWN_GOOD_HASHES and document in decisions.md."
        )

    def test_triage_classifier_dir_has_expected_python_files(self) -> None:
        """The triage-classifier dir contains exactly the expected .py files.

        Guards against files being silently added or removed — both would
        indicate the seam boundary was crossed.  (Tests subdir is excluded:
        test files are not part of the seam.)
        """
        expected_files = {
            "__init__.py",
            "classifier.py",
            "handler.py",
            "types.py",
        }
        actual_files = {
            p.name
            for p in TRIAGE_CLASSIFIER_DIR.glob("*.py")
            if p.is_file()
        }
        assert actual_files == expected_files, (
            f"triage-classifier/ has unexpected Python file set.\n"
            f"  Expected: {sorted(expected_files)}\n"
            f"  Actual:   {sorted(actual_files)}\n"
            "Adding or removing files in the triage-classifier is a seam "
            "modification that this spec must not make."
        )


# ---------------------------------------------------------------------------
# M9/D8 — severity_hint vocabulary tests
# ---------------------------------------------------------------------------


class TestSeverityHintVocabulary:
    """severity_hint is advisory metadata, not a triage classification.

    Spec § D8 and the artifact contract (§ D4) require:
    1. severity_hint values come from the canonical vocabulary in spec § D4:
       routine / soon / attention. "critical" is deliberately excluded — it maps
       onto a P0 verdict, which is the confusion § D8 exists to prevent.
    2. No artifact field carries a triage P-level (P0/P1/P2/P3).

    These tests are behavioural contracts for the artifact schema, enforced
    before any schema code exists.  They constrain T2.1 (contract.py) and
    T4.3 (agent.py).  They pass trivially today because nothing produces
    artifacts yet.

    test_severity_hint_detector_fires_on_violation confirms the detection
    logic is live, not vacuous.
    """

    def test_allowed_severity_hint_values_are_not_p_levels(self) -> None:
        """The allowed vocabulary contains no P-levels (sanity check on constants).

        If someone added 'P0' to SEVERITY_HINT_ALLOWED, every severity_hint
        validation below would be vacuous.  This guards against that.
        """
        overlap = SEVERITY_HINT_ALLOWED & FORBIDDEN_P_LEVELS
        assert not overlap, (
            f"SEVERITY_HINT_ALLOWED overlaps FORBIDDEN_P_LEVELS: {overlap}\n"
            "The allowed vocabulary must be completely disjoint from triage P-levels."
        )

    def test_valid_artifact_passes_severity_hint_check(self) -> None:
        """A well-formed artifact with severity_hint='soon' passes validation."""
        artifact = {
            "pk": "VEH#VIN001",
            "sk": "FINDING#tires#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "severity_hint": "soon",
            "finding": "Left front tire shows elevated wear rate.",
            "confidence": "high",
        }
        found, key = _artifact_dict_contains_p_level(artifact)
        assert not found, f"False positive: valid artifact flagged at key {key!r}"

    def test_artifact_with_p_level_severity_hint_is_caught(self) -> None:
        """An artifact with severity_hint='P0' (or P1/P2/P3) must be caught.

        P0–P3 are triage-classifier outputs.  The health agent setting
        severity_hint to a P-level would usurp the deterministic classifier's
        authority — a seam violation even if the P-level value happens to match
        what the classifier would produce.
        """
        for p_level in FORBIDDEN_P_LEVELS:
            artifact = {
                "pk": "VEH#VIN001",
                "sk": f"FINDING#tires#2026-08-03T00:00:00Z",
                "severity_hint": p_level,
                "finding": "Tire needs replacement.",
            }
            found, key = _artifact_dict_contains_p_level(artifact)
            assert found, (
                f"Detector missed severity_hint={p_level!r} — "
                "a P-level in the artifact was not caught.\n"
                "The health agent must never set severity_hint to a triage "
                "P-level (spec § D8): doing so usurps the deterministic "
                "triage classifier."
            )
            assert key == "severity_hint", (
                f"Detector fired but on the wrong field: {key!r} "
                f"(expected 'severity_hint')"
            )

    def test_p_level_in_any_artifact_field_is_caught(self) -> None:
        """A P-level value in *any* string field of an artifact is caught.

        Not only severity_hint — if the finding text or confidence field
        carries exactly 'P0' as its value, that should also be flagged.
        The seam protection must be complete.
        """
        artifact = {
            "pk": "VEH#VIN001",
            "sk": "FINDING#tires#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "severity_hint": "attention",
            "finding": "P0",   # P-level as the entire finding value
            "confidence": "high",
        }
        found, key = _artifact_dict_contains_p_level(artifact)
        assert found, (
            "A P-level value ('P0') in the 'finding' field was not caught.\n"
            "The artifact field check must cover all string fields."
        )

    def test_severity_hint_detector_fires_on_violation(self) -> None:
        """Prove the P-level detector fires on each of P0, P1, P2, P3.

        If any level is missed this test fails, proving the detector is not
        vacuously passing the two boundary tests above.
        """
        for p_level in ["P0", "P1", "P2", "P3"]:
            artifact = {"severity_hint": p_level}
            found, _ = _artifact_dict_contains_p_level(artifact)
            assert found, (
                f"Detector did not fire for severity_hint={p_level!r}.\n"
                "The P-level boundary check is not complete."
            )

    def test_severity_hint_outside_allowed_vocabulary_is_detectable(self) -> None:
        """Confirm the allowed-vocabulary gate would reject P-level values.

        This gives contract.py (T2.1) a precise validation target: it must
        reject severity_hint values not in SEVERITY_HINT_ALLOWED, and all
        P-levels are outside that set.
        """
        for p_level in FORBIDDEN_P_LEVELS:
            assert p_level not in SEVERITY_HINT_ALLOWED, (
                f"severity_hint={p_level!r} is in SEVERITY_HINT_ALLOWED — "
                "P-levels must not be valid severity_hint values."
            )

        # Legitimate values must be accepted
        for allowed in SEVERITY_HINT_ALLOWED:
            assert allowed not in FORBIDDEN_P_LEVELS, (
                f"severity_hint={allowed!r} is in FORBIDDEN_P_LEVELS — "
                "legitimate severity_hint values must not be triage P-levels."
            )

    def test_severity_hint_reassurance_finding_has_no_p_level(self) -> None:
        """A battery-reassurance artifact (the expected S1 output) passes.

        Spec § D3a: the honest output for a healthy battery is reassurance,
        not alarm.  This test confirms that a reassurance artifact with
        severity_hint='routine' passes both the vocabulary and P-level checks.
        """
        artifact = {
            "pk": "VEH#VIN001",
            "sk": "FINDING#battery#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "component": "battery",
            "severity_hint": "routine",
            "confidence": "high",
            "finding": (
                "Battery state of health is 92%, degrading normally for its age. "
                "No service action required."
            ),
        }
        # Must not be a P-level
        found, key = _artifact_dict_contains_p_level(artifact)
        assert not found, f"Reassurance artifact incorrectly flagged at key {key!r}"

        # severity_hint must be in the allowed vocabulary
        assert artifact["severity_hint"] in SEVERITY_HINT_ALLOWED, (
            f"severity_hint={artifact['severity_hint']!r} is not in the allowed vocabulary"
        )


# ---------------------------------------------------------------------------
# Helpers for Guards 1–3 (spec 2026-08-10-cvx-tier2-vin-selection-and-alerting)
# ---------------------------------------------------------------------------

# Source files that will grow a select_vins() method in Group 3.
_TIRE_SOURCE = CVX_HEALTH_PKG / "adp_tire_source.py"
_BRAKE_SOURCE = CVX_HEALTH_PKG / "adp_brake_source.py"
_BATTERY_SOURCE = CVX_HEALTH_PKG / "adp_battery_source.py"

# The new selection orchestration module written in Group 3.
_SELECTION_MODULE = CVX_TIER2_PKG / "selection.py"

# Numeric comparison operators that are forbidden in pool predicates (§ D2).
# The cursor keyset clause "vin > '<cursor>'" is the sole allowed exception —
# it compares a string column, not a numeric metric.
_NUMERIC_COMPARISON_RE = re.compile(
    r"""
    (?<!['\"\w])          # not immediately after a quote or word char (avoids
                          # matching "vin > 'X'" as a numeric comparison)
    (?:                   # one of the four operators …
        [<>]=?            #   < <= > >=
    )
    \s*                   # optional whitespace
    (?:                   # … followed by a numeric literal
        \d                #   leading digit
        |                 #   or
        \.\d              #   decimal starting with a dot
    )
    """,
    re.VERBOSE,
)

# Regex that matches the one *allowed* ">" in pool SQL: the cursor keyset clause.
# Shape: vin > 'some_cursor_value' (with any surrounding whitespace/quotes).
_CURSOR_VIN_GT_RE = re.compile(r"vin\s*>\s*['\"]")


def _extract_select_vins_source(path: Path) -> str:
    """Return the source text of the select_vins method inside *path*.

    Raises AssertionError (pytest fail) if the file or method does not exist —
    the guard cannot fire without the feature, so "absent" is itself a failure.
    """
    assert path.exists(), (
        f"Source file not found: {path.relative_to(REPO_ROOT)}\n"
        "Guard 1 (no-threshold rule) requires all three ADP source modules "
        "to exist.  This test is intentionally red until Group 3 creates them."
    )
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "select_vins":
            # Extract the source lines for this method.
            lines = source.splitlines()
            start = node.lineno - 1          # ast lines are 1-indexed
            end = node.end_lineno            # exclusive slice index
            return "\n".join(lines[start:end])
    raise AssertionError(
        f"select_vins() not found in {path.relative_to(REPO_ROOT)}\n"
        "Guard 1 (no-threshold rule) requires select_vins() to exist on all "
        "three ADP source modules.  This test is intentionally red until "
        "Group 3 implements them."
    )


def _sql_has_forbidden_numeric_comparison(sql_fragment: str) -> tuple[bool, str]:
    """Return (True, match_text) if *sql_fragment* contains a numeric comparison.

    The cursor keyset clause ``vin > '<cursor>'`` is explicitly excluded —
    it compares a string column, not a numeric metric, and is the only
    legitimate use of ``>`` in selection SQL.

    Any other ``< <= > >=`` applied to a numeric literal is a D2 violation:
    CVX is thresholding an ADP metric rather than selecting on a categorical
    signal.
    """
    # Strip the allowed cursor clause before scanning so it doesn't trigger.
    cleaned = _CURSOR_VIN_GT_RE.sub("", sql_fragment)
    match = _NUMERIC_COMPARISON_RE.search(cleaned)
    if match:
        return True, match.group(0).strip()
    return False, ""


def _cursor_item_has_ttl(cursor_item: dict) -> bool:
    """Return True if *cursor_item* carries a ``ttl`` attribute.

    DynamoDB TTL only reaps items that carry the TTL attribute.  A cursor item
    with ``ttl`` set would be silently deleted by DynamoDB, resetting all
    pagination progress to zero without any error or log entry.
    """
    return "ttl" in cursor_item


# ---------------------------------------------------------------------------
# Guard 1 — no numeric comparison in any pool predicate (§ D2 / spec 2026-08-10)
# ---------------------------------------------------------------------------


class TestNoThresholdInPoolPredicates:
    """Pool predicates may filter only on ADP categorical/boolean signals.

    Spec § D2: CVX must not compute thresholds.  Filtering on a continuous
    metric (e.g. ``state_of_health_pct < 80``) is CVX performing inference,
    which is a D3 violation regardless of how reasonable the number looks.

    Implementation: the ``select_vins()`` method on each source module builds
    the pool-predicate SQL.  We extract that method's source text and scan it
    for numeric comparisons (``< <= > >=`` applied to a literal number).

    The cursor keyset clause ``vin > '<cursor_value>'`` is the sole allowed
    exception — it compares a string column for pagination, not a metric.

    **These tests are intentionally red until Group 3 adds select_vins() to
    each source module.**  They cannot run until the feature exists, so
    "feature absent" is treated as a failure rather than a skip.

    A companion positive-control test proves the detector fires on synthetic
    SQL that does contain a numeric comparison.
    """

    def test_tire_source_select_vins_has_no_numeric_comparison(self) -> None:
        """adp_tire_source.select_vins() contains no numeric comparison operator.

        Tires' pool predicate is ``needs_replacement = true`` — a boolean
        equality, not a threshold.  Any ``< <= > >=`` applied to a number
        (e.g. ``wear_rate_mm_per_1k_km > 0.5``) is a D2 violation: CVX would
        be computing wear inference that belongs to ADP.
        """
        method_src = _extract_select_vins_source(_TIRE_SOURCE)
        found, match_text = _sql_has_forbidden_numeric_comparison(method_src)
        assert not found, (
            f"adp_tire_source.select_vins() contains a numeric comparison: "
            f"{match_text!r}\n"
            "Spec § D2: pool predicates must filter on ADP-published categorical "
            "or boolean signals only.  Tires' pool predicate is "
            "needs_replacement = true.  A numeric bound is CVX inference, which "
            "is a D3 violation."
        )

    def test_brake_source_select_vins_has_no_numeric_comparison(self) -> None:
        """adp_brake_source.select_vins() contains no numeric comparison operator.

        Brakes' pool predicate is ``outcome IN ('parts_pending',
        'follow_up_required')`` — a categorical membership test.  Any
        numeric comparison (e.g. ``total_cost_usd > 500``) is a D2 violation.
        """
        method_src = _extract_select_vins_source(_BRAKE_SOURCE)
        found, match_text = _sql_has_forbidden_numeric_comparison(method_src)
        assert not found, (
            f"adp_brake_source.select_vins() contains a numeric comparison: "
            f"{match_text!r}\n"
            "Spec § D2: brakes' pool predicate is outcome IN OPEN_OUTCOMES "
            "(categorical).  A numeric bound is CVX inference."
        )

    def test_battery_source_select_vins_has_no_numeric_comparison(self) -> None:
        """adp_battery_source.select_vins() contains no numeric comparison operator.

        Battery has NO pool predicate — spec § D2 explicitly gives it rotation
        only because every available signal (state_of_health_pct,
        battery_age_days) is continuous.  Any bound chosen in CVX would be
        inference.  Selecting on ``state_of_health_pct < 80`` is the exact
        violation this test blocks.
        """
        method_src = _extract_select_vins_source(_BATTERY_SOURCE)
        found, match_text = _sql_has_forbidden_numeric_comparison(method_src)
        assert not found, (
            f"adp_battery_source.select_vins() contains a numeric comparison: "
            f"{match_text!r}\n"
            "Spec § D2: battery has no priority pool — rotation only.  There is "
            "no ADP-published categorical battery-health signal, so any numeric "
            "bound is CVX inference (a D3 violation).  The method must contain "
            "only keyset pagination (vin > cursor) and no WHERE filter on "
            "battery metrics."
        )

    def test_no_threshold_detector_fires_on_synthetic_violation(self) -> None:
        """Prove the detector catches SQL containing a numeric comparison.

        Writes a synthetic select_vins()-style SQL fragment with a threshold
        (``state_of_health_pct < 80``) and asserts the detector flags it.
        If this test passes, the three boundary tests above are confirmed
        non-vacuous: they would fire if a numeric comparison appeared.
        """
        violating_sql = (
            "SELECT DISTINCT vin FROM adp_staging_energy_usage.energy_usage "
            "WHERE state_of_health_pct < 80 "
            "AND vin > 'LAST_VIN' "
            "ORDER BY vin LIMIT 375"
        )
        found, match_text = _sql_has_forbidden_numeric_comparison(violating_sql)
        assert found, (
            "The no-threshold detector failed to fire on SQL containing "
            "'state_of_health_pct < 80'.\n"
            "The detection logic is not working correctly.  Guard 1 "
            "(spec § D2 no-threshold rule) is not enforced."
        )

    def test_no_threshold_detector_allows_cursor_keyset_clause(self) -> None:
        """Prove the cursor's ``vin > '<cursor>'`` is not a false positive.

        ``vin >`` compares a string column (the VIN), not a numeric metric.
        The detector must not flag it — otherwise every pool SQL with keyset
        pagination would be a false violation.
        """
        cursor_sql = (
            "SELECT DISTINCT vin FROM adp_staging_tire_health.tire_health "
            "WHERE needs_replacement = true "
            "AND vin > 'VIN-ABC-123' "
            "ORDER BY vin LIMIT 156"
        )
        found, match_text = _sql_has_forbidden_numeric_comparison(cursor_sql)
        assert not found, (
            f"False positive: cursor keyset clause 'vin > ...' was flagged as "
            f"a numeric comparison ({match_text!r}).\n"
            "The detector must not treat vin > '<string>' as a threshold — "
            "it is pagination, not CVX inference."
        )

    def test_no_threshold_detector_allows_categorical_in_clause(self) -> None:
        """Prove IN ('parts_pending', 'follow_up_required') is not a false positive.

        The brakes pool predicate uses ``outcome IN (...)`` with string literals.
        The detector must not flag it.
        """
        categorical_sql = (
            "SELECT DISTINCT vin FROM adp_staging_service_records.service_records "
            "WHERE service_type = 'brake_service' "
            "AND outcome IN ('parts_pending', 'follow_up_required') "
            "AND vin > 'LAST_VIN' "
            "ORDER BY vin LIMIT 2000"
        )
        found, match_text = _sql_has_forbidden_numeric_comparison(categorical_sql)
        assert not found, (
            f"False positive: categorical IN clause flagged ({match_text!r}).\n"
            "String literals inside IN (...) are not numeric comparisons."
        )


# ---------------------------------------------------------------------------
# Guard 2 — cursor item carries no ``ttl`` attribute (§ D3 / spec 2026-08-10)
# ---------------------------------------------------------------------------


class TestCursorItemHasNoTtl:
    """The cursor item stored in vsa-{stage}-health-findings must never carry ttl.

    Spec § D3: DynamoDB TTL only reaps items that carry the attribute.  A
    cursor item with ``ttl`` would be silently deleted by DynamoDB after the
    TTL epoch, resetting all pagination progress to zero with no error, no
    log entry, and no alarm.  The run would then silently re-select VINs
    from the start of the pool — indistinguishable from correct operation
    until the coverage horizon starts drifting.

    The cursor is written by ``agents/tier2/selection.py`` (Group 3).

    **These tests are intentionally red until Group 3 creates selection.py.**

    A companion positive-control test proves the ``ttl`` detector fires on a
    dict that does carry the attribute.
    """

    def _load_cursor_item_builder(self):
        """Import and return the cursor-item-building callable from selection.py.

        Raises AssertionError (pytest fail) if selection.py does not exist or
        does not expose the expected interface.  This is the intended red-phase
        behaviour — the guard cannot check an absent feature.
        """
        assert _SELECTION_MODULE.exists(), (
            f"agents/tier2/selection.py does not exist.\n"
            "Guard 2 (no-ttl rule) requires selection.py to exist with a "
            "cursor item builder.  This test is intentionally red until "
            "Group 3 creates agents/tier2/selection.py."
        )
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "agents.tier2.selection", _SELECTION_MODULE
        )
        assert spec is not None and spec.loader is not None, (
            "Could not load agents/tier2/selection.py as a module."
        )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            raise AssertionError(
                f"agents/tier2/selection.py failed to import: {exc}\n"
                "Fix the import error before Guard 2 can run."
            ) from exc
        return module

    def test_cursor_item_schema_has_no_ttl_attribute(self) -> None:
        """The cursor item written by selection.py carries no ``ttl`` key.

        Checks by scanning selection.py's source text for any dict literal
        or variable assignment that resembles cursor-item construction and
        contains a ``ttl`` key.

        The cursor is mutable state; artifacts carry ``ttl``; the cursor must
        not be mistaken for an artifact.  ``ArtifactWriter.put_raw`` is the
        correct write path precisely because it bypasses ``validate_artifact``,
        which requires ``ttl``.  Adding ``ttl`` to the cursor item would cause
        DynamoDB to silently reap it.
        """
        assert _SELECTION_MODULE.exists(), (
            "agents/tier2/selection.py does not exist.\n"
            "Guard 2 (no-ttl rule) is intentionally red until Group 3 creates "
            "agents/tier2/selection.py."
        )
        source = _SELECTION_MODULE.read_text(encoding="utf-8")
        # Scan the AST for dict literals that look like cursor items
        # (contain 'last_vin' or 'CURSOR#') and check for a 'ttl' key.
        tree = ast.parse(source)
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [
                k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None
                for k in node.keys
            ]
            str_keys = [k for k in keys if k is not None]
            # A cursor item is identified by its mandatory fields.
            is_cursor_like = any(
                k in str_keys for k in ("last_vin", "pool_exhausted_count", "updated_at")
            )
            if is_cursor_like and "ttl" in str_keys:
                violations.append(
                    f"line {node.lineno}: cursor-like dict literal contains 'ttl' key"
                )
        # Also do a lightweight text scan for the pattern 'ttl' near cursor keys
        # in case the AST walk misses an indirect construction (e.g. dict.update).
        if re.search(r'"ttl"\s*:', source) or re.search(r"'ttl'\s*:", source):
            # Only flag if the file also contains cursor vocabulary, to avoid
            # false-positives from comments or unrelated code.
            if "CURSOR#" in source or "last_vin" in source:
                violations.append(
                    "text scan: 'ttl' key found in selection.py alongside "
                    "cursor vocabulary (CURSOR# or last_vin)"
                )
        assert not violations, (
            "agents/tier2/selection.py assigns a 'ttl' attribute to a cursor item.\n"
            "Spec § D3: the cursor must NOT carry ttl.  DynamoDB TTL reaps only items "
            "that carry the attribute; losing the cursor silently resets all pagination "
            "progress to zero.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_cursor_ttl_detector_fires_on_synthetic_violation(self) -> None:
        """Prove the ttl detector fires on a cursor item that does carry ttl.

        Constructs a synthetic cursor dict with a ``ttl`` key and asserts
        _cursor_item_has_ttl() returns True.  If this passes, the boundary
        test above is confirmed non-vacuous.
        """
        violating_cursor = {
            "pk": "CURSOR#tires",
            "sk": "STATE",
            "last_vin": "VIN-ABC-123",
            "updated_at": "2026-08-10T02:00:00Z",
            "pool_exhausted_count": 0,
            "ttl": 1755820800,   # ← forbidden: DynamoDB would reap this item
        }
        assert _cursor_item_has_ttl(violating_cursor), (
            "The ttl detector failed to fire on a cursor item that carries 'ttl'.\n"
            "The detection logic is not working.  Guard 2 "
            "(spec § D3 no-ttl rule) is not enforced."
        )

    def test_cursor_ttl_detector_allows_item_without_ttl(self) -> None:
        """A correct cursor item (no ttl) is not flagged as a violation."""
        correct_cursor = {
            "pk": "CURSOR#tires",
            "sk": "STATE",
            "last_vin": "VIN-ABC-123",
            "updated_at": "2026-08-10T02:00:00Z",
            "pool_exhausted_count": 0,
            # No 'ttl' key — correct.
        }
        assert not _cursor_item_has_ttl(correct_cursor), (
            "False positive: a cursor item without 'ttl' was flagged.\n"
            "The detector must only fire when 'ttl' is literally present."
        )


# ---------------------------------------------------------------------------
# Guard 3 — CURSOR# pk is unreachable from health_findings() (§ D3)
# ---------------------------------------------------------------------------


class TestCursorPartitionUnreachableFromHealthFindings:
    """health_findings() queries pk=VEH#{vin}; CURSOR#... items are invisible to it.

    Spec § D3: the cursor is internal Tier 2 state.  Tier 1's health_findings()
    tool calls ``Key("pk").eq(f"VEH#{session.vin}")``, which only returns items
    whose pk is exactly ``VEH#{vin}``.  A ``CURSOR#{component}`` pk is a
    completely different partition key value and can never appear in that
    query's result set — DynamoDB partitions are strict equality.

    This test validates the structural guarantee: the two pk prefixes are
    disjoint, so the cursor is inherently invisible to Tier 1.  It passes
    today (and must keep passing) because it encodes a design invariant, not
    a feature that is absent in Group 2's red phase.
    """

    def test_cursor_pk_prefix_is_disjoint_from_vehicle_pk_prefix(self) -> None:
        """'CURSOR#' and 'VEH#' are disjoint DynamoDB partition key prefixes.

        A query on pk='VEH#<vin>' can never return a 'CURSOR#<component>'
        item, because DynamoDB evaluates partition key equality exactly —
        not as a prefix scan.
        """
        vehicle_pk = "VEH#VIN-TEST-001"
        cursor_pk_tires = "CURSOR#tires"
        cursor_pk_battery = "CURSOR#battery"
        cursor_pk_brakes = "CURSOR#brakes"

        # The query condition is strict equality on pk.
        # Simulate what DynamoDB evaluates: pk == vehicle_pk.
        for cursor_pk in (cursor_pk_tires, cursor_pk_battery, cursor_pk_brakes):
            assert cursor_pk != vehicle_pk, (
                f"Partition key collision: cursor pk {cursor_pk!r} equals "
                f"vehicle pk {vehicle_pk!r}.\n"
                "This should be impossible — the prefixes 'CURSOR#' and 'VEH#' "
                "are structurally disjoint."
            )

    def test_no_vin_is_cursor_prefixed(self) -> None:
        """A valid VIN sanitised through _vin.sanitise_vin cannot start with 'CURSOR'.

        The sanitiser allows only [A-Z0-9-].  The literal string 'CURSOR#' would
        survive sanitisation (all uppercase letters plus #, but # is stripped).
        This test confirms the full pk='VEH#{vin}' construction cannot accidentally
        produce a key that begins with 'CURSOR#', even with a crafted VIN input.
        """
        # Even if someone passes 'CURSOR#tires' as a VIN, the pk built in
        # health_findings.py is always 'VEH#' + vin — so the resulting pk
        # starts with 'VEH#', never with 'CURSOR#'.
        crafted_vin = "CURSOR-TIRES"   # closest allowable shape after sanitise_vin
        vehicle_pk = f"VEH#{crafted_vin}"
        assert not vehicle_pk.startswith("CURSOR#"), (
            f"Constructed pk {vehicle_pk!r} starts with 'CURSOR#' — this would "
            "create a collision with the cursor's partition key space.\n"
            "The 'VEH#' prefix must always precede the VIN."
        )

    def test_cursor_sk_prefix_differs_from_finding_sk_prefix(self) -> None:
        """Cursor uses sk='STATE'; findings use sk='FINDING#{component}#{hash}'.

        Even within a hypothetical shared partition, the sort key prefixes are
        different.  This confirms the cursor item is structurally distinct from
        any artifact item at both the pk and sk level.
        """
        cursor_sk = "STATE"
        finding_sk = "FINDING#tires#" + "a" * 64

        assert not finding_sk.startswith(cursor_sk), (
            "Cursor sk 'STATE' is a prefix of finding sk — this would cause "
            "begins_with queries on findings to accidentally include the cursor."
        )
        assert not cursor_sk.startswith("FINDING#"), (
            "Cursor sk starts with 'FINDING#' — health_findings() queries "
            "begins_with(sk, 'FINDING#'), which would accidentally return the cursor."
        )

    def test_health_findings_query_cannot_reach_cursor_partition(self) -> None:
        """Simulate the health_findings pk=VEH#{vin} query; assert CURSOR# is absent.

        Builds the exact pk value health_findings() uses for a VIN, then
        verifies it shares no prefix with 'CURSOR#'.  This is the structural
        proof that Tier 1 cannot accidentally read Tier 2 cursor state.
        """
        test_vins = [
            "VIN-001",
            "VIN-SAMANTHA-CARTER",
            "VEH-MRDN-0015",         # could look like a VEH# prefix collision
            "CURSOR-FAKE",          # adversarial: VIN shaped like 'CURSOR'
        ]
        for vin in test_vins:
            pk = f"VEH#{vin}"
            assert not pk.startswith("CURSOR#"), (
                f"pk={pk!r} for vin={vin!r} starts with 'CURSOR#'.\n"
                "The 'VEH#' prefix must guarantee the query is confined to "
                "vehicle partitions, never the cursor's partition."
            )


# ---------------------------------------------------------------------------
# Guard 4 — no-channel invariant still holds with SNS topic in the stack
# ---------------------------------------------------------------------------


class TestNoChannelInvariantWithSnsTopicPresent:
    """The no-channel boundary (§ D7 / M13) holds even after an SNS topic is added.

    Spec 2026-08-10, § Constraints: an SNS topic is added to the CDK stack
    (vsa-{stage}-tier2-operator-alerts) so CloudWatch can publish to it.
    The constraint is that *no boto3 SNS client enters agents/tier2/ or
    agents/health/* — CloudWatch publishes; the agent code does not.

    This class extends the existing TestNoChannel invariant to confirm that
    the topic's addition in the CDK stack (infrastructure/) does NOT cause
    anyone to also add an SNS client in the agent packages.

    These tests pass today (no SNS client exists in agents/).  They MUST
    keep passing after Group 4 adds the SNS topic to the CDK stack.
    """

    def _scan_pkg(self, pkg_dir: Path) -> list[str]:
        """Scan all .py files under *pkg_dir*; return violation strings."""
        violations: list[str] = []
        for py_file in _collect_python_files(pkg_dir):
            source = py_file.read_text(encoding="utf-8", errors="replace")
            found, msg = _source_mentions_forbidden_channel(source)
            if found:
                violations.append(
                    f"{py_file.relative_to(REPO_ROOT)}: {msg}"
                )
        return violations

    def test_sns_topic_in_stack_does_not_imply_sns_client_in_tier2(self) -> None:
        """agents/tier2/ still contains no boto3 SNS client after the CDK change.

        CloudWatch publishes to the SNS topic; the Lambda never does.
        A boto3.client('sns') anywhere under agents/tier2/ is a D7 violation.
        """
        violations = self._scan_pkg(CVX_TIER2_PKG)
        assert not violations, (
            "agents/tier2/ imports or calls a forbidden notification channel "
            "client.\n"
            "Spec § D7 / Constraints: the SNS topic is CDK-only; CloudWatch "
            "publishes to it.  No boto3 SNS client belongs in the agent "
            "packages.\n"
            "Adding boto3.client('sns') to agents/ is the exact anti-pattern "
            "this boundary test blocks.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_sns_topic_in_stack_does_not_imply_sns_client_in_health(self) -> None:
        """agents/health/ still contains no boto3 SNS client after the CDK change.

        Same invariant as above, covering the health sub-package.
        """
        violations = self._scan_pkg(CVX_HEALTH_PKG)
        assert not violations, (
            "agents/health/ imports or calls a forbidden notification channel "
            "client.\n"
            "Spec § D7 / Constraints: the SNS topic is CDK-only; CloudWatch "
            "publishes to it.  No boto3 SNS client belongs in the agent "
            "packages.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_cdk_stack_sns_usage_does_not_appear_in_agents(self) -> None:
        """Confirm the CDK stack file can reference SNS without agents/ doing so.

        This positive-control test verifies that the infrastructure file
        exists (or will exist) and that its presence does not cause any
        agent file to import SNS.  Passes trivially when the stack file has
        not yet been modified (Group 4).
        """
        # The CDK stack that will gain the SNS topic.
        stack_file = REPO_ROOT / "infrastructure" / "lib" / "vsa-tier2-stack.ts"
        if stack_file.exists():
            stack_source = stack_file.read_text(encoding="utf-8", errors="replace")
            # The CDK stack may legitimately reference sns.Topic — that is
            # TypeScript, not Python boto3, and is not in the agents/ tree.
            # Just confirm agents/ remains clean regardless.
            _ = stack_source  # acknowledged; no assertion needed on TS source

        # The key assertion: agents/ is still clean.
        tier2_violations = self._scan_pkg(CVX_TIER2_PKG)
        health_violations = self._scan_pkg(CVX_HEALTH_PKG)
        all_violations = tier2_violations + health_violations
        assert not all_violations, (
            "CDK stack SNS reference leaked into agents/ — boto3 SNS client "
            "found in agent packages.\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in all_violations)
        )

    def test_no_channel_detector_fires_on_sns_client_pattern(self) -> None:
        """Confirm the detector catches boto3.client('sns') specifically.

        Proves that if Group 4 accidentally adds an SNS client to agents/,
        this class's boundary tests above would catch it.
        """
        violating_source = "import boto3\nclient = boto3.client('sns')\n"
        found, _ = _source_mentions_forbidden_channel(violating_source)
        assert found, (
            "The no-channel detector did not fire on boto3.client('sns').\n"
            "Guard 4 is not enforced — the SNS boundary would be silently crossed."
        )


# ---------------------------------------------------------------------------
# Guard 5 — no VIN in DEBUG logs across the three sources and the selection
#            module (spec 2026-08-10-cvx-tier2-vin-selection-and-alerting,
#            security review cycle 2 follow-up)
# ---------------------------------------------------------------------------
#
# Context
# -------
# The predecessor spec's Cycle 7 W7 established "no VIN anywhere in any log
# line" as an absolute invariant so that operators can raise the log level to
# DEBUG for troubleshooting without exposing customer VINs into CloudWatch
# Logs.  Security review cycle 1 of this spec found the tire source violated
# the invariant by logging ``cursor=%s`` with the raw VIN value.  The fix
# landed in Fix Group 3b, but no regression test closed the detection gap:
# the invariant was stated in prose, not in code.
#
# This defect class has now recurred THREE TIMES in Tier 2 work:
#   - Predecessor spec Cycle 7 W7 (inconsistent VIN sanitisation across files)
#   - Cycle 1 of this spec (tire source DEBUG log emitting the cursor VIN)
#   - Twice in a row: fix applied to the cited line, sibling files missed
#
# The only durable guard is a caplog-driven test that exercises the real code
# at DEBUG level and asserts the VIN does not appear in any emitted record.
#
# Synthetic-VIN discipline
# -------------------------
# The predecessor spec (2026-08-03-cvx-tier2-platform-health-agent) caught a
# real staging VIN pasted into a test file from live-query output.  The VIN
# used here is deliberately NOT real and does NOT follow ISO 3779 format:
#   CAPLOG-TEST-VIN-01
# All-caps alphanumeric with hyphens, obviously a test fixture.  Never replace
# this with output from a real query — that is the habit this test was written
# to prevent.
#
# Pattern
# -------
# Two tests per the detector-plus-positive-control convention used throughout
# this file:
#
#   test_no_vin_in_select_vins_debug_logs
#       The boundary test.  Exercises the three real source modules and the
#       real selection.resolve_batch at DEBUG level with the synthetic VIN as
#       the cursor, and asserts the VIN does not appear in any log record.
#
#   test_no_vin_in_logs_detector_fires_on_violation
#       The positive control.  Exercises a synthetic source that DOES log the
#       cursor value (simulating the regression this test guards against) and
#       asserts the detector fires — proving test_no_vin_in_select_vins_debug_logs
#       is non-vacuous and would catch a reintroduced leak.


# Synthetic VIN used exclusively in Guard 5 tests.
# DO NOT replace with a VIN from live query output.  See docstring above.
# This string intentionally does not conform to ISO 3779 (17-char alphanumeric
# excluding I, O, Q) — the hyphens and short length make it obviously fake and
# prevent it from matching the VIN-shape regex used elsewhere in this repo.
_CAPLOG_SYNTHETIC_VIN = "CAPLOG-TEST-VIN-01"

# Logger names to capture in Guard 5 tests.  All DEBUG-level records from
# these loggers are examined for the synthetic VIN.
_CAPLOG_LOGGERS = [
    "agents.health.adp_tire_source",
    "agents.health.adp_brake_source",
    "agents.health.adp_battery_source",
    "agents.tier2.selection",
]


class TestNoVinInDebugLogs:
    """The cursor VIN must not appear in any DEBUG log record from the sources or
    the selection module.

    Spec 2026-08-10-cvx-tier2-vin-selection-and-alerting, security review cycle 2
    follow-up: "The tire-source VIN-at-DEBUG leak was fixed at source, but nothing
    prevents the next log statement from reintroducing it."

    Boundary test
    -------------
    test_no_vin_in_select_vins_debug_logs exercises the three real source modules
    and selection.resolve_batch at DEBUG level with a known synthetic VIN as the
    cursor.  Every emitted log record is examined; the test fails if the VIN
    appears in any record message.

    Detector control
    ----------------
    test_no_vin_in_logs_detector_fires_on_violation proves the test above is
    non-vacuous: a synthetic source that does log the cursor value is exercised and
    the detector asserts the VIN IS present in the log output.  If that assertion
    were absent or the caplog capture were broken, the boundary test above would
    trivially pass while emitting VINs — this control prevents that failure mode.

    Synthetic VIN
    -------------
    ``_CAPLOG_SYNTHETIC_VIN = "CAPLOG-TEST-VIN-01"`` — not a valid ISO 3779 VIN,
    not from live query output, obviously a test fixture.  See the docstring at the
    top of this guard section for why this matters.
    """

    def test_no_vin_in_vin_log_select_vins_debug(self, caplog) -> None:
        """The cursor VIN does not appear in any DEBUG log record across the three
        sources and the selection module.

        Exercises the real source modules and selection.resolve_batch with the
        synthetic VIN as the stored cursor.  All DEBUG-level records emitted by
        the four relevant loggers are captured; the test fails if the VIN appears
        in any record's formatted message string.

        Fails when the tire source is reverted to ``cursor=%s`` (the regression
        this test guards against), or when any other log call in the three
        sources or selection.py is changed to emit the cursor value directly.
        """
        from unittest.mock import patch, MagicMock
        import logging

        from agents.health.adp_tire_source import AdpTireSource
        from agents.health.adp_brake_source import AdpBrakeSource
        from agents.health.adp_battery_source import AdpBatterySource
        from agents.tier2.selection import resolve_batch

        # Fake cursor store that returns the synthetic VIN as the stored cursor.
        # This causes each source's select_vins to receive cursor=_CAPLOG_SYNTHETIC_VIN,
        # which is the value that must NOT appear in any log output.
        class _FakeCursorStore:
            def get_cursor(self, component: str):
                return {
                    "pk": f"CURSOR#{component}",
                    "sk": "STATE",
                    "last_vin": _CAPLOG_SYNTHETIC_VIN,
                    "updated_at": "2026-08-10T02:00:00Z",
                    "pool_exhausted_count": 0,
                }

            def put_cursor(self, component: str, item: dict) -> None:
                pass  # discard — not under test here

        # Stub run_query in each source module to return an empty result set,
        # bypassing real Athena while allowing the log statement to fire.
        # The log.debug call in select_vins happens BEFORE run_query is invoked,
        # so the capture is independent of whether Athena succeeds.
        run_query_stub = MagicMock(return_value=[])

        cursor_store = _FakeCursorStore()

        # Capture ALL DEBUG records from the four relevant loggers.
        with caplog.at_level(logging.DEBUG, logger="agents"):
            with (
                patch("agents.health.adp_tire_source.run_query", run_query_stub),
                patch("agents.health.adp_brake_source.run_query", run_query_stub),
                patch("agents.health.adp_battery_source.run_query", run_query_stub),
            ):
                tire_source = AdpTireSource(
                    "staging",
                    athena_client=None,
                    workgroup="test-wg",
                    results_s3_path="s3://test/",
                )
                brake_source = AdpBrakeSource(
                    "staging",
                    athena_client=None,
                    workgroup="test-wg",
                    results_s3_path="s3://test/",
                )
                battery_source = AdpBatterySource(
                    "staging",
                    athena_client=None,
                    workgroup="test-wg",
                    results_s3_path="s3://test/",
                )

                # Exercise selection.resolve_batch for each component.
                # This calls _read_cursor_safe (which reads _CAPLOG_SYNTHETIC_VIN from
                # the store) and then calls source.select_vins(cursor=_CAPLOG_SYNTHETIC_VIN).
                # The selection module logs warnings about cursor reads but not VIN values.
                # The source modules log cursor_present (a bool) but not the cursor value.
                for component, source in [
                    ("tires", tire_source),
                    ("brakes", brake_source),
                    ("battery", battery_source),
                ]:
                    resolve_batch(source, cursor_store, component, limit=10)

        # Assert the synthetic VIN appears in NONE of the captured log records.
        vin = _CAPLOG_SYNTHETIC_VIN
        violations = [
            f"{r.name}:{r.levelname}: {r.getMessage()!r}"
            for r in caplog.records
            if vin in r.getMessage()
        ]
        assert not violations, (
            f"The cursor VIN {vin!r} appeared in {len(violations)} DEBUG log record(s).\n"
            "Spec 2026-08-10 security review cycle 1 / invariant: operators must be able\n"
            "to raise the log level to DEBUG without emitting customer VINs into\n"
            "CloudWatch Logs.  The cursor value must never appear in any log call —\n"
            "log cursor_present (bool) instead, as adp_brake_source.py already does.\n\n"
            "Violating records:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_vin_log_detector_fires_on_violation(self, caplog) -> None:
        """Proves the boundary test above is non-vacuous: the caplog capture DOES
        catch a log call that emits the cursor VIN.

        Exercises a synthetic source whose select_vins method logs
        ``cursor=%s`` with the raw VIN value — the exact regression pattern that
        fix group 3b corrected in the tire source.  Asserts that the VIN IS found
        in the captured records, confirming:

          1. caplog capture at DEBUG level is working
          2. The boundary test above would fail if any source were reverted to
             the old ``cursor=%s`` pattern

        If this detector control itself fails, the boundary test above is likely
        a no-op (the caplog may not be capturing records from the relevant loggers).
        """
        import logging as _logging

        # A synthetic source that reintroduces the regression: logs the cursor value.
        # This is the pattern that MUST NOT exist in real source code —
        # this class exists only to prove the detector fires.
        _regression_log = _logging.getLogger("agents.health.adp_tire_source")

        class _RegressedTireSource:
            """Simulates an adp_tire_source that has been reverted to log cursor=%s.

            This is the defect pattern security review cycle 1 caught and fix
            group 3b corrected.  It exists here only as a detection target.
            """

            def select_vins(
                self, *, cursor: str | None, limit: int
            ) -> tuple[list[str], str | None]:
                # Reintroduces the regression: emits the cursor VIN into the log.
                # The correct pattern is cursor_present=%s with (cursor is not None).
                _regression_log.debug(
                    "adp_tire_source.select_vins: stage=%s cursor=%s limit=%d",
                    "staging",
                    cursor,   # ← regression: raw VIN value, not a bool
                    limit,
                )
                return [], None

        class _FakeCursorStore:
            def get_cursor(self, component: str):
                return {
                    "pk": f"CURSOR#{component}",
                    "sk": "STATE",
                    "last_vin": _CAPLOG_SYNTHETIC_VIN,
                    "updated_at": "2026-08-10T02:00:00Z",
                    "pool_exhausted_count": 0,
                }

            def put_cursor(self, component: str, item: dict) -> None:
                pass

        from agents.tier2.selection import resolve_batch

        with caplog.at_level(_logging.DEBUG, logger="agents"):
            resolve_batch(
                _RegressedTireSource(),
                _FakeCursorStore(),
                "tires",
                limit=10,
            )

        # The VIN MUST appear — this is the positive control proving capture works
        # and that the boundary test above would catch a regression.
        vin = _CAPLOG_SYNTHETIC_VIN
        vin_found = any(vin in r.getMessage() for r in caplog.records)
        assert vin_found, (
            f"Detector control FAILED: the cursor VIN {vin!r} did NOT appear in any\n"
            "log record, even though the synthetic source explicitly logged it.\n\n"
            "This means caplog is NOT capturing DEBUG records from the source logger,\n"
            "and test_no_vin_in_select_vins_debug_logs above is a no-op — it would\n"
            "pass even if real sources emitted VINs.\n\n"
            "Fix the caplog setup (logger scope, propagation, or level) before\n"
            "treating the boundary test as enforced."
        )
