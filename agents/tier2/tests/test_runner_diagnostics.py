"""The diagnostics component: active CMS DTCs -> Findings (spec 2026-09-25-avx-own-vehicle-findings T4.2).

Stubbed-client tests. The live counterpart is T4.5 (inject a fault on staging, watch the card).
"""
from __future__ import annotations

import datetime
import pathlib
import sys
from typing import Any

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
for p in (_REPO, _REPO / "lambdas"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agents.health import cms_dtc_source as src  # noqa: E402
from agents.tier2 import runner  # noqa: E402
from agents.tier2 import writer as writer_mod  # noqa: E402
from agents.tier2.writer import compute_finding_key  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────────────────

class _Dtc:
    def __init__(self, code, first_seen_at="1790000000000"):
        self.code = code
        self.first_seen_at = first_seen_at


class _FakeCms:
    def __init__(self, active):
        self.active = active
        self.asked = []

    def get_active_dtcs(self, vin):
        self.asked.append(vin)
        return tuple(self.active.get(vin, ()))


class _FakeScanDdb:
    """Two scan pages; ``fail_on`` raises on that page index.

    ``registry`` stands in for ``cms-{stage}-storage-vehicles`` (GetItem by vehicleId):
    the source takes each Finding's VIN from it, never from a row's own ``vin``.
    """

    DEFAULT_REGISTRY = {"VEH-1": "VIN1", "VEH-2": "VIN2"}

    def __init__(self, pages, fail_on=None, registry=None):
        self.pages = pages
        self.fail_on = fail_on
        self.registry = dict(self.DEFAULT_REGISTRY if registry is None else registry)
        self.calls = []
        self.get_item_calls = []

    def scan(self, **kw):
        # A scan without ExclusiveStartKey starts over at page 0.
        i = int(kw["ExclusiveStartKey"]["k"]["S"]) + 1 if "ExclusiveStartKey" in kw else 0
        self.calls.append(kw)
        if self.fail_on == i:
            raise RuntimeError("scan page failed")
        out = {"Items": self.pages[i]}
        if i + 1 < len(self.pages):
            out["LastEvaluatedKey"] = {"k": {"S": str(i)}}
        return out

    def get_item(self, **kw):
        self.get_item_calls.append(kw)
        vin = self.registry.get(kw["Key"]["vehicleId"]["S"])
        return {"Item": {"vin": {"S": vin}}} if vin else {}

    def query(self, **kw):  # the registry's vin-index
        vin = kw["ExpressionAttributeValues"][":v"]["S"]
        return {"Items": [{"vehicleId": {"S": v}} for v, r in self.registry.items() if r == vin]}


def _row(vid, vin, code, desc="", ts="1"):
    return {"vehicleId": {"S": vid}, "vin": {"S": vin}, "code": {"S": code},
            "description": {"S": desc}, "timestamp": {"N": ts}}


def _source(active, pages=None, fail_on=None):
    pages = pages or [[_row("VEH-1", "VIN1", "P0562", "System Voltage Low"),
                       _row("VEH-1", "VIN1", "P0420", "Catalyst System Efficiency Below Threshold")],
                      [_row("VEH-2", "VIN2", "C1234", "Brake fault")]]
    ddb = _FakeScanDdb(pages, fail_on)
    return src.CmsDtcSource(dtc_table="cms-staging-storage-dtc-history",
                            vehicles_table="cms-staging-storage-vehicles", ddb_client=ddb,
                            cms_client=_FakeCms(active)), ddb


class _EmptyQueryClient:
    """No live row for any finding_key (the ADP-overlap guard's lookup)."""

    def query(self, **kw):
        return {"Items": []}


class _RecordingWriter:
    def __init__(self, fail_vins=()):
        self.calls = []
        self.fail_vins = set(fail_vins)
        self.table_name = "vsa-staging-avx-findings"
        self.dynamodb_client = _EmptyQueryClient()

    def write_finding(self, finding, *, standing=None, refresh_on_noop=False, utc_now_fn=None):
        self.calls.append({"finding": finding, "standing": standing, "refresh_on_noop": refresh_on_noop})
        if finding["scope"]["vin"] in self.fail_vins:
            raise RuntimeError("ddb down")
        return {"action": "put", "finding_id": finding["finding_id"]}

    def put(self, artifact):  # the legacy path must never be called
        raise AssertionError("diagnostics must not write the legacy health-findings table")


# ── source ─────────────────────────────────────────────────────────────────

def test_source_groups_by_component_drops_p0_and_pages_the_scan():
    source, ddb = _source({"VIN1": [_Dtc("P0562"), _Dtc("P0420")], "VIN2": [_Dtc("C1234")]})
    groups = source.fetch_groups()
    assert len(ddb.calls) == 2, "the discovery scan must follow LastEvaluatedKey"
    assert [(g.vin, g.component, [d["code"] for d in g.dtcs]) for g in groups] == [
        ("VIN1", "battery", ["P0562"]),
        ("VIN1", "diagnostics", ["P0420"]),
    ]  # VIN2's only code is P0 (C1234): no group at all
    assert groups[0].dtcs[0]["description"] == "System Voltage Low"


def test_source_only_asks_cms_which_codes_are_active():
    """A row in the table is not an active fault; get_active_dtcs decides."""
    source, _ = _source({"VIN1": [], "VIN2": []})
    assert source.fetch_groups() == []
    assert sorted(source._cms.asked) == ["VIN1", "VIN2"]


def test_source_scan_failure_raises():
    source, _ = _source({"VIN1": [_Dtc("P0562")]}, fail_on=1)
    with pytest.raises(RuntimeError):
        source.fetch_groups()


def test_fields_never_carry_p0_severity_and_quote_the_cms_description():
    source, _ = _source({"VIN1": [_Dtc("P0562"), _Dtc("P0420")]})
    for g in source.fetch_groups():
        f = g.agent_fields()
        assert f["severity_hint"] in {"routine", "soon", "attention"}
        assert f["headline"] and f["headline"] != f["finding"]
        assert all(e["provenance"] == "live" and e["value"]["status"] == "ACTIVE" for e in f["evidence"])
    battery = source.fetch_groups()[0].agent_fields()
    assert "System Voltage Low" in battery["finding"] and "P0562" in battery["finding"]


# ── run_diagnostics ────────────────────────────────────────────────────────

def _parse(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_run_writes_one_finding_per_group_with_kind_ttl_and_keepalive():
    source, _ = _source({"VIN1": [_Dtc("P0562"), _Dtc("P0420")]})
    w = _RecordingWriter()
    summary = runner.run_diagnostics("staging", source=source, avx_writer=w)
    kinds = sorted(c["finding"]["finding_kind"] for c in w.calls)
    assert kinds == ["health.battery", "health.diagnostics"]
    for c in w.calls:
        f = c["finding"]
        assert c["refresh_on_noop"] is True
        assert (_parse(f["expires_at"]) - _parse(f["computed_at"])).total_seconds() == src.DIAGNOSTICS_TTL_SECONDS
        assert f["headline"] != f["detail"]
    assert summary["vins_selected"] == 1
    assert summary["artifacts_written"] == 2


def test_run_hash_is_the_code_set_not_the_timestamps():
    a, _ = _source({"VIN1": [_Dtc("P0420", "1")]})
    b, _ = _source({"VIN1": [_Dtc("P0420", "999999")]})
    wa, wb = _RecordingWriter(), _RecordingWriter()
    runner.run_diagnostics("staging", source=a, avx_writer=wa)
    runner.run_diagnostics("staging", source=b, avx_writer=wb)
    assert wa.calls[0]["finding"]["inputs_hash"] == wb.calls[0]["finding"]["inputs_hash"]


def test_run_keeps_going_after_a_failed_write_then_raises():
    source, _ = _source({"VIN1": [_Dtc("P0420")], "VIN2": [_Dtc("P0299")]})
    w = _RecordingWriter(fail_vins={"VIN1"})
    with pytest.raises(runner.DiagnosticsRunError):
        runner.run_diagnostics("staging", source=source, avx_writer=w)
    assert {c["finding"]["scope"]["vin"] for c in w.calls} == {"VIN1", "VIN2"}


def test_lambda_handler_dispatches_diagnostics(monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "_run_diagnostics_from_env", lambda stage: seen.setdefault("stage", stage) or {"ok": 1})
    runner.lambda_handler({"stage": "staging", "component": "diagnostics"}, None)
    assert seen == {"stage": "staging"}


# ── writer keep-alive ──────────────────────────────────────────────────────

class _Client:
    def __init__(self, live, fail_code=None):
        self.live = live
        self.updates = []
        self.fail_code = fail_code

    def query(self, **kw):
        return {"Items": [writer_mod._to_ddb_item(self.live)] if self.live else []}

    def update_item(self, **kw):
        self.updates.append(kw)
        if self.fail_code:
            err = Exception("cond")
            err.response = {"Error": {"Code": self.fail_code}}
            raise err


def _finding(**over):
    f = {"finding_key": "k", "finding_id": "FIND-new", "agent_id": "agent-2-vehicle-health",
         "scope": {"vin": "VIN1"}, "finding_kind": "health.diagnostics", "inputs_hash": "h1",
         "expires_at": "2026-09-27T01:30:00Z", "computed_at": "2026-09-27T01:00:00Z",
         "headline": "h", "detail": "d", "evidence": [], "agent_version": "0.1.0",
         "audit_trail": []}
    f.update(over)
    return f


def _live():
    return {"finding_id": "FIND-live", "inputs_hash": "h1", "expires_at": "2026-09-27T01:20:00Z"}


def test_refresh_on_noop_extends_expiry_without_an_audit_entry():
    c = _Client(_live())
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   refresh_on_noop=True, utc_now_fn=lambda: "2026-09-27T01:00:00Z")
    assert out == {"action": "refresh", "finding_id": "FIND-live"}
    (u,) = c.updates
    assert u["Key"] == {"finding_id": {"S": "FIND-live"}}
    assert "audit_trail" not in u["UpdateExpression"]
    assert "expires_at < :e" in u["ConditionExpression"]
    assert u["ExpressionAttributeValues"][":e"] == {"S": "2026-09-27T01:30:00Z"}


def test_default_noop_does_not_write():
    c = _Client(_live())
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   utc_now_fn=lambda: "2026-09-27T01:00:00Z")
    assert out["action"] == "noop" and c.updates == []


