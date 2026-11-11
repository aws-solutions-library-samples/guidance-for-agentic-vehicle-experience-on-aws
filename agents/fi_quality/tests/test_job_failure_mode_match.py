"""Job-level: which failure mode the rules path names for a pattern group (G3F4.7).

Review cycle 4 b W2 and W5. The bundle builder and the fallback run for real; only
``readers.run_query`` is faked, by SQL content, and the failure-mode query honours its
component filter the way Athena does (from ``g3-cycle4-probes/probe_b1.py``).

Behaviour pinned: a plant or build-window pattern is investigated with the symptom-matched
failure mode, never ``unknown``, including when its group also holds a lower-ranked
supplier-lot candidate for another component. The top-ranked candidate decides: a top-ranked
lot keeps its component filter.

Mutants: ``caller-skip`` (``read_failure_modes(...) if component_ids_for_fm else []``) and
reverting the top-rank rule each FAIL here.
"""
import re
from unittest.mock import patch

import pytest

import agents.fi_quality.readers as readers
from agents.fi_quality.fallback import investigation_fallback
from agents.fi_quality.job import _build_investigation_bundle

VIN1, VIN2 = "MRDVEH00000000020", "MRDVEH00000000021"


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")


def _prow(pid, dim, val, rank, code="P0AA6"):
    return {"pattern_id": pid, "code_family": code, "cohort_dimension": dim, "cohort_value": val,
            "candidate_rank": str(rank), "pattern_group_id": "grp-1", "vehicles_affected": "42",
            "vehicles_in_service": "500", "rate": "0.08", "rate_rest": "0.01", "rate_ratio": "6.0",
            "p_value": "0.0001", "first_detected_month": "2026-07-01", "trend": "rising",
            "method": "poisson_bonferroni", "affected_vins_sample_csv": f"{VIN1},{VIN2}",
            "computed_at": "2026-10-05 01:00:00.000", "parameters_version": "1.0.0"}


def _brow(vin, comp, lot):
    return {"vin": vin, "model": "Azimuth", "model_year": "2024", "build_month": "2024-03",
            "plant": "CGA", "component_id": comp, "supplier_id": "SUP-A", "supplier_lot": lot,
            "part_number": "X-1"}


def _fmrow(fm, comp, sym, opt, cause):
    return {"failure_mode_id": fm, "component_id": comp, "mechanism": "m", "symptom_codes_csv": sym,
            "cause_dimension": cause, "do_not_drive": "false", "park_outside": "false",
            "remedy_option_id": opt, "remedy_type": "hardware_recall",
            "parts_cost_per_vehicle": "100", "labor_hours_per_vehicle": "1", "lead_time_weeks": "2",
            "over_the_air": "false", "trade_off_notes": ""}


_CATALOG = [
    _fmrow("fm-hv-plant", "hv_battery_module", "P0AA6,P0AA7", "opt-hv-1", "assembly_plant"),
    _fmrow("fm-brake", "brake_caliper", "C0035", "opt-brk-1", "supplier_lot"),
]
_BUILD = [_brow(VIN1, "hv_battery_module", "L-HV-2024-03-CGA"),
          _brow(VIN1, "brake_caliper", "L-BRK-2024-03-CGA"),
          _brow(VIN2, "hv_battery_module", "L-HV-2024-03-CGA"),
          _brow(VIN2, "brake_caliper", "L-BRK-2024-03-CGA")]


def _run(pattern_rows):
    fm_queries = []

    def fake(sql, **kw):
        if "engineering_failure_modes" in sql:
            fm_queries.append(sql)
            m = re.search(r"WHERE fm\.component_id IN \(([^)]*)\)", sql)
            if m:
                comps = {c.strip().strip("'") for c in m.group(1).split(",")}
                return [r for r in _CATALOG if r["component_id"] in comps]
            return list(_CATALOG)
        if "fault_patterns" in sql:
            return pattern_rows
        if "vehicle_build_components" in sql:
            return _BUILD
        return []

    with patch.object(readers, "run_query", side_effect=fake):
        bundle = _build_investigation_bundle(
            pattern_id="p", stage="staging", athena_client=object(),
            workgroup="wg", results_s3_path="s3://b/p/")
    assert fm_queries, "the job must query the failure-mode catalog"
    return bundle, investigation_fallback(bundle)


@pytest.mark.parametrize("rows", [
    pytest.param([_prow("p-plant", "assembly_plant", "CGA", 1)], id="plant-only"),
    pytest.param([_prow("p-bw", "build_month", "2024-03", 1)], id="build-window-only"),
    pytest.param([_prow("p-plant", "assembly_plant", "CGA", 1),
                  _prow("p-bw", "build_month", "2024-03", 2)], id="plant-and-build-window"),
    pytest.param([_prow("p-plant", "assembly_plant", "CGA", 1),
                  _prow("p-lot", "supplier_lot", "L-BRK-2024-03-CGA", 2)],
                 id="plant-above-another-components-lot"),
])
def test_plant_and_build_window_patterns_name_the_symptom_matched_mode(rows):
    bundle, result = _run(rows)
    assert result["failure_mode_id"] == "fm-hv-plant", result["failure_mode_id"]
    assert result["recommended_option_id"] == "opt-hv-1", result["recommended_option_id"]
    assert bundle["candidate_components"] == {}, bundle["candidate_components"]


def test_a_top_ranked_lot_keeps_its_component_filter():
    bundle, _ = _run([_prow("p-lot", "supplier_lot", "L-BRK-2024-03-CGA", 1),
                      _prow("p-plant", "assembly_plant", "CGA", 2)])
    assert bundle["candidate_components"] == {"brake_caliper": 1}
