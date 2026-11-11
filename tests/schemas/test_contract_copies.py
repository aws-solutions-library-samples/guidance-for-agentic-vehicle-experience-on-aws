"""
test_contract_copies.py — Tests for scripts/check-contract-copies.py.

Two categories:

1. **Standalone tests** (no sibling checkouts required) — always run:
   - ``test_script_exists``
   - ``test_script_comparison_is_byte_based_not_name_based``
   - ``test_missing_root_gives_exit_2``
   - ``test_missing_file_gives_exit_2_and_names_repo_and_file``
   - ``test_stale_file_names_repo_and_file``

2. **Live check** (requires sibling checkouts; opt-in) — runs only when
   ``CONTRACT_COPIES=1``:
   - ``TestContractCopiesMatchAVX.test_all_copies_match_avx_canonical``

Skip reason when not set:
    "CONTRACT_COPIES not set — skipped; set CONTRACT_COPIES=1 with sibling checkouts present"

Usage (live check):
    CONTRACT_COPIES=1 .venv/bin/python -m pytest tests/schemas/test_contract_copies.py -q

Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/ D14, task 1.9 Accept #4
"""

import os
import pathlib
import subprocess
import sys

import pytest

_SKIP_REASON = (
    "CONTRACT_COPIES not set — skipped; "
    "set CONTRACT_COPIES=1 with sibling checkouts present"
)

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-contract-copies.py"


# ---------------------------------------------------------------------------
# Helpers — build minimal temp trees for the standalone tests
# ---------------------------------------------------------------------------

def _canonical_bytes(schema: str) -> bytes:
    if schema == "dtc_severity":
        return (_REPO_ROOT / "lambdas" / "shared" / "dtc_severity.yaml").read_bytes()
    return (_REPO_ROOT / "agents" / "tier2" / "schemas" / f"{schema}.schema.json").read_bytes()


def _make_avx_tree(tmp: pathlib.Path) -> pathlib.Path:
    avx = tmp / "avx"
    schemas = avx / "agents" / "tier2" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "finding.schema.json").write_bytes(_canonical_bytes("finding"))
    (schemas / "action.schema.json").write_bytes(_canonical_bytes("action"))
    # dtc_severity.yaml canonical (AVX source, added task 3.5)
    dtc_dir = avx / "lambdas" / "shared"
    dtc_dir.mkdir(parents=True)
    (dtc_dir / "dtc_severity.yaml").write_bytes(_canonical_bytes("dtc_severity"))
    return avx


def _make_cms_tree(tmp: pathlib.Path, *, stale_finding: bool = False, missing_finding: bool = False) -> pathlib.Path:
    cms = tmp / "cms"
    avx_dir = (
        cms / "clients" / "ios" / "MeridianMotorsCompanionTests" / "Fixtures" / "avx"
    )
    avx_dir.mkdir(parents=True)
    if not missing_finding:
        finding_bytes = (
            _canonical_bytes("finding")[:-1] + bytes([_canonical_bytes("finding")[-1] ^ 0xFF])
            if stale_finding
            else _canonical_bytes("finding")
        )
        (avx_dir / "finding.schema.json").write_bytes(finding_bytes)
    (avx_dir / "action.schema.json").write_bytes(_canonical_bytes("action"))
    return cms


def _make_dms_tree(tmp: pathlib.Path, *, stale_finding: bool = False, missing_finding: bool = False) -> pathlib.Path:
    dms = tmp / "dms"
    avx_dir = dms / "tests" / "schemas" / "avx"
    avx_dir.mkdir(parents=True)
    if not missing_finding:
        finding_bytes = (
            _canonical_bytes("finding")[:-1] + bytes([_canonical_bytes("finding")[-1] ^ 0xFF])
            if stale_finding
            else _canonical_bytes("finding")
        )
        (avx_dir / "finding.schema.json").write_bytes(finding_bytes)
    (avx_dir / "action.schema.json").write_bytes(_canonical_bytes("action"))
    return dms


