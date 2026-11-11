"""No U9 threshold is written in agent code (user's answer 2026-10-14).

The thresholds live in ``deployment/config/avx-policy-defaults.json`` and reach the run
through SSM (``agents/fi_rebalancing/policy.py``). This scans the agent's code (every
non-test module of ``agents/fi_rebalancing`` and the rebalance functions of
``agents/tier2/runner.py``) for:

1. a number equal to a U9 value anywhere: 0.70 (surplus below, applied by ADP), and every value
   of the config file's policy (0.85, 0.90, 0.95, the minimum net benefit, the vehicles cap),
   read from the file so a changed value is still found (review g6fix cycle 1 W2: an ``else 50``
   default survived the name-only check). Two exceptions, both because the $100 minimum net
   benefit (user-approved 2026-10-16) shares its value with code that isn't a threshold: the
   integer 100 as an operand of ``*``, ``/`` or ``//`` (dollars to cents, ratio to percent),
   and the value assigned to one of policy.py's validation bounds (``_BOUND_NAMES``);
2. any number bound to a policy name (assignment, annotated assignment, dict entry, keyword
   argument, parameter default, or a ``.get("<name>", <number>)``-style call), for the
   policy's keys plus ``source_util_cap`` and ``surplus_below``.

Mutants MP1 (``source_util_cap: float | None = 0.85`` back in validate.py) and MP2
(``"min_net_benefit_usd": 50.0`` back in a candidates dict) fail here.
"""
from __future__ import annotations

import ast
import math
import pathlib

from agents.fi_rebalancing import policy
from agents.fi_rebalancing.tests.policy_file import policy_from_config_file

_REPO = pathlib.Path(__file__).resolve().parents[3]
_PACKAGE = _REPO / "agents" / "fi_rebalancing"
_RUNNER = _REPO / "agents" / "tier2" / "runner.py"
_RUNNER_FUNCTIONS = ("run_rebalance", "_run_rebalance_from_env", "_rebalance_record_fields")

_FORBIDDEN_VALUES = tuple(sorted({0.85, 0.70, 0.90, 0.95, *map(float, policy_from_config_file().values())}))
_POLICY_NAMES = frozenset({*policy.POLICY_KEYS, "source_util_cap", "surplus_below"})
#: Rule 1's exceptions (module docstring): unit scaling by the integer 100, and policy.py's
#: bounds that turn a typo into a refusal (1..100 vehicles, 0..1,000,000 USD).
_SCALE = 100
_BOUND_NAMES = frozenset({"_VEHICLES_CEILING", "_NET_BENEFIT_CEILING_USD"})


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool))


def _names_policy(name: str | None) -> bool:
    if not name:
        return False
    low = name.lower().strip("_")
    return any(key in low for key in _POLICY_NAMES)


def _target_name(t: ast.AST) -> str | None:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant):
        return str(t.slice.value)
    return None


