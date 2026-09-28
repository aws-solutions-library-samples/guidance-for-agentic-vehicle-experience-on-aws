#!/usr/bin/env python3
"""Auto-fill VSA_AGENTCORE_RUNTIME_ARN in the iOS staging LOCAL xcconfig.

Reads the deployed runtime ARN from .bedrock_agentcore.yaml
(`agents.vsa_supervisor_bidi_staging.bedrock_agentcore.agent_arn`) and
updates the matching `VSA_AGENTCORE_RUNTIME_ARN = ...` line in the CMS
repo's `Staging.local.xcconfig`.

WHY THE `.local.` FILE (fixed 2026-07-28): this script previously wrote to
the TRACKED `Staging.xcconfig`, replacing its placeholder
`arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/YOUR_AGENTCORE_RUNTIME_ID`
with a real ARN containing a real 12-digit AWS account id. Every
`make deploy-staging` therefore left an account id staged for commit in a
repo that publishes to a public GitHub mirror; it was caught in a
pre-commit canary scan and reverted, but the script would have re-created
it on the next deploy. `Staging.local.xcconfig` is gitignored
(`Config/*.local.xcconfig`) and is exactly the intended home for real
per-deployment values — see the header of `Staging.xcconfig`, which states
the placeholder pattern is intentional and permanent.

The CMS repo is expected as a sibling directory of CVX. Override with
`--cms-repo` for non-standard layouts.

If the CMS repo path is absent (CI without CMS checked out): logs a
warning and exits 0. iOS xcconfig back-fill is a local-dev convenience.

If the local override does not exist yet it is seeded from
`Staging.local.xcconfig.example` when available, so a fresh clone still
gets a working back-fill.

Idempotent. Safe to re-run.

Usage:
    python3 scripts/update-staging-xcconfig.py
    python3 scripts/update-staging-xcconfig.py --cms-repo /custom/path

Spec: .kiro/specs/2026-05-28-agentcore-staging-infra/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CMS_REPO = REPO_ROOT.parent / "connected-mobility-guidance-on-aws"
# Write the LOCAL (gitignored) override, never the tracked template — see the
# module docstring for the account-id leak this prevents.
XCCONFIG_REL = Path("clients/ios/VSACompanion/Config/Staging.local.xcconfig")
XCCONFIG_EXAMPLE_REL = Path("clients/ios/VSACompanion/Config/Staging.local.xcconfig.example")
TRACKED_XCCONFIG_REL = Path("clients/ios/VSACompanion/Config/Staging.xcconfig")
YAML_PATH = REPO_ROOT / ".bedrock_agentcore.yaml"
AGENT_KEY = "vsa_supervisor_bidi_staging"
LINE_RE = re.compile(r"^VSA_AGENTCORE_RUNTIME_ARN\s*=.*$", re.MULTILINE)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--cms-repo",
        type=Path,
        default=DEFAULT_CMS_REPO,
        help=f"Path to CMS repo root (default: {DEFAULT_CMS_REPO})",
    )
    args = p.parse_args()

    xcconfig_path = args.cms_repo / XCCONFIG_REL
    if not xcconfig_path.exists():
        # Seed the gitignored override from the checked-in example so a fresh
        # clone still gets a working back-fill. Falls back to the tracked
        # template only as a source to COPY FROM — we never write to it.
        seeded_from = None
        for candidate in (args.cms_repo / XCCONFIG_EXAMPLE_REL,
                          args.cms_repo / TRACKED_XCCONFIG_REL):
            if candidate.exists():
                seeded_from = candidate
                break
        if seeded_from is None:
            print(
                f"⚠️  {xcconfig_path} not found and no template to seed it from "
                f"— skipping iOS xcconfig back-fill (CI/headless env, or CMS "
                f"repo not at sibling path)"
            )
            return 0
        xcconfig_path.write_text(seeded_from.read_text())
        print(f"ℹ️  Seeded {xcconfig_path.name} from {seeded_from.name}")

    if not YAML_PATH.exists():
        print(f"✗ {YAML_PATH} not found", file=sys.stderr)
        return 2

    try:
        config = yaml.safe_load(YAML_PATH.read_text())
    except yaml.YAMLError as exc:
        print(f"✗ Failed to parse {YAML_PATH}: {exc}", file=sys.stderr)
        return 2

    agent = (config or {}).get("agents", {}).get(AGENT_KEY, {}) or {}
    arn = (agent.get("bedrock_agentcore") or {}).get("agent_arn")
    if not arn:
        print(
            f"✗ {AGENT_KEY}.bedrock_agentcore.agent_arn missing or null in {YAML_PATH}. "
            f"Has `agentcore deploy` been run for this agent?",
            file=sys.stderr,
        )
        return 2

    text = xcconfig_path.read_text()
    new_text, n = LINE_RE.subn(f"VSA_AGENTCORE_RUNTIME_ARN = {arn}", text)
    if n == 0:
        print(
            f"✗ No `VSA_AGENTCORE_RUNTIME_ARN = ...` line found in {xcconfig_path}",
            file=sys.stderr,
        )
        return 2

    if new_text == text:
        print(f"= VSA_AGENTCORE_RUNTIME_ARN already up to date in {xcconfig_path}")
        return 0

    xcconfig_path.write_text(new_text)
    print(f"✓ Updated VSA_AGENTCORE_RUNTIME_ARN in {xcconfig_path}: {arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
