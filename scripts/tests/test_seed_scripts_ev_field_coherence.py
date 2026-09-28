"""
Cross-repo regression guard: EV vehicle rows must not carry combustion-only
signals, and must not seed a zero energy level.

Prior art / incidents:
  * cvx/issues/2026-08-19-ev-vehicle-rows-carry-ice-signals/{report,summary}.md
  * The sibling guard for the same *class* of defect (a seed asserting state it
    has no evidence for): ``test_seed_scripts_no_fake_connected.py``.

The bug, three times over:

  1. ``VEH-MRDN-0015`` was converted from ICE to EV directly in DynamoDB. Its
     ``fuelLevel`` and ``engineTemp`` were zeroed rather than set, so the iOS
     home screen rendered "Battery 0%" and "Engine 0°F" on a healthy electric
     SUV. ``scripts/seed-persona-users.py`` was never brought along, so
     re-seeding would have reverted the vehicle to a petrol pickup.
  2. ``VEH-VO-001`` was seeded that way from the start — ``fuelLevel: 0`` and
     ``engineTemp: 22`` on a battery-electric hatchback. A plausible-looking 22
     is worse than a zero: it reads as a real measurement.
  3. ``cms/deployment/scripts/seed_public_demo_fleet.py`` wrote
     ``engineTemp: 0, engineRPM: 0, fuelLevel: 0`` unconditionally for every
     vehicle in the fleet, regardless of ``fuelType`` — the generator of the
     defect rather than an instance of it, and squarely against its own module
     docstring, which rejects seeding live state the seed has no evidence for.

Two invariants follow, and both are about the difference between *absent* and
*zero*. An absent optional decodes to an em dash and the UI says "unknown"; a
zero decodes to a number and the UI says "this is the reading". For a signal a
BEV does not physically have, only the first is true:

  * A BEV row carries no ``engineTemp`` / ``engineRPM`` / coolant field.
  * No vehicle row seeds ``fuelLevel: 0``. For a BEV that field carries state of
    charge (the backend aliases the Redis ``ev_soc`` signal onto it), so a zero
    claims a flat battery.

Hybrids are deliberately exempt from the first invariant: they have an engine,
so the engine fields apply.

Scope. Two complementary passes, because seeds come in two shapes:

  * ``test_no_literal_ev_dict_with_ice_fields`` walks the AST of every
    ``seed*.py`` in both repos and inspects dict literals. This is what catches
    the hand-written persona blocks (incidents 1 and 2). It is AST-based rather
    than regex-based because the defect spans multiple lines of one dict — a
    line-oriented pattern cannot see that ``engineTemp`` and
    ``fuelType: "electric"`` belong to the same object.
  * ``test_generated_rows_are_coherent`` imports seed modules that build rows
    programmatically and validates what they actually emit. This is what catches
    incident 3, whose values are computed and therefore invisible to the AST
    pass.

If the sibling CMS repo is absent (CI checking out one repo), its scan is
skipped with a note. The CVX scan always runs.

Manual invocation::

    cd ~/guidance-for-connected-vehicle-experience-on-aws
    python3 -m pytest scripts/tests/test_seed_scripts_ev_field_coherence.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EV_FUEL_TYPES = {"electric", "bev", "ev"}

#: Signals that require a combustion engine to exist.
ICE_ONLY_FIELDS = ("engineTemp", "engineRPM", "coolantTemp", "coolantTempC")

CVX_REPO = Path(__file__).resolve().parents[2]


def _discover_cms_repo() -> Path | None:
    env = os.environ.get("CMS_REPO_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    candidate = CVX_REPO.parent / "connected-mobility-guidance-on-aws"
    return candidate if candidate.is_dir() else None


CMS_REPO = _discover_cms_repo()

EXCLUDED_PARTS = {
    ".venv", "venv", "node_modules", "cdk.out", "build", "dist",
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
}

#: Modules that build vehicle rows programmatically, as
#: ``(repo-relative path, callable name)``. Values are computed, so the AST pass
#: cannot see them and they must be exercised instead.
GENERATED_ROW_SOURCES = [
    ("deployment/scripts/seed_public_demo_fleet.py", "vehicle_items", "cms"),
    # Added 2026-08-20. This seed landed the day after the guard did, and the AST
    # pass could not see it: its `fuelType` is a loop variable, so every dict was
    # skipped and the file contributed nothing while the suite stayed green. It
    # was in fact compliant on the engine fields — but a guard that passes by
    # scanning nothing is indistinguishable from one that passes by checking, and
    # the omission did hide a real gap (no `batteryCapacityKwh` on any EV row).
    ("deployment/scripts/seed_meridian_fleet.py", "build_plan", "cms"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_seed_scripts(root: Path | None) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    out: list[Path] = []
    for path in root.rglob("seed*.py"):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if not path.name.startswith("seed"):
            continue
        out.append(path)
    return out


def _literal_dict_fields(node: ast.Dict) -> dict[str, ast.AST]:
    """Map constant string keys to their value nodes, ignoring ``**spread``.

    A ``**spread`` entry has a ``None`` key. Those are skipped deliberately:
    the value is not knowable statically, and the generated-row pass covers it.
    """
    out: dict[str, ast.AST] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
    return out


def _const(node: ast.AST | None):
    """Unwrap a literal, including ``Decimal("72")`` as it appears in seeds."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    # Decimal("72") / Decimal(72)
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name == "Decimal":
            inner = node.args[0]
            if isinstance(inner, ast.Constant):
                try:
                    return float(inner.value)
                except (TypeError, ValueError):
                    return None
    return None