def _rule1_exempt(tree: ast.AST) -> set[int]:
    """ids of the constants rule 1 skips: the integer ``_SCALE`` as an operand of ``*``, ``/``
    or ``//``, and the value assigned to a name in ``_BOUND_NAMES``."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div, ast.FloorDiv)):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and type(side.value) is int and side.value == _SCALE:
                    skip.add(id(side))
        if isinstance(node, ast.Assign) and any(_target_name(t) in _BOUND_NAMES for t in node.targets):
            skip.add(id(node.value))
        if (isinstance(node, ast.AnnAssign) and node.value is not None
                and _target_name(node.target) in _BOUND_NAMES):
            skip.add(id(node.value))
    return skip


def findings_in(tree: ast.AST, label: str) -> list[str]:
    """Every forbidden literal in *tree*, as ``label:line: what``."""
    out: list[str] = []
    exempt = _rule1_exempt(tree)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if _is_number(node) and isinstance(node, ast.Constant) and id(node) not in exempt:
            if any(math.isclose(float(node.value), v) for v in _FORBIDDEN_VALUES):
                out.append(f"{label}:{line}: number {node.value}")
        if isinstance(node, ast.Assign) and _is_number(node.value):
            for t in node.targets:
                if _names_policy(_target_name(t)):
                    out.append(f"{label}:{line}: {_target_name(t)} = number")
        if isinstance(node, ast.AnnAssign) and node.value is not None and _is_number(node.value):
            if _names_policy(_target_name(node.target)):
                out.append(f"{label}:{line}: {_target_name(node.target)} = number")
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and _names_policy(k.value) and _is_number(v)):
                    out.append(f"{label}:{line}: {{{k.value!r}: number}}")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if _names_policy(kw.arg) and _is_number(kw.value):
                    out.append(f"{label}:{line}: {kw.arg}=number")
            names = [a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                     and _names_policy(a.value)]
            if names and any(_is_number(a) for a in node.args):
                out.append(f"{label}:{line}: call with {names[0].value!r} and a number")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            for a, d in zip(positional[len(positional) - len(args.defaults):], args.defaults):
                if _names_policy(a.arg) and _is_number(d):
                    out.append(f"{label}:{line}: default {a.arg}=number")
            for a, d in zip(args.kwonlyargs, args.kw_defaults):
                if d is not None and _names_policy(a.arg) and _is_number(d):
                    out.append(f"{label}:{line}: default {a.arg}=number")
    return out


def _agent_trees() -> list[tuple[str, ast.AST]]:
    trees = []
    for path in sorted(_PACKAGE.glob("*.py")):
        trees.append((f"fi_rebalancing/{path.name}", ast.parse(path.read_text(), filename=str(path))))
    runner = ast.parse(_RUNNER.read_text(), filename=str(_RUNNER))
    funcs = {n.name: n for n in runner.body if isinstance(n, ast.FunctionDef)}
    for name in _RUNNER_FUNCTIONS:
        trees.append((f"runner.{name}", funcs[name]))
    return trees


def test_scan_covers_the_agent_modules_and_runner_functions():
    labels = [label for label, _ in _agent_trees()]
    for module in ("agent.py", "candidates.py", "fallback.py", "formulas.py", "llm.py",
                   "validate.py", "policy.py", "selection.py", "write.py", "runner.py"):
        assert f"fi_rebalancing/{module}" in labels
    assert [l for l in labels if l.startswith("runner.")] == [f"runner.{n}" for n in _RUNNER_FUNCTIONS]


def test_no_u9_threshold_is_a_literal_in_agent_code():
    found = [f for label, tree in _agent_trees() for f in findings_in(tree, label)]
    assert found == []


def test_the_scanner_finds_each_shape():
    """Positive control: each shape the guard looks for is reported. The net-benefit and
    vehicles values come from the config file, so the sample follows a policy change."""
    pol = policy_from_config_file()
    net, veh = float(pol["min_net_benefit_usd"]), int(pol["max_vehicles_per_move"])
    src = f'''
x = 0.85
deficit_above = 0.9
y: float = 0.95
cfg = {{"min_net_benefit_usd": {net!r}}}
f(max_vehicles_per_move={veh!r})
cfg.get("target_utilization", 0.7)
def g(source_util_cap: float = 0.8, *, max_utilization=1): pass
_DEFAULT_SOURCE_UTIL_CAP = 1
'''
    found = findings_in(ast.parse(src), "t")
    assert sorted(f.split(": number ")[1] for f in found if ": number " in f) == sorted(
        ["0.7", "0.85", "0.9", "0.95", repr(veh), repr(net)])
    for needle in ("deficit_above = number", "{'min_net_benefit_usd': number}",
                   "max_vehicles_per_move=number", "call with 'target_utilization'",
                   "default source_util_cap=number", "default max_utilization=number",
                   "_DEFAULT_SOURCE_UTIL_CAP = number"):
        assert any(needle in f for f in found), needle


def test_rule1_skips_only_scaling_by_100_and_the_validator_bounds():
    """The two exceptions are for $100 alone, and stop there: a 100 compared, defaulted,
    assigned, added, or a float 100.0 multiplied, is still found."""
    assert float(policy_from_config_file()["min_net_benefit_usd"]) == 100.0, (
        "the exceptions exist because the floor is $100; revisit them with the value")
    ignored = ("cents = int(round(x * 100))\npct = 100 * r\nh = y / 100\nq = y // 100\n"
               "_VEHICLES_CEILING: int = 100\n_NET_BENEFIT_CEILING_USD: int = 100\n")
    assert findings_in(ast.parse(ignored), "t") == []
    found = ("floor = 100\nif net < 100:\n    pass\nm = a if b else 100\nz = x * 100.0\n"
             "w = 100 + x\n_OTHER_CEILING = 100\n_VEHICLES_CEILING = cap = 100 - 0\n")
    hits = [f for f in findings_in(ast.parse(found), "t") if ": number 100" in f]
    assert sorted(int(f.split(":")[1]) for f in hits) == [1, 2, 4, 5, 6, 7, 8], hits


def test_the_scanner_ignores_unrelated_numbers():
    src = 'max_model_calls = 12\nratio = 0.5\ncfg = {"count": 11}\nf(timeout=30)\n'
    assert findings_in(ast.parse(src), "t") == []


def test_forbidden_values_include_every_policy_value():
    """MP5-style: whatever the file holds is what the guard forbids."""
    for value in policy_from_config_file().values():
        assert any(math.isclose(float(value), v) for v in _FORBIDDEN_VALUES)
    assert any(math.isclose(0.70, v) for v in _FORBIDDEN_VALUES)
