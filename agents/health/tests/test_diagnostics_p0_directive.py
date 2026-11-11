"""A diagnostics group with a P0 code is a p0 Finding that carries only the directive.

Spec ``2026-11-05-avx-proactive-p0-alert`` (§ P, § Deterministic seams) replaced D4 of
``2026-09-25-avx-own-vehicle-findings``. Under D4 a code the triage classifier rates P0 produced
no Finding, because a Finding restating P0 could soften "stop driving", and the old
``test_diagnostics_never_p0.py`` pinned that (issue ``2026-10-23-tier2-writer-frozen-severity``
§ D1 named the failure: a p0 push carrying "Book a service check soon."). That gap left the
driver with no push for a stop-driving fault (C1234 on VEH-MRDN-0015 was on no Finding).

The new rule keeps the reason D4 existed: a P0 group's ``finding`` is exactly
``p0_directive(faults_for_codes(<its P0 codes>), None)``. No advice line, no driver name, no
model text, and the headline carries no instruction. Groups without a P0 code are unchanged
and never p0. The grade comes only from the classifier's rule table.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "lambdas") not in sys.path:
    sys.path.insert(0, str(_REPO / "lambdas"))

from agents.supervisor.dtc_response_catalog import (  # noqa: E402
    faults_for_codes,
    is_p0_directive,
    p0_directive,
)


def _classifier():
    if "triage_classifier" not in sys.modules:
        root = _REPO / "lambdas" / "triage-classifier"
        spec = importlib.util.spec_from_file_location(
            "triage_classifier", root / "__init__.py", submodule_search_locations=[str(root)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["triage_classifier"] = pkg
        spec.loader.exec_module(pkg)
    from triage_classifier import classifier
    from triage_classifier.types import ActiveDtc, Level
    return classifier, ActiveDtc, Level


def _codes_by_p0():
    classifier, ActiveDtc, Level = _classifier()
    p0, other = set(), set()
    for code in classifier._SEVERITY_TABLE:
        levels = [lv for lv, _ in classifier._classify_dtcs((ActiveDtc(code=code, first_seen_at="t"),))]
        (p0 if Level.P0 in levels else other).add(code)
    assert p0 and other, "the classifier table must rate some codes P0 and some not"
    assert "C1234" in p0, "C1234 (the field-video brake fault) must be a classifier P0 code"
    return p0, other


def _row(code):
    from agents.health.cms_dtc_source import _level_and_system
    level, system = _level_and_system(code)
    return {"code": code, "first_seen_at": "2026-11-05", "description": "d", "system": system, "level": level}


_ADVICE_WORDS = ("book", "service check", "next service", "no action", "looked at")
_INSTRUCTION_WORDS = ("pull over", "stop", "get out", "exit", "call 911", "shut off", "hazard")


def test_every_p0_code_has_a_component():
    from agents.health.cms_dtc_source import component_for_code
    p0, _ = _codes_by_p0()
    for code in sorted(p0):
        assert component_for_code(code) in {"battery", "brakes", "tires", "diagnostics"}, code


def test_c1234_alone_is_a_p0_brakes_finding_with_the_directive_verbatim():
    from agents.health.cms_dtc_source import build_fields, component_for_code
    assert component_for_code("C1234") == "brakes"
    fields = build_fields("brakes", [_row("C1234")])
    expected = p0_directive(faults_for_codes(["C1234"]), None)
    assert fields["severity_hint"] == "p0"
    assert fields["finding"] == expected
    assert is_p0_directive(fields["finding"])
    assert expected == p0_directive([{"template_id": "brake_emergency"}], None)
    assert fields["headline"] == "Brake system: critical fault code C1234 is active"
    assert [e["value"]["code"] for e in fields["evidence"]] == ["C1234"]
    assert fields["evidence"][0]["value"]["classifier_level"] == "P0"


@pytest.mark.parametrize("p0_code", sorted(_codes_by_p0()[0]))
def test_every_p0_code_yields_exactly_its_directive(p0_code):
    from agents.health.cms_dtc_source import build_fields, component_for_code
    fields = build_fields(component_for_code(p0_code), [_row(p0_code)])
    assert fields["severity_hint"] == "p0"
    assert fields["finding"] == p0_directive(faults_for_codes([p0_code]), None)


def test_p0_group_beside_lower_codes_carries_only_the_directive():
    """A brake P0 next to a brake P1: one p0 Finding, the directive alone, both codes cited."""
    from agents.health.cms_dtc_source import build_fields
    _, other = _codes_by_p0()
    lower_brake = sorted(c for c in other if _row(c)["system"] in ("brakes", "abs"))
    assert lower_brake, "need a non-P0 brake code for this test"
    fields = build_fields("brakes", [_row("C1234"), _row(lower_brake[0])])
    assert fields["severity_hint"] == "p0"
    assert fields["finding"] == p0_directive(faults_for_codes(["C1234"]), None)
    for w in _ADVICE_WORDS:
        assert w not in fields["finding"].lower().replace(p0_directive(faults_for_codes(["C1234"]), None).lower(), "")
    assert lower_brake[0] not in fields["finding"]
    assert {e["value"]["code"] for e in fields["evidence"]} == {"C1234", lower_brake[0]}
    assert "C1234" in fields["headline"]


def test_p0_text_never_names_the_driver_and_headline_never_instructs():
    from agents.health.cms_dtc_source import build_fields
    p0, other = _codes_by_p0()
    for code in sorted(p0):
        f = build_fields("diagnostics", [_row(code), _row(sorted(other)[0])])
        assert f["finding"] == p0_directive(faults_for_codes([code]), None)
        assert len(f["headline"]) <= 120
        low = f["headline"].lower()
        for w in _INSTRUCTION_WORDS:
            assert w not in low, (code, f["headline"])


def test_no_lower_code_group_is_ever_p0_and_keeps_its_advice():
    from agents.health.cms_dtc_source import _ADVICE_BY_SEVERITY, build_fields, component_for_code
    _, other = _codes_by_p0()
    for code in sorted(other):
        f = build_fields(component_for_code(code), [_row(code)])
        assert f["severity_hint"] in {"attention", "soon", "routine"}, code
        assert f["finding"].endswith(_ADVICE_BY_SEVERITY[f["severity_hint"]]), code
        assert not is_p0_directive(f["finding"]), code


def test_the_grade_comes_from_the_classifier_table(monkeypatch):
    """Re-grade C1234 as P1 in the classifier's own table: it is no longer p0."""
    import agents.health.cms_dtc_source as src
    real = src._level_and_system
    monkeypatch.setattr(src, "_level_and_system",
                        lambda code: ("P1", real(code)[1]) if code == "C1234" else real(code))
    f = src.build_fields("brakes", [{**_row("C1234"), "level": src._level_and_system("C1234")[0]}])
    assert f["severity_hint"] == "attention"


