#!/usr/bin/env python3
"""
check-contract-copies.py — Compare CMS iOS, DMS vendored, and ADP AVX schema copies
against AVX's canonical bytes.

Exit codes:
  0  All five copies match AVX's canonical files.
  1  One or more copies are stale; each is named with "repo" and "file".
  2  A required root directory or expected file is missing (never 0 on missing).

Usage:
  python scripts/check-contract-copies.py \\
      [--cms-root ~/connected-mobility-guidance-on-aws] \\
      [--dms-root ~/guidance-for-dealer-management-system-on-aws] \\
      [--adp-root ~/automotive-data-platform-on-aws] \\
      [--avx-root <default: parent of this script's directory>]

Refresh steps (when a schema is updated in AVX):
  1. Copy the updated files to each consumer repo:
       cp agents/tier2/schemas/finding.schema.json \\
           ../connected-mobility-guidance-on-aws/clients/ios/MeridianMotorsCompanionTests/Fixtures/avx/finding.schema.json
       cp agents/tier2/schemas/action.schema.json \\
           ../connected-mobility-guidance-on-aws/clients/ios/MeridianMotorsCompanionTests/Fixtures/avx/action.schema.json
       cp agents/tier2/schemas/finding.schema.json \\
           ../guidance-for-dealer-management-system-on-aws/tests/schemas/avx/finding.schema.json
       cp agents/tier2/schemas/action.schema.json \\
           ../guidance-for-dealer-management-system-on-aws/tests/schemas/avx/action.schema.json
  2. Copy dtc_severity.yaml when updated:
       cp lambdas/shared/dtc_severity.yaml \\
           ../automotive-data-platform-on-aws/platform-foundation/source/data-products/fleet_fault_events/tests/data/dtc_severity.yaml
  3. Update each consumer's pin constant with the AVX commit SHA as a comment
     (e.g. ``FINDING_SCHEMA_SHA256 = "..."  # AVX <commit>``).
  4. Run this script and confirm exit 0.

Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/ D14, task 1.9
ADP dtc_severity.yaml copy: .kiro/specs/2026-10-05-fi-fleet-health-stage-d/ task 3.5
"""

import argparse
import hashlib
import pathlib
import sys


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _check(
    avx_root: pathlib.Path,
    cms_root: pathlib.Path,
    dms_root: pathlib.Path,
    adp_root: pathlib.Path,
) -> int:
    """
    Returns 0 (all match), 1 (stale copies), or 2 (missing root/file).
    Prints a line for every problem found.
    """
    # -----------------------------------------------------------------------
    # Validate roots
    # -----------------------------------------------------------------------
    missing_roots = []
    for label, root in [("AVX", avx_root), ("CMS", cms_root), ("DMS", dms_root), ("ADP", adp_root)]:
        if not root.is_dir():
            print(f"ERROR: {label} root not found: {root}", file=sys.stderr)
            missing_roots.append(label)
    if missing_roots:
        return 2

    # -----------------------------------------------------------------------
    # Canonical source files (AVX)
    # -----------------------------------------------------------------------
    canonical: dict[str, pathlib.Path] = {
        "finding": avx_root / "agents" / "tier2" / "schemas" / "finding.schema.json",
        "action": avx_root / "agents" / "tier2" / "schemas" / "action.schema.json",
        "dtc_severity": avx_root / "lambdas" / "shared" / "dtc_severity.yaml",
    }
    for name, path in canonical.items():
        if not path.exists():
            print(
                f"ERROR: AVX canonical schema missing: {path}",
                file=sys.stderr,
            )
            return 2
    canonical_hashes = {name: _sha256(path) for name, path in canonical.items()}

    # -----------------------------------------------------------------------
    # Known copies: (repo_label, repo_root, path, schema_name)
    # repo_root is carried so relative_to() works for any depth, including
    # short relative paths (W1 — parents[6] raised IndexError on shallow trees).
    # -----------------------------------------------------------------------
    copies: list[tuple[str, pathlib.Path, pathlib.Path, str]] = [
        (
            "CMS iOS",
            cms_root,
            cms_root
            / "clients"
            / "ios"
            / "MeridianMotorsCompanionTests"
            / "Fixtures"
            / "avx"
            / "finding.schema.json",
            "finding",
        ),
        (
            "CMS iOS",
            cms_root,
            cms_root
            / "clients"
            / "ios"
            / "MeridianMotorsCompanionTests"
            / "Fixtures"
            / "avx"
            / "action.schema.json",
            "action",
        ),
        (
            "DMS",
            dms_root,
            dms_root / "tests" / "schemas" / "avx" / "finding.schema.json",
            "finding",
        ),
        (
            "DMS",
            dms_root,
            dms_root / "tests" / "schemas" / "avx" / "action.schema.json",
            "action",
        ),
        (
            "ADP",
            adp_root,
            adp_root
            / "platform-foundation"
            / "source"
            / "data-products"
            / "fleet_fault_events"
            / "tests"
            / "data"
            / "dtc_severity.yaml",
            "dtc_severity",
        ),
    ]

    # -----------------------------------------------------------------------
    # Compare
    # -----------------------------------------------------------------------
    missing: list[str] = []
    stale: list[str] = []

    for repo_label, repo_root, copy_path, schema_name in copies:
        if not copy_path.exists():
            print(
                f"MISSING  repo={repo_label!r}  file={copy_path.relative_to(repo_root)}",
                file=sys.stderr,
            )
            missing.append(str(copy_path))
            continue

        copy_hash = _sha256(copy_path)
        expected_hash = canonical_hashes[schema_name]
        if copy_hash != expected_hash:
            display = copy_path.relative_to(repo_root)
            print(
                f"STALE    repo={repo_label!r}  "
                f"file={display}  "
                f"got={copy_hash[:16]}…  "
                f"want={expected_hash[:16]}…"
            )
            stale.append(str(copy_path))

    if missing:
        return 2
    if stale:
        return 1
    print("OK  all five copies match AVX canonical bytes.")
    return 0


def main() -> None:
    _default_avx = pathlib.Path(__file__).parent.parent.resolve()

    parser = argparse.ArgumentParser(
        description="Compare CMS iOS and DMS vendored AVX schemas against AVX canonical bytes."
    )
    parser.add_argument(
        "--avx-root",
        type=pathlib.Path,
        default=_default_avx,
        help=f"AVX repo root (default: {_default_avx})",
    )
    parser.add_argument(
        "--cms-root",
        type=pathlib.Path,
        default=pathlib.Path.home() / "connected-mobility-guidance-on-aws",
        help="CMS repo root",
    )
    parser.add_argument(
        "--dms-root",
        type=pathlib.Path,
        default=pathlib.Path.home() / "guidance-for-dealer-management-system-on-aws",
        help="DMS repo root",
    )
    parser.add_argument(
        "--adp-root",
        type=pathlib.Path,
        default=pathlib.Path.home() / "automotive-data-platform-on-aws",
        help="ADP repo root",
    )
    args = parser.parse_args()

    rc = _check(
        avx_root=args.avx_root.expanduser(),
        cms_root=args.cms_root.expanduser(),
        dms_root=args.dms_root.expanduser(),
        adp_root=args.adp_root.expanduser(),
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
