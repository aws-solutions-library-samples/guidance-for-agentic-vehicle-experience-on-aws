"""The investigation's campaign reads run against the tables' real columns (FI stage D 9.3).

Issue ``issues/2026-10-20-fi-quality-campaign-reads-name-missing-columns``: both queries in
``read_campaigns`` named columns their tables never had (recall ``rcr.name`` /
``rcr.status``; OTA ``h.component``), so every investigation failed at the step, and no test
saw it because every test stubbed ``run_query`` with rows the test wrote.

Here both SHIPPED queries run in DuckDB over tables built only from the producers' real
columns (live Glue, us-east-1, read 2026-10-20; ``ota_campaigns`` is partitioned by
``campaign_id``, ``ota_campaign_events`` by ``dispatch_date``), so a column the producer
lacks fails as Athena would (Binder Error), and the rows reach the real reader and the real
model context. Mutants: put ``rcr.name`` / ``rcr.status`` / ``h.component`` back (Binder
Error on the producer's columns); select the wrong column under a picked key.
"""
from __future__ import annotations

import datetime as _dt
import decimal

import duckdb
import pytest

from agents.fi_quality import llm, readers

_STAGE = "test"

#: live Glue column lists (us-east-1, 2026-10-20), partition keys appended.
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "fleet_recall.recall_vehicle_status": [
        ("campaign_id", "VARCHAR"), ("vin", "VARCHAR"), ("fleet_id", "VARCHAR"),
        ("match_basis", "VARCHAR"), ("confirming_code", "VARCHAR"),
        ("confirming_seen_on", "DATE"), ("confirming_source", "VARCHAR"),
        ("remedied", "BOOLEAN"), ("remedy_ref", "VARCHAR"), ("remedied_on", "DATE"),
        ("rule_version", "VARCHAR"), ("managed_as_of", "DATE")],
    "fleet_recall.recall_campaigns_received": [
        ("campaign_id", "VARCHAR"), ("manufacturer", "VARCHAR"), ("component", "VARCHAR"),
        ("summary", "VARCHAR"), ("consequence", "VARCHAR"), ("remedy", "VARCHAR"),
        ("do_not_drive", "BOOLEAN"), ("park_outside", "BOOLEAN"), ("models", "VARCHAR[]"),
        ("model_year_min", "INTEGER"), ("model_year_max", "INTEGER"), ("plants", "VARCHAR[]"),
        ("build_from", "DATE"), ("build_to", "DATE"), ("fault_codes", "VARCHAR[]"),
        ("signature_lookback_days", "INTEGER"), ("remedy_labor_op", "VARCHAR"),
        ("remedy_part_numbers", "VARCHAR[]"), ("remedy_flat_rate_hours", "DECIMAL(5,2)"),
        ("remedy_available_from", "DATE"), ("received_date", "DATE"),
        ("catalog_version", "VARCHAR"), ("recall_class", "VARCHAR")],
    "ota_campaigns.ota_campaigns": [
        ("campaign_name", "VARCHAR"), ("release_version", "VARCHAR"), ("target_make", "VARCHAR"),
        ("target_model", "VARCHAR"), ("target_model_year_min", "INTEGER"),
        ("target_model_year_max", "INTEGER"), ("target_software_version_min", "VARCHAR"),
        ("package_size_mb", "DOUBLE"), ("category", "VARCHAR"), ("severity", "VARCHAR"),
        ("dispatch_start_date", "DATE"), ("dispatch_end_date", "DATE"),
        ("phased_rollout_pct", "DOUBLE"), ("status", "VARCHAR"),
        ("campaign_id", "VARCHAR")],                       # partition key
    "ota_campaigns.ota_campaign_events": [
        ("campaign_id", "VARCHAR"), ("vin", "VARCHAR"), ("dispatch_time", "TIMESTAMP"),
        ("download_started_time", "TIMESTAMP"), ("download_completed_time", "TIMESTAMP"),
        ("install_started_time", "TIMESTAMP"), ("install_completed_time", "TIMESTAMP"),
        ("final_status", "VARCHAR"), ("failure_reason", "VARCHAR"),
        ("previous_software_version", "VARCHAR"), ("new_software_version", "VARCHAR"),
        ("event_time", "TIMESTAMP"), ("ingest_time", "TIMESTAMP"),
        ("dispatch_date", "DATE")],                        # partition key
}

_V1, _V2, _V3 = "MRDVEH00000000015", "MRDVEH00000000016", "MRDVEH00000000017"
_OUTSIDE = "MRDVEH00000000099"   # a managed VIN the investigation did not ask about