def test_refresh_never_shortens_a_longer_lived_row():
    c = _Client(_live(), fail_code="ConditionalCheckFailedException")
    out = writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                   refresh_on_noop=True, utc_now_fn=lambda: "2026-09-27T01:00:00Z")
    assert out["action"] == "noop"


def test_refresh_other_errors_propagate():
    c = _Client(_live(), fail_code="ProvisionedThroughputExceededException")
    with pytest.raises(Exception):
        writer_mod.write_finding(_finding(), table_name="t", dynamodb_client=c,
                                 refresh_on_noop=True, utc_now_fn=lambda: "2026-09-27T01:00:00Z")


# ── C1: _query_live_row must paginate past expired rows ────────────────────

class _LimitBeforeFilterClient:
    """Simulates DynamoDB applying Limit before FilterExpression.

    Page 0: an expired row (Limit=1 would stop here and return Items=[]).
    Page 1: the live row.
    """

    def __init__(self, expired_row: dict[str, Any], live_row: dict[str, Any]) -> None:
        self._pages = [
            # page 0: the expired row's DDB wire format; FilterExpression applied here
            # returns empty Items because expires_at <= now.  In a real stub that
            # honours FilterExpression this would be Items=[]; to model limit-before-filter
            # we return Items=[] with a LastEvaluatedKey so the caller must ask page 1.
            {"Items": [], "LastEvaluatedKey": {"finding_key": {"S": "k"}, "finding_id": {"S": "EXPIRED"}}},
            # page 1: the live row passes the filter.
            {"Items": [writer_mod._to_ddb_item(live_row)]},
        ]
        self.calls: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        idx = 1 if "ExclusiveStartKey" in kw else 0
        return self._pages[idx]

    def update_item(self, **kw: Any) -> None:
        pass


