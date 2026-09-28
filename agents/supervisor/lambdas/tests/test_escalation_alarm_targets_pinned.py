"""Pin the escalation alarms' target-system list to the executor registry (F32.8).

vsa-tier2-stack.ts creates one CloudWatch alarm per target system on
``AVX/Executor`` / ``AVXActionTerminalFailure`` / ``target_system=<value>``,
because CloudWatch alarms cannot use SEARCH. If a target system is added to
``agents/executors/registry.py`` and not to the stack, its terminal failures
emit a metric that no alarm watches, so they reach nobody. This test fails in
either direction of drift.

It lives here, in Python, because this directory runs in the gating CI job
(F32.7) and ``infrastructure/test/`` (jest) runs in no CI job.

Both sides are read with parsers, not imports, so importing the registry's
executors (and their clients) is not a precondition.
"""
from __future__ import annotations

import ast
import os
import re

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_REGISTRY = os.path.join(_REPO_ROOT, "agents", "executors", "registry.py")
_STACK = os.path.join(_REPO_ROOT, "infrastructure", "lib", "vsa-tier2-stack.ts")


def _registry_targets() -> set[str]:
    tree = ast.parse(open(_REGISTRY).read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TARGET_SYSTEMS" for t in node.targets)):
            return set(ast.literal_eval(node.value))
    raise AssertionError("TARGET_SYSTEMS assignment not found in registry.py")


def _stack_targets() -> set[str]:
    src = open(_STACK).read()
    m = re.search(r"const brokerTargetSystems:\s*string\[\]\s*=\s*\[(.*?)\];", src, re.S)
    assert m, "brokerTargetSystems array not found in vsa-tier2-stack.ts"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_stack_alarm_targets_match_registry():
    registry, stack = _registry_targets(), _stack_targets()
    assert stack == registry, (
        f"missing alarms for {sorted(registry - stack)}; "
        f"alarms for unknown targets {sorted(stack - registry)}"
    )


def test_both_sides_are_non_empty():
    """Anti-vacuity: a parser that matches nothing on both sides compares equal."""
    registry = _registry_targets()
    assert len(registry) >= 6 and "dms" in registry and "ios-owner" in registry
    assert _stack_targets() == registry
