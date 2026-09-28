"""Canonical scanner tests for publish-toolkit.

CMS + ADP union superset — 13 test functions.
Sourced from:
  - CMS  ~/connected-mobility-guidance-on-aws/scripts/lib/test_secret_scan.py  (10 tests)
  - ADP  ~/automotive-data-platform-on-aws/scripts/lib/test_secret_scan.py     (11 tests)

Spec: guidance-for-connected-vehicle-experience-on-aws/.kiro/specs/
      2026-08-02-publish-flow-extract/
Decisions:
  - make_config() uses neutral fixture string "TESTCANARY_FIXTURE" (not any
    repo-specific codename) so the canonical has no repo affiliation.
  - make_config() uses synthetic 12-digit account ID "999988887777" (not a real account)
    as the aws_account_id fixture regex, replacing the real account ID that previously
    appeared here. See issue 2026-08-02-cms-mirror-account-id-in-scanner-blinded-fixture.
  - test_real_config_account_id_allowlist: PROMOTED with a generalized body that
    discovers the consumer's .publish-secrets-scan.yml relative to the test file,
    extracts the aws_account_id negative-lookahead allowlist entries, asserts each
    is NOT flagged, and a synthetic non-allowlisted 12-digit value IS flagged.
    pytest.skip when config is absent. Contains NO real AWS account ID.
    (decisions.md § "ADP-only test_real_config_account_id_allowlist: generalize,
    do not drop or per-repo-append")
  - Uses only version-agnostic pytest API (plain assert, tmp_path,
    pytest.mark.parametrize) — runs under pytest 8.x and 9.x.
    (decisions.md § "pytest 9.0.2, not pinned 8.x")
"""
import ast
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

SCANNER = Path(__file__).parent / "secret-scan.py"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_config(tmp_path, extra_patterns=None, extra_strings=None, scan_exclude=None):
    """Write a minimal YAML config and return its path.

    Uses the neutral fixture string "TESTCANARY_FIXTURE" so this canonical
    file has no affiliation with any consumer repo's real codenames.

    Uses the synthetic 12-digit account ID "999988887777" (not a real AWS
    account ID) as the aws_account_id fixture regex.  The real internal
    account ID that previously appeared here was replaced to avoid vendoring
    a sensitive literal into three public-mirror consumer repos.
    """
    patterns = {
        "aws_account_id": {
            "regex": r"\b999988887777\b",
            "severity": "critical",
            "description": "Synthetic AWS account ID fixture (not a real account)",
        },
        "warning_pattern": {
            "regex": r"\bSECRET_WARN\b",
            "severity": "warning",
            "description": "Warning-level test pattern",
        },
    }
    if extra_patterns:
        patterns.update(extra_patterns)

    strings = ["TESTCANARY_FIXTURE"]
    if extra_strings:
        strings.extend(extra_strings)

    excludes = list(scan_exclude or [])

    cfg = {
        "forbidden_patterns": patterns,
        "forbidden_strings": strings,
        "scan_exclude": excludes,
    }
    config_path = tmp_path / "scan-config.yml"
    config_path.write_text(yaml.dump(cfg))
    return config_path


def run_scanner(*args):
    """Run the scanner and return (returncode, parsed_json_or_None, stderr)."""
    # argv is a static list with no shell: `sys.executable` and `SCANNER` are
    # module constants and `args` is supplied only by this file's own tests.
    # shell=False (the default) is in force. semgrep flags this solely because
    # the argv entries are expressions rather than string literals.
    # Keep `nosemgrep` as the last comment line before the target.
    # nosemgrep: dangerous-subprocess-use-audit
    result = subprocess.run(
        [sys.executable, str(SCANNER)] + list(args),
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        data = None
    return result.returncode, data, result.stderr


# ---------------------------------------------------------------------------
# Tests: shared (CMS + ADP)
# ---------------------------------------------------------------------------

def test_clean_tree(tmp_path):
    """Empty/scrubbed tree returns clean=true, exit 0."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("Hello world\n")
    config = make_config(tmp_path)

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data is not None
    assert data["clean"] is True
    assert data["findings"] == []


def test_dirty_tree_account_id(tmp_path):
    """File containing the synthetic account ID fixture triggers a critical finding."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.py").write_text("ACCOUNT = '999988887777'\n")
    config = make_config(tmp_path)

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1
    assert data is not None
    assert data["clean"] is False
    assert any(f["pattern_name"] == "aws_account_id" for f in data["findings"])


