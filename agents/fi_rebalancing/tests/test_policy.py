"""The rebalancing policy: config file → SSM → the run (user's answer 2026-10-14).

Mutants (results in the spec's ``review-fragments/g6-fixes/``): MP3 the runner ignores
``ssm_client``; MP4 ``load_from_ssm`` swallows a read error; MP5 the config file's values
change; MP6 the ordering check removed. MP1/MP2 (literals put back) are in
``test_policy_no_literals.py``.
"""
from __future__ import annotations

import json

import pytest

from agents.fi_rebalancing import policy
from agents.fi_rebalancing.tests.policy_file import policy_entry, policy_from_config_file

_GOOD = {"target_utilization": 0.85, "deficit_above": 0.9, "max_utilization": 0.95,
         "min_net_benefit_usd": 50, "max_vehicles_per_move": 10}


class _FakeSsm:
    def __init__(self, value=None, exc=None):
        self.value, self.exc, self.names = value, exc, []

    def get_parameter(self, *, Name):
        self.names.append(Name)
        if self.exc is not None:
            raise self.exc
        return {"Parameter": {"Name": Name, "Value": self.value}}


# --- the config file ---------------------------------------------------------

def test_config_file_holds_the_approved_values():
    """U9 (spec, approved 2026-10-06): target 85%, deficit above 90%, at most 95% achievable,
    at most 10 vehicles a move, at least $100 net benefit a move. deficit_above and
    target_utilization also equal ADP's fleet_bookings parameters.yaml (0.90 / 0.85), which the
    gaps view applies. The agent used $50 from task 4.1c until the user approved U9's $100 for
    Group 6 (decisions.md 2026-10-16). MP5."""
    assert policy_from_config_file() == {
        "target_utilization": 0.85, "deficit_above": 0.90, "max_utilization": 0.95,
        "min_net_benefit_usd": 100.0, "max_vehicles_per_move": 10,
    }


def test_config_entry_is_a_string_parameter_the_seed_script_json_encodes():
    entry = policy_entry()
    assert entry["type"] == "String"
    assert isinstance(entry["staging_default"], dict)
    # The seed script writes json.dumps(default, separators=(",", ":")); that must parse.
    encoded = json.dumps(entry["staging_default"], separators=(",", ":"))
    assert policy.parse(encoded) == policy_from_config_file()


def test_the_installed_test_policy_is_the_config_files():
    assert policy.current() == policy_from_config_file()


# --- parse ------------------------------------------------------------------

def test_parse_accepts_json_and_dict():
    assert policy.parse(json.dumps(_GOOD)) == {**_GOOD, "min_net_benefit_usd": 50.0}
    assert policy.parse(_GOOD)["max_vehicles_per_move"] == 10


@pytest.mark.parametrize("change, match", [
    ({"target_utilization": None}, "must be a number"),
    ({"deficit_above": True}, "must be a number"),
    ({"max_utilization": "0.95"}, "must be a number"),
    ({"max_utilization": 1.2}, r"must be in \(0, 1\]"),
    ({"target_utilization": 0.0}, r"must be in \(0, 1\]"),
    ({"target_utilization": 0.9}, "target_utilization < deficit_above"),        # MP6
    ({"deficit_above": 0.96}, "deficit_above <= max_utilization"),            # MP6
    ({"min_net_benefit_usd": -1}, "min_net_benefit_usd must be from 0"),
    ({"min_net_benefit_usd": float("nan")}, "must be finite"),
    ({"max_vehicles_per_move": 0}, "max_vehicles_per_move must be an integer"),
    ({"max_vehicles_per_move": 10.0}, "max_vehicles_per_move must be an integer"),
    ({"max_vehicles_per_move": 101}, "max_vehicles_per_move must be an integer"),
])
def test_parse_refuses_bad_values(change, match):
    with pytest.raises(policy.PolicyUnavailable, match=match):
        policy.parse({**_GOOD, **change})


def test_parse_refuses_missing_and_extra_keys():
    with pytest.raises(policy.PolicyUnavailable, match=r"missing \['deficit_above'\]"):
        policy.parse({k: v for k, v in _GOOD.items() if k != "deficit_above"})
    with pytest.raises(policy.PolicyUnavailable, match=r"extra \['surplus_below'\]"):
        policy.parse({**_GOOD, "surplus_below": 0.7})


def test_parse_refuses_non_json_and_non_object():
    with pytest.raises(policy.PolicyUnavailable, match="not JSON"):
        policy.parse("{not json")
    with pytest.raises(policy.PolicyUnavailable, match="must be a JSON object"):
        policy.parse("[1, 2]")


# --- SSM --------------------------------------------------------------------