def test_query_live_row_follows_last_evaluated_key_past_expired_rows():
    """C1 regression: Limit-before-filter must not stop at an expired-only page."""
    expired = {"finding_id": "EXPIRED", "inputs_hash": "old", "expires_at": "2020-01-01T00:00:00Z"}
    live = {"finding_id": "FIND-live", "inputs_hash": "h1", "expires_at": "2099-01-01T00:00:00Z"}

    c = _LimitBeforeFilterClient(expired_row=expired, live_row=live)
    result = writer_mod._query_live_row(
        finding_key="k",
        table_name="t",
        dynamodb_client=c,
        now_str="2026-09-27T01:00:00Z",
    )
    assert result is not None, "_query_live_row must not stop at an empty page when LastEvaluatedKey is set"
    assert result["finding_id"] == "FIND-live"
    assert len(c.calls) == 2, "_query_live_row must issue two queries (page 0 then page 1)"


def test_query_live_row_returns_none_when_all_pages_empty():
    """No live row on any page → None."""

    class _AllEmptyClient:
        def __init__(self) -> None:
            self.calls: list = []

        def query(self, **kw: Any) -> dict[str, Any]:
            self.calls.append(kw)
            # Two empty pages then done.
            if "ExclusiveStartKey" not in kw:
                return {"Items": [], "LastEvaluatedKey": {"finding_key": {"S": "k"}, "finding_id": {"S": "X"}}}
            return {"Items": []}

    c = _AllEmptyClient()
    result = writer_mod._query_live_row(
        finding_key="k",
        table_name="t",
        dynamodb_client=c,
        now_str="2026-09-27T01:00:00Z",
    )
    assert result is None
    assert len(c.calls) == 2


