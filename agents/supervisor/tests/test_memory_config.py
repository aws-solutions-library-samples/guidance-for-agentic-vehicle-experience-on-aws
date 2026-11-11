"""Red-phase tests for T1.2 of spec 2026-09-22-cvx-text-session-memory.

These tests MUST fail until T2.3 adds `memory_id` and `memory_region` to
`Config.from_env()`.  A passing suite here means the red phase was skipped.

Do NOT fix config.py in this task — the fix lives in T2.3.

Call-site census (verified by grep 2026-09-22 — not trusting the issue report):
  Direct `config.memory_id` in agents/supervisor/tools/:
    energy_summary.py:66, service_history.py:44, interaction_history.py:44,
    telemetry_summary.py:53, vehicle_identity.py:48, charging_history.py:41,
    customer_profile.py:43, ota_history.py:44
  → 8 direct accesses across 8 tool files (matches report's file list)

  Direct `config.memory_id` in agents/supervisor/ (outside tools/):
    persona_loader.py:313, persona_loader.py:445
  → 2 more direct accesses — report said "10 direct accesses across 8 tool
    files" but 2 of the 10 are in persona_loader, not a tool file.

  `getattr(config, "memory_region", "us-west-2")` in agents/supervisor/tools/:
    energy_summary.py:37, service_history.py:28, interaction_history.py:28,
    telemetry_summary.py:37, vehicle_identity.py:30, charging_history.py:26,
    customer_profile.py:26, ota_history.py:28
  → 8 getattr guards, NOT 1 as the issue report claimed.
    The report stated "exactly 1 getattr (in vehicle_identity.py)"; the actual
    count is 8 — every tool file uses getattr for memory_region defensively.

This discrepancy is recorded here so T2.3's fix does not under-cover the
blast radius.

Census matcher limitation (2026-09-22):
  _collect_memory_callsites matches direct accesses only when the receiver is
  a bare `config` Name node (i.e. `config.memory_id`). It will NOT match
  `self.config.memory_id` or other attribute-chain receivers. At the time of
  writing all 8 tool-file call-sites use a local `config` variable (not
  `self.config`), so the census is complete for the current codebase. If a
  future call-site uses an attribute chain ending in `.config`, this matcher
  will not count it and the census will silently under-report. See the comment
  inside _collect_memory_callsites for the exact AST pattern matched.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest

from agents.supervisor.config import Config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SUPERVISOR_ROOT = _REPO_ROOT / "agents" / "supervisor"
_TOOLS_DIR = _SUPERVISOR_ROOT / "tools"

# Hermetic env baseline: VSA_API_BASE="" passes assert_api_base_region_matches
# because _api_base_region("") returns None, which short-circuits the guard.
# AWS_REGION is pinned so the default in config._DEFAULTS cannot leak from the
# ambient shell (a stale VSA_API_BASE pointing at a different region would
# otherwise cause a ValueError before the hasattr assertions are reached —
# exactly the incident documented in config.py:110-130 and
# issues/2026-09-22-stale-env-vsa-api-base-leaked-into-staging-runtime/).
_PIN_API_BASE = ""        # empty → _api_base_region returns None → guard passes
_PIN_AWS_REGION = "us-west-2"


# ---------------------------------------------------------------------------
# Test 1 — Config.from_env() must expose memory_id and memory_region
#
# Expected failure (until T2.3):
#   AttributeError: 'Config' object has no attribute 'memory_id'
#   AttributeError: 'Config' object has no attribute 'memory_region'
# ---------------------------------------------------------------------------


def test_config_from_env_exposes_memory_id_and_memory_region(monkeypatch):
    """Config.from_env() must return an object with both memory_id and memory_region.

    RED PHASE: fails with AttributeError until T2.3 adds both fields.

    Both from_env() calls are preceded by hermetic env pins so this test fails
    on the hasattr assertion regardless of the ambient shell environment. Without
    the pins, a stale VSA_API_BASE pointing at a different region from AWS_REGION
    causes a ValueError inside from_env() before any hasattr check runs — the
    sentinel failure (config.py:110-130) fires instead of the intended assertion.

    After T2.3: the second block verifies the values come from the canonical
    env vars.
    """
    # Pin VSA_API_BASE and AWS_REGION to a coherent pair before the first call.
    # _api_base_region("") returns None, so the cross-region guard passes.
    monkeypatch.setenv("VSA_API_BASE", _PIN_API_BASE)
    monkeypatch.setenv("AWS_REGION", _PIN_AWS_REGION)

    # Prevent STS calls from the ADP account-id fallback path.
    with mock.patch("agents.supervisor.config.boto3.client"):
        config = Config.from_env()

    # Both attributes must exist — bare attribute access, not getattr, because
    # the spec (D5) requires these to be proper fields, not silently-missing ones.
    assert hasattr(config, "memory_id"), (
        "Config.from_env() has no 'memory_id' attribute — "
        "tools will raise AttributeError at runtime (report.md §3)"
    )
    assert hasattr(config, "memory_region"), (
        "Config.from_env() has no 'memory_region' attribute — "
        "tools fall back to a hardcoded 'us-west-2' via getattr instead of "
        "reading from the deployed environment"
    )

    # After T2.3: verify the values come from the canonical env vars.
    # We set them here so this block is self-contained at the green boundary.
    # VSA_API_BASE and AWS_REGION are already pinned above; they remain set
    # for this second call.
    monkeypatch.setenv("VSA_FOUNDATION_MEMORY_ID", "test-memory-id-abc123")
    monkeypatch.setenv("VSA_MEMORY_REGION", "us-east-2")

    with mock.patch("agents.supervisor.config.boto3.client"):
        config2 = Config.from_env()

    assert config2.memory_id == "test-memory-id-abc123", (
        f"memory_id should come from VSA_FOUNDATION_MEMORY_ID, got {config2.memory_id!r}"
    )
    assert config2.memory_region == "us-east-2", (
        f"memory_region should come from VSA_MEMORY_REGION, got {config2.memory_region!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Pin the call-site count so it cannot drift silently
#
# This test enumerates actual call-sites at test-run time using AST traversal.
# It pins the counts we verified by grep on 2026-09-22:
#   - 8 direct `config.memory_id` accesses in tools/
#   - 8 `getattr(config, "memory_region", ...)` accesses in tools/
#     (report claimed 1 — grep found 8; test pins the real number)
#
# Expected failure (until T2.3):
#   AssertionError on the hasattr check at the start of this test.
#   The census assertions (counts and file sets) are unreachable until that
#   first hasattr passes — they cannot fail independently while the red phase
#   is active.  Once T2.3 lands and hasattr passes, the census assertions
#   become the active guard against call-site drift.
# ---------------------------------------------------------------------------


class _CallSite(NamedTuple):
    file: str          # relative path from repo root
    line: int
    kind: str          # "direct" or "getattr"


def _collect_memory_callsites(directory: Path) -> list[_CallSite]:
    """Walk *.py files under `directory` and return all call-sites that read
    memory_id or memory_region from a `config` object.

    Detects two patterns:
      Direct:   config.memory_id         — receiver must be a bare Name node
                                            with id == "config"
      Getattr:  getattr(config, "memory_region", ...)
                                          — first arg must be a bare Name node
                                            with id == "config"

    KNOWN LIMITATION: the matcher only handles bare `config` receivers.
    An attribute-chain receiver such as `self.config.memory_id` will NOT be
    matched. At the time of writing (2026-09-22) all 8 tool-file call-sites
    use a local `config` variable, so the census is complete for the current
    codebase. If a future call-site uses a chain receiver the pinned count
    will not reflect it — the census will under-report silently rather than
    over-report, so a new call-site of that shape would escape notice until
    someone audits the blast radius manually. This is documented in the module
    docstring so it is a known, bounded limitation rather than an invisible gap.
    """
    sites: list[_CallSite] = []
    for py_file in sorted(directory.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(py_file.relative_to(_REPO_ROOT))
        for node in ast.walk(tree):
            # Pattern 1: config.memory_id  (Attribute node, bare Name receiver)
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "config"
                and node.attr == "memory_id"
            ):
                sites.append(_CallSite(file=rel, line=node.lineno, kind="direct"))
            # Pattern 2: getattr(config, "memory_region", ...)  (Call node, bare Name arg)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "config"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "memory_region"
            ):
                sites.append(_CallSite(file=rel, line=node.lineno, kind="getattr"))
    return sites


def test_memory_config_callsite_census(monkeypatch):
    """Enumerate every call-site that reads config.memory_id or getattr(config,
    "memory_region", ...) in tools/, and assert that Config actually satisfies them.

    Two assertions in sequence — NOT independent during the red phase:
      1. Config has the attributes the call-sites expect — fails (red phase)
         until T2.3.  This is the same property as test 1, approached from the
         blast-radius angle.
      2. The call-site count is exactly what grep measured on 2026-09-22 — the
         census assertions are ONLY reachable once assertion 1 passes (i.e. after
         T2.3 lands).  Until then, this test fails on assertion 1 with the same
         AttributeError as test 1.  Once T2.3 lands, assertion 2 becomes the
         active guard against drift.

    Pinned counts (verified by grep, NOT trusting the issue report):
      - direct `config.memory_id` in tools/:                 8  (one per tool file)
      - getattr(config, "memory_region", ...) in tools/:     8  (one per tool file)
        NOTE: report.md claimed 1 getattr (only vehicle_identity.py).
              grep found 8 — every tool file has it.  Test pins the real number.

    If this test fails after T2.3 with a HIGHER count, a new call-site was
    added without updating this census.  If it fails with a LOWER count, a
    call-site was removed without a corresponding update here.
    """
    # Pin VSA_API_BASE and AWS_REGION so from_env() does not raise a ValueError
    # on stale-shell cross-region mismatch before reaching the hasattr check.
    monkeypatch.setenv("VSA_API_BASE", _PIN_API_BASE)
    monkeypatch.setenv("AWS_REGION", _PIN_AWS_REGION)

    # Assert 1: Config provides memory_id — fails (red) until T2.3.
    # This assertion gives this test its own distinct failure reason so it fails
    # independently of test 1 even when run in isolation.
    with mock.patch("agents.supervisor.config.boto3.client"):
        config = Config.from_env()

    assert hasattr(config, "memory_id"), (
        "Config.from_env() has no 'memory_id' attribute — "
        "the 8 direct call-sites in tools/ will raise AttributeError at runtime"
    )

    # Collect only from the tools/ subdirectory (the blast radius the report
    # describes); persona_loader.py is a separate surface not enumerated here.
    tool_sites = _collect_memory_callsites(_TOOLS_DIR)

    direct_sites = [s for s in tool_sites if s.kind == "direct"]
    getattr_sites = [s for s in tool_sites if s.kind == "getattr"]

    # --- Direct config.memory_id in tools/ ---
    # 8 sites, one per tool file (grep-verified 2026-09-22):
    #   energy_summary.py:66, service_history.py:44, interaction_history.py:44,
    #   telemetry_summary.py:53, vehicle_identity.py:48, charging_history.py:41,
    #   customer_profile.py:43, ota_history.py:44
    _EXPECTED_DIRECT_COUNT = 8
    _EXPECTED_DIRECT_FILES = frozenset([
        "agents/supervisor/tools/energy_summary.py",
        "agents/supervisor/tools/service_history.py",
        "agents/supervisor/tools/interaction_history.py",
        "agents/supervisor/tools/telemetry_summary.py",
        "agents/supervisor/tools/vehicle_identity.py",
        "agents/supervisor/tools/charging_history.py",
        "agents/supervisor/tools/customer_profile.py",
        "agents/supervisor/tools/ota_history.py",
    ])

    direct_files = frozenset(s.file for s in direct_sites)
    assert len(direct_sites) == _EXPECTED_DIRECT_COUNT, (
        f"Expected {_EXPECTED_DIRECT_COUNT} direct config.memory_id accesses in tools/, "
        f"found {len(direct_sites)}.\n"
        + textwrap.indent(
            "\n".join(f"  {s.file}:{s.line}" for s in direct_sites), "  "
        )
    )
    assert direct_files == _EXPECTED_DIRECT_FILES, (
        f"Direct config.memory_id call-site files changed.\n"
        f"  Expected: {sorted(_EXPECTED_DIRECT_FILES)}\n"
        f"  Got:      {sorted(direct_files)}"
    )

    # --- getattr(config, "memory_region", ...) in tools/ ---
    # 8 sites — one per tool file (grep-verified 2026-09-22).
    # The issue report claimed 1 (only vehicle_identity.py); that was WRONG.
    _EXPECTED_GETATTR_COUNT = 8
    _EXPECTED_GETATTR_FILES = frozenset([
        "agents/supervisor/tools/energy_summary.py",
        "agents/supervisor/tools/service_history.py",
        "agents/supervisor/tools/interaction_history.py",
        "agents/supervisor/tools/telemetry_summary.py",
        "agents/supervisor/tools/vehicle_identity.py",
        "agents/supervisor/tools/charging_history.py",
        "agents/supervisor/tools/customer_profile.py",
        "agents/supervisor/tools/ota_history.py",
    ])

    getattr_files = frozenset(s.file for s in getattr_sites)
    assert len(getattr_sites) == _EXPECTED_GETATTR_COUNT, (
        f"Expected {_EXPECTED_GETATTR_COUNT} getattr(config, 'memory_region', ...) "
        f"calls in tools/, found {len(getattr_sites)}.\n"
        f"  NOTE: issue report claimed 1 (only vehicle_identity.py); "
        f"grep found {len(getattr_sites)}.\n"
        + textwrap.indent(
            "\n".join(f"  {s.file}:{s.line}" for s in getattr_sites), "  "
        )
    )
    assert getattr_files == _EXPECTED_GETATTR_FILES, (
        f"getattr(config, 'memory_region') call-site files changed.\n"
        f"  Expected: {sorted(_EXPECTED_GETATTR_FILES)}\n"
        f"  Got:      {sorted(getattr_files)}"
    )



# ---------------------------------------------------------------------------
# T4.2 — VSA_TOOL_MEMORY_READS defaults OFF on the deployed config path
#
# Added 2026-09-22 after T4.2 measured the cost the flag gates. Nothing pinned the
# default before, so it could drift back to True without a test noticing — and the
# consequence of that drift is a silent voice-latency regression, not a test failure.
# ---------------------------------------------------------------------------


class TestToolMemoryReadsDefaultsOff:
    """`Config.from_env()` must leave the 8 tool memory reads GATED by default.

    Measured against the live staging Memory resource (T4.2), mirroring
    `tools/vehicle_identity.py`'s exact call:

        boto3.client("bedrock-agentcore")  warm p50   1.6 ms   (built PER tool call)
        retrieve_memory_records            p50  341.5 ms, p95 514.7 ms
        -------------------------------------------------------------
        per gated tool call                p50  343.1 ms, p95 516.4 ms

    Before spec D5 those call-sites raised `AttributeError` immediately, so the added
    cost is the whole figure rather than a delta. The Nova Sonic bidi loop assumes a
    tool returns inside ~1 s, so one call consumes ~34-52% of that and two in a turn
    cross 1 s at p95 on user-perceived latency. It is also the FLOOR: the measured
    namespaces returned 0 records, so a populated namespace can only be slower.
    All three voice personas carry gated tools (consumer 6, fleet_driver 6,
    retail_advisor 3).

    Defaulting OFF costs nothing that works today — these call-sites have raised
    `AttributeError` on every invocation since `fe18930` (2026-06-18) and returned
    `{"available": False, "reason": "<x> not yet loaded"}`. Flag off returns that same
    dict without the network call.

    Per spec T4.2's Constraints: "if it regresses, default the flag off and file the
    follow-up — do NOT revert hydration, which is independent." Hydration is governed
    by `memory_id`, not by this flag, and is unaffected.
    """

    @staticmethod
    def _from_env(monkeypatch, raw=None):
        monkeypatch.setenv("VSA_API_BASE", "")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        if raw is None:
            monkeypatch.delenv("VSA_TOOL_MEMORY_READS", raising=False)
        else:
            monkeypatch.setenv("VSA_TOOL_MEMORY_READS", raw)
        with mock.patch("agents.supervisor.config.boto3.client"):
            return Config.from_env()

    def test_absent_env_var_leaves_reads_gated(self, monkeypatch):
        """The deployed runtime does NOT set this var — absent must mean OFF.

        Verified 2026-09-22: `VSA_TOOL_MEMORY_READS` appears 0 times in `Makefile`
        and is absent from the live text runtime's environment. So this default is
        what production actually runs on, not a fallback.
        """
        cfg = self._from_env(monkeypatch, None)
        assert cfg.tool_memory_reads is False, (
            "Config.from_env() enabled the 8 tool memory reads by default. Each costs "
            "~343 ms p50 / ~516 ms p95 against a ~1 s per-tool budget, on voice "
            "personas carrying 3-6 of them, where before D5 they cost ~0 ms. If this "
            "default is being changed deliberately, update T4.2 in the spec and the "
            "field comment in config.py with fresh numbers — do not just flip it."
        )

    @pytest.mark.parametrize("raw", ["", "  ", "0", "false", "False", "off", "no", "nope", "2"])
    def test_only_an_explicit_truthy_value_enables(self, monkeypatch, raw):
        """Blank, falsy, and unrecognised values all leave the reads gated.

        Fails CLOSED on an unrecognised value, which is the opposite of the previous
        parser: it used `not in (falsy…)`, so a typo like "ture" or "yes-please"
        silently ENABLED a 343 ms-per-call regression. Now only the explicit truthy
        set switches it on.
        """
        cfg = self._from_env(monkeypatch, raw)
        assert cfg.tool_memory_reads is False, f"{raw!r} must not enable tool memory reads"

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "on", "yes", " true "])
    def test_explicit_truthy_values_enable(self, monkeypatch, raw):
        """The escape hatch still works — an operator can switch the reads on."""
        cfg = self._from_env(monkeypatch, raw)
        assert cfg.tool_memory_reads is True, f"{raw!r} should enable tool memory reads"

    def test_flag_does_not_gate_hydration(self, monkeypatch):
        """Hydration is independent of this flag (T4.2 Constraints).

        Turning the tool reads off must NOT disable text-path session memory, which is
        governed by `memory_id`. Pins the separation so a future change cannot collapse
        the two and "fix" a latency regression by reverting the feature this spec
        exists to deliver.
        """
        monkeypatch.setenv("VSA_FOUNDATION_MEMORY_ID", "mem-abc123")
        cfg = self._from_env(monkeypatch, "false")
        assert cfg.tool_memory_reads is False
        assert cfg.memory_id == "mem-abc123", (
            "memory_id must be unaffected by VSA_TOOL_MEMORY_READS — hydration and the "
            "tool reads are independent switches"
        )
