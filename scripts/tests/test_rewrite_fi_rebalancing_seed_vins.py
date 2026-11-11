"""Offline tests for scripts/rewrite-fi-rebalancing-seed-vins.py (FI stage E 2.4 one-off repair).

No AWS: plan_rewrites is pure; the guard is exercised on real botocore clients aimed at a dead
endpoint (an allowed call reaches the network and fails to connect; a refused one never does);
main() runs against an in-memory fake client.
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import botocore.session
import pytest
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError

_PATH = pathlib.Path(__file__).resolve().parents[1] / "rewrite-fi-rebalancing-seed-vins.py"
_spec = importlib.util.spec_from_file_location("rewrite_seed_vins_under_test", _PATH)
rw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rw)

MOVES = "cms-staging-storage-vehicle-moves"


def vin_for(vid: str) -> str:
    return "MRD" + hashlib.sha256(vid.encode()).hexdigest()[:14].upper()


def veh(vid, fleet="demo-lastmile-delivery", loc="loc-del-sac", vin="<derive>"):
    item = {"vehicleId": {"S": vid}, "fleetId": {"S": fleet}, "locationId": {"S": loc}}
    if vin == "<derive>":
        vin = vin_for(vid)
    if vin is not None:
        item["vin"] = {"S": vin}
    return item


def move(move_id, vins, fleet="demo-lastmile-delivery", loc="loc-del-sac", kind="seed"):
    return {"fleetId": {"S": fleet}, "moveId": {"S": move_id}, "toLocationId": {"S": loc},
            "decidedByKind": {"S": kind}, "vins": {"L": [{"S": x} for x in vins]}}


def fixture():
    good = [vin_for(f"G{i}") for i in range(3)]
    vehicles = [veh("VEH-A-0001"), veh("VEH-A-0002"),
                veh("VEH-B-0001", fleet="demo-city-public", loc="loc-city-pdx-north")]
    moves = [
        move("m-sac", sorted(good[:2] + ["VEH-A-0001", "VEH-A-0002"])),
        move("m-pdx", ["VEH-B-0001", good[2]], fleet="demo-city-public", loc="loc-city-pdx-north"),
        move("m-clean", [vin_for("C1")], loc="loc-del-mia"),
        move("m-operator", ["VEH-A-0001"], kind="operator"),
    ]
    return vehicles, moves


class TestPlan:
    def test_exactly_the_bad_seed_records_and_entries(self):
        vehicles, moves = fixture()
        plan, errors = rw.plan_rewrites(vehicles, moves)
        assert errors == []
        assert [p["moveId"] for p in plan] == ["m-sac", "m-pdx"]
        sac = plan[0]
        assert sac["swaps"] == [("VEH-A-0001", vin_for("VEH-A-0001")),
                                ("VEH-A-0002", vin_for("VEH-A-0002"))]
        assert sac["new"] == sorted([vin_for("G0"), vin_for("G1"),
                                     vin_for("VEH-A-0001"), vin_for("VEH-A-0002")])
        assert len(sac["new"]) == len(sac["old"])
        assert sac["old"] == [x["S"] for x in moves[0]["vins"]["L"]]

    @pytest.mark.parametrize("case", [
        "unknown-id", "vin-missing", "vin-malformed", "vin-wrong-wmi", "wrong-location",
        "wrong-fleet", "vin-in-another-record", "duplicate-after-rewrite", "same-vin-planned-twice",
    ])
    def test_refusals(self, case):
        vehicles, moves = fixture()
        if case == "unknown-id":
            moves[0]["vins"]["L"].append({"S": "VEH-NOBODY"})
        elif case == "vin-missing":
            vehicles[0] = veh("VEH-A-0001", vin=None)
        elif case == "vin-malformed":
            vehicles[0] = veh("VEH-A-0001", vin="MRD123")
        elif case == "vin-wrong-wmi":
            vehicles[0] = veh("VEH-A-0001", vin="1FT" + vin_for("VEH-A-0001")[3:])
        elif case == "wrong-location":
            vehicles[0] = veh("VEH-A-0001", loc="loc-del-mia")
        elif case == "wrong-fleet":
            vehicles[0] = veh("VEH-A-0001", fleet="demo-field-service")
        elif case == "vin-in-another-record":
            moves[2]["vins"]["L"].append({"S": vin_for("VEH-A-0001")})
        elif case == "duplicate-after-rewrite":
            moves[0]["vins"]["L"].append({"S": vin_for("VEH-A-0001")})
        elif case == "same-vin-planned-twice":
            vehicles[2] = veh("VEH-B-0001", fleet="demo-city-public", loc="loc-city-pdx-north",
                              vin=vin_for("VEH-A-0001"))
        _plan, errors = rw.plan_rewrites(vehicles, moves)
        assert errors, f"{case} must be refused"


def _client(service):
    s = botocore.session.get_session()
    return s.create_client(service, region_name="us-west-2", endpoint_url="http://127.0.0.1:1",
                           aws_access_key_id="x", aws_secret_access_key="y",
                           config=Config(retries={"max_attempts": 1}, connect_timeout=1))


class TestGuard:
    KEY = {"fleetId": {"S": "f"}, "moveId": {"S": "m"}}

    def _ddb(self, allow):
        c = _client("dynamodb")
        c.meta.events.register("before-parameter-build.dynamodb", rw._guard(allow))
        return c

    @staticmethod
    def _outcome(call, **kw):
        """'refused' if the guard stopped it, 'passed' if it reached the (dead) endpoint."""
        try:
            call(**kw)
        except RuntimeError as e:
            return "refused" if "REFUSED by guard" in str(e) else f"other: {e}"
        except EndpointConnectionError:
            return "passed"
        return "returned"

    def test_dry_run_refuses_update(self):
        c = self._ddb(None)
        assert self._outcome(c.update_item, TableName=MOVES, Key=self.KEY, UpdateExpression="SET a = :a",
                             ExpressionAttributeValues={":a": {"S": "x"}}) == "refused"

    def test_reads_pass_the_guard(self):
        assert self._outcome(self._ddb(None).describe_table, TableName=MOVES) == "passed"

    def test_apply_admits_update_on_moves_only(self):
        c = self._ddb(MOVES)
        upd = dict(UpdateExpression="SET a = :a", ExpressionAttributeValues={":a": {"S": "x"}})
        assert self._outcome(c.update_item, TableName=MOVES, Key=self.KEY, **upd) == "passed"
        assert self._outcome(c.update_item, TableName="cms-staging-storage-vehicles",
                             Key={"vehicleId": {"S": "v"}}, **upd) == "refused"
        assert self._outcome(c.put_item, TableName=MOVES, Item=self.KEY) == "refused"
        assert self._outcome(c.delete_item, TableName=MOVES, Key=self.KEY) == "refused"

    @pytest.mark.parametrize("allow", [None, MOVES], ids=["dry-run", "apply"])
    def test_other_write_shaped_ops_are_refused(self, allow):
        """Exact allowlist: write ops whose names share a prefix with an admitted one are
        refused (security review cycle 1 W: SEC-M2 'Update*', SEC-M3 'Batch*')."""
        c = self._ddb(allow)
        cases = [
            (c.update_table, dict(TableName=MOVES, BillingMode="PAY_PER_REQUEST")),
            (c.update_time_to_live, dict(TableName=MOVES, TimeToLiveSpecification={
                "Enabled": True, "AttributeName": "ttl"})),
            (c.batch_write_item, dict(RequestItems={MOVES: [{"PutRequest": {"Item": self.KEY}}]})),
            (c.batch_execute_statement, dict(Statements=[{"Statement": "SELECT * FROM t"}])),
            (c.execute_statement, dict(Statement="SELECT * FROM t")),
            (c.transact_write_items, dict(TransactItems=[{"Put": {"TableName": MOVES, "Item": self.KEY}}])),
            (c.query, dict(TableName=MOVES, KeyConditionExpression="fleetId = :f",
                           ExpressionAttributeValues={":f": {"S": "f"}})),
        ]
        for call, kw in cases:
            assert self._outcome(call, **kw) == "refused", call.__name__

    def test_the_scripts_own_reads_pass(self):
        c = self._ddb(None)
        assert self._outcome(c.scan, TableName=MOVES) == "passed"
        assert self._outcome(c.get_item, TableName=MOVES, Key=self.KEY) == "passed"
        sts = _client("sts")
        sts.meta.events.register("before-parameter-build.sts", rw._guard(None))
        assert self._outcome(sts.get_caller_identity) == "passed"
        assert self._outcome(sts.assume_role, RoleArn="arn:aws:iam::" + "1" * 12 + ":role/r",
                             RoleSessionName="session") == "refused"


class _Events:
    def __init__(self):
        self.hooks = []

    def register(self, name, fn):
        self.hooks.append(fn)


class _Model:
    def __init__(self, name):
        self.name = name


class FakeDdb:
    """In-memory DynamoDB low-level client: vehicles + moves; runs the registered guard."""

    def __init__(self, vehicles, moves, readback_override=None):
        self.meta = type("M", (), {"events": _Events()})()
        self.vehicles, self.moves = vehicles, {(m["fleetId"]["S"], m["moveId"]["S"]): m for m in moves}
        self.updates = []
        self.readback_override = readback_override

    def _g(self, op, **params):
        for h in self.meta.events.hooks:
            h(params=params, model=_Model(op))

    def describe_table(self, TableName):
        self._g("DescribeTable", TableName=TableName)
        return {"Table": {"KeySchema": [{"AttributeName": "fleetId"}, {"AttributeName": "moveId"}]}}

    def get_paginator(self, _name):
        fake = self

        class P:
            def paginate(self, TableName, **_kw):
                fake._g("Scan", TableName=TableName)
                rows = fake.vehicles if TableName.endswith("vehicles") else list(fake.moves.values())
                return [{"Items": rows}]
        return P()

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        self._g("UpdateItem", TableName=TableName)
        self.updates.append(dict(Key=Key, ConditionExpression=ConditionExpression,
                                 ExpressionAttributeValues=ExpressionAttributeValues))
        rec = self.moves[(Key["fleetId"]["S"], Key["moveId"]["S"])]
        assert ConditionExpression == "vins = :old AND decidedByKind = :seed"
        if rec["vins"] != ExpressionAttributeValues[":old"] or rec["decidedByKind"] != {"S": "seed"}:
            raise RuntimeError("ConditionalCheckFailedException")
        rec["vins"] = ExpressionAttributeValues[":new"]
        return {}

    def get_item(self, TableName, Key, ConsistentRead, ProjectionExpression):
        self._g("GetItem", TableName=TableName)
        assert ConsistentRead is True
        rec = self.moves[(Key["fleetId"]["S"], Key["moveId"]["S"])]
        return {"Item": {"vins": self.readback_override or rec["vins"]}}


class FakeSts:
    def __init__(self):
        self.meta = type("M", (), {"events": _Events()})()

    def get_caller_identity(self):
        return {"Account": "1" * 12}


def run_main(monkeypatch, ddb, *args):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **_k: ddb if name == "dynamodb" else FakeSts())
    return rw.main(["--stage", "staging", *args])


class TestMain:
    def test_count_mismatch_refuses_before_any_write(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        code = run_main(monkeypatch, ddb, "--apply", "--account-id", "1" * 12)  # defaults 9 / 101
        assert code == 2 and ddb.updates == []

    def test_wrong_account_refuses(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        code = run_main(monkeypatch, ddb, "--apply", "--account-id", "2" * 12,
                        "--expect-records", "2", "--expect-vins", "3")
        assert code == 2 and ddb.updates == []

    @pytest.mark.parametrize("args", [[], ["--account-id", "2" * 12]], ids=["missing", "wrong"])
    def test_dry_run_needs_the_right_account(self, monkeypatch, args):
        """Security review cycle 1 S2: no scan in an account the operator didn't name."""
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        scans = []
        real = ddb.get_paginator
        ddb.get_paginator = lambda n: (scans.append(n), real(n))[1]
        code = run_main(monkeypatch, ddb, *args, "--expect-records", "2", "--expect-vins", "3")
        assert code == 2 and scans == []

    def test_dry_run_writes_nothing(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        assert run_main(monkeypatch, ddb, "--account-id", "1" * 12,
                        "--expect-records", "2", "--expect-vins", "3") == 0
        assert ddb.updates == []
        # main's own guard on its dynamodb client refuses an UpdateItem in dry run.
        assert ddb.meta.events.hooks, "main must register its guard on the dynamodb client"
        for hook in ddb.meta.events.hooks:
            with pytest.raises(RuntimeError, match="REFUSED by guard"):
                hook(params={"TableName": MOVES}, model=_Model("UpdateItem"))

    def test_apply_guard_refuses_other_writes(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        assert run_main(monkeypatch, ddb, "--apply", "--account-id", "1" * 12,
                        "--expect-records", "2", "--expect-vins", "3") == 0
        assert ddb.meta.events.hooks
        for hook in ddb.meta.events.hooks:
            for op, table in [("PutItem", MOVES), ("DeleteItem", MOVES),
                              ("UpdateItem", "cms-staging-storage-vehicles")]:
                with pytest.raises(RuntimeError, match="REFUSED by guard"):
                    hook(params={"TableName": table}, model=_Model(op))

    def test_apply_rewrites_exactly_the_planned_records_conditionally(self, monkeypatch):
        vehicles, moves = fixture()
        before = {k: [x["S"] for x in m["vins"]["L"]] for k, m in
                  {(m["fleetId"]["S"], m["moveId"]["S"]): m for m in moves}.items()}
        ddb = FakeDdb(vehicles, moves)
        code = run_main(monkeypatch, ddb, "--apply", "--account-id", "1" * 12,
                        "--expect-records", "2", "--expect-vins", "3")
        assert code == 0
        assert [u["Key"]["moveId"]["S"] for u in ddb.updates] == ["m-sac", "m-pdx"]
        for u in ddb.updates:
            k = (u["Key"]["fleetId"]["S"], u["Key"]["moveId"]["S"])
            assert [x["S"] for x in u["ExpressionAttributeValues"][":old"]["L"]] == before[k]
        assert [x["S"] for x in ddb.moves[("demo-lastmile-delivery", "m-sac")]["vins"]["L"]] == sorted(
            [vin_for("G0"), vin_for("G1"), vin_for("VEH-A-0001"), vin_for("VEH-A-0002")])
        assert [x["S"] for x in ddb.moves[("demo-lastmile-delivery", "m-operator")]["vins"]["L"]] == ["VEH-A-0001"]
        assert [x["S"] for x in ddb.moves[("demo-lastmile-delivery", "m-clean")]["vins"]["L"]] == [vin_for("C1")]

    def test_changed_record_fails_the_condition_and_stops(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves)
        real = ddb.update_item

        def changed_first(**kw):
            ddb.moves[("demo-lastmile-delivery", "m-sac")]["vins"] = {"L": [{"S": "changed"}]}
            ddb.update_item = real
            return real(**kw)
        ddb.update_item = changed_first
        code = run_main(monkeypatch, ddb, "--apply", "--account-id", "1" * 12,
                        "--expect-records", "2", "--expect-vins", "3")
        assert code == 1
        assert len(ddb.updates) == 1, "must stop after the first failed write"

    def test_readback_mismatch_stops(self, monkeypatch):
        vehicles, moves = fixture()
        ddb = FakeDdb(vehicles, moves, readback_override={"L": [{"S": "other"}]})
        code = run_main(monkeypatch, ddb, "--apply", "--account-id", "1" * 12,
                        "--expect-records", "2", "--expect-vins", "3")
        assert code == 1 and len(ddb.updates) == 1
