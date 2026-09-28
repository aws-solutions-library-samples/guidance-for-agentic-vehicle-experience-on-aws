"""Red tests for the CMS active-DTC health source (spec 2026-09-25-avx-own-vehicle-findings, T1.3).

The diagnostics source turns a vehicle's active CMS DTCs into health Findings. P0 belongs to
the deterministic triage classifier and the escalation card, so a code the classifier rates
P0 must never become a Finding: a Finding restating it could soften "stop driving".

xfail(strict=True) until T4.2 lands (the module does not exist yet; imports are inside the
tests so the file still collects). Strict mode makes T4.2 delete the marker, and
``raises=ModuleNotFoundError`` means only the missing module counts as the expected failure:
a helper error in this file fails the run instead of passing as red (review Cycle 1 S8).

``ActiveDtc`` carries ``first_seen_at`` and no ``lastSeenAt``, so the Accept's "stable across
lastSeenAt" holds by construction; the hash tests vary ``first_seen_at``, the only timestamp
the type has.

Interface T4.2 must provide in ``agents.health.cms_dtc_source``:
  component_for_code(code: str) -> str | None   # "battery"|"brakes"|"tires"|"diagnostics", None for P0
  inputs_hash_for(active_dtcs) -> str           # over the sorted set of codes only
  DIAGNOSTICS_TTL_SECONDS == 1800
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "lambdas") not in sys.path:
    sys.path.insert(0, str(_REPO / "lambdas"))

pytestmark: list = []  # was xfail(strict=True, raises=ModuleNotFoundError) until T4.2


def _classifier():
    """Load lambdas/triage-classifier as the `triage_classifier` package (hyphenated dir)."""
    if "triage_classifier" not in sys.modules:
        root = _REPO / "lambdas" / "triage-classifier"
        spec = importlib.util.spec_from_file_location(
            "triage_classifier", root / "__init__.py", submodule_search_locations=[str(root)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["triage_classifier"] = pkg
        spec.loader.exec_module(pkg)
    from triage_classifier import classifier
    from triage_classifier.types import ActiveDtc, Level
    return classifier, ActiveDtc, Level


def _classifier_p0_codes():
    classifier, ActiveDtc, Level = _classifier()
    out = set()
    for code in classifier._SEVERITY_TABLE:
        levels = [lv for lv, _ in classifier._classify_dtcs((ActiveDtc(code=code, first_seen_at="t"),))]
        if Level.P0 in levels:
            out.add(code)
    assert out, "the classifier table rates no code P0; this test would be vacuous"
    return out


def _dtc(code, first_seen="1790000000000"):
    """The ActiveDtc type `CmsClientReal.get_active_dtcs` returns (triage_classifier.types)."""
    _, ActiveDtc, _ = _classifier()
    return ActiveDtc(code=code, first_seen_at=first_seen)


def test_every_code_the_classifier_rates_p0_produces_no_finding():
    from agents.health.cms_dtc_source import component_for_code
    for code in sorted(_classifier_p0_codes()):
        assert component_for_code(code) is None, code


@pytest.mark.parametrize("code,component", [
    ("P0562", "battery"),       # system voltage low
    ("P0299", "diagnostics"),   # turbo underboost
    ("P0420", "diagnostics"),   # catalyst efficiency
    ("P0001", "diagnostics"),   # fuel volume regulator
    ("B1234", "diagnostics"),
])
def test_non_p0_codes_map_to_a_component(code, component):
    from agents.health.cms_dtc_source import component_for_code
    assert component_for_code(code) == component


def test_inputs_hash_ignores_timestamps_and_order():
    from agents.health.cms_dtc_source import inputs_hash_for
    a = inputs_hash_for([_dtc("P0562", "1"), _dtc("P0420", "2")])
    b = inputs_hash_for([_dtc("P0420", "999"), _dtc("P0562", "888")])
    assert a == b


def test_inputs_hash_changes_when_the_code_set_changes():
    from agents.health.cms_dtc_source import inputs_hash_for
    assert inputs_hash_for([_dtc("P0562")]) != inputs_hash_for([_dtc("P0562"), _dtc("P0420")])


def test_diagnostics_findings_expire_thirty_minutes_after_their_last_write():
    from agents.health.cms_dtc_source import DIAGNOSTICS_TTL_SECONDS
    assert DIAGNOSTICS_TTL_SECONDS == 30 * 60


# ── W4: VIN→vehicleId cache seeding and per-VIN failure detection ─────────

class _DtcW4:
    """Minimal ActiveDtc-shaped stub for W4 tests in this file."""
    def __init__(self, code: str) -> None:
        self.code = code
        self.first_seen_at = "0"

class _VinCacheClient:
    """A minimal cms_client that records vin-index query calls and supports injected failures."""

    def __init__(self, active: dict[str, list], fail_vins: set[str] | None = None) -> None:
        self.active = active
        self.fail_vins = fail_vins or set()
        self.asked: list[str] = []
        # Simulates RealCmsClient._vin_to_vehicle_id attribute.
        self._vin_to_vehicle_id: dict[str, str] | None = None

    def get_active_dtcs(self, vin: str) -> tuple:
        self.asked.append(vin)
        if vin in self.fail_vins:
            raise RuntimeError(f"DDB partition query failed for {vin}")
        _, ActiveDtc, _ = _classifier()
        return tuple(ActiveDtc(code=d.code, first_seen_at="0") for d in self.active.get(vin, []))


def _dtc_source_with_cache_client(active: dict, fail_vins: set | None = None):
    """Build a CmsDtcSource with a _VinCacheClient that records cache seeding."""
    from agents.health.cms_dtc_source import CmsDtcSource
    pages = [[
        {"vehicleId": {"S": "VEH-1"}, "vin": {"S": "VIN1"}, "code": {"S": "P0562"},
         "description": {"S": "desc"}, "timestamp": {"N": "1"}},
        {"vehicleId": {"S": "VEH-2"}, "vin": {"S": "VIN2"}, "code": {"S": "P0299"},
         "description": {"S": "desc"}, "timestamp": {"N": "1"}},
    ]]

    class _SimpleScanDdb:
        REGISTRY = {"VEH-1": "VIN1", "VEH-2": "VIN2"}

        def scan(self, **kw):
            return {"Items": pages[0]}

        def get_item(self, **kw):
            vin = self.REGISTRY.get(kw["Key"]["vehicleId"]["S"])
            return {"Item": {"vin": {"S": vin}}} if vin else {}

        def query(self, **kw):  # the registry's vin-index
            vin = kw["ExpressionAttributeValues"][":v"]["S"]
            return {"Items": [{"vehicleId": {"S": v}} for v, r in self.REGISTRY.items() if r == vin]}

    cms = _VinCacheClient(active=active, fail_vins=fail_vins)
    return CmsDtcSource(
        dtc_table="t", vehicles_table="v", ddb_client=_SimpleScanDdb(), cms_client=cms
    ), cms


def test_fetch_groups_seeds_vin_cache_from_scan():
    """W4: after fetch_groups the cms_client cache must contain VINs from the scan."""
    from agents.health.cms_dtc_source import CmsDtcSource
    source, cms = _dtc_source_with_cache_client({"VIN1": [_DtcW4("P0562")], "VIN2": []})
    source.fetch_groups()
    # Cache must map VIN → vehicleId without a vin-index query (get_active_dtcs is the
    # only call — the cache is pre-seeded so _resolve_vehicle_id must not run at all).
    assert cms._vin_to_vehicle_id is not None
    assert "VIN1" in cms._vin_to_vehicle_id
    assert "VIN2" in cms._vin_to_vehicle_id


def test_fetch_groups_failed_vin_raises_diagnostics_read_error():
    """W4: a VIN whose DTC query raises must cause DiagnosticsReadError at end of scan."""
    from agents.health.cms_dtc_source import DiagnosticsReadError
    source, cms = _dtc_source_with_cache_client(
        {"VIN1": [_DtcW4("P0562")], "VIN2": [_DtcW4("P0299")]},
        fail_vins={"VIN1"},
    )
    with pytest.raises(DiagnosticsReadError) as exc_info:
        source.fetch_groups()
    err = exc_info.value
    assert "VIN1" in err.failed_vins
    # The partial result must still carry VIN2's group.
    assert len(err.groups) >= 1
    vins_in_groups = {g.vin for g in err.groups}
    assert "VIN2" in vins_in_groups
    assert "VIN1" not in vins_in_groups  # VIN1 failed, not in output


def test_run_diagnostics_raises_at_end_when_a_vin_dtc_read_fails():
    """W4: run_diagnostics must raise DiagnosticsRunError after processing all other VINs."""
    from agents.health.cms_dtc_source import DiagnosticsReadError
    from agents.tier2 import runner as runner_mod

    source, _ = _dtc_source_with_cache_client(
        {"VIN1": [_DtcW4("P0562")], "VIN2": [_DtcW4("P0299")]},
        fail_vins={"VIN1"},
    )

    class _SimpleRecordingWriter:
        def __init__(self):
            self.calls = []
        def write_finding(self, finding, *, standing=None, refresh_on_noop=False, utc_now_fn=None):
            self.calls.append(finding)
            return {"action": "put", "finding_id": finding["finding_id"]}
        def put(self, artifact):
            raise AssertionError("legacy put must not be called")
        @property
        def table_name(self):
            return "t"
        @property
        def dynamodb_client(self):
            return _SimpleQueryClient()

    class _SimpleQueryClient:
        def query(self, **kw):
            return {"Items": []}
        def update_item(self, **kw):
            pass

    w = _SimpleRecordingWriter()
    with pytest.raises(runner_mod.DiagnosticsRunError):
        runner_mod.run_diagnostics("staging", source=source, avx_writer=w)
    # VIN2 must still have been written.
    assert any(f["scope"]["vin"] == "VIN2" for f in w.calls)



# ── Registry VIN (issue 2026-09-28-diagnostics-vin-taken-from-dtc-rows) ───────
#
# Staging dtc-history rows carry a `vin` that is stale, empty or absent (VEH-MRDN-0015's
# oldest rows carry an old seed VIN; 48 OEM1 vehicles carry none). The Finding's VIN is the
# registry's, the one the owner resolver and /findings/me key standing on, and it is kept
# only when the registry's vin-index holds exactly this vehicle for it (review G7 cycle 1, C1).
#
# Synthetic ids only (test_boundaries.py "Synthetic-VIN discipline"): never a real VIN.

class _RegistryDdb:
    """A dtc-history scan, the registry GetItem and its vin-index Query, all recorded.

    ``registry`` is every registry item (vehicleId → vin), including vehicles with no DTC
    rows. ``index`` overrides what vin-index returns for a VIN (default: derived from
    ``registry``). ``page_size`` splits vin-index answers into pages.
    """

    def __init__(self, rows, registry, *, fail_get=(), fail_index=(), index=None, page_size=None):
        self.rows = rows
        self.registry = registry
        self.fail_get = set(fail_get)
        self.fail_index = set(fail_index)
        self.index = index or {}
        self.page_size = page_size
        self.get_item_calls = []
        self.query_calls = []

    def scan(self, **kw):
        return {"Items": self.rows}

    def get_item(self, **kw):
        vid = kw["Key"]["vehicleId"]["S"]
        self.get_item_calls.append(kw)
        if vid in self.fail_get:
            raise RuntimeError(f"registry read failed for {vid}")
        vin = self.registry.get(vid)
        return {"Item": {"vin": {"S": vin}}} if vin is not None else {}

    def query(self, **kw):
        self.query_calls.append(kw)
        if len(self.query_calls) > 50:
            raise AssertionError("vin-index paging made no progress")
        assert kw["IndexName"] == "vin-index"
        vin = kw["ExpressionAttributeValues"][":v"]["S"]
        if vin in self.fail_index:
            raise RuntimeError(f"vin-index query failed for {vin}")
        holders = self.index.get(vin, [v for v, reg in sorted(self.registry.items()) if reg == vin])
        start = int(kw.get("ExclusiveStartKey", {}).get("i", {}).get("N", "0"))
        size = self.page_size or max(len(holders), 1)
        page = holders[start:start + size]
        out = {"Items": [{"vehicleId": {"S": h}, "vin": {"S": vin}} for h in page]}
        if start + size < len(holders):
            out["LastEvaluatedKey"] = {"i": {"N": str(start + size)}}
        return out


def _reg_row(vid, code, *, vin=None, desc="", ts="1"):
    row = {"vehicleId": {"S": vid}, "code": {"S": code}, "timestamp": {"N": ts}}
    if vin is not None:
        row["vin"] = {"S": vin}
    if desc:
        row["description"] = {"S": desc}
    return row


def _registry_source(rows, registry, active, **ddb_kw):
    from agents.health.cms_dtc_source import CmsDtcSource

    ddb = _RegistryDdb(rows, registry, **ddb_kw)
    cms = _VinCacheClient(active=active)
    return CmsDtcSource(dtc_table="t", vehicles_table="cms-staging-storage-vehicles",
                        ddb_client=ddb, cms_client=cms), ddb, cms


def test_the_finding_vin_is_the_registry_vin_not_a_row_vin():
    """VEH-MRDN-0015's shape: every row VIN is stale, the registry VIN is on no row."""
    rows = [
        _reg_row("TEST-VEH-9", "P0420", vin="TEST-STALE-OLDEST", desc="Catalyst System Efficiency Below Threshold", ts="1"),
        _reg_row("TEST-VEH-9", "B1234", vin="", ts="2"),
        _reg_row("TEST-VEH-9", "P0171", ts="3"),
        _reg_row("TEST-VEH-9", "P0299", vin="TEST-STALE-NEWEST", ts="4"),
    ]
    source, ddb, cms = _registry_source(
        rows, {"TEST-VEH-9": "TEST-VIN-REG"}, {"TEST-VIN-REG": [_DtcW4("P0420")]})
    groups = source.fetch_groups()
    assert cms.asked == ["TEST-VIN-REG"], "the DTC read must be asked for the registry VIN only"
    assert [(g.vin, [d["code"] for d in g.dtcs]) for g in groups] == [("TEST-VIN-REG", ["P0420"])]
    assert groups[0].dtcs[0]["description"] == "Catalyst System Efficiency Below Threshold"
    assert cms._vin_to_vehicle_id == {"TEST-VIN-REG": "TEST-VEH-9"}
    assert [c["TableName"] for c in ddb.get_item_calls] == ["cms-staging-storage-vehicles"]
    assert [c["TableName"] for c in ddb.query_calls] == ["cms-staging-storage-vehicles"]


