"""Publish-safety invariant: a file may be unscanned, or it may ship. Not both.

Canonical union of:
  - CMS  ~/connected-mobility-guidance-on-aws/scripts/lib/test_publish_scan_exclude_invariant.py
          (classes TestNoRealAccountIdReachesTheMirror, TestScanExcludeDoesNotSilentlyGrow,
           TestPublishGateActuallyGates — 4 tests, account-ID-specific)
  - CVX  ~/guidance-for-connected-vehicle-experience-on-aws/scripts/tests/test_publish_scan_exclude_invariant.py
          (classes TestScanExcludeDoesNotHideShippedContent, TestScannerStillBlocksKnownDirtyContent
           — 5 tests, general "any findings" form)

Toolkit version: 0.3.0

THE BUG CLASS
-------------
Two independent suppression mechanisms, easily conflated because they sound alike:

  .publish-secrets-scan.yml -> scan_exclude   suppresses DETECTION
  .publish-exclude                            suppresses SHIPPING

A file in scan_exclude but not covered by .publish-exclude is published without
ever being inspected. The scanner reports "clean" precisely because it did not look.

Three real instances in five weeks, all the same shape:

  1. CMS .gitignore — carried real AWS account IDs in filename patterns whose purpose
     was to PREVENT committing files named after those accounts. scan-excluded AND
     shipped. Reached the public mirror. A denylist written in terms of the secret
     embeds the secret.
  2. CMS docs/DEPLOYMENT.md — an account ID added while documenting a bucket rename.
     Caught only because a security reviewer read the diff, not because the scanner
     could see it.
  3. CVX infrastructure/bin/vsa.ts — real KMS key ARNs, AgentCore runtime ARNs, and
     Cognito pool IDs. Excluded to stop scanner noise; the file then shipped unexamined.

In all three the scanner passed. The defect is in the mechanism, not the operator.

THE INVARIANT
-------------
    every scan-excluded file is EITHER publish-excluded OR genuinely clean

Equivalently: fine not to scan a file you never publish; fine to publish a file you
scan. Publishing a file you refuse to scan is the hole.

IMPORTANT: the violation is "unscanned AND shipped AND actually dirty", NOT "unscanned
AND shipped". Binary and asset globs (*.png, *.parquet, **/node_modules/**) legitimately
live in scan_exclude and may ship — they are excluded for encoding/size reasons, not to
hide content. Tests assert only on CRITICAL findings in text files.

GENERALISATIONS FROM THE PER-REPO VARIANTS
-------------------------------------------
  - TestNoRealAccountIdReachesTheMirror (CMS): account-ID digest set is derived at
    runtime from the consumer repo's forbidden_patterns regexes via
    _extract_account_id_literals(). pytest.skip when the config uses a generic
    \\d{12} pattern with no per-account literals, and when no config is present.
    No real account ID is hardcoded in this file.
  - TestScanExcludeDoesNotSilentlyGrow (CMS): the pinned baseline set is left EMPTY
    in the canonical. When vendored into a consumer, the consumer maintains its own
    baseline in a repo-local override file scripts/lib/scan_exclude_baseline.py,
    loaded by _load_consumer_baseline(). pytest.skip when that file is absent (expected
    when running from ~/.kiro/publish-toolkit/). This avoids CMS-specific paths
    appearing in the canonical.
  - TestScanExcludeDoesNotHideShippedContent.test_scan_exclude_patterns_covering_shipped_files_are_an_explicit_set
    (CVX): accepted set follows the same override pattern as above.
  - Both ratchet tests converge on a single _consumer_baseline() helper so there is
    one place to override, not two.
"""

import fnmatch
import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------
# When vendored at scripts/lib/test_publish_scan_exclude_invariant.py:
#   parents[0] = scripts/lib/
#   parents[1] = scripts/
#   parents[2] = <repo-root>/
_ROOT = Path(__file__).resolve().parents[2]
_SCAN_CFG = _ROOT / ".publish-secrets-scan.yml"
_PUBLISH_EXCLUDE = _ROOT / ".publish-exclude"