def _make_adp_tree(tmp: pathlib.Path, *, stale_dtc: bool = False, missing_dtc: bool = False) -> pathlib.Path:
    adp = tmp / "adp"
    data_dir = (
        adp
        / "platform-foundation"
        / "source"
        / "data-products"
        / "fleet_fault_events"
        / "tests"
        / "data"
    )
    data_dir.mkdir(parents=True)
    if not missing_dtc:
        dtc_bytes = (
            _canonical_bytes("dtc_severity")[:-1] + bytes([_canonical_bytes("dtc_severity")[-1] ^ 0xFF])
            if stale_dtc
            else _canonical_bytes("dtc_severity")
        )
        (data_dir / "dtc_severity.yaml").write_bytes(dtc_bytes)
    return adp


def _run_script(avx: pathlib.Path, cms: pathlib.Path, dms: pathlib.Path, adp: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    if adp is None:
        adp = _make_adp_tree(avx.parent)  # fresh, canonical by default
    return subprocess.run(
        [
            sys.executable, str(_SCRIPT),
            "--avx-root", str(avx),
            "--cms-root", str(cms),
            "--dms-root", str(dms),
            "--adp-root", str(adp),
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Standalone tests — run without CONTRACT_COPIES or sibling checkouts
# ---------------------------------------------------------------------------

def test_script_exists() -> None:
    """The check script must be present in the repository."""
    assert _SCRIPT.exists(), (
        f"check-contract-copies.py not found at {_SCRIPT}. "
        "Create it (spec D14, task 1.9)."
    )


def test_script_comparison_is_byte_based_not_name_based(tmp_path: pathlib.Path) -> None:
    """
    Mutation guard: verify the script actually compares file contents and labels
    CMS copies as 'CMS iOS'.

    Creates a scratch copy with a single byte changed in the CMS finding and
    asserts the script exits 1 and labels the stale line with 'CMS iOS'.

    Catches mutant N3 (both CMS entries relabelled "DMS"): if the label were
    wrong the 'CMS iOS' assertion below would fail.

    Why this matters: a script that checks only file names (e.g. 'finding.schema.json
    exists') would pass even when the content differs — exactly the class of drift
    D14 exists to prevent.
    """
    avx = _make_avx_tree(tmp_path)
    cms = _make_cms_tree(tmp_path, stale_finding=True)  # one byte flipped
    dms = _make_dms_tree(tmp_path)
    adp = _make_adp_tree(tmp_path)

    result = _run_script(avx, cms, dms, adp)
    assert result.returncode == 1, (
        f"Mutation test FAILED: script returned {result.returncode} instead of 1 "
        "when a copy had a mutated byte. "
        "The script is comparing file names only, not content — fix the comparison."
    )
    assert "CMS iOS" in result.stdout, (
        f"Expected 'CMS iOS' in STALE line to name the repo, got:\n{result.stdout}\n"
        "Mutant N3 not caught: CMS entries were mislabelled as 'DMS'."
    )


def test_missing_root_gives_exit_2(tmp_path: pathlib.Path) -> None:
    """
    Mutation guard: a missing root directory must give exit 2, never 0 or 1.

    A script that exits 0 on a missing root would silently pass when the sibling
    checkout is absent, treating 'could not find the copies' as 'all copies match'.
    """
    avx = _make_avx_tree(tmp_path)
    adp = _make_adp_tree(tmp_path)

    result = _run_script(avx, tmp_path / "nonexistent-cms", tmp_path / "nonexistent-dms", adp)
    assert result.returncode == 2, (
        f"Mutation test FAILED: script returned {result.returncode} instead of 2 "
        "when root directories were missing. "
        "A missing root must be exit 2, never 0 (silent pass)."
    )


def test_missing_file_gives_exit_2_and_names_repo_and_file(tmp_path: pathlib.Path) -> None:
    """
    A missing copy file must give exit 2, naming the repo and file.

    Catches mutant B1 (missing.append removed): without this test a missing DMS
    copy would give exit 0 and print "OK".

    Asserts:
      - exit code 2 (not 0 or 1)
      - stderr output names "DMS" as the repo
      - stderr output names the missing file's relative path
    """
    avx = _make_avx_tree(tmp_path)
    cms = _make_cms_tree(tmp_path)
    dms = _make_dms_tree(tmp_path, missing_finding=True)  # DMS finding.schema.json absent
    adp = _make_adp_tree(tmp_path)

    result = _run_script(avx, cms, dms, adp)
    output = result.stdout + result.stderr

    assert result.returncode == 2, (
        f"Expected exit 2 for missing DMS copy, got {result.returncode}.\n"
        f"Output: {output}\n"
        "Mutant B1 not caught: missing.append was removed."
    )
    assert "DMS" in output, (
        f"Expected 'DMS' in output to name the repo, got:\n{output}"
    )
    assert "finding.schema.json" in output, (
        f"Expected 'finding.schema.json' in output to name the file, got:\n{output}"
    )


def test_stale_file_names_repo_and_file(tmp_path: pathlib.Path) -> None:
    """
    A stale copy must give exit 1 with a STALE line naming repo and file.

    Catches mutant B2 (STALE print suppressed): without this test the script
    would exit 1 silently, making it impossible to identify which file is stale.

    Asserts:
      - exit code 1
      - stdout contains "STALE"
      - stdout names "DMS" as the repo
      - stdout names the file's relative path
    """
    avx = _make_avx_tree(tmp_path)
    cms = _make_cms_tree(tmp_path)
    dms = _make_dms_tree(tmp_path, stale_finding=True)  # DMS finding one byte flipped
    adp = _make_adp_tree(tmp_path)

    result = _run_script(avx, cms, dms, adp)

    assert result.returncode == 1, (
        f"Expected exit 1 for stale DMS copy, got {result.returncode}.\n"
        f"Output: {result.stdout + result.stderr}"
    )
    assert "STALE" in result.stdout, (
        f"Expected 'STALE' in stdout, got:\n{result.stdout}\n"
        "Mutant B2 not caught: STALE line was suppressed."
    )
    assert "DMS" in result.stdout, (
        f"Expected 'DMS' in STALE line to name the repo, got:\n{result.stdout}"
    )
    assert "finding.schema.json" in result.stdout, (
        f"Expected 'finding.schema.json' in STALE line to name the file, got:\n{result.stdout}"
    )


def test_relative_roots_stale_dms_exit_1(tmp_path: pathlib.Path) -> None:
    """
    Catch mutant N1: the W1 fix (``relative_to(repo_root)``) must work with
    relative ``--avx-root`` / ``--cms-root`` / ``--dms-root`` / ``--adp-root`` paths.

    Runs the script from ``tmp_path`` as cwd with only relative arguments
    (``--avx-root avx  --cms-root cms  --dms-root dms  --adp-root adp``).  The DMS
    ``finding.schema.json`` is one byte stale so the script must exit 1.

    Asserts:
      - exit code 1
      - stdout contains ``STALE``
      - stdout names ``DMS`` as the repo
      - stdout names the relative path ``tests/schemas/avx/finding.schema.json``
      - stderr contains no ``Traceback``

    Under N1 (``relative_to(copy_path.parents[6])``), the 5-part relative path
    ``dms/tests/schemas/avx/finding.schema.json`` has only 5 parents (indices
    0–4); ``parents[6]`` raises ``IndexError``, which is not caught by
    ``except ValueError``.  The exception propagates before the STALE line is
    printed, so stdout is empty and stderr contains the Traceback.
    """
    _make_avx_tree(tmp_path)
    _make_cms_tree(tmp_path)
    _make_dms_tree(tmp_path, stale_finding=True)
    _make_adp_tree(tmp_path)

    result = subprocess.run(
        [
            sys.executable, str(_SCRIPT),
            "--avx-root", "avx",
            "--cms-root", "cms",
            "--dms-root", "dms",
            "--adp-root", "adp",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 1, (
        f"Expected exit 1 for stale DMS copy with relative roots, "
        f"got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "STALE" in result.stdout, (
        f"Expected 'STALE' in stdout with relative roots.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\n"
        "Mutant N1 not caught: relative_to() crashed before the STALE line."
    )
    assert "DMS" in result.stdout, (
        f"Expected 'DMS' in STALE line.\nstdout: {result.stdout}"
    )
    assert "tests/schemas/avx/finding.schema.json" in result.stdout, (
        f"Expected relative path in STALE line.\nstdout: {result.stdout}"
    )
    assert "Traceback" not in result.stderr, (
        f"Traceback in stderr — relative_to() crashed under relative roots.\n"
        f"stderr: {result.stderr}"
    )


def test_adp_stale_dtc_severity_exit_1(tmp_path: pathlib.Path) -> None:
    """
    A stale ADP dtc_severity.yaml copy must give exit 1 naming repo=ADP and the file.

    Mutation guard for the ADP entry added in task 3.5
    (spec 2026-10-05-fi-fleet-health-stage-d). Without the ADP entry in the script's
    known-copies list this test would fail to catch drift between AVX's
    lambdas/shared/dtc_severity.yaml and ADP's byte-copy (1.5 item 7 agreement test,
    U2).

    Asserts:
      - exit code 1
      - stdout contains "STALE"
      - stdout names "ADP" as the repo
      - stdout names "dtc_severity.yaml"
    """
    avx = _make_avx_tree(tmp_path)
    cms = _make_cms_tree(tmp_path)
    dms = _make_dms_tree(tmp_path)
    adp = _make_adp_tree(tmp_path, stale_dtc=True)  # one byte flipped

    result = _run_script(avx, cms, dms, adp)

    assert result.returncode == 1, (
        f"Expected exit 1 for stale ADP dtc_severity.yaml, got {result.returncode}.\n"
        f"Output: {result.stdout + result.stderr}"
    )
    assert "STALE" in result.stdout, (
        f"Expected 'STALE' in stdout.\n{result.stdout}"
    )
    assert "ADP" in result.stdout, (
        f"Expected 'ADP' in STALE line to name the repo.\n{result.stdout}"
    )
    assert "dtc_severity.yaml" in result.stdout, (
        f"Expected 'dtc_severity.yaml' in STALE line.\n{result.stdout}"
    )


def test_adp_missing_dtc_severity_exit_2(tmp_path: pathlib.Path) -> None:
    """
    A missing ADP dtc_severity.yaml must give exit 2, naming ADP and the file.

    Mutation guard: a missing ADP copy that exits 0 would silently allow the
    drift that U2 exists to prevent (AVX's dtc_severity.yaml and ADP's copy
    must be byte-identical; absence is not a match).
    """
    avx = _make_avx_tree(tmp_path)
    cms = _make_cms_tree(tmp_path)
    dms = _make_dms_tree(tmp_path)
    adp = _make_adp_tree(tmp_path, missing_dtc=True)

    result = _run_script(avx, cms, dms, adp)
    output = result.stdout + result.stderr

    assert result.returncode == 2, (
        f"Expected exit 2 for missing ADP dtc_severity.yaml, got {result.returncode}.\n"
        f"Output: {output}"
    )
    assert "ADP" in output, (
        f"Expected 'ADP' in error output.\n{output}"
    )
    assert "dtc_severity.yaml" in output, (
        f"Expected 'dtc_severity.yaml' in error output.\n{output}"
    )


# ---------------------------------------------------------------------------
# Live check — requires CONTRACT_COPIES=1 and sibling checkouts
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("CONTRACT_COPIES") != "1",
    reason=_SKIP_REASON,
)
class TestContractCopiesMatchAVX:
    """
    Run scripts/check-contract-copies.py and assert exit 0.

    Exit codes from the script:
      0  All five copies match AVX canonical bytes.
      1  One or more copies are stale (names the files).
      2  A sibling checkout or expected file is missing.
    """

    def test_all_copies_match_avx_canonical(self) -> None:
        """
        The script must exit 0 — all five vendored copies byte-identical to AVX.

        Exit 1 means a stale copy (the script names the file); fix by running the
        refresh steps in scripts/check-contract-copies.py's docstring.

        Exit 2 means a sibling checkout is missing; ensure
          ~/connected-mobility-guidance-on-aws,
          ~/guidance-for-dealer-management-system-on-aws  and
          ~/automotive-data-platform-on-aws  exist.
        """
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
        )
        output = (result.stdout + result.stderr).strip()
        assert result.returncode == 0, (
            f"check-contract-copies.py exited {result.returncode}:\n{output}\n\n"
            "One or more vendored schema copies are stale or missing. "
            "Run the script directly to see which files need refreshing."
        )
