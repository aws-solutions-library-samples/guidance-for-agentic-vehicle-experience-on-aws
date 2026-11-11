"""Items 1 and 7 of scripts/live-verify-fi-quality.py (task 5.3), on live-shaped data.

Item 1 runs on the real patterns rows captured read-only from staging on 2026-10-16
(``fi_quality_data/``: the full ``adp-staging-fault-patterns`` scan and the published
``fault_patterns.parquet``) and the 8 planted patterns of ADP
``fleet_fault_events/plants.py`` (the live answer key's rows, copied below). Item 7's
fake IAM answers like ``simulate_principal_policy`` does: one EvaluationResult per action.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest
from botocore.exceptions import ClientError

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality_17", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lv)

_DATA = _HERE / "fi_quality_data"
_ITEMS = json.loads((_DATA / "fault_patterns_items_2026-10-16.json").read_text())["items"]
_PARQUET = _DATA / "fault_patterns_2026-10-16.parquet"

#: The live answer key (8 rows), as ADP plants.py generates it for the seed-42 manifest.
_AK = [
    ("P1", "P0AA", "build_window", "2024-02:2024-03:2024-04:2024-05", 55, "2025-02-01", "FM-HV-ISO-01"),
    ("P2", "P0A0", "plant", "RNO", 44, "2024-03-01", "FM-HV-ILK-01"),
    ("P3", "P1A0", "build_window", "2023-01:2023-02:2023-03:2023-04", 53, "2025-06-01", "FM-BCP-01"),
    ("P4", "P1B1", "build_window", "2022-09:2022-10:2022-11", 86, "2024-09-01", "FM-THVLV-01"),
    ("P5", "U007", "build_window", "2025-07:2025-08:2025-09", 48, "2026-04-01", "FM-COMM-01"),
    ("P6", "P0A7", "build_window", "2025-10:2025-11:2025-12:2026-01:2026-02:2026-03", 76,
     "2027-01-01", "FM-MCTRL-01"),
    ("PL1", "P1AB", "supplier_lot", "SUP-B/OBC-L01-B", 48, "2025-09-01", "FM-CHG-01"),
    ("PL2", "P1B0", "supplier_lot", "SUP-B/DUI-L05-B", 48, "2025-12-01", "FM-DU-01"),
]
_ACCOUNT = "123456789012"


@pytest.fixture()
def answer_key(tmp_path: Path) -> str:
    path = tmp_path / "answer_key.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE ak (pattern_id VARCHAR, code_family VARCHAR, cause_dimension VARCHAR,"
                " cause_value VARCHAR, planted_count BIGINT, start_date DATE, failure_mode_id VARCHAR)")
    con.executemany("INSERT INTO ak VALUES (?, ?, ?, ?, ?, ?, ?)", _AK)
    con.execute(f"COPY ak TO '{path}' (FORMAT PARQUET)")
    con.close()
    return str(path)


class _Table:
    """DynamoDB resource Table: scan pages of ``page_size`` with LastEvaluatedKey."""

    def __init__(self, items: list[dict], page_size: int = 40) -> None:
        self.items, self.page_size, self.calls = items, page_size, 0

    def scan(self, **kw: Any) -> dict:
        self.calls += 1
        start = int(kw.get("ExclusiveStartKey", {}).get("i", 0))
        page = self.items[start:start + self.page_size]
        out: dict[str, Any] = {"Items": copy.deepcopy(page)}
        if start + self.page_size < len(self.items):
            out["LastEvaluatedKey"] = {"i": start + self.page_size}
        return out


class _Boto:
    """Patches boto3.client/resource for one item run."""

    def __init__(self, items: list[dict] | None = None, parquet: Path = _PARQUET) -> None:
        self.table = _Table(items if items is not None else _ITEMS)
        self.parquet = parquet
        self.iam = _Iam()
        self.bucket_policy: str | None = '{"Version":"2012-10-17","Statement":[]}'
        self.glue_tables: dict[str, list[dict]] = {"adp_staging_fleet_health": [],
                                                   "adp_staging_engineering": []}
        self.glue_error: str | None = None

    def client(self, service: str, **_: Any) -> Any:
        outer = self

        class C:
            def get_caller_identity(self) -> dict:
                return {"Account": _ACCOUNT}

            def download_file(self, bucket: str, key: str, dest: str) -> None:
                assert bucket == f"adp-staging-foundation-lake-{_ACCOUNT}-us-east-1"
                assert key == "curated/engineering/fault_patterns/fault_patterns.parquet"
                shutil.copy(outer.parquet, dest)

            def get_bucket_policy(self, Bucket: str) -> dict:
                if outer.bucket_policy is None:
                    raise ClientError({"Error": {"Code": "NoSuchBucketPolicy", "Message": "x"}},
                                      "GetBucketPolicy")
                return {"Policy": outer.bucket_policy}

            def get_tables(self, **kw: Any) -> dict:
                if outer.glue_error:
                    raise ClientError({"Error": {"Code": outer.glue_error, "Message": "x"}}, "GetTables")
                tables = outer.glue_tables[kw["DatabaseName"]]
                if "NextToken" in kw:
                    return {"TableList": tables[1:]}
                return {"TableList": tables[:1], **({"NextToken": "t"} if len(tables) > 1 else {})}

        if service == "iam":
            return self.iam
        return C()

    def resource(self, service: str, **_: Any) -> Any:
        assert service == "dynamodb"
        outer = self

        class R:
            def Table(self, name: str) -> _Table:
                assert name == "adp-staging-fault-patterns"
                return outer.table
        return R()


def _resource_policy_decision(policy: str, principal_arn: str, action: str, resource: str,
                              identity: str) -> str:
    """Combine the identity decision with a bucket policy's statements (review cycle 2 W4)."""
    import fnmatch

    def _as_list(v: Any) -> list:
        return v if isinstance(v, list) else ([] if v is None else [v])

    doc = json.loads(policy)
    allowed = deny = False
    for st in _as_list(doc.get("Statement")):
        pr = st.get("Principal")
        prs = ["*"] if pr == "*" else _as_list((pr or {}).get("AWS"))
        if not any(p == "*" or p == principal_arn for p in prs):
            continue
        if not any(fnmatch.fnmatchcase(action, a) for a in _as_list(st.get("Action"))):
            continue
        if not any(fnmatch.fnmatchcase(resource, r) for r in _as_list(st.get("Resource"))):
            continue
        if st.get("Effect") == "Deny":
            deny = True
        elif st.get("Effect") == "Allow":
            allowed = True
    if deny or identity == "explicitDeny":
        return "explicitDeny"
    return "allowed" if (allowed or identity == "allowed") else identity