def test_a_vehicle_whose_rows_carry_no_vin_is_still_covered():
    """The 48 OEM1 vehicles' shape: rows without a vin attribute, vehicleId equal to the VIN."""
    oem = "TEST-OEM1-FLEET-VIN-01"
    rows = [_reg_row(oem, "P0171"), _reg_row(oem, "U0100", ts="2")]
    source, _, cms = _registry_source(rows, {oem: oem}, {oem: [_DtcW4("P0171")]})
    groups = source.fetch_groups()
    assert cms.asked == [oem]
    assert [g.vin for g in groups] == [oem]


def test_an_unregistered_vehicle_is_skipped_not_failed():
    rows = [_reg_row("TEST-VEH-GONE", "P0562", vin="TEST-VIN-ON-ROW"), _reg_row("TEST-VEH-1", "P0299", vin="TEST-VIN1")]
    source, ddb, cms = _registry_source(
        rows, {"TEST-VEH-1": "TEST-VIN1"},  # no registry item for TEST-VEH-GONE
        {"TEST-VIN-ON-ROW": [_DtcW4("P0562")], "TEST-VIN1": [_DtcW4("P0299")]})
    groups = source.fetch_groups()  # no DiagnosticsReadError
    assert cms.asked == ["TEST-VIN1"], "an unregistered vehicle's row VIN must never be read"
    assert [g.vin for g in groups] == ["TEST-VIN1"]
    assert [c["ExpressionAttributeValues"][":v"]["S"] for c in ddb.query_calls] == ["TEST-VIN1"], \
        "an unregistered vehicle gets no VIN at all, so nothing is looked up for it"


