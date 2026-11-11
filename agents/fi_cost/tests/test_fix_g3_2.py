"""Fix Group G3-2 (Group 3 review cycle 2, agent area): tests that pin each fix.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/ (decisions.md 2026-10-05 "Group 3 review
cycle 2").  Each test names the mutant or finding it covers.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.fi_cost.tests.test_guardrail import _EVIDENCE, _guardrail_none, _valid_converse_reply
from agents.fi_cost.tests.test_sql_duckdb import _EVIDENCE_SQL, _make_evidence_conn, _run_evidence

_GOOD_REPLY = _valid_converse_reply()["output"]["message"]["content"][0]["toolUse"]["input"]


def _written(write_mock: MagicMock) -> dict:
    assert write_mock.call_count == 1
    args, kwargs = write_mock.call_args
    return kwargs.get("finding", args[0] if args else None)


# ---- Critical: a reply with the wrong JSON types is a failed attempt ---------------------

@pytest.mark.parametrize("patch", [
    {"cause": ["charging_rate"]},
    {"action": 3},
    {"evidence_refs": None},
    {"evidence_refs": [1, 2]},
    {"headline": 5},
    {"confidence": None},
])
def test_wrong_types_fail_validation_without_raising(patch):
    from agents.fi_cost.validate import validate_tool_input

    ok, msg = validate_tool_input({**_GOOD_REPLY, **patch}, _EVIDENCE)
    assert ok is False and msg


def test_non_dict_reply_fails_validation():
    from agents.fi_cost.validate import validate_tool_input

    assert validate_tool_input(["not", "a", "dict"], _EVIDENCE)[0] is False


def test_malformed_replies_fall_back_to_rules_after_one_retry():
    """Two replies with a list `cause`: two converse calls, then a rules Finding is written."""
    from agents.fi_cost.agent import process_vehicle

    bad = _valid_converse_reply()
    bad["output"]["message"]["content"][0]["toolUse"]["input"]["cause"] = ["charging_rate"]
    stub = MagicMock()
    stub.converse.return_value = bad
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    process_vehicle(_EVIDENCE, None, stub, write)
    assert stub.converse.call_count == 2
    sources = [e["value"] for e in _written(write)["evidence"] if e["kind"] == "cost.diagnosis_source"]
    assert sources == ["rules"]


def test_unreadable_response_is_a_failed_attempt():
    from agents.fi_cost.agent import process_vehicle

    stub = MagicMock()
    stub.converse.return_value = {"output": {"message": {"content": "not a list"}}}
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    process_vehicle(_EVIDENCE, None, stub, write)
    assert stub.converse.call_count == 2
    assert write.call_count == 1


# ---- W1: the depot off-peak price falls back to the cohort's -----------------------------

def _conn_with_off_peak():
    conn = _make_evidence_conn()
    # MRDDBTEST002 and 003 charged at the depot off-peak (0.10 and 0.14 USD/kWh);
    # MRDDBTEST001 charged only at depot peak.
    conn.execute("""
        INSERT INTO adp_test_fleet_cost.fleet_cost_events VALUES
          ('MRDDBTEST002', 'energy_depot', 'off_peak', 10.0, 1.0, '2026-09'),
          ('MRDDBTEST003', 'energy_depot', 'off_peak', 10.0, 1.4, '2026-09')
    """)
    return conn


def test_vehicle_without_off_peak_gets_the_cohort_median_price():
    """N1: the SQL must COALESCE the vehicle's own price with the fleet-model cohort median."""
    rows = {r["vin"]: r for r in _run_evidence(_conn_with_off_peak(), _EVIDENCE_SQL)}
    assert rows["MRDDBTEST001"]["energy.depot_off_peak_price_per_kwh"] == pytest.approx(0.12)
    assert rows["MRDDBTEST002"]["energy.depot_off_peak_price_per_kwh"] == pytest.approx(0.10)
    assert rows["MRDDBTEST003"]["energy.depot_off_peak_price_per_kwh"] == pytest.approx(0.14)


