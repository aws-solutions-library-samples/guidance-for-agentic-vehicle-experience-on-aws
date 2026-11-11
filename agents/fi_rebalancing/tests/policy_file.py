"""The FI rebalancing policy as the repo's config file holds it (tests only).

Production reads the same values from SSM (``agents.fi_rebalancing.policy.load_from_ssm``),
seeded from this file by ``deployment/scripts/seed_avx_policy_params.sh``. Tests install the
file's values, so no test carries its own copy of the thresholds.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from agents.fi_rebalancing import policy

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CONFIG_FILE = REPO_ROOT / "deployment" / "config" / "avx-policy-defaults.json"


def policy_entry(path: pathlib.Path = CONFIG_FILE) -> dict[str, Any]:
    """The one ``parameters`` entry whose ``_key_template`` is the rebalancing policy."""
    data = json.loads(path.read_text())
    matches = [p for p in data["parameters"] if p.get("_key_template") == policy.POLICY_PARAM]
    if len(matches) != 1:
        raise AssertionError(f"{path}: expected one {policy.POLICY_PARAM} entry, got {len(matches)}")
    return matches[0]


def policy_from_config_file(path: pathlib.Path = CONFIG_FILE) -> dict[str, Any]:
    """The validated policy the seed script would write for staging."""
    return policy.parse(policy_entry(path)["staging_default"])