def test_dirty_tree_forbidden_string(tmp_path):
    """File containing the canonical fixture string 'TESTCANARY_FIXTURE' triggers a critical finding."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "labels.ts").write_text("const codename = 'TESTCANARY_FIXTURE internal';\n")
    config = make_config(tmp_path)

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1
    assert data is not None
    assert data["clean"] is False
    assert any("TESTCANARY_FIXTURE" in f["pattern_name"] for f in data["findings"])


def test_allow_finding_suppresses(tmp_path):
    """--allow-finding suppresses the specific match; result is clean."""
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "config.py"
    target.write_text("ACCOUNT = '999988887777'\n")
    config = make_config(tmp_path)

    # Determine the relative path the scanner will use
    rel = str(target.relative_to(root))
    allow_arg = f"aws_account_id:{rel}:1"

    rc, data, _ = run_scanner(
        "--config", str(config), "--root", str(root),
        "--allow-finding", allow_arg,
    )
    assert rc == 0
    assert data is not None
    assert data["clean"] is True
    assert data["findings"] == []


def test_strict_mode_escalates(tmp_path):
    """Warning-level pattern exits 0 normally but exits 1 with --strict."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "note.txt").write_text("This is a SECRET_WARN value\n")
    config = make_config(tmp_path)

    # Without --strict: exit 0, clean=True
    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data["clean"] is True

    # With --strict: exit 1, clean=False
    rc2, data2, _ = run_scanner("--config", str(config), "--root", str(root), "--strict")
    assert rc2 == 1
    assert data2["clean"] is False


def test_scan_exclude_skips(tmp_path):
    """Files matching scan_exclude glob are not scanned."""
    root = tmp_path / "repo"
    root.mkdir()
    node_modules = root / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.js").write_text("var x = '999988887777';\n")
    config = make_config(tmp_path, scan_exclude=["**/node_modules/**"])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data["clean"] is True
    assert data["findings"] == []


def test_binary_files_skipped(tmp_path):
    """Binary file (contains NULL byte) is not scanned."""
    root = tmp_path / "repo"
    root.mkdir()
    binary = root / "image.bin"
    binary.write_bytes(b"999988887777\x00binary data here")
    config = make_config(tmp_path)

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data["clean"] is True
    assert data["findings"] == []


def test_large_files_skipped(tmp_path):
    """File >5MB is skipped with a log message (not a finding)."""
    root = tmp_path / "repo"
    root.mkdir()
    large = root / "bundle.cjs"
    # Write just over 5MB
    chunk = b"999988887777 " * 1000
    with open(large, "wb") as f:
        while f.tell() < 5 * 1024 * 1024 + 1:
            f.write(chunk)
    config = make_config(tmp_path)

    rc, data, stderr = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data["clean"] is True
    assert data["findings"] == []
    assert "skipping large file" in stderr


# ---------------------------------------------------------------------------
# Tests: CMS-only promotions
# ---------------------------------------------------------------------------

