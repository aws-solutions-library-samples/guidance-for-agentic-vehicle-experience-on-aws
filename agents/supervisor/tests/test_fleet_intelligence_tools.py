"""fleet_lifecycle and fleet_cost_outliers: the fleet_manager persona's Fleet Intelligence reads.

issues/2026-09-29-fleet-manager-tools-call-removed-cms-routes/. The previous four
fleet tools called CMS routes removed on 2026-09-06. These pin the replacement's
contract: the caller's token is forwarded, the fleet comes only from the sanitized
screen context, provenance and computedAt travel with the data, and no failure
reads as "nothing flagged".

Mutations tried (2026-09-29), each caught by the named test:
  (a) screen fleetId ignored: test_fleet_id_comes_from_screen_context FAILED.
  (b) 403 returned as an empty success: test_errors_are_unavailable_not_empty FAILED.
  (c) insufficient-data rows kept in soonestCrossover: test_soonest_crossover_is_reliable_and_sorted FAILED.
  (d) provenance dropped from the lifecycle result: test_lifecycle_carries_provenance_and_computed_at FAILED.
  (e) outliers not sorted highest first: test_outliers_sorted_highest_first FAILED.

Live check: scripts/live-verify-webchat-claimless-context.py case C.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from agents.supervisor.tools import fleet_intelligence as fi

BASE = "https://cms.example.invalid/prod"


def _ctx(screen=None):
    return SimpleNamespace(jwt="tok-123", screen_context=screen, session_id="s-1")


def _cfg(base=BASE):
    return SimpleNamespace(cms_api_base=base)


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    return r


LIFECYCLE = {
    "summary": {"totalVehicles": 4, "sellRecommendedCount": 1, "sellSoonCount": 1, "healthyCount": 1,
                "insufficientDataCount": 1, "tiresNeedReplacementCount": 1, "avgMonthsToCrossover": 6.333},
    "rows": [
        {"vehicleId": "V-A", "vin": "A", "rSquaredMeaningful": True, "monthsUntilCrossover": 9,
         "currentMonthlyMaintenance": 101.239, "monthlyDepreciation": 400.0, "tireStatus": "ok"},
        {"vehicleId": "V-B", "vin": "B", "rSquaredMeaningful": True, "monthsUntilCrossover": 2,
         "tireStatus": "needs_replacement", "tirePositions": ["LF"]},
        {"vehicleId": "V-C", "vin": "C", "rSquaredMeaningful": False, "monthsUntilCrossover": 1},
        {"vehicleId": "V-D", "vin": "D", "rSquaredMeaningful": True, "monthsUntilCrossover": None},
    ],
    "provenance": "simulated",
    "computedAt": "2026-09-29T15:10:18Z",
}

OUTLIERS = {
    "threshold": 1.8265, "fleetMeanCpm": 1.2177, "outlierMultiple": 1.5,
    "data": [
        {"group": "V-2", "cpm": 2.1, "totalCost": 900.0, "totalMiles": 428.6},
        {"group": "V-1", "cpm": 3.69, "totalCost": 6025.94, "totalMiles": 1632.06},
    ],
    "provenance": "simulated", "computedAt": "2026-09-29T15:10:18Z",
}


def _call(factory, body, screen=None, status=200, cfg=None):
    with patch.object(fi.requests, "get", return_value=_resp(status, body)) as get:
        out = factory(_ctx(screen), cfg or _cfg())()
    return out, get


# --- Request shape ---------------------------------------------------------------

@pytest.mark.parametrize("factory,path", [
    (fi.make_fleet_lifecycle_tool, "/api/v1/fleet-intelligence/lifecycle"),
    (fi.make_fleet_cost_outliers_tool, "/api/v1/fleet-intelligence/cpm/outliers"),
])
def test_forwards_token_to_the_fleet_intelligence_route(factory, path):
    _, get = _call(factory, LIFECYCLE if "lifecycle" in path else OUTLIERS)
    args, kwargs = get.call_args
    assert args[0] == BASE + path
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["params"] == {}
    assert kwargs["timeout"] > 0


def test_fleet_id_comes_from_screen_context():
    _, get = _call(fi.make_fleet_lifecycle_tool, LIFECYCLE,
                   screen={"page": "fleet-detail", "entities": {"fleetId": "FLEET-001"}})
    assert get.call_args.kwargs["params"] == {"fleetId": "FLEET-001"}


def test_malformed_screen_fleet_id_is_ignored():
    _, get = _call(fi.make_fleet_lifecycle_tool, LIFECYCLE,
                   screen={"entities": {"fleetId": "a b&c=d"}})
    assert get.call_args.kwargs["params"] == {}


@pytest.mark.parametrize("factory", [fi.make_fleet_lifecycle_tool, fi.make_fleet_cost_outliers_tool])
def test_not_configured_makes_no_request(factory):
    with patch.object(fi.requests, "get") as get:
        out = factory(_ctx(), _cfg(base=""))()
    assert out == {"available": False, "reason": fi._NOT_CONFIGURED}
    get.assert_not_called()


@pytest.mark.parametrize("factory", [fi.make_fleet_lifecycle_tool, fi.make_fleet_cost_outliers_tool])
def test_tools_take_no_arguments(factory):
    t = factory(_ctx(), _cfg())
    assert set(t.tool_spec["inputSchema"]["json"].get("properties", {})) == set()


# --- Failures ---------------------------------------------------------------------

@pytest.mark.parametrize("status,reason", [
    (401, fi._EXPIRED), (403, fi._FORBIDDEN), (404, fi._UNREACHABLE), (500, fi._UNREACHABLE),
])
@pytest.mark.parametrize("factory", [fi.make_fleet_lifecycle_tool, fi.make_fleet_cost_outliers_tool])
def test_errors_are_unavailable_not_empty(factory, status, reason):
    out, _ = _call(factory, {"data": [], "rows": []}, status=status)
    assert out == {"available": False, "reason": reason}


def test_forbidden_on_a_fleet_page_names_that_fleet():
    out, _ = _call(fi.make_fleet_lifecycle_tool, {}, status=403,
                   screen={"entities": {"fleetId": "FLEET-009"}})
    assert out == {"available": False, "reason": fi._FORBIDDEN_FLEET}


def test_network_error_is_unavailable():
    with patch.object(fi.requests, "get", side_effect=requests.Timeout("slow")):
        out = fi.make_fleet_cost_outliers_tool(_ctx(), _cfg())()
    assert out == {"available": False, "reason": fi._UNREACHABLE}


def test_non_json_is_unavailable():
    r = _resp(200)
    r.json.side_effect = ValueError("not json")
    with patch.object(fi.requests, "get", return_value=r):
        out = fi.make_fleet_lifecycle_tool(_ctx(), _cfg())()
    assert out["available"] is False


# --- fleet_lifecycle ---------------------------------------------------------------

def test_soonest_crossover_is_reliable_and_sorted():
    out, _ = _call(fi.make_fleet_lifecycle_tool, LIFECYCLE)
    assert [v["vehicleId"] for v in out["soonestCrossover"]] == ["V-B", "V-A"]
    assert out["soonestCrossover"][1]["currentMonthlyMaintenance"] == 101.24


def test_lifecycle_summary_and_tires():
    out, _ = _call(fi.make_fleet_lifecycle_tool, LIFECYCLE)
    assert out["summary"]["sellRecommendedCount"] == 1
    assert out["summary"]["avgMonthsToCrossover"] == 6.33
    assert [v["vehicleId"] for v in out["tiresNeedReplacement"]] == ["V-B"]
    assert out["tiresNeedReplacement"][0]["tirePositions"] == ["LF"]


def test_lifecycle_carries_provenance_and_computed_at():
    out, _ = _call(fi.make_fleet_lifecycle_tool, LIFECYCLE)
    assert out["provenance"] == "simulated" and out["computedAt"] == "2026-09-29T15:10:18Z"


# --- fleet_cost_outliers -------------------------------------------------------------

def test_outliers_sorted_highest_first():
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS)
    assert [o["vehicleId"] for o in out["outliers"]] == ["V-1", "V-2"]
    # Row now carries flag/severity/ratioToComparable; old OUTLIERS fixture has none
    # of those fields so they arrive as None — check the numeric fields individually.
    row = out["outliers"][0]
    assert row["vehicleId"] == "V-1"
    assert row["costPerMile"] == 3.69
    assert row["totalCost"] == 6025.94
    assert row["totalMiles"] == 1632.0
    assert out["fleetMeanCostPerMile"] == 1.22 and out["provenance"] == "simulated"


def test_no_lifecycle_rows_is_no_data_not_a_healthy_fleet():
    body = {**LIFECYCLE, "rows": [], "summary": {**LIFECYCLE["summary"], "totalVehicles": 0}}
    out, _ = _call(fi.make_fleet_lifecycle_tool, body)
    assert out == {"available": False, "reason": fi._NO_LIFECYCLE_DATA}


def test_no_cost_rows_is_no_data_not_no_outliers():
    out, _ = _call(fi.make_fleet_cost_outliers_tool, {**OUTLIERS, "fleetMeanCpm": 0.0, "data": []})
    assert out == {"available": False, "reason": fi._NO_COST_DATA}


def test_no_outliers_is_an_empty_success():
    out, _ = _call(fi.make_fleet_cost_outliers_tool, {**OUTLIERS, "data": []})
    assert out["available"] is True and out["outliers"] == []


# --- Registration -------------------------------------------------------------------

def test_registered_for_fleet_manager_only_as_the_fleet_data_tools():
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES

    fm = PERSONA_TOOL_FACTORIES["fleet_manager"]
    assert fi.make_fleet_lifecycle_tool in fm and fi.make_fleet_cost_outliers_tool in fm



# --- Deploy wiring ------------------------------------------------------------------
# VSA_CMS_API_BASE was never set on any runtime, so the fleet tools always said
# "not configured". Mutation: the text runtime's --env line removed -> FAILED.

@pytest.mark.parametrize("runtime", ["vsa_supervisor_bidi_staging", "vsa_supervisor_text_staging"])
def test_makefile_passes_cms_api_base_to_each_staging_runtime(runtime):
    from pathlib import Path

    make = (Path(__file__).resolve().parents[3] / "Makefile").read_text()
    start = make.index(f"agentcore deploy -a {runtime} ")
    end = make.find("agentcore deploy -a ", start + 10)
    deploy = make[start:end if end != -1 else len(make)]
    assert "--env VSA_CMS_API_BASE=$$CMS_API_BASE" in deploy
    # And it is looked up, not left to an unset variable.
    assert "--stack-name cms-staging-ui" in make[:start] and "OutputKey=='APIEndpoint'" in make[:start]
    # The lookup fails the deploy when it resolves nothing (security review S2).
    guard = make.rfind('if [ -z "$$CMS_API_BASE" ]', 0, start)
    assert guard != -1
    block = make[guard:make.index("fi;", guard) + 3]  # this guard only, not a neighbour's
    assert block.rstrip().endswith("exit 1; fi;"), block


# --- Host guard on the token destination (security review W1) ------------------------
# Mutation: safe_cms_api_base returns its input -> test_rejects_wrong_hosts FAILED.

@pytest.mark.parametrize("value", [
    "https://yourapi000.execute-api.us-east-1.amazonaws.com/prod",   # other region
    "http://yourapi000.execute-api.us-west-2.amazonaws.com/prod",    # not https
    "https://evil.example.com/prod",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com.evil.com/prod",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod?x=https://evil",
])
def test_rejects_wrong_hosts(value):
    from agents.supervisor.config import safe_cms_api_base

    assert safe_cms_api_base(value, "us-west-2") == ""


def test_accepts_the_cms_api_gateway_url():
    from agents.supervisor.config import safe_cms_api_base

    # safe_cms_api_base validates the host must be [a-z0-9]{10}.execute-api.*
    # Use a 10-char all-lowercase-alphanumeric placeholder ("yourapi000") that
    # satisfies the regex without being a real API Gateway ID.
    assert safe_cms_api_base("https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/", "us-west-2") == \
        "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod"


def test_config_default_applies_the_guard(monkeypatch):
    from agents.supervisor import config as cfg_mod

    monkeypatch.setenv("VSA_CMS_API_BASE", "https://evil.example.com/prod")
    monkeypatch.setenv("VSA_CMS_REGION", "us-west-2")
    assert cfg_mod.Config.__dataclass_fields__["cms_api_base"].default_factory() == ""


def test_from_env_applies_the_guard():
    import inspect

    from agents.supervisor.config import Config

    src = inspect.getsource(Config.from_env)
    assert "safe_cms_api_base(" in src and 'os.environ.get("VSA_CMS_API_BASE", "")\n' not in src.replace(" ", "")


# ─────────────────────────────────────────────────────────────────────────────
# T1.4 — fleet_cost_outliers description drift fix
# RED tests added first; all 7 fail until fleet_intelligence.py is updated.
#
# Mutations each test must catch:
#   (a) test_outliers_doc_does_not_mention_multiple_of_mean  → "restore multiple of the mean"
#   (b) test_outliers_doc_describes_adp_flag_rule            → weakened docstring
#   (c) test_outliers_drops_threshold_and_outlier_multiple   → "return null fields again"
#   (d) test_outlier_row_has_flag_severity_ratio_to_comparable → "drop flag"
#   (e) test_ratio_to_comparable_uses_energy_ratio_for_energy_flag → "pick ratioCohort for energy row"
#   (f) test_ratio_to_comparable_uses_cohort_ratio_for_non_energy_flag → "pick ratioCohortEnergy for total"
#   (g) test_ratio_to_comparable_none_when_ratio_is_none     → "return 0 instead of None"
# ─────────────────────────────────────────────────────────────────────────────

# CMS-shaped fixture — rows carry flag/severity/ratioCohort/ratioCohortEnergy
# matching the shape emitted by CMS services/fleet_intelligence/index.py:1120-1161.
OUTLIERS_V2 = {
    "fleetMeanCpm": 1.2177,
    "data": [
        # total-cost outlier: ratioToComparable = _round(ratioCohort) = 2.34
        {
            "group": "V-1", "cpm": 3.69, "totalCost": 6025.94, "totalMiles": 1632.06,
            "ratioCohort": 2.34167, "ratioCohortEnergy": None,
            "flag": "total", "severity": "attention",
        },
        # energy-cost outlier: ratioToComparable = _round(ratioCohortEnergy) = 2.67
        {
            "group": "V-2", "cpm": 2.1, "totalCost": 900.0, "totalMiles": 428.6,
            "ratioCohort": 1.85, "ratioCohortEnergy": 2.674,
            "flag": "energy", "severity": "soon",
        },
        # total-cost outlier, ratioCohort=None: ratioToComparable stays None
        {
            "group": "V-3", "cpm": 2.05, "totalCost": 820.0, "totalMiles": 400.0,
            "ratioCohort": None, "ratioCohortEnergy": None,
            "flag": "total", "severity": "routine",
        },
    ],
    "provenance": "simulated",
    "computedAt": "2026-09-29T15:10:18Z",
}


def test_outliers_doc_does_not_mention_multiple_of_mean():
    """Docstring must not describe outliers as a multiple of the mean (T1.4 fix)."""
    import inspect as _inspect
    tool = fi.make_fleet_cost_outliers_tool(_ctx(), _cfg())
    doc = _inspect.getdoc(tool) or ""
    assert "multiple of the mean" not in doc.lower(), (
        "docstring still says 'multiple of the mean'; update to describe the ADP flag rule"
    )


def test_outliers_doc_describes_adp_flag_rule():
    """Docstring must describe: threshold vs own-history AND comparable-vehicle median,
    severity derived from the cohort comparison."""
    import inspect as _inspect
    tool = fi.make_fleet_cost_outliers_tool(_ctx(), _cfg())
    doc = (_inspect.getdoc(tool) or "").lower()
    assert "threshold" in doc, "docstring must mention 'threshold'"
    assert "median" in doc or "comparable" in doc, (
        "docstring must mention 'median' or 'comparable' vehicles"
    )
    assert "severity" in doc, "docstring must mention 'severity'"


def test_outliers_drops_threshold_and_outlier_multiple():
    """Return must not include always-null threshold or outlierMultiple fields."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS)
    assert out.get("available") is True
    assert "threshold" not in out, "T1.4: 'threshold' must be dropped from return"
    assert "outlierMultiple" not in out, "T1.4: 'outlierMultiple' must be dropped from return"