def test_a_registry_item_without_a_vin_is_unregistered():
    rows = [_reg_row("TEST-VEH-NOVIN", "P0562")]
    source, _, cms = _registry_source(rows, {"TEST-VEH-NOVIN": "  "}, {})
    assert source.fetch_groups() == []
    assert cms.asked == []


def test_a_registry_read_failure_fails_the_run_after_the_others():
    """The failing vehicle sorts first, so stopping at the failure would drop TEST-VEH-1 (W1)."""
    from agents.health.cms_dtc_source import DiagnosticsReadError

    rows = [_reg_row("TEST-VEH-0", "P0562"), _reg_row("TEST-VEH-1", "P0299")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-0": "TEST-VIN0", "TEST-VEH-1": "TEST-VIN1"},
        {"TEST-VIN0": [_DtcW4("P0562")], "TEST-VIN1": [_DtcW4("P0299")]}, fail_get={"TEST-VEH-0"})
    with pytest.raises(DiagnosticsReadError) as exc_info:
        source.fetch_groups()
    assert exc_info.value.failed_vins == {"TEST-VEH-0"}
    assert [g.vin for g in exc_info.value.groups] == ["TEST-VIN1"], "the other vehicles still run"
    assert "TEST-VIN0" not in cms.asked


def test_a_vin_index_failure_fails_the_run_after_the_others():
    from agents.health.cms_dtc_source import DiagnosticsReadError

    rows = [_reg_row("TEST-VEH-0", "P0562"), _reg_row("TEST-VEH-1", "P0299")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-0": "TEST-VIN0", "TEST-VEH-1": "TEST-VIN1"},
        {"TEST-VIN0": [_DtcW4("P0562")], "TEST-VIN1": [_DtcW4("P0299")]}, fail_index={"TEST-VIN0"})
    with pytest.raises(DiagnosticsReadError) as exc_info:
        source.fetch_groups()
    assert exc_info.value.failed_vins == {"TEST-VEH-0"}
    assert [g.vin for g in exc_info.value.groups] == ["TEST-VIN1"]
    assert "TEST-VIN0" not in cms.asked