class _Iam:
    """simulate_principal_policy over a decision table keyed by (role, resource suffix),
    then the given ResourcePolicy (same account: a matching Allow there allows, a matching
    Deny denies), as IAM's simulator does with ``--resource-policy``."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.override: dict[tuple[str, str], str] = {}
        self.drop_action: str | None = None
        self.roles = [{"RoleName": "cms-staging-ui-FleetIntelligenceRole18594AC3-abc",
                       "Arn": f"arn:aws:iam::{_ACCOUNT}:role/cms-staging-ui-FleetIntelligenceRole18594AC3-abc"}]

    def list_roles(self, **kw: Any) -> dict:
        if "Marker" in kw:
            return {"Roles": self.roles, "IsTruncated": False}
        return {"Roles": [{"RoleName": "other", "Arn": "x"}], "IsTruncated": True, "Marker": "m"}

    def simulate_principal_policy(self, **kw: Any) -> dict:
        self.calls.append(kw)
        role = kw["PolicySourceArn"].rsplit("/", 1)[1]
        res = kw["ResourceArns"][0].split("/", 1)[1]
        out = []
        for a in kw["ActionNames"]:
            if a == self.drop_action:
                continue
            dec = self.override.get((role[:10], res))
            if dec is None:
                if res.startswith("restricted/"):
                    dec = "explicitDeny" if role.startswith("vsa-") else "implicitDeny"
                else:
                    dec = "allowed"
            if "ResourcePolicy" in kw:
                dec = _resource_policy_decision(kw["ResourcePolicy"], kw["PolicySourceArn"], a,
                                                kw["ResourceArns"][0], dec)
            out.append({"EvalActionName": a, "EvalDecision": dec})
        if len(out) > 1 and "Marker" not in kw:  # second page carries the rest
            return {"EvaluationResults": out[:1], "IsTruncated": True, "Marker": "p2"}
        if "Marker" in kw:
            return {"EvaluationResults": out[1:], "IsTruncated": False}
        return {"EvaluationResults": out, "IsTruncated": False}


def _run(item: str, boto: _Boto, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> lv.Results:
    import boto3
    monkeypatch.setattr(boto3, "client", boto.client)
    monkeypatch.setattr(boto3, "resource", boto.resource)
    res = lv.Results()
    kw.setdefault("answer_key", None)
    lv._run_items([item], res, allow_writes=False, stage="staging", **kw)
    return res


def _status(res: lv.Results) -> dict[str, str]:
    return {label: status for label, status, _ in res.rows}


# ---------------------------------------------------------------------------
# Item 1
# ---------------------------------------------------------------------------

def test_item1_live_rows_pass(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    res = _run("1", boto, monkeypatch, answer_key=answer_key)
    st = _status(res)
    assert set(st.values()) == {"PASS"}, res.rows
    assert boto.table.calls == 3  # 85 rows in pages of 40: every page read
    detail = dict((label, d) for label, _, d in res.rows)
    assert detail["item-1-found-active"].startswith("7/7 ")
    assert detail["item-1-found-all"].startswith("7/8 ")


def test_item1_single_page_read_would_miss_plants(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows matching PL1/PL2 sit on the last page, so a reader that stops at page 1 fails."""
    plants = {"P1AB", "P1B0"}
    items = ([r for r in _ITEMS if r["code_family"] not in plants]
             + [r for r in _ITEMS if r["code_family"] in plants])
    boto = _Boto(items)
    boto.table.page_size = len(items) - sum(r["code_family"] in plants for r in _ITEMS)
    res = _run("1", boto, monkeypatch, answer_key=answer_key)
    assert set(_status(res).values()) == {"PASS"}, res.rows
    assert boto.table.calls == 2


