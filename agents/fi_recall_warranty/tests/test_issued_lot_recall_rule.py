"""An issued lot recall's evidence names its lot rule (FI stage D 9.3, flag 5).

Since stage D 9.1 an issued hardware recall whose cause is a supplier lot reaches AVX
through ADP's received view with "every model, model years 0-9999" (ADP
``engineering/interface.md`` § 12: ``model_year_min`` is ``0`` and ``model_year_max``
``9999`` unless the cause is a model year), and its real rule, ``component_lots``
(``<supplier_id>/<supplier_lot>``), is not in that view at all. So ``recall.rule``
would tell an operator, and the fleet-manager agent narrating it, that the recall
covers every model year 0 to 9999, and never name the lot.

Pinned here:
  1. the recall query reads the lots from ``adp_{stage}_engineering.recall_campaigns_issued``
     for issued rows only, never for a Stage C campaign (ADP's view rule: an issued row
     reusing a Stage C id never narrows it), run in DuckDB over tables built from the
     producers' real columns;
  2. the reader turns Athena's ``array<string>`` text into a list;
  3. ``recall.rule`` names each lot as ``{supplier_id, supplier_lot}`` and writes the
     "no bound" year sentinels 0 / 9999 as null;
  4. a Stage C campaign's ``recall.rule`` and ``inputs_hash`` are byte-identical to
     before 9.3 (no re-plan, no model calls on deploy day).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from agents.fi_recall_warranty import selection
from agents.fi_recall_warranty.evidence import build_recall_evidence, compute_inputs_hash

_SQL = (Path(__file__).resolve().parents[1] / "sql" / "select_recalls.sql").read_text()
_STAGE = "test"

# ── Producer columns (read 2026-10-20) ──────────────────────────────────────
# recall_vehicle_status, recall_campaigns_received: live Glue, adp_staging_fleet_recall.
# recall_campaigns: ADP recall_campaigns/interface.md § 1 (the received view's columns).
# recall_campaigns_issued: ADP digital_thread_writer/handler.py ISSUED_RECALL_COLUMNS
# (ADP 9bb82e96; the Glue table is pinned to it).
_RVS = [("campaign_id", "VARCHAR"), ("vin", "VARCHAR"), ("fleet_id", "VARCHAR"),
        ("match_basis", "VARCHAR"), ("confirming_code", "VARCHAR"),
        ("confirming_seen_on", "DATE"), ("confirming_source", "VARCHAR"),
        ("remedied", "BOOLEAN"), ("remedy_ref", "VARCHAR"), ("remedied_on", "DATE"),
        ("rule_version", "VARCHAR"), ("managed_as_of", "DATE")]
_CATALOG = [("campaign_id", "VARCHAR"), ("manufacturer", "VARCHAR"), ("component", "VARCHAR"),
            ("summary", "VARCHAR"), ("consequence", "VARCHAR"), ("remedy", "VARCHAR"),
            ("do_not_drive", "BOOLEAN"), ("park_outside", "BOOLEAN"), ("models", "VARCHAR[]"),
            ("model_year_min", "INTEGER"), ("model_year_max", "INTEGER"), ("plants", "VARCHAR[]"),
            ("build_from", "DATE"), ("build_to", "DATE"), ("fault_codes", "VARCHAR[]"),
            ("signature_lookback_days", "INTEGER"), ("remedy_labor_op", "VARCHAR"),
            ("remedy_part_numbers", "VARCHAR[]"), ("remedy_flat_rate_hours", "DECIMAL(5,2)"),
            ("remedy_available_from", "DATE"), ("received_date", "DATE"),
            ("catalog_version", "VARCHAR"), ("recall_class", "VARCHAR")]
_ISSUED = [("campaign_id", "VARCHAR"), ("thread_id", "VARCHAR"), ("status", "VARCHAR"),
           ("manufacturer", "VARCHAR"), ("recall_class", "VARCHAR"), ("component", "VARCHAR"),
           ("summary", "VARCHAR"), ("consequence", "VARCHAR"), ("remedy", "VARCHAR"),
           ("do_not_drive", "BOOLEAN"), ("park_outside", "BOOLEAN"), ("models", "VARCHAR[]"),
           ("model_year_min", "INTEGER"), ("model_year_max", "INTEGER"), ("plants", "VARCHAR[]"),
           ("build_from", "DATE"), ("build_to", "DATE"), ("component_lots", "VARCHAR[]"),
           ("fault_codes", "VARCHAR[]"), ("signature_lookback_days", "INTEGER"),
           ("remedy_labor_op", "VARCHAR"), ("remedy_part_numbers", "VARCHAR[]"),
           ("remedy_flat_rate_hours", "DECIMAL(5,2)"), ("remedy_available_from", "DATE"),
           ("received_date", "DATE"), ("catalog_version", "VARCHAR"), ("created_at", "VARCHAR"),
           ("issued_at", "VARCHAR"), ("affected_vins_count", "INTEGER")]

_ALL_MODELS = ["Azimuth", "Meridian", "Windrose"]
_LOT = "SUP-B/OBC-L01-B"


def _create(conn, table: str, cols: list[tuple[str, str]]) -> None:
    conn.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in cols)})")


def _insert(conn, table: str, cols: list[tuple[str, str]], row: dict) -> None:
    unknown = set(row) - {c for c, _ in cols}
    assert not unknown, f"fixture names columns {table} does not have: {unknown}"
    names = [c for c, _ in cols]
    conn.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in names)})",
                 [row.get(c) for c in names])


def _catalog_row(cid: str, **kw) -> dict:
    base = {"campaign_id": cid, "manufacturer": "Meridian Motors", "component": "Onboard charger",
            "summary": "s", "consequence": "c", "remedy": "r", "do_not_drive": False,
            "park_outside": False, "models": ["Azimuth"], "model_year_min": 2024,
            "model_year_max": 2024, "received_date": "2026-01-01", "catalog_version": "1.0.0",
            "recall_class": "non_safety"}
    base.update(kw)
    return base


def _issued_row(cid: str, *, lots, status: str = "issued") -> dict:
    return {**_catalog_row(cid, models=_ALL_MODELS, model_year_min=0, model_year_max=9999,
                           catalog_version="1.1.0"),
            "thread_id": f"T-{cid}", "status": status, "component_lots": lots,
            "created_at": "2026-10-20T00:00:00Z", "issued_at": "2026-10-20T01:00:00Z",
            "affected_vins_count": 3}


def _rvs_row(cid: str, vin: str, **kw) -> dict:
    base = {"campaign_id": cid, "vin": vin, "fleet_id": "F-1", "match_basis": "POPULATION",
            "remedied": False, "rule_version": "1.0.0", "managed_as_of": "2026-10-19"}
    base.update(kw)
    return base


def _run(fixture) -> list[dict]:
    """Build the producers' tables, apply *fixture*, run the shipped SQL, return rows."""
    conn = duckdb.connect()
    fr, eng = f"adp_{_STAGE}_fleet_recall", f"adp_{_STAGE}_engineering"
    conn.execute(f"CREATE SCHEMA {fr}")
    conn.execute(f"CREATE SCHEMA {eng}")
    t = {"rvs": (f"{fr}.recall_vehicle_status", _RVS),
         "rcr": (f"{fr}.recall_campaigns_received", _CATALOG),
         "rc": (f"{fr}.recall_campaigns", _CATALOG),
         "ri": (f"{eng}.recall_campaigns_issued", _ISSUED)}
    for name, cols in t.values():
        _create(conn, name, cols)
    for key, row in fixture:
        _insert(conn, *t[key], row)
    res = conn.execute(_SQL.replace("${stage}", _STAGE))
    cols = [d[0] for d in res.description]
    return [dict(zip(cols, r)) for r in res.fetchall()]