def test_brand_canary_case_insensitive(tmp_path):
    """Brand-canary regexes match case-insensitively after the IGNORECASE
    default added 2026-06-04 (issue 2026-06-03-public-mirror-scanner-case-
    sensitivity). Word-bounded patterns like ``\\bacme\\b`` and ``\\bglobex\\b``
    must catch real-world casing variants (``Acme``, ``ACME``, ``Globex``,
    ``GLOBEX``) — not only the all-lowercase form authored in the YAML.

    Fixture brands are deliberately FICTIONAL. This file is vendored into every
    consumer accelerator, and at least one of them ships ``scripts/lib/`` to a
    public mirror while ``scan_exclude``-ing it, so a real vendor attribution
    here would be published without the scanner ever objecting. See
    ``issues/2026-08-02-cms-mirror-account-id-in-scanner-blinded-fixture``.
    The assertion under test is casing behaviour; the brand strings are
    arbitrary.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Fixture shapes mirror the real-world forms that previously slipped past a
    # case-sensitive scanner: prose attribution, a bare vendor tag, an all-caps
    # variant, a mixed-case compound identifier, and a hyphenated pipe name.
    (root / "leak.py").write_text(
        "# Header per Acme Pro Postman collection Vehicle Enrollment 2.0\n"
        "# vendor: Globex\n"
        "# all-caps: ACME\n"
        "# mixed-case fleet id: InitechFleet\n"
        "# hyphenated external pipe: Umbrella-External\n"
    )
    config = make_config(
        tmp_path,
        extra_patterns={
            "brand_primary": {
                "regex": r"\bacme\b",
                "severity": "critical",
                "description": "Fictional primary brand canary",
            },
            "brand_vendor": {
                "regex": r"\bglobex\b",
                "severity": "critical",
                "description": "Fictional vendor brand canary",
            },
            "brand_compound": {
                "regex": r"\binitechfleet\b",
                "severity": "critical",
                "description": "Fictional compound-identifier canary",
            },
            "brand_hyphenated": {
                "regex": r"\bumbrella-external\b",
                "severity": "critical",
                "description": "Fictional hyphenated-pipe canary",
            },
        },
    )

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1, "scanner must exit non-zero when brand canaries fire"
    assert data is not None
    assert data["clean"] is False

    matched = {(f["pattern_name"], f["matched"]) for f in data["findings"]}
    # Every variant authored in the leak file MUST trip the corresponding
    # canary regardless of case.
    assert ("brand_primary", "Acme") in matched, f"missing Acme match; got {matched}"
    assert ("brand_primary", "ACME") in matched, f"missing ACME match; got {matched}"
    assert ("brand_vendor", "Globex") in matched, f"missing Globex match; got {matched}"
    assert ("brand_compound", "InitechFleet") in matched, f"missing InitechFleet match; got {matched}"
    assert ("brand_hyphenated", "Umbrella-External") in matched, f"missing Umbrella-External match; got {matched}"


def test_nhtsa_recall_data_scan_excluded(tmp_path):
    """Files matching the NHTSA recall-data scan_exclude entries must not
    fire brand canaries. Regression for issue
    2026-06-08-cms-recall-data-scan-exclude — confirms that recall API
    payloads containing factual public-domain vehicle data do not trip
    brand canaries once the path is on the exclude list.

    Uses synthetic oem1_primary / oem1_secondary patterns and
    runtime-constructed tmp_path directory layout so this test has no
    dependency on any consumer repo's real directory tree.

    Fixture brands are deliberately FICTIONAL (Acme, Globex) — this file
    is vendored into consumer accelerators whose scripts/ directories may
    be shipped to a public mirror, so real vendor attributions must not
    appear here. The assertion under test is scan_exclude mechanics; the
    brand strings are arbitrary.
    """
    root = tmp_path / "repo"
    # Mirror the production repo layout that the scan_exclude entries target.
    recall_int = root / "services" / "recall-integration"
    recall_int.mkdir(parents=True)
    recall_warranty = (
        root / "modules" / "cms_ui" / "source" / "frontend" / "src"
        / "components" / "recall-warranty"
    )
    recall_warranty.mkdir(parents=True)

    # Synthetic recall payload — would fire oem1_primary canary if scanned.
    nhtsa_json = (
        '{"recalls":[{"manufacturer":"Acme Motor Company","make":"Acme",'
        '"model":"A-150","yearFrom":2015,"yearTo":2020,"campaignId":"23V123"}]}\n'
    )
    nhtsa_ts = (
        "// Auto-generated from NHTSA Recalls API\n"
        "export const nhtsaRecalls = ["
        '{ manufacturer: "Acme Motor Company", make: "Acme", model: "A-150" },'
        '{ manufacturer: "Globex", make: "Globex", model: "Navigator" }'
        "];\n"
    )

    (recall_int / "nhtsa_fleet_recalls.json").write_text(nhtsa_json)
    (recall_int / "nhtsaRecallData.ts").write_text(nhtsa_ts)
    (recall_warranty / "nhtsaRecallData.ts").write_text(nhtsa_ts)

    # Sanity: a non-excluded sibling with the same content MUST still fire,
    # so we know the canary is active in this test environment.
    (root / "leaked_recall_dump.ts").write_text(nhtsa_ts)

    config = make_config(
        tmp_path,
        extra_patterns={
            "oem1_primary": {
                "regex": r"\bacme\b",
                "severity": "critical",
                "description": "OEM1 primary brand canary (fictional)",
            },
            "oem1_secondary": {
                "regex": r"\bglobex\b",
                "severity": "critical",
                "description": "OEM1 secondary brand canary (fictional)",
            },
        },
        scan_exclude=[
            "services/recall-integration/nhtsa_fleet_recalls.json",
            "services/recall-integration/nhtsaRecallData.ts",
            "modules/cms_ui/source/frontend/src/components/recall-warranty/nhtsaRecallData.ts",
        ],
    )

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    # Sibling outside the exclude list must still fire — confirms canary
    # is active and the IGNORECASE default is working.
    assert rc == 1
    assert data is not None

    finding_files = {f["file"] for f in data["findings"]}
    # Excluded paths MUST NOT appear in findings.
    excluded_paths = {
        "services/recall-integration/nhtsa_fleet_recalls.json",
        "services/recall-integration/nhtsaRecallData.ts",
        "modules/cms_ui/source/frontend/src/components/recall-warranty/nhtsaRecallData.ts",
    }
    leaked_through_exclude = excluded_paths & finding_files
    assert not leaked_through_exclude, (
        f"scan_exclude failed for: {leaked_through_exclude}"
    )
    # Sibling MUST appear (control assertion).
    assert "leaked_recall_dump.ts" in finding_files, (
        f"control sibling did not fire — canary not active in this test? "
        f"findings={data['findings']}"
    )


# ---------------------------------------------------------------------------
# Tests: ADP-only promotions
# ---------------------------------------------------------------------------

def test_forbidden_patterns_case_insensitive(tmp_path):
    """Forbidden_patterns regexes match case-insensitively after the IGNORECASE
    default added 2026-06-04 (issue 2026-06-04-adp-scanner-case-sensitivity,
    portfolio follow-on of CMS 2026-06-03-public-mirror-scanner-case-sensitivity).

    Exercises infrastructure patterns shipped in `.publish-secrets-scan.yml`:
      - ``(AKIA|ASIA)`` access-key prefix (must match lowercase / mixed variants)
      - an internal VCS hostname (must match title-case and upper-case variants)
      - an internal code-review hostname (must match ``Code.Example.Com``)
      - ``\\b[a-z0-9-]+\\.awsi\\.example\\.dev`` (must match title-case variant)
      - ``-----BEGIN ... PRIVATE KEY-----`` (must match lowercase ``begin``)
      - an internal email domain warning (must match upper-case variant)

    The motivating defense-in-depth concern: any future canary added in bare
    ``\\bword\\b`` form would inherit a case-sensitivity blind spot without this
    flag. Pattern-level ``(?-i:...)`` remains the escape hatch for case-sensitive
    matching when explicitly required.

    Note: the synthetic AWS-key fixtures are constructed at runtime via
    concatenation rather than written as literals so they do not trip the
    repo-level pre-commit secret hook (which correctly flags
    ``[A-Z0-9]{16}``-shaped tokens after ``AKIA``/``ASIA``). The 16-char body is a
    purely synthetic test pattern and not a real credential.

    Fixture hostnames and email domains are deliberately FICTIONAL (*.example
    forms). This file is vendored into consumer accelerators that may ship
    scripts/lib/ to a public mirror, so real internal hostnames must not
    appear here. The assertion under test is IGNORECASE behaviour; the
    hostname strings are arbitrary.
    """
    # Construct synthetic key fixtures at runtime to avoid tripping the repo
    # pre-commit hook on literal AKIA/ASIA-prefixed 20-char tokens.
    body = "ABCDEFGHIJKLMNOP"  # 16 alphanumerics
    upper_akia = "A" + "K" + "I" + "A" + body
    lower_akia = upper_akia.lower()
    mixed_asia = "A" + "s" + "i" + "a" + body  # "Asia" + body; tests IGNORECASE on ASIA branch

    root = tmp_path / "repo"
    root.mkdir()
    (root / "leak.py").write_text(
        f"# Canonical upper-case access key prefix: {upper_akia}\n"
        f"# Lowercase variant: {lower_akia}\n"
        f"# Mixed-case Asia variant: {mixed_asia}\n"
        "# Mixed-case VCS hostname: Git.example.internal\n"
        "# Upper-case VCS: GIT.EXAMPLE.INTERNAL\n"
        "# Mixed-case code-review: Code.Example.Com\n"
        "# Mixed-case awsi prefix: Example.awsi.example.dev\n"
        "# Lowercase BEGIN: -----begin private key-----\n"
        "# Upper-case email: noreply@EXAMPLE.COM\n"
    )
    config = make_config(
        tmp_path,
        extra_patterns={
            "aws_access_key": {
                "regex": r"\b(AKIA|ASIA)[A-Z0-9]{16}\b",
                "severity": "critical",
                "description": "AWS access key ID prefix",
            },
            "internal_vcs": {
                "regex": r"git\.example\.internal",
                "severity": "critical",
                "description": "Internal VCS hostname (fictional)",
            },
            "internal_review": {
                "regex": r"code\.example\.com",
                "severity": "critical",
                "description": "Internal code-review hostname (fictional)",
            },
            "internal_awsi": {
                "regex": r"\b[a-z0-9-]+\.awsi\.example\.dev",
                "severity": "critical",
                "description": "Internal awsi.example.dev hostname (fictional)",
            },
            "private_key": {
                "regex": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
                "severity": "critical",
                "description": "Private key material",
            },
            "internal_email": {
                "regex": r"\b[a-zA-Z0-9._-]+@example\.com\b",
                "severity": "warning",
                "description": "Internal example.com email address (fictional)",
            },
        },
    )

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root), "--strict")
    assert rc == 1, "scanner must exit non-zero with --strict when patterns fire"
    assert data is not None
    assert data["clean"] is False

    matched = {(f["pattern_name"], f["matched"]) for f in data["findings"]}

    # Access-key prefix — uppercase (pre-fix) and lowercase / mixed (post-fix)
    assert ("aws_access_key", upper_akia) in matched, \
        f"missing canonical upper-case AKIA-style match; got {matched}"
    assert ("aws_access_key", lower_akia) in matched, \
        f"missing lowercase akia-style match (IGNORECASE); got {matched}"
    assert ("aws_access_key", mixed_asia) in matched, \
        f"missing mixed-case Asia-style match (IGNORECASE); got {matched}"

    # Hostname canaries — case variants
    assert ("internal_vcs", "Git.example.internal") in matched, \
        f"missing Git.example.internal match; got {matched}"
    assert ("internal_vcs", "GIT.EXAMPLE.INTERNAL") in matched, \
        f"missing GIT.EXAMPLE.INTERNAL match; got {matched}"
    assert ("internal_review", "Code.Example.Com") in matched, \
        f"missing Code.Example.Com match; got {matched}"
    assert ("internal_awsi", "Example.awsi.example.dev") in matched, \
        f"missing Example.awsi.example.dev match; got {matched}"

    # Private-key header — lowercase variant
    assert ("private_key", "-----begin private key-----") in matched, \
        f"missing lowercase BEGIN match; got {matched}"

    # Email warning — uppercase domain
    assert ("internal_email", "noreply@EXAMPLE.COM") in matched, \
        f"missing @EXAMPLE.COM match; got {matched}"
def test_case_sensitive_opt_out_with_inline_flag(tmp_path):
    """A pattern using the inline ``(?-i:...)`` flag remains case-sensitive even
    though the scanner now compiles with re.IGNORECASE by default. Locks in the
    documented escape hatch for patterns that genuinely need case-sensitivity.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "data.py").write_text(
        "# Should match: SECRET_TOKEN\n"
        "# Should NOT match (case opt-out): secret_token\n"
    )
    config = make_config(
        tmp_path,
        extra_patterns={
            "case_sensitive_token": {
                "regex": r"(?-i:\bSECRET_TOKEN\b)",
                "severity": "critical",
                "description": "Case-sensitive opt-out pattern",
            },
        },
    )

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1
    matches = [f for f in data["findings"] if f["pattern_name"] == "case_sensitive_token"]
    matched_strs = {f["matched"] for f in matches}
    assert "SECRET_TOKEN" in matched_strs, \
        f"uppercase variant must match; got {matched_strs}"
    assert "secret_token" not in matched_strs, \
        f"lowercase variant MUST NOT match under (?-i:...); got {matched_strs}"


