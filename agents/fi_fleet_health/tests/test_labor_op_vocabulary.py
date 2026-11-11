"""One labor-op vocabulary for AVX's bookings and ADP's code_labor_op_map (G6-D2).

The user's answer (2026-10-17): DMS's labor-op names (``LAB-*``), used by both AVX's
bookings and ADP's map, with a test across the repos that pins them. ADP's view counts a
booked repair only when the RO's labor op is listed for the fault's code (and a PM task as
done only when the op is listed for the task), so every op AVX can book must be in ADP's
list for what it books.

``adp_code_labor_op_map.json`` is ADP's ``fleet_fault_events/code_labor_op_map.json``,
vendored. ``test_vendored_map_is_pinned`` pins it here; ``test_vendored_map_equals_adps``
compares it with the ADP checkout when one is found (``AVX_ADP_REPO``, else
``~/automotive-data-platform-on-aws``), and fails if a found checkout lacks the file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from agents.fi_fleet_health import loop

_DIR = Path(loop.__file__).resolve().parent
_VENDORED = _DIR / "adp_code_labor_op_map.json"
_VENDORED_SHA256 = "e13b70c7a25a2a358b0f8e46b8528ef4791a5d41135477ae2a77a5a183a09cd4"
_ADP_REL = Path("platform-foundation/source/data-products/fleet_fault_events/code_labor_op_map.json")
_DMS_OP = re.compile(r"LAB-[A-Z]+-[0-9]{2,3}")


def _map() -> dict:
    return json.loads(_VENDORED.read_text(encoding="utf-8"))


def _config() -> dict:
    return loop.load_needs_config()


def test_vendored_map_is_pinned() -> None:
    actual = hashlib.sha256(_VENDORED.read_bytes()).hexdigest()
    assert actual == _VENDORED_SHA256, (
        f"adp_code_labor_op_map.json changed (sha256 {actual}). Re-vendor it from ADP's "
        f"{_ADP_REL} and update _VENDORED_SHA256, then re-check needs_config.yaml."
    )


def _adp_root() -> "Path | None":
    """Return the ADP checkout root for cross-repo comparison, or None (skip).

    When AVX_ADP_REPO is set, use exactly that path and *fail* immediately if
    it is not an ADP checkout (i.e. has no ``platform-foundation/``).  A typo
    in the env var must never silently fall back to a different tree.

    When unset, fall back to ``~/automotive-data-platform-on-aws``.  Return
    None if that path also lacks ``platform-foundation/``; the caller skips the
    test and the sha256 pin still enforces byte-level identity.
    """
    av = os.environ.get("AVX_ADP_REPO")
    if av is not None:
        repo = Path(av)
        assert (repo / "platform-foundation").is_dir(), (
            f"AVX_ADP_REPO={repo} is not an ADP checkout (no platform-foundation/)"
        )
        return repo
    home_repo = Path.home() / "automotive-data-platform-on-aws"
    return home_repo if (home_repo / "platform-foundation").is_dir() else None


def test_vendored_map_equals_adps() -> None:
    found = _adp_root()
    if found is None:
        pytest.skip("no ADP checkout found; the sha256 pin still holds")
    adp = found / _ADP_REL
    assert adp.is_file(), f"ADP checkout {found} has no {_ADP_REL}"
    assert adp.read_bytes() == _VENDORED.read_bytes(), (
        f"{adp} differs from the vendored copy; re-vendor and re-check needs_config.yaml"
    )


class TestAdpRoot:
    """Unit tests for _adp_root() resolution logic using tmp_path fakes."""

    def test_env_set_valid_checkout(self, tmp_path, monkeypatch) -> None:
        fake = tmp_path / "adp"
        (fake / "platform-foundation").mkdir(parents=True)
        monkeypatch.setenv("AVX_ADP_REPO", str(fake))
        assert _adp_root() == fake

    def test_env_set_not_a_checkout_fails(self, tmp_path, monkeypatch) -> None:
        empty = tmp_path / "not-adp"
        empty.mkdir()
        monkeypatch.setenv("AVX_ADP_REPO", str(empty))
        with pytest.raises(AssertionError, match="not an ADP checkout"):
            _adp_root()

    def test_env_unset_home_checkout_present(self, tmp_path, monkeypatch) -> None:
        fake_home = tmp_path
        (fake_home / "automotive-data-platform-on-aws" / "platform-foundation").mkdir(parents=True)
        monkeypatch.delenv("AVX_ADP_REPO", raising=False)
        monkeypatch.setenv("HOME", str(fake_home))
        assert _adp_root() == fake_home / "automotive-data-platform-on-aws"

    def test_env_unset_no_checkout_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("AVX_ADP_REPO", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _adp_root() is None


def test_every_bookable_fault_code_books_an_op_adp_counts() -> None:
    """For every code ADP lists, the op AVX books (whatever the band) is in ADP's list."""
    codes = _map()["fault_codes"]
    wrong = {}
    for code, ops in codes.items():
        for band in ("p0", "attention", "soon"):
            got = loop.map_need_to_labor_op(
                {"kind": "fault", "code": code, "severity_band": band}, _config())["labor_op"]
            if got not in ops:
                wrong[(code, band)] = (got, ops)
    assert codes and not wrong, f"(code, band) -> (booked, ADP's ops): {wrong}"


def test_needs_config_fault_codes_are_exactly_adps() -> None:
    """No stale code in AVX's config, and none of ADP's codes left to the band fallback."""
    assert set(_config()["fault_codes"]) == set(_map()["fault_codes"])