def _standard_fixture():
    stage_c = _catalog_row("MRD-2026-003")
    issued_lot = _issued_row("MRD-2026-501", lots=[_LOT, "SUP-C/OBC-L02-C"])
    issued_model = _issued_row("MRD-2026-502", lots=None)
    # An issued row that reuses Stage C's id: ADP's view keeps Stage C's row and never
    # narrows it, so its lots must not reach Stage C's evidence either.
    clash = _issued_row("MRD-2026-003", lots=["SUP-X/LOT-X"])
    return [
        ("rc", stage_c), ("rcr", stage_c),
        ("rcr", {k: v for k, v in issued_lot.items() if k in dict(_CATALOG)}),
        ("rcr", {k: v for k, v in issued_model.items() if k in dict(_CATALOG)}),
        ("ri", issued_lot), ("ri", issued_model), ("ri", clash),
        ("rvs", _rvs_row("MRD-2026-003", "VIN-C")),
        ("rvs", _rvs_row("MRD-2026-501", "VIN-L", rule_version="1.1.0")),
        ("rvs", _rvs_row("MRD-2026-502", "VIN-M", rule_version="1.1.0")),
        ("rvs", _rvs_row("MRD-2026-501", "VIN-R", remedied=True)),
    ]


class TestRecallQueryReadsTheLots:
    def test_issued_lot_rows_carry_their_lots_and_nothing_else_does(self):
        rows = _run(_standard_fixture())
        got = [(r["campaign_id"], r["vin"], r.get("component_lots")) for r in rows]
        assert got == [
            ("MRD-2026-003", "VIN-C", None),
            ("MRD-2026-501", "VIN-L", [_LOT, "SUP-C/OBC-L02-C"]),
            ("MRD-2026-502", "VIN-M", None),
        ]

    def test_a_draft_row_never_supplies_lots(self):
        fx = [(k, r) for k, r in _standard_fixture() if not (k == "ri" and r["campaign_id"] == "MRD-2026-501")]
        fx.append(("ri", _issued_row("MRD-2026-501", lots=[_LOT], status="draft")))
        rows = {(r["campaign_id"], r["vin"]): r for r in _run(fx)}
        assert rows[("MRD-2026-501", "VIN-L")]["component_lots"] is None

    def test_a_null_catalog_id_doesnt_strip_every_issued_recalls_lots(self):
        # Review g93 c1 S3: without the subquery's `campaign_id IS NOT NULL`, one NULL id in
        # Stage C's catalog turns NOT IN into NULL for every row and every lot recall loses
        # its lots (flag 5 again). Stage C's catalog key is never NULL today; this pins it.
        fx = _standard_fixture() + [("rc", _catalog_row(None))]
        rows = {(r["campaign_id"], r["vin"]): r for r in _run(fx)}
        assert rows[("MRD-2026-501", "VIN-L")].get("component_lots") == [_LOT, "SUP-C/OBC-L02-C"]

    def test_one_row_per_campaign_and_vin_and_stage_c_columns_unchanged(self):
        rows = _run(_standard_fixture())
        assert len(rows) == len({(r["campaign_id"], r["vin"]) for r in rows}) == 3
        stage_c = next(r for r in rows if r["campaign_id"] == "MRD-2026-003")
        assert stage_c["model_year_min"] == 2024 and stage_c["models"] == ["Azimuth"]