def _athena_text(v):
    """A cell as GetQueryResults renders it (NULL -> None)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    return str(v)


@pytest.fixture()
def lake(monkeypatch):
    conn = duckdb.connect()
    for qualified, cols in _COLUMNS.items():
        db, table = qualified.split(".")
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{_STAGE}_{db}")
        conn.execute(f"CREATE TABLE adp_{_STAGE}_{qualified} "
                     f"({', '.join(f'{c} {t}' for c, t in cols)})")

    def put(qualified: str, row: dict) -> None:
        cols = [c for c, _ in _COLUMNS[qualified]]
        assert not set(row) - set(cols), f"{qualified} has no {set(row) - set(cols)}"
        conn.execute(f"INSERT INTO adp_{_STAGE}_{qualified} VALUES ({', '.join('?' for _ in cols)})",
                     [row.get(c) for c in cols])

    calls: list[str] = []

    def fake_run_query(sql, *, athena_client, workgroup, results_s3_path):
        calls.append(sql)
        res = conn.execute(sql)
        names = [d[0] for d in res.description]
        return [{n: _athena_text(v) for n, v in zip(names, r)} for r in res.fetchall()]

    monkeypatch.setattr(readers, "run_query", fake_run_query)
    return put, calls


def _seed(put) -> None:
    put("fleet_recall.recall_campaigns_received",
        {"campaign_id": "MRD-2026-004", "manufacturer": "Meridian Motors",
         "component": "High-voltage battery contactor", "summary": "s", "consequence": "c",
         "remedy": "r", "do_not_drive": False, "park_outside": False, "models": ["Azimuth"],
         "model_year_min": 2023, "model_year_max": 2023, "received_date": "2026-01-01",
         "catalog_version": "1.0.0", "recall_class": "safety"})
    for vin in (_V1, _V2):
        put("fleet_recall.recall_vehicle_status",
            {"campaign_id": "MRD-2026-004", "vin": vin, "fleet_id": "F-1",
             "match_basis": "POPULATION", "remedied": False, "rule_version": "1.0.0",
             "managed_as_of": "2026-10-19"})
    put("ota_campaigns.ota_campaigns",
        {"campaign_id": "OTA-2026-007", "campaign_name": "FW-2026-MAY-SAFETY",
         "release_version": "4.2.1", "category": "safety_recall", "severity": "high",
         "status": "active"})
    put("ota_campaigns.ota_campaign_events",
        {"campaign_id": "OTA-2026-007", "vin": _V1, "final_status": "installed",
         "dispatch_date": "2026-05-01"})
    put("ota_campaigns.ota_campaign_events",
        {"campaign_id": "OTA-2026-007", "vin": _V3, "final_status": "installed",
         "dispatch_date": "2026-05-01"})
    # Review g93 c1 S2: campaigns only VINs outside the requested set carry. The model context
    # must see only the affected VINs' campaigns, so dropping either VIN filter fails.
    put("fleet_recall.recall_campaigns_received",
        {"campaign_id": "MRD-2026-005", "manufacturer": "Meridian Motors",
         "component": "Brake booster", "summary": "s", "consequence": "c", "remedy": "r",
         "do_not_drive": False, "park_outside": False, "models": ["Azimuth"],
         "model_year_min": 2024, "model_year_max": 2024, "received_date": "2026-01-01",
         "catalog_version": "1.0.0", "recall_class": "non_safety"})
    put("fleet_recall.recall_vehicle_status",
        {"campaign_id": "MRD-2026-005", "vin": _OUTSIDE, "fleet_id": "F-2",
         "match_basis": "POPULATION", "remedied": False, "rule_version": "1.0.0",
         "managed_as_of": "2026-10-19"})
    put("ota_campaigns.ota_campaigns",
        {"campaign_id": "OTA-2026-009", "campaign_name": "FW-2026-JUN-FEATURE",
         "release_version": "4.3.0", "category": "feature_add", "severity": "low",
         "status": "active"})
    put("ota_campaigns.ota_campaign_events",
        {"campaign_id": "OTA-2026-009", "vin": _OUTSIDE, "final_status": "installed",
         "dispatch_date": "2026-06-01"})


def _read(vins):
    """read_campaigns; an Athena-style column error is this test's failure, not an error."""
    try:
        return readers.read_campaigns(vins, _STAGE, athena_client=None,
                                      workgroup="wg", results_s3_path="s3://b/p/")
    except duckdb.Error as exc:
        raise AssertionError(f"a campaign query names a column its table lacks: {exc}") from exc


def test_both_shipped_queries_run_on_the_real_columns_and_reach_the_reader(lake):
    put, calls = lake
    _seed(put)
    got = _read([_V1, _V2, _V3])
    assert len(calls) == 2   # both queries ran; neither raised
    assert sorted(got["recall"], key=lambda r: r["vin"]) == [
        {"campaign_id": "MRD-2026-004", "component": "High-voltage battery contactor", "vin": _V1},
        {"campaign_id": "MRD-2026-004", "component": "High-voltage battery contactor", "vin": _V2},
    ]
    assert got["ota"] == [{"campaign_id": "OTA-2026-007", "campaign_name": "FW-2026-MAY-SAFETY",
                           "status": "active", "release_version": "4.2.1"}]


def test_the_model_context_gets_each_campaign_once_without_a_vin(lake):
    put, _ = lake
    _seed(put)
    got = _read([_V1, _V2, _V3])
    ctx = llm.investigation_context({"pattern": {"existing_recall_campaigns": got["recall"],
                                                 "existing_ota_campaigns": got["ota"]}})
    assert ctx["pattern"]["existing_recall_campaigns"] == [
        {"campaign_id": "MRD-2026-004", "campaign_name": None,
         "component": "High-voltage battery contactor", "status": None}]
    assert ctx["pattern"]["existing_ota_campaigns"] == [
        {"campaign_id": "OTA-2026-007", "campaign_name": "FW-2026-MAY-SAFETY",
         "component": None, "status": "active"}]
    blob = repr(ctx)
    for vin in (_V1, _V2, _V3):
        assert vin not in blob