# ── W3: ADP overlap redirect ───────────────────────────────────────────────

class _QueryWriter:
    """A writer stub that pre-seeds live rows for the overlap detection query."""

    def __init__(self, live_rows: dict[str, dict[str, Any]]) -> None:
        """*live_rows*: finding_key -> plain-Python row dict with 'evidence' list."""
        self._live_rows = live_rows
        self.written: list[dict[str, Any]] = []

    # runner.py reads these properties to call _query_live_row
    @property
    def table_name(self) -> str:
        return "t"

    @property
    def dynamodb_client(self) -> "_QueryClient":
        return _QueryClient(self._live_rows)

    def write_finding(
        self, finding: dict[str, Any], *, standing: Any = None,
        refresh_on_noop: bool = False, utc_now_fn: Any = None
    ) -> dict[str, Any]:
        self.written.append({"finding": finding})
        return {"action": "put", "finding_id": finding["finding_id"]}

    def put(self, artifact: Any) -> None:  # legacy path must not be called
        raise AssertionError("diagnostics must not use the legacy put() path")


class _QueryClient:
    """DDB client that returns a pre-seeded live row for a known finding_key, nothing for others."""

    def __init__(self, live_rows: dict[str, dict[str, Any]]) -> None:
        self._live = live_rows  # finding_key -> plain-Python row

    def query(self, **kw: Any) -> dict[str, Any]:
        fk_val = kw.get("ExpressionAttributeValues", {}).get(":fk", {}).get("S")
        row = self._live.get(fk_val)
        if row:
            return {"Items": [writer_mod._to_ddb_item(row)]}
        return {"Items": []}

    def update_item(self, **kw: Any) -> None:
        pass


def _adp_finding_key(vin: str, component: str) -> str:
    return writer_mod.compute_finding_key(
        agent_id="agent-2-vehicle-health",
        scope_field="vin",
        scope_value=vin,
        finding_kind=f"health.{component}",
    )


def test_adp_overlap_redirects_battery_codes_to_diagnostics():
    """W3: a battery group whose finding_key has a live ADP row must not overwrite it;
    its codes become a health.diagnostics Finding instead."""
    # ADP has an existing live battery Finding for VIN1 (no cms.dtc.active evidence).
    adp_row = {
        "finding_id": "FIND-ADP-battery",
        "inputs_hash": "adp-hash",
        "expires_at": "2099-01-01T00:00:00Z",
        "evidence": [{"kind": "adp.tire_health", "source": "adp"}],  # NOT cms.dtc.active
    }
    fk = _adp_finding_key("VIN1", "battery")
    w = _QueryWriter(live_rows={fk: adp_row})

    source, _ = _source({"VIN1": [_Dtc("P0562")]})  # P0562 → battery component
    summary = runner.run_diagnostics("staging", source=source, avx_writer=w)

    # The write must go to health.diagnostics, not health.battery.
    assert len(w.written) == 1
    written_kind = w.written[0]["finding"]["finding_kind"]
    assert written_kind == "health.diagnostics", (
        f"expected health.diagnostics (ADP overlap redirect), got {written_kind!r}"
    )
    assert summary["artifacts_written"] == 1


def test_no_adp_overlap_battery_codes_write_normally():
    """W3: when no live ADP row exists for the finding_key, the battery group writes normally."""
    # No pre-existing live rows.
    w = _QueryWriter(live_rows={})

    source, _ = _source({"VIN1": [_Dtc("P0562")]})
    runner.run_diagnostics("staging", source=source, avx_writer=w)

    # Should write health.battery directly.
    assert len(w.written) == 1
    written_kind = w.written[0]["finding"]["finding_kind"]
    assert written_kind == "health.battery", (
        f"expected health.battery (no overlap), got {written_kind!r}"
    )