class TestReaderParsesTheLots:
    @pytest.mark.parametrize("raw, want", [
        ("[SUP-B/OBC-L01-B, SUP-C/OBC-L02-C]", ["SUP-B/OBC-L01-B", "SUP-C/OBC-L02-C"]),
        ("[SUP-B/OBC-L01-B]", ["SUP-B/OBC-L01-B"]),
        ("[]", []),
        (None, []),
    ])
    def test_component_lots_athena_text_becomes_a_list(self, raw, want):
        row = {"campaign_id": "MRD-2026-501", "vin": "V", "do_not_drive": "false",
               "remedy_part_numbers": None, "component_lots": raw}
        rows, refused = selection.select_unremedied_recalls(
            "staging", athena_client=None, workgroup="wg", results_s3_path="s3://b/p/",
            _run_query=lambda *a, **k: [row])
        assert refused == 0
        assert rows[0].get("component_lots") == want


_BASE_ROW = {
    "vin": "MRDVEH00000000015", "campaign_id": "MRD-2026-003", "recall_class": "non_safety",
    "do_not_drive": False, "park_outside": False, "received_date": "2026-01-01",
    "catalog_version": "1.0.0", "match_basis": "POPULATION", "rule_version": "1.0.0",
    "models": "[Azimuth]", "model_year_min": "2024", "model_year_max": "2024",
    "plants": None, "build_from": None, "build_to": None,
}