# ---------------------------------------------------------------------------
# Real-config allowlist regression — promoted with generalized body
# (decisions.md § "ADP-only test_real_config_account_id_allowlist: generalize,
# do not drop or per-repo-append")
# ---------------------------------------------------------------------------

def _discover_consumer_config() -> Path:
    """Discover the consumer's .publish-secrets-scan.yml relative to this file.

    When vendored into a consumer repo at ``scripts/lib/test_secret_scan.py``,
    the config lives two levels up at ``<repo-root>/.publish-secrets-scan.yml``.
    Returns the resolved path regardless of whether it exists (caller checks).
    """
    # scripts/lib/ → scripts/ → repo-root/
    return Path(__file__).resolve().parents[2] / ".publish-secrets-scan.yml"


def _extract_account_id_allowlist(config_path: Path) -> list:
    """Parse the aws_account_id regex from the config and return the list of
    12-digit values in the negative-lookahead allowlist.

    The canonical ADP pattern shape is:
      ``\\b(?!VAL1\\b)(?!VAL2\\b)...\\d{12}\\b``

    Returns a list of strings, e.g. ``["123456789012", "000000000000", ...]``.
    Returns an empty list if the pattern has no negative-lookaheads (e.g. CMS/CVX
    which use a bare ``\\b<account>\\b`` pattern with no allowlist).
    """
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    patterns = cfg.get("forbidden_patterns", {})
    entry = patterns.get("aws_account_id", {})
    regex_str = entry.get("regex", "")

    # Extract all (?!NNN\b) lookahead literals — each NNN is a 12-digit string
    return re.findall(r"\(\?!(\d{12})\\b\)", regex_str)