def test_shift_without_any_off_peak_price_is_rejected():
    """N2: with no price at all, shift_to_depot_off_peak fails validation."""
    from agents.fi_cost.validate import validate_tool_input

    ev = {**_EVIDENCE, "energy.depot_off_peak_price_per_kwh": None}
    assert validate_tool_input(_GOOD_REPLY, ev)[0] is False
    assert validate_tool_input(_GOOD_REPLY, _EVIDENCE)[0] is True


def test_no_price_at_all_gives_an_informational_finding():
    """The two planted rows of cycle 1 had peak share 1.0; with no cohort price either, the
    rules fallback writes a Finding without a recommended_action, never a TypeError."""
    from agents.fi_cost.diagnose import run_cost_diagnosis
    from agents.fi_cost.fallback import rules_fallback

    ev = {**_EVIDENCE, "energy.peak_share": 1.0, "energy.depot_off_peak_price_per_kwh": None}
    reply = rules_fallback(ev)
    finding = run_cost_diagnosis(ev, reply, diagnosis_source="rules")
    assert "recommended_action" not in finding


# ---- W2: schedule_inspection needs maintenance above the cohort --------------------------

@pytest.mark.parametrize("delta", [0.0, -25.0])
def test_schedule_inspection_needs_positive_maintenance_delta(delta):
    from agents.fi_cost.validate import validate_tool_input

    ev = {**_EVIDENCE, "energy.vehicle_kwh_per_100mi": 35.0, "energy.cohort_kwh_per_100mi": 25.0,
          "delta.maintenance_usd": delta}
    reply = {**_GOOD_REPLY, "cause": "efficiency", "action": "schedule_inspection",
             "evidence_refs": ["energy.vehicle_kwh_per_100mi"]}
    assert validate_tool_input(reply, ev)[0] is False
    assert validate_tool_input(reply, {**ev, "delta.maintenance_usd": 48.0})[0] is True


# ---- W3: which evidence keys the Finding carries ------------------------------------------

def test_finding_evidence_keys():
    """Deltas always; the energy-rule keys on an energy outlier; every key the model cited."""
    from agents.fi_cost.diagnose import run_cost_diagnosis

    ev = {**_EVIDENCE, "cost.flag": "energy", "cost.energy_ratio_own": 1.2,
          "cost.energy_ratio_cohort": 1.3, "cost.energy_excess_usd": 40.0,
          "cost.energy_method": "own 6-month mean ..."}
    reply = {**_GOOD_REPLY, "evidence_refs": ["energy.public_and_peak_kwh", "energy.peak_share"]}
    kinds = {e["kind"] for e in run_cost_diagnosis(ev, reply, diagnosis_source="model")["evidence"]}
    for key in ("delta.energy_usd", "delta.maintenance_usd", "delta.lease_usd", "delta.insurance_usd",
                "cost.energy_ratio_own", "cost.energy_ratio_cohort", "cost.energy_excess_usd",
                "cost.energy_method", "energy.public_and_peak_kwh", "energy.peak_share",
                "cost.diagnosis_source"):
        assert key in kinds, f"Finding evidence is missing {key}"

    total = {**ev, "cost.flag": "total"}
    kinds_total = {e["kind"] for e in run_cost_diagnosis(total, reply, diagnosis_source="model")["evidence"]}
    assert "cost.energy_excess_usd" not in kinds_total



def test_validator_raising_is_still_a_failed_attempt(monkeypatch):
    """B2: if validation itself raises (any defect we haven't foreseen), the attempt fails and
    the run falls back to rules instead of aborting (D6 step 5)."""
    import agents.fi_cost.agent as agent_mod

    def _boom(reply, evidence):
        raise RuntimeError("unexpected validator defect")

    monkeypatch.setattr(agent_mod, "validate_tool_input", _boom)
    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply()
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    agent_mod.process_vehicle(_EVIDENCE, None, stub, write)
    assert stub.converse.call_count == 2
    sources = [e["value"] for e in _written(write)["evidence"] if e["kind"] == "cost.diagnosis_source"]
    assert sources == ["rules"]