def _rule(row: dict) -> dict:
    ev = build_recall_evidence(row=row, item="book", attempt=1, rooftops=[], origin=None,
                               open_item_ids=[], diagnosis_source="rules")
    (rule,) = [e["recall.rule"] for e in ev if "recall.rule" in e]
    return rule


class TestRecallRuleNamesTheLot:
    def test_issued_lot_recall_rule(self):
        row = {**_BASE_ROW, "campaign_id": "MRD-2026-501", "rule_version": "1.1.0",
               "models": "[Azimuth, Meridian, Windrose]", "model_year_min": "0",
               "model_year_max": "9999", "component_lots": [_LOT, "SUP-C/OBC-L02-C"]}
        assert _rule(row) == {
            "rule_version": "1.1.0",
            "models": "[Azimuth, Meridian, Windrose]",
            "model_year_min": None,
            "model_year_max": None,
            "plants": None,
            "build_from": None,
            "build_to": None,
            "component_lots": [
                {"supplier_id": "SUP-B", "supplier_lot": "OBC-L01-B"},
                {"supplier_id": "SUP-C", "supplier_lot": "OBC-L02-C"},
            ],
        }

    def test_issued_one_lot_recall_rule(self):
        # The shape ADP's writer actually writes: exactly one lot, [cause_value]
        # (handler.py recall_population; review g93 c1 S6).
        row = {**_BASE_ROW, "campaign_id": "MRD-2026-501", "rule_version": "1.1.0",
               "models": "[Azimuth, Meridian, Windrose]", "model_year_min": "0",
               "model_year_max": "9999", "component_lots": [_LOT]}
        rule = _rule(row)
        assert rule.get("component_lots") == [{"supplier_id": "SUP-B", "supplier_lot": "OBC-L01-B"}]
        assert (rule["model_year_min"], rule["model_year_max"]) == (None, None)

    @pytest.mark.parametrize("lo, hi, want_lo, want_hi", [
        ("0", "9999", None, None),
        (0, 9999, None, None),
        ("2024", "2024", "2024", "2024"),
        ("1", "9998", "1", "9998"),
        ("0", "2024", None, "2024"),
        ("2024", "9999", "2024", None),
    ])
    def test_only_the_unbounded_sentinels_become_null(self, lo, hi, want_lo, want_hi):
        rule = _rule({**_BASE_ROW, "model_year_min": lo, "model_year_max": hi})
        assert (rule["model_year_min"], rule["model_year_max"]) == (want_lo, want_hi)

    @pytest.mark.parametrize("lots", [None, [], "absent"])
    def test_no_lots_no_component_lots_key(self, lots):
        row = dict(_BASE_ROW) if lots == "absent" else {**_BASE_ROW, "component_lots": lots}
        assert "component_lots" not in _rule(row)


class TestStageCUnchanged:
    def test_stage_c_rule_and_hash_are_what_they_were_before_9_3(self):
        # Literal recall.rule and inputs_hash computed at AVX 6e760f6a (before 9.3) for this
        # row; a Stage C Finding must not re-plan on the deploy day.
        row = {**_BASE_ROW, "component_lots": []}
        assert _rule(row) == {
            "rule_version": "1.0.0", "models": "[Azimuth]", "model_year_min": "2024",
            "model_year_max": "2024", "plants": None, "build_from": None, "build_to": None,
        }
        ev = build_recall_evidence(row=row, item="book", attempt=1, rooftops=[], origin=None,
                                   open_item_ids=[], diagnosis_source="rules")
        assert compute_inputs_hash(ev) == _PRE_9_3_HASH


#: compute_inputs_hash of the evidence above at AVX 6e760f6a (build_recall_evidence ignores
#: the row's component_lots there). Recorded by the architect before the change.
_PRE_9_3_HASH = "c79c85c9ed63f8a1d3643a9d2defa4287b98c605450c86a43327fc2e4d17fb1e"