def _is_zero(value) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value) == 0.0
    if isinstance(value, str):
        try:
            return float(value) == 0.0
        except ValueError:
            return False
    return False


def _scan_ast(path: Path) -> list[str]:
    """Return human-readable violations found in dict literals in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = _literal_dict_fields(node)
        if "fuelType" not in fields:
            continue
        fuel = _const(fields["fuelType"])
        if not isinstance(fuel, str):
            continue
        is_ev = fuel.lower() in EV_FUEL_TYPES
        where = f"{path}:{node.lineno}"

        if is_ev:
            for f in ICE_ONLY_FIELDS:
                if f in fields:
                    val = _const(fields[f])
                    violations.append(
                        f"{where}: fuelType={fuel!r} but {f}={val!r} is present. "
                        f"A BEV has no engine — omit the field rather than "
                        f"seeding a number the UI will render as a reading."
                    )

        if "fuelLevel" in fields and _is_zero(_const(fields["fuelLevel"])):
            label = "state of charge" if is_ev else "fuel level"
            violations.append(
                f"{where}: fuelType={fuel!r} with fuelLevel=0 seeds a flat "
                f"{label}, which the app renders as '0%'. Seed a real value, or "
                f"omit the field if it is genuinely unknown."
            )
    return violations


def assert_row_coherent(row: dict, origin: str) -> list[str]:
    """Validate one emitted vehicle row. Shared by both passes."""
    problems: list[str] = []
    vid = row.get("vehicleId", "<no-id>")
    fuel = str(row.get("fuelType") or "").lower()
    is_ev = fuel in EV_FUEL_TYPES

    if is_ev:
        for f in ICE_ONLY_FIELDS:
            if f in row:
                problems.append(
                    f"{origin}: {vid} fuelType={fuel!r} emits {f}={row[f]!r} — "
                    f"combustion-only field on a battery-electric vehicle."
                )
    if "fuelLevel" in row and _is_zero(row["fuelLevel"]):
        problems.append(
            f"{origin}: {vid} emits fuelLevel=0, which renders as "
            f"{'state of charge' if is_ev else 'fuel level'} 0%."
        )
    if is_ev and not row.get("batteryCapacityKwh"):
        problems.append(
            f"{origin}: {vid} is electric but emits no batteryCapacityKwh, so the "
            f"battery card shows a charge percentage with no energy behind it."
        )
    return problems


def _vehicle_rows(result) -> list[dict]:
    """Pull vehicle rows out of a seed's return value.

    Seeds differ: some return a flat list of vehicle rows, others a tuple of
    (fleets, vehicles, enrollments). Identify vehicle rows structurally — by the
    presence of ``vehicleId`` — rather than by position, so a seed reordering its
    return tuple cannot quietly drop this file out of coverage.
    """
    candidates = result if isinstance(result, (list, tuple)) else [result]
    rows: list[dict] = []
    for group in candidates:
        if isinstance(group, dict):
            group = [group]
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if isinstance(item, dict) and "vehicleId" in item:
                rows.append(item)
    return rows


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_seedmod_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sibling_cms_repo_discovered_or_explicitly_absent():
    """Make the scan's coverage visible rather than silently one-repo."""
    if CMS_REPO is None:
        pytest.skip(
            "sibling CMS repo not found; set CMS_REPO_PATH to include it. "
            "CVX-side scanning still ran in the other tests."
        )
    assert (CMS_REPO / "deployment").is_dir(), (
        f"CMS_REPO_PATH={CMS_REPO} does not look like the CMS repo."
    )


def test_seed_scripts_are_discovered():
    """A guard that scans nothing passes for the wrong reason."""
    found = _find_seed_scripts(CVX_REPO) + _find_seed_scripts(CMS_REPO)
    assert found, "no seed*.py found in either repo — the scan globbed nothing."


def test_no_literal_ev_dict_with_ice_fields():
    """Hand-written EV blocks must not carry engine fields or a zero energy level."""
    violations: list[str] = []
    for path in _find_seed_scripts(CVX_REPO) + _find_seed_scripts(CMS_REPO):
        violations += _scan_ast(path)

    assert not violations, (
        f"{len(violations)} incoherent EV field(s) in seed dict literals:\n\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_generated_rows_are_coherent():
    """Programmatic row builders must emit coherent rows.

    The AST pass cannot see computed values, so the generator that produced the
    2026-08-19 public-demo-fleet defect is exercised directly.
    """
    problems: list[str] = []
    checked = 0
    for rel, fn_name, repo in GENERATED_ROW_SOURCES:
        root = CMS_REPO if repo == "cms" else CVX_REPO
        if root is None:
            continue
        path = root / rel
        if not path.is_file():
            continue
        module = _load_module(path)
        fn = getattr(module, fn_name, None)
        # Loudly, not silently. An earlier revision skipped a missing callable,
        # so naming the wrong function left the suite green while checking
        # nothing — the same "passes by scanning nothing" failure this pass
        # exists to prevent.
        assert callable(fn), (
            f"{rel} declares no callable {fn_name!r}; GENERATED_ROW_SOURCES is "
            f"stale and this seed is NOT being checked."
        )
        rows = _vehicle_rows(fn())
        assert rows, f"{rel}::{fn_name} yielded no vehicle rows to check."
        for row in rows:
            checked += 1
            problems += assert_row_coherent(row, f"{rel}::{fn_name}")

    if checked == 0:
        pytest.skip("no generated-row sources available in this checkout.")

    assert not problems, (
        f"{len(problems)} incoherent row(s) emitted:\n\n"
        + "\n".join(f"  - {p}" for p in problems)
    )