# Binary/asset extensions excluded for encoding/size reasons, not to hide content.
# A scan_exclude entry that only matches files with these extensions is NOT a
# violation of the invariant — those files are legitimately unscanned.
_BINARY_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf",
    ".mp4", ".zip", ".pdf", ".pyc", ".bin", ".parquet",
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tracked_files():
    """Return git-tracked relative paths under _ROOT."""
    return [
        f
        for f in subprocess.run(
            ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
        ).stdout.split("\n")
        if f.strip()
    ]


def _publish_exclude_patterns():
    """Return non-comment, non-negation lines from .publish-exclude."""
    if not _PUBLISH_EXCLUDE.exists():
        return []
    return [
        ln.strip()
        for ln in _PUBLISH_EXCLUDE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith("!")
    ]


def _is_publish_excluded(path: str, patterns) -> bool:
    """Return True if path is matched by any .publish-exclude pattern."""
    for p in patterns:
        p = p.rstrip("/")
        if (
            fnmatch.fnmatch(path, p)
            or fnmatch.fnmatch(path, f"{p}/*")
            or fnmatch.fnmatch(path, f"*/{p}")
            or path.startswith(f"{p}/")
            or fnmatch.fnmatch(path, f"{p}/**")
        ):
            return True
    return False


def _load_scanner():
    """Import secret-scan.py as a module (for API-presence assertions)."""
    scan_path = _ROOT / "scripts" / "lib" / "secret-scan.py"
    if not scan_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_secret_scan", scan_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _findings_ignoring_scan_exclude(cfg, files):
    """Scan *files* for forbidden patterns/strings with scan_exclude IGNORED.

    That omission is the entire point: this inspects exactly the files the real
    scanner refuses to look at. Returns only CRITICAL findings in non-binary files.
    """
    compiled = [
        (name, re.compile(d["regex"], re.IGNORECASE), d.get("severity", "critical"))
        for name, d in (cfg.get("forbidden_patterns") or {}).items()
    ]
    strings = cfg.get("forbidden_strings") or []
    out = []
    for rel in files:
        if rel.endswith(_BINARY_EXT):
            continue
        f = _ROOT / rel
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for name, rx, sev in compiled:
            if sev != "critical":
                continue
            for m in rx.finditer(text):
                out.append((rel, name, m.group(0), sev))
        for s in strings:
            if s in text:
                out.append((rel, f"forbidden_string:{s}", s, "critical"))
    return out


def _consumer_baseline() -> set:
    """Load the repo-local scan_exclude baseline override if present.

    Consumers maintain their pinned set in scripts/lib/scan_exclude_baseline.py
    as a module-level variable BASELINE (a set of strings). This function imports
    it at runtime so the canonical file carries no repo-specific paths.

    Returns an empty set when the override file is absent (expected when running
    canonically from ~/.kiro/publish-toolkit/).
    """
    override = _ROOT / "scripts" / "lib" / "scan_exclude_baseline.py"
    if not override.exists():
        return set()
    spec = importlib.util.spec_from_file_location("_scan_exclude_baseline", override)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(getattr(mod, "BASELINE", set()))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cfg():
    if not _SCAN_CFG.exists():
        pytest.skip(f"Scan config not found at {_SCAN_CFG} — skipping (expected when "
                    "running canonically from ~/.kiro/publish-toolkit/)")
    return yaml.safe_load(_SCAN_CFG.read_text())


@pytest.fixture(scope="module")
def shipped_files():
    pats = _publish_exclude_patterns()
    return [f for f in _tracked_files() if not _is_publish_excluded(f, pats)]

# ---------------------------------------------------------------------------
# Helpers for account-ID assertions (CMS variant, generalised)
# ---------------------------------------------------------------------------

