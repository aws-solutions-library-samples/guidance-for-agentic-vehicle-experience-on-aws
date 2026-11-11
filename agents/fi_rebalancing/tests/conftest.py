"""Every test here runs with the policy from ``deployment/config/avx-policy-defaults.json``.

The agent reads its U9 thresholds only from the installed policy (``policy.current()``), which
the Tier 2 runner loads from SSM in production. A test that needs other values passes them in
``config`` or installs its own with ``policy.active(...)``.
"""
from __future__ import annotations

import pytest

from agents.fi_rebalancing import policy
from agents.fi_rebalancing.tests.policy_file import policy_from_config_file


@pytest.fixture(autouse=True)
def _rebalancing_policy():
    with policy.active(policy_from_config_file()) as installed:
        yield installed