def test_every_pm_task_books_an_op_adp_counts() -> None:
    tasks = _map()["pm_tasks"]
    assert set(_config()["pm_tasks"]) == set(tasks)
    wrong = {}
    for task, ops in tasks.items():
        got = loop.map_need_to_labor_op({"kind": "pm_task", "task_code": task}, _config())["labor_op"]
        if got not in ops:
            wrong[task] = (got, ops)
    assert not wrong, f"task -> (booked, ADP's ops): {wrong}"


def test_every_op_avx_can_book_is_a_dms_name() -> None:
    """Every configured op, every Agent 2 kind and every fallback (unknown code, task, kind)."""
    cfg = _config()
    ops = [e["labor_op"] for section in ("fault_bands", "fault_codes", "pm_tasks", "agent2_findings")
           for e in cfg[section].values()]
    for need in (
        {"kind": "fault", "code": "ZZZZZ", "severity_band": "p0"},
        {"kind": "pm_task", "task_code": "NOT_A_TASK"},
        {"kind": "agent2_finding", "finding_kind": "health.unknown"},
        {"kind": "nothing"},
    ):
        ops.append(loop.map_need_to_labor_op(need, cfg)["labor_op"])
    for need in (
        {"kind": "fault", "code": "ZZZZZ", "severity_band": "p0"},
        {"kind": "pm_task", "task_code": "NOT_A_TASK"},
        {"kind": "agent2_finding", "finding_kind": "health.unknown"},
    ):
        ops.append(loop.map_need_to_labor_op(need, {})["labor_op"])
    bad = sorted({op for op in ops if not _DMS_OP.fullmatch(op)})
    assert len(ops) > 70 and not bad, f"not DMS LAB-* names: {bad}"


# ---------------------------------------------------------------------------
# Option (a), user 2026-10-18 (review g6e-code-avx c1 W3, S1): three op sets kept apart
# ---------------------------------------------------------------------------
# ADP's map keeps fault repairs apart from PM tasks (ADP's tests). AVX owns the third set,
# Agent 2's bookings, and its fallbacks: ADP's map must list none of them, or a completed
# Agent 2 booking (or a fallback booking) clears a fault or marks a PM task done. Both sets
# are named literally, so an invented or renamed op fails here, not only a shape check.

#: Agent 2's booking ops by finding kind.
_AGENT2_OPS = {
    "battery": "LAB-BATSVC-01",
    "brakes": "LAB-BRKSVC-01",
    "tires": "LAB-TIRSVC-01",
    "diagnostics": "LAB-DIAGSVC-01",
    "default": "LAB-DIAGSVC-01",
}
#: What the band entries and loop.py's fallbacks book: DMS's generic electrical op.
_FALLBACK_OPS = {"LAB-ELE-07"}


def _listed_in_adps_map() -> dict[str, list[str]]:
    """op -> every place in ADP's map that lists it (``fault_codes:P0AA6``, ``pm_tasks:...``)."""
    m = _map()
    out: dict[str, list[str]] = {}
    for section in ("fault_codes", "pm_tasks"):
        for key, ops in m[section].items():
            for op in ops:
                out.setdefault(op, []).append(f"{section}:{key}")
    for system, op in m["system_repair_ops"].items():
        out.setdefault(op, []).append(f"system_repair_ops:{system}")
    return out


def test_agent2_ops_are_the_named_set() -> None:
    got = {k: e["labor_op"] for k, e in _config()["agent2_findings"].items()}
    assert got == _AGENT2_OPS


def test_agent2_bookings_clear_no_fault_and_complete_no_pm_task() -> None:
    """An Agent 2 tire booking must not mark TIRE_ROTATION done, a battery booking must not
    mark HV_BATTERY_CHECK done or clear an HV fault, and so on: ADP's map lists none of the
    ops Agent 2 books, for any of its kinds (including the default)."""
    listed = _listed_in_adps_map()
    cfg = _config()
    booked = {
        kind: loop.map_need_to_labor_op(
            {"kind": "agent2_finding", "finding_kind": f"health.{kind}"}, cfg)["labor_op"]
        for kind in ("battery", "brakes", "tires", "diagnostics", "not-a-kind")
    }
    assert set(booked.values()) <= set(_AGENT2_OPS.values()), booked
    shared = {kind: (op, listed[op]) for kind, op in booked.items() if op in listed}
    assert not shared, f"Agent 2 kind -> (op, where ADP's map lists it): {shared}"


def test_fallback_ops_clear_no_fault_and_complete_no_pm_task() -> None:
    """The band entries and every fallback (unknown code, task or kind, empty config) book
    exactly ``_FALLBACK_OPS``, which ADP's map lists nowhere."""
    cfg = _config()
    ops = {e["labor_op"] for e in cfg["fault_bands"].values()}
    for config in (cfg, {}):
        for need in (
            {"kind": "fault", "code": "ZZZZZ", "severity_band": "p0"},
            {"kind": "pm_task", "task_code": "NOT_A_TASK"},
            {"kind": "agent2_finding", "finding_kind": "health.unknown"} if not config else None,
            {"kind": "nothing"},
        ):
            if need is not None:
                ops.add(loop.map_need_to_labor_op(need, config)["labor_op"])
    assert ops == _FALLBACK_OPS, f"fallback ops {sorted(ops)} != {sorted(_FALLBACK_OPS)}"
    listed = _listed_in_adps_map()
    assert not (ops & set(listed)), {op: listed[op] for op in ops & set(listed)}