def _extract_account_id_literals(cfg) -> set:
    """Return 12-digit literal account IDs embedded in the aws_account_id regex.

    Two pattern shapes exist across the three consumer repos:

      bare literal:           r'\\b123456789012\\b'
        -> returns {'123456789012'}

      negative-lookahead:     r'\\b(?!123456789012\\b)(?!000000000000\\b)...\\d{12}\\b'
        -> returns {} (no per-account literals to assert on; use the generic path)

    Returns an empty set when the pattern uses only a generic \\d{12} form.
    The caller pytest.skips on an empty set.

    NOTE on the example values above: they are deliberately the AWS-docs placeholder
    rather than any real account ID. This file ships to consumers, several of which
    publish to public mirrors, and a docstring illustrating the guard by naming a real
    identifier is the same "a denylist written in terms of the secret embeds the
    secret" mistake this test exists to catch. It happened: toolkit <=0.3.3 used a real
    account ID here, and the publish gate flagged it in two repos on 2026-08-04. Keep
    the examples allowlisted — a non-allowlisted 12-digit value here would itself trip
    the generic pattern in any consumer using the lookahead shape.
    """
    patterns = cfg.get("forbidden_patterns") or {}
    entry = patterns.get("aws_account_id", {})
    regex_str = entry.get("regex", "")
    # Match bare 12-digit literals, e.g. \\b<12 digits>\\b (with or without \\b)
    literals = set(re.findall(r"\\b(\d{12})\\b", regex_str))
    # Exclude any that appear inside (?!...) lookaheads — those are allowlisted, not guarded
    excluded = set(re.findall(r"\(\?!(\d{12})\\b\)", regex_str))
    return literals - excluded


def _real_account_ids_in_text(text: str, account_ids: set) -> set:
    """Return the subset of *account_ids* that appear as 12-digit tokens in *text*."""
    found = set(re.findall(r"\b\d{12}\b", text))
    return found & account_ids


# ---------------------------------------------------------------------------
# Class: TestNoRealAccountIdReachesTheMirror  (from CMS variant)
# ---------------------------------------------------------------------------

class TestNoRealAccountIdReachesTheMirror:
    """Account-ID-specific checks. Generalised: IDs are derived from the consumer's
    own config at runtime. pytest.skip when the config uses a generic pattern
    (no per-account literals) or is absent.

    Origin: CMS scripts/lib/test_publish_scan_exclude_invariant.py
    """

    def test_no_shipped_file_contains_a_real_account_id(self, cfg, shipped_files):
        """The concrete class that leaked twice. Checked over the whole shipped tree
        with scan_exclude IGNORED — the only way to see it.

        Derives the account IDs to check from the consumer config's
        aws_account_id regex at runtime, so this file never hardcodes them.
        """
        ids = _extract_account_id_literals(cfg)
        if not ids:
            pytest.skip(
                "aws_account_id pattern uses a generic \\d{12} form with no per-account "
                "literals — no specific IDs to assert against (ADP/generic posture). "
                "TestScanExcludeDoesNotHideShippedContent covers the general case."
            )

        offenders = {}
        for rel in shipped_files:
            if rel.endswith(_BINARY_EXT):
                continue
            f = _ROOT / rel
            if not f.is_file():
                continue
            try:
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            hits = _real_account_ids_in_text(text, ids)
            if hits:
                offenders[rel] = hits

        assert not offenders, (
            "These files ship to the public mirror and contain a real AWS account ID "
            "(value redacted — check locally):\n  "
            + "\n  ".join(sorted(offenders))
            + "\n\nReplace with a placeholder (<account-id> or 123456789012), or derive "
            "the value at run time. Do NOT resolve this by adding the file to "
            "scan_exclude — that is precisely how these reach the mirror."
        )

    def test_every_real_account_id_has_a_scanner_pattern(self, cfg):
        """A known-sensitive identifier with no pattern behind it is undetectable.

        Two of the three real account IDs in CMS had no pattern, which is why
        audit findings marked 'MUST be stripped' never turned into failing publishes.

        Also accepts the ADP/generic posture: a \\d{12} pattern with a negative-
        lookahead allowlist covers all accounts without naming each individually.
        """
        ids = _extract_account_id_literals(cfg)
        if not ids:
            pytest.skip(
                "aws_account_id pattern uses a generic \\d{12} form — all 12-digit "
                "values are covered by construction; no per-account assertion needed."
            )

        patterns = cfg.get("forbidden_patterns") or {}
        # Collect all 12-digit literals that appear in any regex (outside lookaheads)
        covered = set()
        for entry in patterns.values():
            blob = entry.get("regex", "")
            raw_literals = set(re.findall(r"\\b(\d{12})\\b", blob))
            excluded_in_entry = set(re.findall(r"\(\?!(\d{12})\\b\)", blob))
            covered |= raw_literals - excluded_in_entry

        # Generic \\d{12} pattern also satisfies the intent
        generic = any(
            r"\d{12}" in entry.get("regex", "")
            for entry in patterns.values()
        )

        missing = ids - covered
        assert not missing or generic, (
            f"{len(missing)} account ID(s) appear in no forbidden_patterns regex "
            "so the scanner cannot detect them. Add a critical pattern per ID, "
            r"or adopt a generic \b\d{12}\b pattern with a negative-lookahead "
            "allowlist of known-public IDs (ADP posture, which would catch all "
            "of these automatically)."
        )

