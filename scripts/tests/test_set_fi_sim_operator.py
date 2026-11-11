"""scripts/set-fi-sim-operator.py (FI D10a, task 11.4): dry run by default, --apply, read-back, invoke."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

_PATH = Path(__file__).resolve().parents[1] / "set-fi-sim-operator.py"
_spec = importlib.util.spec_from_file_location("set_fi_sim_operator", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_D10A = {
    "fleets": {f: {"enabled": True} for f in sorted(
        ["demo-lastmile-delivery", "demo-field-service", "demo-city-public", "demo-rental-carshare"])},
    "daily_cap": 20, "hold_vins": ["MRDN0000000000015"], "hold_vehicle_ids": ["MRDN0000000000015"],
}


class _Ssm:
    def __init__(self, current=None, readback=None):
        self.value, self.readback, self.puts = current, readback, []

    def get_parameter(self, Name):
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter")
        return {"Parameter": {"Value": self.readback if self.readback is not None and self.puts else self.value}}

    def put_parameter(self, **kw):
        self.puts.append(kw)
        self.value = kw["Value"]


class _Lambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 200, "Payload": io.BytesIO(b'{"status": "ok"}')}


class _World:
    def __init__(self, **ssm):
        self.ssm, self.lam, self.read_only = _Ssm(**ssm), _Lambda(), []

    def factory(self, region, *, read_only):
        self.read_only.append(read_only)
        world = self

        class _S:
            def client(self, name):
                return {"ssm": world.ssm, "lambda": world.lam}[name]
        return _S()


def test_default_value_is_d10a_and_the_parser_accepts_it():
    v = mod.build_value(fleets=list(mod.DEMO_FLEET_IDS), off=False, cap=20,
                        hold_vins=list(mod.DEFAULT_HOLD_VINS), hold_vehicle_ids=list(mod.DEFAULT_HOLD_VEHICLE_IDS))
    assert v == _D10A
    assert mod._validate(v) is None


def test_dry_run_reads_writes_nothing_and_uses_a_read_only_session():
    w = _World()
    assert mod.main(["--stage", "staging"], session_factory=w.factory) == 0
    assert w.ssm.puts == [] and w.read_only == [True]


def test_apply_writes_exactly_the_value_and_reads_it_back():
    w = _World()
    assert mod.main(["--stage", "staging", "--apply"], session_factory=w.factory) == 0
    assert w.read_only == [False]
    (put,) = w.ssm.puts
    assert put["Name"] == "/cvx/staging/fi/sim-operator" and put["Type"] == "String" and put["Overwrite"] is True
    assert json.loads(put["Value"]) == _D10A


def test_read_back_that_differs_exits_2():
    w = _World(current=json.dumps({"x": 1}), readback=json.dumps({"fleets": {}}))
    assert mod.main(["--stage", "staging", "--apply"], session_factory=w.factory) == 2


def test_off_writes_every_fleet_disabled_and_keeps_holds():
    w = _World()
    assert mod.main(["--stage", "staging", "--off", "--apply"], session_factory=w.factory) == 0
    v = json.loads(w.ssm.puts[0]["Value"])
    assert all(f["enabled"] is False for f in v["fleets"].values()) and v["hold_vins"] == ["MRDN0000000000015"]


@pytest.mark.parametrize("argv", [
    ["--fleets", "demo-lastmile-delivery,customer-fleet-1"],
    ["--cap", "21"], ["--cap", "-1"],
    ["--invoke", "observe", "--apply"],
    ["--observe-now", "--invoke", "dry_run"],
])
def test_refusals_touch_no_aws(argv):
    w = _World()
    assert mod.main(["--stage", "staging", *argv], session_factory=w.factory) == 2
    assert w.read_only == []


def test_approve_is_never_invocable():
    with pytest.raises(SystemExit):
        mod.main(["--stage", "staging", "--invoke", "approve"], session_factory=_World().factory)


def test_observe_now_invokes_the_function_in_observe_with_nothing_written():
    w = _World(current=json.dumps(_D10A))
    assert mod.main(["--stage", "staging", "--observe-now"], session_factory=w.factory) == 0
    assert w.ssm.puts == []
    (call,) = w.lam.calls
    assert call["FunctionName"] == "vsa-staging-avx-sim-operator-us-west-2"
    assert json.loads(call["Payload"]) == {"mode": "observe"}


def test_the_real_dry_run_session_refuses_a_write(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
    import readonly_guard
    ssm = mod._session("us-west-2", read_only=True).client("ssm")
    with pytest.raises(readonly_guard.ReadOnlyViolation):
        ssm.put_parameter(Name="/cvx/staging/fi/sim-operator", Value="{}", Type="String", Overwrite=True)