def test_adp_overlap_redirect_merges_into_existing_diagnostics_group():
    """W3: redirected codes merge with an existing diagnostics group for the same VIN."""
    # VIN1 has P0562 (battery, ADP overlap) AND P0420 (diagnostics).
    adp_row = {
        "finding_id": "FIND-ADP-battery",
        "inputs_hash": "adp-hash",
        "expires_at": "2099-01-01T00:00:00Z",
        "evidence": [{"kind": "adp.tire_health", "source": "adp"}],
    }
    fk = _adp_finding_key("VIN1", "battery")
    w = _QueryWriter(live_rows={fk: adp_row})

    source, _ = _source({"VIN1": [_Dtc("P0562"), _Dtc("P0420")]})
    runner.run_diagnostics("staging", source=source, avx_writer=w)

    # P0562 redirected to diagnostics merges with P0420's diagnostics group → 1 write.
    assert len(w.written) == 1
    f = w.written[0]["finding"]
    assert f["finding_kind"] == "health.diagnostics"
    # Both codes must appear in the evidence.
    evidence_codes = {e["value"]["code"] for e in f.get("evidence", [])}
    assert "P0562" in evidence_codes
    assert "P0420" in evidence_codes


def test_adp_overlap_does_not_redirect_when_existing_row_is_diagnostics():
    """W3: a live row with cms.dtc.active evidence is from diagnostics; do NOT redirect."""
    # Live row already has cms.dtc.active evidence → written by diagnostics.
    diag_row = {
        "finding_id": "FIND-diag-battery",
        "inputs_hash": "diag-hash",
        "expires_at": "2099-01-01T00:00:00Z",
        "evidence": [{"kind": "cms.dtc.active", "source": "cms.dtc_history"}],
    }
    fk = _adp_finding_key("VIN1", "battery")
    w = _QueryWriter(live_rows={fk: diag_row})

    source, _ = _source({"VIN1": [_Dtc("P0562")]})
    runner.run_diagnostics("staging", source=source, avx_writer=w)

    # Must write health.battery (keep-alive of our own row), not redirect.
    assert len(w.written) == 1
    assert w.written[0]["finding"]["finding_kind"] == "health.battery"



# ── Review g4-diag cycle 2: failures through the REAL client, and a guard that cannot skip ──

class _PagedTable:
    """A dtc-history partition: page 1 returns one ACTIVE row, page 2 raises."""

    def __init__(self, fail_page2):
        self.fail_page2 = fail_page2

    def query(self, **kw):
        if "ExclusiveStartKey" in kw:
            if self.fail_page2:
                raise RuntimeError("ProvisionedThroughputExceeded")
            return {"Items": []}
        return {"Items": [{"code": "P0420", "status": "ACTIVE", "timestamp": 1}],
                "LastEvaluatedKey": {"k": 1}}


class _Resource:
    def __init__(self, fail_page2):
        self.table = _PagedTable(fail_page2)

    def Table(self, name):  # noqa: N802
        return self.table


def _real_client_source(fail_page2):
    pages = [[_row("VEH-1", "VIN1", "P0420", "Catalyst")]]
    return src.CmsDtcSource(dtc_table="cms-staging-storage-dtc-history",
                            vehicles_table="cms-staging-storage-vehicles",
                            ddb_client=_FakeScanDdb(pages), ddb_resource=_Resource(fail_page2))


def test_real_client_partial_read_is_a_failure_not_a_shorter_list():
    """RealCmsClient.get_active_dtcs swallows the page-2 error; the source must not."""
    with pytest.raises(src.DiagnosticsReadError) as exc:
        _real_client_source(fail_page2=True).fetch_groups()
    assert exc.value.failed_vins == {"VIN1"}


def test_real_client_clean_read_groups_the_active_code():
    groups = _real_client_source(fail_page2=False).fetch_groups()
    assert [(g.vin, g.component, [d["code"] for d in g.dtcs]) for g in groups] == [
        ("VIN1", "diagnostics", ["P0420"])]


def test_overlap_guard_without_a_table_fails_the_group():
    source, _ = _source({"VIN1": [_Dtc("P0562")]})
    w = _RecordingWriter()
    del w.dynamodb_client
    with pytest.raises((TypeError, runner.DiagnosticsRunError)):
        runner.run_diagnostics("staging", source=source, avx_writer=w)
    assert w.calls == [], "no Finding is written when the overlap guard cannot run"