def test_real_config_account_id_allowlist(tmp_path):
    """The consumer's ``.publish-secrets-scan.yml`` ``aws_account_id`` pattern
    allowlist (negative-lookaheads) must be self-consistent:

    - Every value explicitly listed in the allowlist must NOT be flagged by the
      scanner when running under the real config.
    - A synthetic 12-digit value that is NOT in the allowlist MUST be flagged.

    ``pytest.skip`` when the config is absent (e.g. when running canonically
    from ``~/.kiro/publish-toolkit/`` without a consumer repo present).

    Contains NO real AWS account ID — all values under test are either read
    from the consumer's own config at runtime (so this file never hardcodes
    them) or are obviously synthetic fixtures.

    Generalisation rationale: see decisions.md §
    "ADP-only test_real_config_account_id_allowlist: generalize, do not drop
    or per-repo-append".  Per-repo-append is topologically illegal because
    verify-sync.sh compares vendored file sha256 against the anchor; a repo-
    specific append makes the hash diverge and aborts the publish preflight.
    """
    real_config = _discover_consumer_config()
    if not real_config.exists():
        pytest.skip(
            f"Consumer config not found at {real_config} — "
            "skipping real-config allowlist test (expected when running "
            "canonically from ~/.kiro/publish-toolkit/)"
        )

    allowlisted = _extract_account_id_allowlist(real_config)
    if not allowlisted:
        pytest.skip(
            f"No negative-lookahead allowlist entries found in aws_account_id "
            f"regex in {real_config} — this consumer uses a bare literal pattern "
            "(no allowlist to assert against)"
        )

    # Choose a synthetic value that is definitely NOT in the allowlist.
    # Use a value that is not a plausible real account ID.
    synthetic_flagged = "888877776666"
    assert synthetic_flagged not in allowlisted, (
        f"Test design error: synthetic value {synthetic_flagged!r} is in the "
        f"allowlist {allowlisted!r} — choose a different fixture"
    )

    root = tmp_path / "repo"
    root.mkdir()

    # Write one line per allowlisted value plus the synthetic non-allowlisted value.
    lines = [f"ALLOWLISTED_{i} = '{v}'  # must NOT flag\n" for i, v in enumerate(allowlisted)]
    lines.append(f"NOT_ALLOWLISTED = '{synthetic_flagged}'  # must FLAG\n")
    (root / "fixture.py").write_text("".join(lines))

    rc, data, _ = run_scanner("--config", str(real_config), "--root", str(root))

    matched_account_ids = {
        f["matched"]
        for f in (data["findings"] if data else [])
        if f["pattern_name"] == "aws_account_id"
    }

    # Every allowlisted value must NOT be flagged.
    for val in allowlisted:
        assert val not in matched_account_ids, (
            f"Allowlisted value {val!r} was flagged — the negative-lookahead "
            f"in {real_config} may be broken; findings={matched_account_ids}"
        )

    # The synthetic non-allowlisted value MUST be flagged.
    assert synthetic_flagged in matched_account_ids, (
        f"Synthetic non-allowlisted value {synthetic_flagged!r} was NOT flagged "
        f"— the aws_account_id pattern in {real_config} may be too narrow; "
        f"findings={matched_account_ids}"
    )