def test_fetch_groups_keeps_the_p0_code_and_build_fields_makes_it_p0():
    """A real fetch_groups over one vehicle with C1234 next to two non-P0 codes."""
    from agents.health.cms_dtc_source import CmsDtcSource, build_fields
    _, ActiveDtc, _ = _classifier()
    _, other = _codes_by_p0()
    non_p0 = sorted(other)[:2]
    codes = ["C1234", *non_p0]

    class _Ddb:
        def scan(self, **kw):
            return {"Items": [
                {"vehicleId": {"S": "VEH-1"}, "vin": {"S": "VIN1"}, "code": {"S": c},
                 "description": {"S": "d"}, "timestamp": {"N": "1"}}
                for c in codes]}

        def get_item(self, **kw):
            return {"Item": {"vin": {"S": "VIN1"}}}

        def query(self, **kw):
            return {"Items": [{"vehicleId": {"S": "VEH-1"}}]}

    class _Cms:
        _vin_to_vehicle_id = None

        def get_active_dtcs(self, vin):
            return tuple(ActiveDtc(code=c, first_seen_at="0") for c in codes)

    groups = CmsDtcSource(dtc_table="t", vehicles_table="v", ddb_client=_Ddb(),
                          cms_client=_Cms()).fetch_groups()

    by_code = {d["code"]: g for g in groups for d in g.dtcs}
    assert set(codes) <= set(by_code), "every active code, P0 included, is grouped"
    brake_group = by_code["C1234"]
    assert brake_group.component == "brakes"
    f = build_fields(brake_group.component, brake_group.dtcs)
    assert f["severity_hint"] == "p0"
    assert f["finding"] == p0_directive(faults_for_codes(["C1234"]), None)
    for g in groups:
        if g is not brake_group and not any(d["level"] == "P0" for d in g.dtcs):
            assert build_fields(g.component, g.dtcs)["severity_hint"] != "p0"