# ---------------------------------------------------------------------------
# Class: TestScanExcludeDoesNotSilentlyGrow  (from CMS variant)
# ---------------------------------------------------------------------------

class TestScanExcludeDoesNotSilentlyGrow:
    """Ratchet on scan_exclude patterns that shadow published content.

    The pinned baseline set is empty in the canonical. Consumers maintain their
    own baseline in scripts/lib/scan_exclude_baseline.py (module-level BASELINE set).
    pytest.skip when that file is absent (expected from ~/.kiro/publish-toolkit/).

    Origin: CMS scripts/lib/test_publish_scan_exclude_invariant.py
    """

    def test_scan_exclude_patterns_shadowing_shipped_files_are_pinned(
        self, cfg, shipped_files
    ):
        """Ratchet, at pattern granularity.

        Each entry in the consumer baseline shadows files that DO publish, so their
        content is never inspected. This list is a pinned baseline of a pre-existing
        surface, not an endorsement of it: the point is that it cannot grow silently.
        Adding a new unscanned-but-published pattern should require editing the
        consumer's scan_exclude_baseline.py and saying why.

        pytest.skip when the consumer override file is absent.
        """
        baseline = _consumer_baseline()
        if not baseline:
            pytest.skip(
                "No consumer scan_exclude_baseline.py found — skipping ratchet test "
                "(expected when running canonically from ~/.kiro/publish-toolkit/). "
                "Consumers create scripts/lib/scan_exclude_baseline.py with a BASELINE "
                "set to enable this test."
            )

        scan_ex = cfg.get("scan_exclude") or []
        text_shipped = [f for f in shipped_files if not f.endswith(_BINARY_EXT)]
        shadowing = sorted(
            {
                p
                for p in scan_ex
                if any(
                    fnmatch.fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")
                    for f in text_shipped
                )
            }
        )

        unexpected = [p for p in shadowing if p not in baseline]
        assert not unexpected, (
            "New scan_exclude pattern(s) now shadow files that ship to the public "
            "mirror:\n  " + "\n  ".join(unexpected) + "\n\n"
            "Everything they match publishes without ever being scanned. Either "
            "publish-exclude those files, clean them and stop excluding them, or add "
            "the pattern to scripts/lib/scan_exclude_baseline.py WITH a category "
            "and justification."
        )

# ---------------------------------------------------------------------------
# Class: TestScanExcludeDoesNotHideShippedContent  (from CVX variant)
# ---------------------------------------------------------------------------