def test_load_from_ssm_reads_exactly_the_stage_parameter():
    ssm = _FakeSsm(value=json.dumps(_GOOD))
    assert policy.load_from_ssm("staging", ssm)["deficit_above"] == 0.9
    assert ssm.names == ["/vsa/staging/avx/fi-rebalancing/policy"]


def test_load_from_ssm_raises_on_a_read_error():  # MP4
    ssm = _FakeSsm(exc=RuntimeError("ParameterNotFound"))
    with pytest.raises(policy.PolicyUnavailable, match="fi-rebalancing/policy: RuntimeError"):
        policy.load_from_ssm("staging", ssm)


def test_load_from_ssm_raises_on_a_malformed_value():
    ssm = _FakeSsm(value=json.dumps({**_GOOD, "max_vehicles_per_move": 0}))
    with pytest.raises(policy.PolicyUnavailable, match="max_vehicles_per_move"):
        policy.load_from_ssm("staging", ssm)


@pytest.mark.parametrize("stage", ["", "a/b", "{stage}"])
def test_param_name_refuses_bad_stage(stage):
    with pytest.raises(policy.PolicyUnavailable, match="bad stage"):
        policy.param_name(stage)


# --- active / current ---------------------------------------------------------

def test_current_raises_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(policy, "_ACTIVE", None)
    with pytest.raises(policy.PolicyUnavailable, match="no FI rebalancing policy is installed"):
        policy.current()


def test_active_installs_then_restores():
    before = policy.current()
    with policy.active({**_GOOD, "min_net_benefit_usd": 777}):
        assert policy.current()["min_net_benefit_usd"] == 777.0
    assert policy.current() == before


# --- the runner -------------------------------------------------------------

def _runner_kwargs(**over):
    kw = dict(
        athena_client=object(), workgroup="wg", results_s3_path="s3://b/p/",
        ddb_client=object(), findings_table="f", vehicles_table="v",
        groundings_table="g", fleet_locations_table="l",
        avx_writer=object(), cloudwatch_client=None, run_record_writer=lambda **_: None,
    )
    kw.update(over)
    return kw


def test_runner_installs_the_ssm_policy_for_the_run(monkeypatch):  # MP3
    from agents.fi_rebalancing import agent as rebal_agent
    from agents.tier2 import runner

    seen = []

    def fake_run(**kw):
        seen.append(policy.current())
        return {"move_items_written": 0, "gap_items_written": 0, "items_unchanged": 0}

    monkeypatch.setattr(rebal_agent, "run_rebalance", fake_run)
    monkeypatch.setattr(runner, "emit_metrics", lambda *a, **k: None)
    before = policy.current()
    ssm = _FakeSsm(value=json.dumps({**_GOOD, "min_net_benefit_usd": 777}))
    runner.run_rebalance("staging", **_runner_kwargs(ssm_client=ssm))
    assert ssm.names == ["/vsa/staging/avx/fi-rebalancing/policy"]
    assert [p["min_net_benefit_usd"] for p in seen] == [777.0]
    assert policy.current() == before


def test_runner_aborts_before_the_agent_when_the_policy_is_missing(monkeypatch):  # MP4
    from agents.fi_rebalancing import agent as rebal_agent
    from agents.tier2 import runner

    called, records = [], []
    monkeypatch.setattr(rebal_agent, "run_rebalance", lambda **kw: called.append(kw))
    monkeypatch.setattr(runner, "emit_metrics", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **k: records.append(k))
    ssm = _FakeSsm(exc=RuntimeError("ParameterNotFound"))
    with pytest.raises(policy.PolicyUnavailable):
        runner.run_rebalance("staging", **_runner_kwargs(ssm_client=ssm))
    assert called == []
    assert len(records) == 1
    assert records[0]["summary"]["status"] == "aborted"
    assert records[0]["summary"]["abort_reason"] == "PolicyUnavailable"


def test_production_wiring_passes_an_ssm_client(monkeypatch):
    """_run_rebalance_from_env hands run_rebalance an SSM client in the Lambda's region."""
    import boto3

    from agents.tier2 import artifact_writer, runner

    built = []

    def fake_client(service, region_name=None, **_):
        built.append((service, region_name))
        return f"client:{service}"

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", lambda **kw: object())
    captured = {}
    monkeypatch.setattr(runner, "run_rebalance", lambda stage, **kw: captured.update(kw) or {})
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket")
    monkeypatch.setenv("DYNAMODB_REGION", "us-west-2")
    runner._run_rebalance_from_env("staging")
    assert captured["ssm_client"] == "client:ssm"
    assert ("ssm", "us-west-2") in built