def test_outlier_row_has_flag_severity_ratio_to_comparable():
    """Each outlier row must carry flag, severity, and ratioToComparable."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V2)
    assert out.get("available") is True
    assert out["outliers"], "expected at least one outlier row"
    for row in out["outliers"]:
        assert "flag" in row, f"row missing 'flag': {row}"
        assert "severity" in row, f"row missing 'severity': {row}"
        assert "ratioToComparable" in row, f"row missing 'ratioToComparable': {row}"


def test_ratio_to_comparable_uses_energy_ratio_for_energy_flag():
    """flag=='energy' rows must use ratioCohortEnergy (rounded), not ratioCohort."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V2)
    energy_rows = [r for r in out["outliers"] if r.get("flag") == "energy"]
    assert energy_rows, "expected at least one energy-flag row in OUTLIERS_V2"
    # V-2: ratioCohortEnergy=2.674 → _round → 2.67; ratioCohort=1.85 is wrong pick
    assert energy_rows[0]["ratioToComparable"] == 2.67, (
        f"energy row: ratioToComparable={energy_rows[0]['ratioToComparable']!r}, "
        "expected 2.67 (ratioCohortEnergy rounded); ratioCohort=1.85 would be wrong"
    )


def test_ratio_to_comparable_uses_cohort_ratio_for_non_energy_flag():
    """flag != 'energy' rows must use ratioCohort (rounded), not ratioCohortEnergy."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V2)
    total_rows = [r for r in out["outliers"] if r.get("flag") == "total"]
    assert total_rows, "expected at least one total-flag row in OUTLIERS_V2"
    # V-1: ratioCohort=2.34167 → _round → 2.34
    v1 = next((r for r in total_rows if r["vehicleId"] == "V-1"), None)
    assert v1 is not None, "V-1 not found in outliers"
    assert v1["ratioToComparable"] == 2.34, (
        f"total row V-1: ratioToComparable={v1['ratioToComparable']!r}, "
        "expected 2.34 (ratioCohort=2.34167 rounded)"
    )


def test_ratio_to_comparable_none_when_ratio_is_none():
    """ratioToComparable is None when the relevant ratio field is None."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V2)
    # V-3: flag='total', ratioCohort=None → ratioToComparable must be None
    v3 = next((r for r in out["outliers"] if r["vehicleId"] == "V-3"), None)
    assert v3 is not None, "V-3 not found in outliers"
    assert "ratioToComparable" in v3, "V-3 row missing 'ratioToComparable'"
    assert v3["ratioToComparable"] is None, (
        f"V-3 ratioToComparable={v3['ratioToComparable']!r}, expected None (ratioCohort is None)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# F1.4 — W5: docstring missing "both" flag; W6: severity always from total cost
# Red tests (FAIL before fix):
#   (h) test_outliers_doc_lists_both_flag        → add '"both"' to docstring flag list
#   (i) test_outliers_doc_severity_from_total_cost → say "total cost" near severity
# Green tests (pass now; needed to catch mutants):
#   (j) test_each_row_severity_value             → "severity: None" mutant
#   (k) test_both_row_uses_total_ratio_for_ratio → "energy ratio for a both row" mutant
#   (l) test_both_row_severity_value             → "severity: None" + docstring mutant
# ─────────────────────────────────────────────────────────────────────────────

# "both" fixture: a vehicle where BOTH total-cost and energy-cost are outliers.
# severity comes from ratio_cohort (total cost), not the energy ratio.
# ratioToComparable for a "both" row must use ratioCohort (total), not ratioCohortEnergy.
OUTLIERS_V3 = {
    "fleetMeanCpm": 1.2177,
    "data": [
        {
            "group": "V-4",
            "cpm": 4.1,
            "totalCost": 7200.0,
            "totalMiles": 1756.1,
            # ratioCohort=3.1 → _round → 3.1  (expected ratioToComparable)
            # ratioCohortEnergy=2.5 → _round → 2.5 (wrong pick for a "both" row)
            "ratioCohort": 3.1,
            "ratioCohortEnergy": 2.5,
            "flag": "both",
            "severity": "attention",
        },
    ],
    "provenance": "live",
    "computedAt": "2026-10-05T12:00:00Z",
}


def test_outliers_doc_lists_both_flag():
    """W5: docstring must list '"both"' alongside '"total"' and '"energy"' (F1.4 fix)."""
    import inspect as _inspect
    tool = fi.make_fleet_cost_outliers_tool(_ctx(), _cfg())
    doc = _inspect.getdoc(tool) or ""
    assert '"both"' in doc, (
        'F1.4 W5: docstring missing \'"both"\' flag — '
        'add it to the flag description next to "total" and "energy"'
    )


def test_outliers_doc_severity_from_total_cost():
    """W6: severity description must say 'total cost' (not just 'flagged ratio'),
    whatever the flag (ADP SQL always uses ratio_cohort, not the energy ratio).
    Checks the severity *section* specifically (not the ratioToComparable section)."""
    import inspect as _inspect
    tool = fi.make_fleet_cost_outliers_tool(_ctx(), _cfg())
    doc = (_inspect.getdoc(tool) or "").lower()
    # The severity clause runs from "severity" to "ratioToComparable".
    sev_idx = doc.find("severity")
    ratio_idx = doc.find("ratioToComparable".lower(), sev_idx)
    sev_section = doc[sev_idx:ratio_idx] if ratio_idx > sev_idx > -1 else doc[sev_idx:]
    assert "total cost" in sev_section, (
        "F1.4 W6: docstring severity description must mention 'total cost' — "
        "severity is always from the total cost-per-mile cohort ratio, regardless of flag. "
        f"Severity section: {sev_section!r}"
    )


def test_each_row_severity_value():
    """Catch the 'severity: None' mutant — each OUTLIERS_V2 row must carry its fixture severity."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V2)
    rows = {r["vehicleId"]: r for r in out["outliers"]}
    assert rows["V-1"]["severity"] == "attention", (
        f"V-1 severity={rows['V-1']['severity']!r}, expected 'attention'"
    )
    assert rows["V-2"]["severity"] == "soon", (
        f"V-2 severity={rows['V-2']['severity']!r}, expected 'soon'"
    )
    assert rows["V-3"]["severity"] == "routine", (
        f"V-3 severity={rows['V-3']['severity']!r}, expected 'routine'"
    )


def test_both_row_uses_total_ratio_for_ratio_to_comparable():
    """Catch 'energy ratio for a both row' mutant — a both-flag row must use ratioCohort."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V3)
    assert out.get("available") is True
    row = out["outliers"][0]
    assert row["flag"] == "both"
    # ratioCohort=3.1 is correct; ratioCohortEnergy=2.5 is the wrong pick.
    assert row["ratioToComparable"] == 3.1, (
        f"both-flag row: ratioToComparable={row['ratioToComparable']!r}, "
        "expected 3.1 (ratioCohort); ratioCohortEnergy=2.5 would be wrong"
    )


def test_both_row_severity_value():
    """Catch the 'severity: None' mutant for a both-flag row (F1.4 W6)."""
    out, _ = _call(fi.make_fleet_cost_outliers_tool, OUTLIERS_V3)
    row = out["outliers"][0]
    assert row["severity"] == "attention", (
        f"both-flag row: severity={row['severity']!r}, expected 'attention'"
    )