class TestScanExcludeDoesNotHideShippedContent:
    """General-form invariant tests. Checks "unscanned AND shipped AND dirty"
    rather than account-ID-specific leaks.

    Origin: CVX scripts/tests/test_publish_scan_exclude_invariant.py
    """

    def test_no_file_is_both_unscanned_and_shipped_with_findings(
        self, cfg, shipped_files
    ):
        """THE invariant. A scan-excluded file must be publish-excluded or clean.

        Threshold is CRITICAL, deliberately matching what the scanner itself
        blocks on: secret-scan.py exits non-zero on critical findings, while
        internal_email and other warnings do not stop a publish.

        Binary/asset files are excluded from this assertion: they are in
        scan_exclude for size/encoding reasons, not to hide content. Text
        findings in binary files are not credible signals.

        ADP's 23 binary/asset globs (*.png, *.parquet, **/node_modules/**, etc.)
        all match _BINARY_EXT or produce no text findings, so they do not trip
        this test even though they are scan-excluded and may ship.
        """
        scan_ex = cfg.get("scan_exclude") or []
        unscanned_but_shipped = [
            f
            for f in shipped_files
            if any(
                fnmatch.fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")
                for p in scan_ex
            )
        ]
        # Honour the consumer's pinned baseline of ACCEPTED exemptions. Both source
        # variants did this (CMS via its `baseline` set, CVX via `accepted`), and
        # canonicalization must not silently tighten semantics: doing so turns a green
        # gate red on content each repo had already reviewed and consciously accepted,
        # which trains people to ignore the gate.
        #
        # The ratchet tests are what keep this honest — the baseline may only shrink,
        # so an accepted entry cannot quietly become a habit. Anything NOT in the
        # baseline still fails here, loudly, which is the point.
        baseline = _consumer_baseline()
        unscanned_but_shipped = [
            f for f in unscanned_but_shipped
            if f not in baseline
            and not any(fnmatch.fnmatch(f, b) for b in baseline)
        ]
        findings = _findings_ignoring_scan_exclude(cfg, unscanned_but_shipped)
        # _findings_ignoring_scan_exclude already filters to critical + non-binary
        if findings:
            lines = "\n".join(
                f"    {rel}:{name} -> {matched!r}"
                for rel, name, matched, _ in findings
            )
            pytest.fail(
                "These files are in scan_exclude (so the scanner never inspects them) "
                "AND are not covered by .publish-exclude (so they DO ship to the public "
                f"mirror). They contain CRITICAL forbidden content:\n{lines}\n\n"
                "Fix by ONE of:\n"
                "  (a) remove the forbidden content from the file, then drop it from "
                "scan_exclude — preferred;\n"
                "  (b) add the file to .publish-exclude if it should never publish;\n"
                "  (c) drop it from scan_exclude and let the scanner block the publish "
                "loudly until it is fixed.\n"
                "Do NOT resolve this by widening scan_exclude."
            )

    def test_scan_exclude_patterns_covering_shipped_files_are_an_explicit_set(
        self, cfg, shipped_files
    ):
        """Ratchet on the scan_exclude ENTRIES that shadow published content.

        Asserted at pattern granularity rather than per-file on purpose: adding one
        more eval case under an already-accepted pattern is routine and should not
        fail, whereas adding a NEW unscanned-but-published pattern is a security
        decision and should require editing the consumer's scan_exclude_baseline.py.

        Binary-only patterns are excluded from the ratchet — they are legitimately
        scan-excluded and may ship (see module docstring).

        pytest.skip when the consumer override file is absent.
        """
        baseline = _consumer_baseline()
        if not baseline:
            pytest.skip(
                "No consumer scan_exclude_baseline.py found — skipping ratchet test "
                "(expected when running canonically from ~/.kiro/publish-toolkit/). "
                "Consumers create scripts/lib/scan_exclude_baseline.py with a BASELINE "
                "set to enable this test."
            )

        scan_ex = cfg.get("scan_exclude") or []
        text_shipped = [f for f in shipped_files if not f.endswith(_BINARY_EXT)]

        shadowing = sorted(
            {
                p
                for p in scan_ex
                if any(
                    fnmatch.fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")
                    for f in text_shipped
                )
            }
        )

        unexpected = [p for p in shadowing if p not in baseline]
        assert not unexpected, (
            "New scan_exclude pattern(s) now shadow files that ship to the public "
            "mirror:\n  " + "\n  ".join(unexpected) + "\n\n"
            "Everything they match is published without ever being scanned. Either "
            "publish-exclude those files, clean them and stop excluding them, or add "
            "the pattern to scripts/lib/scan_exclude_baseline.py WITH a comment "
            "justifying why its content is necessarily pattern-like."
        )

    def test_the_two_mechanisms_are_not_confused_for_each_other(self, cfg):
        """Documentation-as-test: scan_exclude != publish-exclude.

        The three leaks that motivated this file all came from treating a
        scan_exclude entry as though it also prevented publication. Assert the
        distinction concretely on a file known to be in one and not the other.

        Uses .publish-secrets-scan.yml as the canonical example: it is always
        in scan_exclude (it self-trips if scanned), but may or may not be in
        .publish-exclude depending on the consumer's posture. The test asserts
        only that the distinction EXISTS (some entries are in scan_exclude but
        not publish-exclude), not that any specific file is in both or neither.
        """
        scan_ex = set(cfg.get("scan_exclude") or [])
        pub_ex = set(_publish_exclude_patterns())

        assert ".publish-secrets-scan.yml" in scan_ex, (
            "Expected the scanner config itself to be scan-excluded; update this "
            "test if that changed deliberately."
        )

        # There MUST be at least one file that is scan-excluded but not
        # publish-excluded — that is what the invariant is designed to guard.
        # If every scan-excluded entry is also publish-excluded, the test no
        # longer demonstrates the distinction and should be rewritten.
        overlap_only_in_scan = scan_ex - pub_ex
        assert overlap_only_in_scan, (
            "Every scan_exclude entry is also a publish-exclude entry. That is safe, "
            "but it means this test no longer demonstrates the scan_exclude vs "
            "publish-exclude distinction — rewrite it rather than deleting it."
        )