def _without(families: set[str]) -> list[dict]:
    return [r for r in _ITEMS if r["code_family"] not in families]


def _parquet_without(families: set[str], tmp_path: Path) -> Path:
    out = tmp_path / "fp.parquet"
    fam = ", ".join(f"'{f}'" for f in sorted(families)) or "''"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_parquet('{_PARQUET}') WHERE code_family NOT IN ({fam})) "
        f"TO '{out}' (FORMAT PARQUET)")
    return out


def test_item1_one_active_plant_missed_fails_the_all_ratio(answer_key: str, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """6/7 active (86%) passes, 6/8 of all planted (75%) fails: both ratios are required."""
    boto = _Boto(_without({"P1B0"}), _parquet_without({"P1B0"}, tmp_path))
    st = _status(_run("1", boto, monkeypatch, answer_key=answer_key))
    assert st["item-1-found-active"] == "PASS"
    assert st["item-1-found-all"] == "FAIL"


def test_item1_two_active_plants_missed_fails_the_active_ratio(answer_key: str, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto(_without({"P1B0", "P1AB"}), _parquet_without({"P1B0", "P1AB"}, tmp_path))
    st = _status(_run("1", boto, monkeypatch, answer_key=answer_key))
    assert st["item-1-found-active"] == "FAIL"


def test_item1_cause_must_match_not_just_the_family(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A P1B0 row on a different lot is not the plant: only the cause value counts."""
    items = copy.deepcopy(_ITEMS)
    for r in items:
        if r["code_family"] in {"P1B0", "P1AB"} and r["cohort_dimension"] == "supplier_lot":
            r["cohort_value"] = "SUP-A/OTHER-L99-A"
    st = _status(_run("1", _Boto(items), monkeypatch, answer_key=answer_key))
    assert st["item-1-found-active"] == "FAIL"


def test_item1_pattern_in_future_only_family_is_noise(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    items = copy.deepcopy(_ITEMS)
    items[0]["code_family"] = "P0A7"
    st = _status(_run("1", _Boto(items), monkeypatch, answer_key=answer_key))
    assert st["item-1-no-noise"] == "FAIL"


def test_item1_dynamodb_and_parquet_must_hold_the_same_ids(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    st = _status(_run("1", _Boto(_ITEMS[1:]), monkeypatch, answer_key=answer_key))
    assert st["item-1-ddb-equals-parquet"] == "FAIL"


def test_item1_mixed_run_dates_fail(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    items = copy.deepcopy(_ITEMS)
    items[3]["run_date"] = "2026-10-15"
    st = _status(_run("1", _Boto(items), monkeypatch, answer_key=answer_key))
    assert st["item-1-one-run-date"] == "FAIL"
    assert "item-1-found-active" not in st


def test_item1_future_plant_is_not_counted_active(answer_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = lv._score_detection(_ITEMS, lv._load_answer_key(answer_key))
    assert sorted(a["pattern_id"] for a in sc["active"]) == ["P1", "P2", "P3", "P4", "P5", "PL1", "PL2"]
    assert sc["future_only"] == ["P0A7"]
    assert sorted(sc["found"]) == ["P1", "P2", "P3", "P4", "P5", "PL1", "PL2"]


def test_item1_matching_rule_is_adps() -> None:
    ak = {"code_family": "P1A0", "cause_dimension": "build_window", "cause_value": "2023-01:2023-02"}
    row = {"code_family": "P1A0", "cohort_dimension": "build_window_3m", "cohort_value": "2023-02:2023-04"}
    assert lv._matches(row, ak)
    assert not lv._matches({**row, "cohort_value": "2023-03:2023-05"}, ak)
    assert not lv._matches({**row, "cohort_dimension": "build_window_5m"}, ak)
    plant = {"code_family": "P0A0", "cause_dimension": "plant", "cause_value": "RNO"}
    assert lv._matches({"code_family": "P0A0", "cohort_dimension": "assembly_plant", "cohort_value": "RNO"}, plant)
    assert not lv._matches({"code_family": "P0A0", "cohort_dimension": "plant", "cohort_value": "RNO"}, plant)
    assert not lv._matches({"code_family": "P0A1", "cohort_dimension": "assembly_plant", "cohort_value": "RNO"}, plant)


def test_item1_without_answer_key_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    res = _run("1", _Boto(), monkeypatch, answer_key=None)
    assert [s for _, s, _ in res.rows] == ["SKIP"]


def test_item1_answer_key_missing_a_column_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ak.parquet"
    duckdb.connect().execute(f"COPY (SELECT 'P1' AS pattern_id, 'P0AA' AS code_family) TO '{path}' (FORMAT PARQUET)")
    st = _status(_run("1", _Boto(), monkeypatch, answer_key=str(path)))
    assert st == {"item-1-answer-key": "FAIL"}


# ---------------------------------------------------------------------------
# Item 7
# ---------------------------------------------------------------------------

def test_item7_denies_with_controls_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    res = _run("7", boto, monkeypatch)
    st = _status(res)
    assert set(st.values()) == {"PASS"}, res.rows
    # 2 roles x 2 modes x (1 control + 2 answer keys), each with both pages followed
    assert len(boto.iam.calls) == 2 * 2 * 3 + 2 * 2 * 2
    with_policy = [c for c in boto.iam.calls if "ResourcePolicy" in c]
    assert with_policy and all(c.get("ResourceOwner") == f"arn:aws:iam::{_ACCOUNT}:root" for c in with_policy)


@pytest.mark.parametrize("role", ["vsa-stagin", "cms-stagin"])
@pytest.mark.parametrize("key", ["restricted/fleet_fault_events/answer_key/answer_key.parquet",
                                 "restricted/engineering/answer_key/answer_key.parquet"])
def test_item7_a_readable_answer_key_fails(role: str, key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.iam.override[(role, key)] = "allowed"
    st = _status(_run("7", boto, monkeypatch))
    failed = [k for k, v in st.items() if v == "FAIL"]
    assert failed and all(key.split("/")[1] in k for k in failed), st


@pytest.mark.parametrize("role,ctrl", [("vsa-stagin", "curated/service_records/x"),
                                       ("cms-stagin", "curated/fleet_recall/x")])
def test_item7_a_denied_control_fails(role: str, ctrl: str, monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.iam.override[(role, ctrl)] = "implicitDeny"
    st = _status(_run("7", boto, monkeypatch))
    assert [k for k, v in st.items() if v == "FAIL" and k.endswith("-control")]


def test_item7_a_missing_decision_fails_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.iam.drop_action = "s3:GetObjectVersion"
    st = _status(_run("7", boto, monkeypatch))
    assert st.get("item-7-exception") == "FAIL"


def test_item7_without_bucket_policy_runs_identity_only(monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.bucket_policy = None
    st = _status(_run("7", boto, monkeypatch))
    assert set(st.values()) == {"PASS"}
    assert not [c for c in boto.iam.calls if "ResourcePolicy" in c]
    assert not [k for k in st if "bucket-policy" in k]


def test_item7_glue_table_under_restricted_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.glue_tables["adp_staging_engineering"] = [
        {"Name": "ok", "StorageDescriptor": {"Location": "s3://b/curated/engineering/x/"}},
        {"Name": "ak", "StorageDescriptor": {"Location": "s3://b/restricted/engineering/answer_key/"}},
    ]
    st = _status(_run("7", boto, monkeypatch))
    assert st["item-7-glue-adp_staging_engineering"] == "FAIL"  # second page read


def test_item7_glue_read_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.glue_error = "AccessDeniedException"
    st = _status(_run("7", boto, monkeypatch))
    assert st.get("item-7-exception") == "FAIL"


@pytest.mark.parametrize("n", [0, 2])
def test_item7_fi_role_must_be_exactly_one(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    boto = _Boto()
    boto.iam.roles = boto.iam.roles * n
    st = _status(_run("7", boto, monkeypatch))
    assert st.get("item-7-exception") == "FAIL"



# ---------------------------------------------------------------------------
# Review cycle 2: W4 (the bucket-policy mode tests the policy), S5 (an infinite ratio)
# ---------------------------------------------------------------------------

def test_item7_a_bucket_policy_grant_fails_only_its_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """W4: a bucket policy granting the FI role an answer key FAILs exactly the bucket-policy
    rows for that key; the identity rows and the other key stay PASS."""
    bucket = f"adp-staging-foundation-lake-{_ACCOUNT}-us-east-1"
    fi_arn = _Iam().roles[0]["Arn"]
    boto = _Boto()
    boto.bucket_policy = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": fi_arn}, "Action": "s3:GetObject*",
        "Resource": f"arn:aws:s3:::{bucket}/restricted/engineering/answer_key/*"}]})
    st = _status(_run("7", boto, monkeypatch))
    failed = sorted(k for k, v in st.items() if v == "FAIL")
    assert failed == ["item-7-fi-bucket-policy-engineering"], st
    assert st["item-7-fi-identity-engineering"] == "PASS"


def test_item1_an_infinite_ratio_does_not_fail_a_passed_item(answer_key: str,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: ADP stores rate_ratio None for an infinite ratio; the report prints it."""
    items = copy.deepcopy(_ITEMS)
    for r in items:
        if r["code_family"] == "P0A0":
            r["rate_ratio"] = None
    st = _status(_run("1", _Boto(items), monkeypatch, answer_key=answer_key))
    assert set(st.values()) == {"PASS"}, st