# ---------------------------------------------------------------------------
# Tests: forbidden_digests — digest-based credential canaries
#
# Synthetic token: 'SYNTHtestTokenForDigestCanaryTestImpl00ABCD0' (44 chars)
# Real sha256:     sha256:4a3c2c7c32e048c618233463d8f8c68d5371c5c5ab24f7737e4042cd96b479bc
#
# NOT a real credential.  The token is constructed to be:
#   - Clearly fictitious (starts with 'SYNTH')
#   - Long enough (>=16 chars) to be found by the tokenizer
#   - Made of characters in [A-Za-z0-9+/=_-] (the tokenizer charset)
# The sha256 value is the real digest of that exact string, computed at
# authoring time and hard-coded so tests are deterministic without calling
# hashlib in the test body (which would make a bug in the hashing logic
# undetectable by its own test).
# ---------------------------------------------------------------------------

# Synthetic token constants — computed once, referenced by all digest tests.
# DO NOT substitute a real credential here.
_SYNTH_TOKEN = "SYNTHtestTokenForDigestCanaryTestImpl00ABCD0"
_SYNTH_TOKEN_DIGEST = "sha256:4a3c2c7c32e048c618233463d8f8c68d5371c5c5ab24f7737e4042cd96b479bc"
# Case-flipped variant: lower-casing the token produces a different sha256,
# so it must NOT match a digest registered for the original casing.
_SYNTH_TOKEN_LOWER = "synthtesttokenfordigestcanarytestimpl00abcd0"
_SYNTH_TOKEN_LOWER_DIGEST = "sha256:dcc860abb1f90f23a6404a7a11c3251b302344e0e0391a7e229e4708f46f6eaf"