def test_two_vehicles_with_one_registry_vin_are_both_skipped():
    """One Finding keyed on a shared VIN would merge two vehicles' faults."""
    rows = [_reg_row("TEST-VEH-4", "P0562"), _reg_row("TEST-VEH-5", "P0420"), _reg_row("TEST-VEH-1", "P0299")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-4": "TEST-VIN-SHARED", "TEST-VEH-5": "TEST-VIN-SHARED", "TEST-VEH-1": "TEST-VIN1"},
        {"TEST-VIN-SHARED": [_DtcW4("P0562"), _DtcW4("P0420")], "TEST-VIN1": [_DtcW4("P0299")]})
    groups = source.fetch_groups()
    assert cms.asked == ["TEST-VIN1"]
    assert [g.vin for g in groups] == ["TEST-VIN1"]
    assert "TEST-VIN-SHARED" not in (cms._vin_to_vehicle_id or {})


def test_a_twin_without_dtc_rows_still_blocks_the_vin():
    """Review C1 P2: the twin is registered but never scanned; the owner resolver's first
    vin-index item could be the twin, projecting its owner onto these faults."""
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X", "TEST-VEH-B": "TEST-VIN-X"},
        {"TEST-VIN-X": [_DtcW4("P0562")]})
    assert source.fetch_groups() == []
    assert cms.asked == []


