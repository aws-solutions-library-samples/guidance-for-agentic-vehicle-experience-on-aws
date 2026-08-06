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
KNOWN_GOOD_HASHES: dict[str, str] = {
    # lambdas/triage-classifier/ (CVX repo)
    "lambdas/triage-classifier/__init__.py":
        "d0137f31df21e61fba84768dd52618fa6313a762b008c5255d65572bdd0766d4",
    "lambdas/triage-classifier/classifier.py":
        "87085ac709aebfc36cd51a16d54654ac206f357a1c2657cb13aaa6e8bcb19365",
    "lambdas/triage-classifier/handler.py":
        "eb78626ebc09be7b4ad0debe5c9ec85576ff636a78b6efc2bd6346bf633bdab0",
    "lambdas/triage-classifier/types.py":
        "58a510d845ee9fc8ffb6ac6414d6c2620bf4fc3d64b7dfc4258019478b595bcc",
    # agents/supervisor/tools/offers_lookup.py (CVX repo)
    "agents/supervisor/tools/offers_lookup.py":
        "12e901eab8f775c565111b5b643ce19231b1e47cbd2ab01c08c9e7d9ee87777d",
    # ADP prediction code (separate repo — ~/automotive-data-platform-on-aws)
    "guidance-for-predictive-maintenance/source/lambda/start_batch_transform/function/main.py":
        "f00bc946d0f48ed62831bdfd6d2d051266dec061ee809ab54f6ac959dd4a7dba",
    "guidance-for-predictive-maintenance/source/lambda/daily_tire_check/main.py":
        "04ce41b7e9bb9eff3541a61f8e14a1ef8c542b0f91ba57a6595ff242085729e9",
    "guidance-for-predictive-maintenance/source/lambda/realtime_blowout_risk/main.py":
        "7315c57ade1d9fa4b61b4bfd0900d3ad78f6b1bfa8ecf56f7df39dc2d0743d55",
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