def _make_digest_config(tmp_path, digests, **kwargs):
    """Write a config with forbidden_digests and return its path."""
    config_path = make_config(tmp_path, **kwargs)
    cfg = yaml.safe_load(config_path.read_text())
    cfg["forbidden_digests"] = list(digests)
    config_path.write_text(yaml.dump(cfg))
    return config_path


def test_digest_match_fires(tmp_path):
    """A file containing the synthetic token triggers a critical finding when
    its sha256 is in forbidden_digests.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Write the token as a config-file-style value; simulates a credential
    # stored in a config that was scan_excluded but not publish_excluded.
    (root / "local.cfg").write_text(f"client_secret = {_SYNTH_TOKEN}\n")
    config = _make_digest_config(tmp_path, [_SYNTH_TOKEN_DIGEST])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1, "scanner must exit 1 when a digest canary fires"
    assert data is not None
    assert data["clean"] is False

    digest_findings = [
        f for f in data["findings"]
        if f["pattern_name"].startswith("forbidden_digest:")
    ]
    assert len(digest_findings) == 1, f"expected exactly 1 digest finding; got {digest_findings}"
    finding = digest_findings[0]
    # pattern_name must embed the first 8 hex chars of the digest
    assert finding["pattern_name"] == "forbidden_digest:sha256:4a3c2c7c...", (
        f"unexpected pattern_name: {finding['pattern_name']!r}"
    )
    assert finding["severity"] == "critical"
    # matched must be redacted (4+...+4 form) — not the full token
    assert finding["matched"] == "SYNT...BCD0", (
        f"unexpected redacted matched: {finding['matched']!r}"
    )


def test_digest_miss_stays_clean(tmp_path):
    """A file that does NOT contain the registered token stays clean."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "safe.cfg").write_text("client_secret = some_other_unrelated_value\n")
    config = _make_digest_config(tmp_path, [_SYNTH_TOKEN_DIGEST])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0
    assert data is not None
    assert data["clean"] is True
    assert not any(
        f["pattern_name"].startswith("forbidden_digest:")
        for f in data["findings"]
    ), f"unexpected digest findings: {data['findings']}"


def test_digest_malformed_entry_exits_2(tmp_path):
    """A malformed forbidden_digests entry (wrong prefix, wrong length, uppercase
    hex) causes load_config to exit 2 with a clear error message.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("hello\n")

    bad_entries = [
        "md5:abcdef1234567890",              # wrong algorithm prefix
        "sha256:ABCD",                        # too short + uppercase
        "sha256:" + "a" * 63,                # 63 hex chars (one short)
        "sha256:" + "A" * 64,                # uppercase hex (not lowercase)
        "4a3c2c7c32e048c618233463d8f8c68d",  # no prefix at all
        "",                                   # empty string
    ]
    for idx, bad_entry in enumerate(bad_entries):
        # Use a numeric subdirectory so filesystem-safe names are guaranteed
        sub = tmp_path / f"bad_{idx}"
        sub.mkdir(parents=True, exist_ok=True)
        config_path = make_config(sub)
        cfg = yaml.safe_load(config_path.read_text())
        cfg["forbidden_digests"] = [bad_entry]
        config_path.write_text(yaml.dump(cfg))

        rc, _, stderr = run_scanner("--config", str(config_path), "--root", str(root))
        assert rc == 2, (
            f"expected exit 2 for malformed entry {bad_entry!r}; got rc={rc}, stderr={stderr}"
        )
        assert "malformed" in stderr or "error" in stderr, (
            f"expected error message for {bad_entry!r}; got stderr={stderr!r}"
        )


def test_digest_missing_key_is_backward_compatible(tmp_path):
    """A config with no forbidden_digests key must behave exactly as today —
    no regression on existing configs (ADP/CVX will not have this key).
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Write the synthetic token; without the digest registered it must NOT fire
    (root / "safe.cfg").write_text(f"client_secret = {_SYNTH_TOKEN}\n")

    # make_config() produces a config with no forbidden_digests key by default
    config = make_config(tmp_path)
    cfg = yaml.safe_load(config.read_text())
    assert "forbidden_digests" not in cfg, "make_config must not add forbidden_digests by default"

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0, "scanner must not fire on token when no digests are registered"
    assert data is not None
    assert data["clean"] is True
    assert not any(
        f["pattern_name"].startswith("forbidden_digest:")
        for f in data["findings"]
    )