# ---------------------------------------------------------------------------
# Class: TestScannerStillBlocksKnownDirtyContent  (from CVX variant)
# ---------------------------------------------------------------------------

class TestScannerStillBlocksKnownDirtyContent:
    """Prove the scanner gate actually gates.

    Origin: CVX scripts/tests/test_publish_scan_exclude_invariant.py
    """

    def test_publish_aborts_on_scanner_findings(self):
        """A gate that reports findings but lets the publish continue is not a gate.

        secret-scan.py exits 1 whenever a CRITICAL finding exists, with or without
        --strict. --strict only escalates warnings to failures as well. So the
        enforcement that matters is that publish-to-github.sh inspects the scanner's
        exit status and aborts.

        Also verifies the scanner module exposes scan_file (API-presence check).
        """
        mod = _load_scanner()
        if mod is not None:
            assert hasattr(mod, "scan_file"), (
                "scanner API changed (scan_file missing); update this test"
            )
        src_path = _ROOT / "scripts" / "publish-to-github.sh"
        if not src_path.exists():
            pytest.skip(f"publish-to-github.sh not found at {src_path}")
        src = src_path.read_text()
        assert "secret-scan.py" in src or "SCAN_SCRIPT" in src, (
            "publish-to-github.sh no longer invokes the secret scanner."
        )
        assert re.search(r"SCAN_EXIT\s*-ne\s*0", src), (
            "publish-to-github.sh no longer aborts on a non-zero scanner exit. "
            "Without that check the scanner's findings are printed and the publish "
            "proceeds anyway, which is how a known-dirty tree reaches the mirror."
        )

    def test_scanner_exits_nonzero_on_a_critical_finding(self, tmp_path):
        """Directly prove the exit-code contract the publish script depends on.

        Origin: CVX variant. Not present in the CMS variant (which assumes the
        exit-code contract rather than proving it).
        """
        scan_script = _ROOT / "scripts" / "lib" / "secret-scan.py"
        if not scan_script.exists():
            pytest.skip(f"secret-scan.py not found at {scan_script}")

        cfg_txt = (
            "forbidden_patterns:\n"
            "  canary:\n"
            "    regex: 'SENTINEL-DO-NOT-SHIP'\n"
            "    severity: critical\n"
            "    description: test fixture\n"
            "forbidden_strings: []\n"
            "scan_exclude: []\n"
        )
        (tmp_path / "cfg.yml").write_text(cfg_txt)
        root = tmp_path / "tree"
        root.mkdir()
        (root / "leak.txt").write_text("value = SENTINEL-DO-NOT-SHIP\n")
        # argv is a static list with no shell: `sys.executable` and the `str(...)`
        # paths are test-local (tmp_path, pytest-owned) and cannot be influenced by
        # an external resource. shell=False (the default) is in force. semgrep flags
        # this solely because the argv entries are expressions, not string literals.
        # Keep `nosemgrep` as the last comment line before the target.
        # nosemgrep: dangerous-subprocess-use-audit
        r = subprocess.run(
            [
                sys.executable,
                str(scan_script),
                "--config", str(tmp_path / "cfg.yml"),
                "--root", str(root),
            ],
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, (
            "secret-scan.py exited 0 despite a critical finding. The publish gate "
            f"depends on a non-zero exit here. stdout={r.stdout!r}"
        )


# ---------------------------------------------------------------------------
# Class: TestPublishGateActuallyGates  (from CMS variant)
# ---------------------------------------------------------------------------

class TestPublishGateActuallyGates:
    """Publish-script gate check (CMS variant form).

    Origin: CMS scripts/lib/test_publish_scan_exclude_invariant.py.
    Merged with TestScannerStillBlocksKnownDirtyContent above for the scanner
    exit-code proof; this class retains the CMS-style assertion on SCAN_EXIT.
    The check is identical to TestScannerStillBlocksKnownDirtyContent.test_publish_aborts_on_scanner_findings
    above — both classes are kept so the canonical contains all original class names
    and the verify-sync.sh anchor-shape test stays green.
    """

    def test_publish_aborts_on_scanner_findings(self):
        """secret-scan.py exits non-zero on a CRITICAL finding with or without
        --strict. So the enforcement that matters is that the publish script checks
        the exit status. Pin exactly that.

        This assertion is identical to
        TestScannerStillBlocksKnownDirtyContent.test_publish_aborts_on_scanner_findings;
        both are preserved so downstream consumers importing by class name find theirs.
        """
        src_path = _ROOT / "scripts" / "publish-to-github.sh"
        if not src_path.exists():
            pytest.skip(f"publish-to-github.sh not found at {src_path}")
        src = src_path.read_text()
        assert re.search(r"SCAN_EXIT\s*-ne\s*0", src), (
            "publish-to-github.sh no longer aborts on a non-zero scanner exit. "
            "Without that check, findings are printed and the publish proceeds."
        )

class TestAccountIdPatternKeepsTheGoodShape:
    """Preserved from ADP's variant during canonicalization (v0.3.2).

    This is the INVERSE of TestNoRealAccountIdReachesTheMirror, and the two are
    complementary rather than redundant: that class checks the data (no real ID
    ships), this one checks the *shape of the rule* (the pattern stays general
    enough to catch an ID nobody registered).

    It is why the canonicalization nearly lost real coverage. ADP had this test and
    the other two repos did not; the first canonical draft dropped it and merely
    pytest.skip-ed the literal-based assertions when it saw a generic pattern — which
    left nothing at all preventing ADP's generic rule from being "tidied" into a
    literal list, the exact shape that let CMS ship two unregistered account IDs.
    """

    def test_account_id_rule_is_generic_not_a_list_of_literals(self, cfg):
        """Regression guard on the design, not the data.

        A repo that catches any 12-digit number minus an allowlist cannot miss an ID
        nobody thought to register. A repo that registers one literal per known ID
        will miss exactly those. CMS shipped two IDs that way, with all three named
        MUST-strip in an audit — a finding with no pattern behind it is a note, not a
        control.

        Skips for repos that never adopted the generic shape, rather than forcing a
        config redesign as a side effect of adopting this toolkit. Where the generic
        form IS present, it must stay.
        """
        pats = cfg.get("forbidden_patterns") or {}
        generic = [n for n, d in pats.items() if r"\d{12}" in (d.get("regex") or "")]
        if not generic:
            pytest.skip(
                "no generic 12-digit account-ID pattern in this repo's config — it uses "
                "per-ID literals, which TestNoRealAccountIdReachesTheMirror covers "
                "instead. Adopting the generic shape is an improvement, not a "
                "requirement this test imposes."
            )
        for n in generic:
            assert (pats[n].get("severity") == "critical"), (
                f"pattern {n!r} must be severity: critical — the scanner only blocks a "
                "publish on critical findings, so a warning here is not a gate."
            )