# ---- Group 3 review cycle 3 (agent W1, W2) ------------------------------------------------

_ANOMALY_BASE_KEYS = {
    "cost.month", "cost.cost_per_mile", "cost.own_baseline", "cost.cohort_baseline",
    "cost.method", "cost.flag", "delta.energy_usd", "delta.maintenance_usd",
    "delta.lease_usd", "delta.insurance_usd", "cost.diagnosis_source",
}
_ENERGY_KEYS = {"cost.energy_ratio_own", "cost.energy_ratio_cohort",
                "cost.energy_excess_usd", "cost.energy_method"}


def _full_evidence(flag: str) -> dict:
    return {**_EVIDENCE, "cost.month": "2026-09", "cost.cost_per_mile": 1.4, "cost.own_baseline": 1.0,
            "cost.cohort_baseline": 1.1, "cost.method": "own 6-month mean ...", "cost.flag": flag,
            "cost.energy_ratio_own": 1.2, "cost.energy_ratio_cohort": 1.3,
            "cost.energy_excess_usd": 40.0, "cost.energy_method": "own 6-month mean ... energy"}


@pytest.mark.parametrize("flag", ["total", "energy", "both"])
def test_anomaly_evidence_key_set_is_pinned(flag):
    """X2/X3: every anomaly Finding carries the base keys (cost.month is the screen's only data
    month); energy and both Findings also carry the four energy-rule keys; total ones don't."""
    from agents.fi_cost.diagnose import run_cost_diagnosis

    reply = {**_GOOD_REPLY, "evidence_refs": []}
    kinds = {e["kind"] for e in run_cost_diagnosis(_full_evidence(flag), reply,
                                                   diagnosis_source="model")["evidence"]}
    assert _ANOMALY_BASE_KEYS <= kinds, _ANOMALY_BASE_KEYS - kinds
    if flag == "total":
        assert not (_ENERGY_KEYS & kinds)
    else:
        assert _ENERGY_KEYS <= kinds, _ENERGY_KEYS - kinds


def test_sell_timing_evidence_key_set_is_pinned():
    """X4: exactly the four keys docs/fi-cost-contract.md § 1.2 names."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding

    f = build_sell_timing_finding(vin="MRDAZ01S0PC00GXY9", fleet_id="demo-lastmile-delivery",
                                  crossover_month="2026-11", purchase_price=52_000.0,
                                  monthly_depreciation=610.0, year_month="2026-09")
    assert {e["kind"] for e in f["evidence"]} == {
        "lifecycle.crossover_month", "lifecycle.purchase_price",
        "lifecycle.monthly_depreciation", "cost.month",
    }
    assert "estimated_value_usd" not in f["recommended_action"]


def test_model_reply_that_cannot_be_built_falls_back_to_rules(monkeypatch):
    """X1: if building the Finding from a validated model reply raises, the rules Finding is
    written instead (agent.py's build-failure branch)."""
    import agents.fi_cost.agent as agent_mod
    from agents.fi_cost.diagnose import run_cost_diagnosis as real_build

    def _build(evidence, reply, *, diagnosis_source=None):
        if diagnosis_source == "model":
            raise RuntimeError("cannot build from this reply")
        return real_build(evidence, reply, diagnosis_source=diagnosis_source)

    monkeypatch.setattr(agent_mod, "run_cost_diagnosis", _build)
    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply()
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    agent_mod.process_vehicle(_EVIDENCE, None, stub, write)
    sources = [e["value"] for e in _written(write)["evidence"] if e["kind"] == "cost.diagnosis_source"]
    assert sources == ["rules"]


@pytest.mark.parametrize("patch", [{"headline": ""}, {"detail": "   "}, {"headline": None}])
def test_narration_is_required(patch):
    """Group 3 review cycle 3 suggestion: a reply without headline or detail is a failed
    attempt, so no model Finding is written with empty narration."""
    from agents.fi_cost.validate import validate_tool_input

    reply = {k: v for k, v in {**_GOOD_REPLY, **patch}.items() if v is not None}
    assert validate_tool_input(reply, _EVIDENCE)[0] is False