def test_digest_short_token_not_matched(tmp_path):
    """Tokens shorter than 16 characters are not fed to the digest matcher.

    This is the documented tradeoff: the tokenizer regex requires >=16 chars.
    A short secret appearing in plain text won't be caught by a digest canary —
    that's the expected behavior, and the reason plaintext forbidden_strings
    entries remain the right choice for short low-entropy codenames.
    """
    import hashlib

    # Construct a short token (< 16 chars) and register its sha256 as a canary.
    short_token = "ShortSecret"
    assert len(short_token) < 16, "test fixture must be < 16 chars"
    short_digest = "sha256:" + hashlib.sha256(short_token.encode()).hexdigest()

    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text(f"value = {short_token}\n")
    config = _make_digest_config(tmp_path, [short_digest])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0, (
        "short token (<16 chars) must NOT be matched by digest canary — "
        "use forbidden_strings for short secrets"
    )
    assert data is not None
    assert data["clean"] is True


def test_digest_case_sensitivity(tmp_path):
    """Digest matching is case-sensitive: a case-flipped token does NOT match
    the digest registered for the original casing.

    This deliberately differs from the re.IGNORECASE default applied to
    forbidden_patterns.  Credentials are case-sensitive; the digest must match
    the exact byte sequence stored in the issuing system.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Write the lower-cased variant of the token
    (root / "creds.txt").write_text(f"token = {_SYNTH_TOKEN_LOWER}\n")
    # Register the digest for the ORIGINAL (mixed-case) token only
    config = _make_digest_config(tmp_path, [_SYNTH_TOKEN_DIGEST])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 0, (
        "case-flipped token must NOT match the digest registered for the original casing"
    )
    assert data is not None
    assert data["clean"] is True


def test_digest_redaction(tmp_path):
    """The matched field in a digest finding must NOT contain the full token.

    Digest canaries exist so that live credential literals do not appear in any
    config, log, or CI output.  Pasting the full token into the finding would
    defeat that purpose.  The redacted form (first 4 + '...' + last 4) gives
    an operator enough context to locate the token without exposing the secret.

    Note: the token is placed after a space (``OAUTH_SECRET = <token>``) so the
    tokenizer (which matches [A-Za-z0-9+/=_-]{16,}) extracts it as an isolated
    token rather than concatenating it with the key name via the '=' separator.
    """
    root = tmp_path / "repo"
    root.mkdir()
    # Space around '=' ensures the token is tokenized in isolation, not merged
    # with 'OAUTH_SECRET' via the '=' character (which is in the tokenizer charset).
    (root / "secrets.env").write_text(f"OAUTH_SECRET = {_SYNTH_TOKEN}\n")
    config = _make_digest_config(tmp_path, [_SYNTH_TOKEN_DIGEST])

    rc, data, _ = run_scanner("--config", str(config), "--root", str(root))
    assert rc == 1
    assert data is not None

    digest_findings = [
        f for f in data["findings"]
        if f["pattern_name"].startswith("forbidden_digest:")
    ]
    assert digest_findings, "expected at least one digest finding"

    finding = digest_findings[0]
    matched = finding["matched"]

    # The full token must NOT appear verbatim in the matched field
    assert _SYNTH_TOKEN not in matched, (
        f"full token must not appear in finding matched field; got {matched!r}"
    )
    # The redacted form must not be the full token
    assert len(matched) < len(_SYNTH_TOKEN), (
        f"matched field must be shorter than the full token; got {matched!r}"
    )
    # Must have the '...' ellipsis indicating redaction
    assert "..." in matched, f"expected '...' in redacted matched field; got {matched!r}"