def test_a_twin_whose_registry_read_fails_still_blocks_the_vin():
    """Review C1 P1: the surviving twin must not be written just because the other's GetItem failed."""
    from agents.health.cms_dtc_source import DiagnosticsReadError

    rows = [_reg_row("TEST-VEH-A", "P0562"), _reg_row("TEST-VEH-B", "P0420")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X", "TEST-VEH-B": "TEST-VIN-X"},
        {"TEST-VIN-X": [_DtcW4("P0562"), _DtcW4("P0420")]}, fail_get={"TEST-VEH-A"})
    with pytest.raises(DiagnosticsReadError) as exc_info:
        source.fetch_groups()
    assert exc_info.value.failed_vins == {"TEST-VEH-A"}
    assert exc_info.value.groups == []
    assert cms.asked == []


def test_a_vin_index_naming_another_vehicle_blocks_the_vin():
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X"}, {"TEST-VIN-X": [_DtcW4("P0562")]},
        index={"TEST-VIN-X": ["TEST-VEH-Z"]})
    assert source.fetch_groups() == []
    assert cms.asked == []


def test_a_twin_on_a_later_vin_index_page_still_blocks_the_vin():
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, ddb, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X", "TEST-VEH-B": "TEST-VIN-X"},
        {"TEST-VIN-X": [_DtcW4("P0562")]}, page_size=1)
    assert source.fetch_groups() == []
    assert len(ddb.query_calls) == 2, "vin-index must be read to its last page"
    assert cms.asked == []


def test_a_twin_on_an_earlier_vin_index_page_still_blocks_the_vin():
    """Keeping only the last page would see just this vehicle and keep it."""
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X"}, {"TEST-VIN-X": [_DtcW4("P0562")]},
        index={"TEST-VIN-X": ["TEST-VEH-B", "TEST-VEH-A"]}, page_size=1)
    assert source.fetch_groups() == []
    assert cms.asked == []


def test_an_empty_vin_index_answer_blocks_the_vin():
    """Index lag: the registry item has a VIN the index does not hold yet (review G7 c2 W1).
    An empty answer must not read as "unique" (testing.md: an empty collection must not
    switch off a fail-closed check)."""
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, _, cms = _registry_source(
        rows, {"TEST-VEH-A": "TEST-VIN-X"}, {"TEST-VIN-X": [_DtcW4("P0562")]},
        index={"TEST-VIN-X": []})
    assert source.fetch_groups() == []
    assert cms.asked == []


def test_a_padded_registry_vin_is_not_found_in_the_index():
    """A stored VIN with spaces: the stripped value is not what the index holds, so the
    owner resolver could not find it either; skip rather than write an unreadable card."""
    rows = [_reg_row("TEST-VEH-A", "P0562")]
    source, ddb, cms = _registry_source(
        rows, {"TEST-VEH-A": " TEST-VIN-PAD "}, {"TEST-VIN-PAD": [_DtcW4("P0562")]})
    assert source.fetch_groups() == []
    assert cms.asked == []
    assert [c["ExpressionAttributeValues"][":v"]["S"] for c in ddb.query_calls] == ["TEST-VIN-PAD"]


def test_source_logs_carry_no_vin_or_vehicle_id(caplog):
    """W2: every source log path, with synthetic ids that would be visible if printed."""
    import logging

    from agents.health.cms_dtc_source import DiagnosticsReadError

    ids = {
        "CAPLOG-TEST-VEH-01": "CAPLOG-TEST-VIN-01",  # GetItem fails
        "CAPLOG-TEST-VEH-02": "CAPLOG-TEST-VIN-02",  # vin-index fails
        "CAPLOG-TEST-VEH-03": "CAPLOG-TEST-VIN-SH",  # shared VIN
        "CAPLOG-TEST-VEH-04": "CAPLOG-TEST-VIN-SH",
        "CAPLOG-TEST-VEH-05": "CAPLOG-TEST-VIN-05",  # DTC read fails
    }
    rows = [_reg_row(v, "P0562") for v in ids] + [_reg_row("CAPLOG-TEST-VEH-06", "P0562")]  # unregistered
    source, _, cms = _registry_source(
        rows, ids, {}, fail_get={"CAPLOG-TEST-VEH-01"}, fail_index={"CAPLOG-TEST-VIN-02"})
    cms.fail_vins = {"CAPLOG-TEST-VIN-05"}
    with caplog.at_level(logging.DEBUG, logger="agents.health.cms_dtc_source"):
        with pytest.raises(DiagnosticsReadError):
            source.fetch_groups()
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "agents.health.cms_dtc_source")
    assert "registry_read_failed" in text and "registry_vin_index_failed" in text  # positive control
    assert "registry_vin_not_unique" in text and "unregistered_vehicles_skipped" in text
    for secret in list(ids) + list(set(ids.values())) + ["CAPLOG-TEST-VEH-06"]:
        assert secret not in text, f"{secret} leaked into a log line"


def test_vehicles_table_is_required():
    from agents.health.cms_dtc_source import CmsDtcSource

    with pytest.raises(ValueError):
        CmsDtcSource(dtc_table="t", vehicles_table="", ddb_client=object(), cms_client=object())
    with pytest.raises(TypeError):
        CmsDtcSource(dtc_table="t", ddb_client=object(), cms_client=object())  # noqa - no default


def test_runner_wiring_passes_the_registry_table(monkeypatch):
    """_run_diagnostics_from_env hands CMS_VEHICLES_TABLE to the source, not only the resolver."""
    from agents.health import cms_dtc_source
    from agents.tier2 import runner as runner_mod

    seen = {}

    class _Probe(cms_dtc_source.CmsDtcSource):
        def __init__(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop after construction")

    class _Boto3:
        @staticmethod
        def client(*a, **k):
            return object()

        @staticmethod
        def resource(*a, **k):
            return object()

    monkeypatch.setattr(cms_dtc_source, "CmsDtcSource", _Probe)
    monkeypatch.setitem(sys.modules, "boto3", _Boto3)
    monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-test-registry-from-env")
    with pytest.raises(RuntimeError, match="stop after construction"):
        runner_mod._run_diagnostics_from_env("staging")
    assert seen["vehicles_table"] == "cms-test-registry-from-env"
