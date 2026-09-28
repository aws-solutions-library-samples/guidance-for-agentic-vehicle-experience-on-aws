"""The triage handler records how the DTC read went, so a failed read is never
stored as an ordinary "no faults" decision.

Review W3 on a137e13 (issues/2026-09-25-triage-drops-active-dtcs-first-seen-over-24h/
review-a137e13-cycle1.md): a first-page failure returned (), the classifier saw no
DTCs, and the decision row was indistinguishable from a vehicle with no faults. That
is the shape of the original incident: a confident P3 while a P0 was active.

Lives in lambdas/shared/tests/ rather than lambdas/triage-classifier/tests/ because
it loads handler.py from SOURCE, and test_warmup.py in that directory deliberately
imports the BUILT bundle under the name `triage_classifier`. This placement does NOT
fully isolate the two: this file and test_cms_client_real_active_dtcs.py both
register the source package at import, so a pytest run that collects them together
with test_warmup.py retargets test_warmup.py at the source tree (true since
a137e13; review cycle 2, S11). Run test_warmup.py on its own to test the bundle.

Run:
    .venv/bin/python3 -m pytest lambdas/shared/tests/test_triage_handler_dtc_read.py -q

Mutations verified (each fails at least one test):
  H1. Drop the `inputsSnapshot["dtcRead"]` write.
  H2. Emit the degraded metric for every read, `complete` included.
  H3. Never emit the degraded metric.
  H4. Record a client without `last_dtc_read` as `complete`.
  H5. Read the outcome before calling `get_active_dtcs`.
  H6. Drop `vehicle_lookup_failed` from the degraded set.
  H7. Add `vehicle_unresolved` to the degraded set.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve()
LAMBDAS = HERE.parents[2]
sys.path.insert(0, str(LAMBDAS))


def _register_triage_classifier_package() -> None:
    """Same rename the bundle applies (scripts/build-triage-classifier.sh)."""
    if "triage_classifier" in sys.modules:
        return
    pkg_dir = LAMBDAS / "triage-classifier"
    spec = importlib.util.spec_from_file_location(
        "triage_classifier", pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_classifier"] = module
    spec.loader.exec_module(module)


_register_triage_classifier_package()

from triage_classifier.types import (  # noqa: E402
    ActiveDtc,
    TelemetrySnapshot,
    TenantThresholds,
    TenantTriageConfig,
)

_spec = importlib.util.spec_from_file_location(
    "_triage_handler_under_test", LAMBDAS / "triage-classifier" / "handler.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

VIN = "MRDN0000000000015"


class _Cms:
    """Stub CMS client. Like RealCmsClient, it sets ``last_dtc_read`` inside
    ``get_active_dtcs``, so a handler that reads the outcome before the call
    reads the previous (here: absent) value and fails these tests (review
    cycle 2, S10)."""

    def __init__(self, dtcs: tuple, last_dtc_read: dict | None = None,
                 report: bool = True) -> None:
        self._dtcs = dtcs
        self._report = report
        self._outcome = last_dtc_read

    def get_telemetry(self, vin: str) -> TelemetrySnapshot:
        return TelemetrySnapshot(stale=True)

    def get_active_dtcs(self, vin: str) -> tuple:
        if self._report:
            self.last_dtc_read = self._outcome
        return self._dtcs

    def get_open_recalls(self, vin: str) -> tuple:
        return ()


class _Table:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3 shape
        self.items.append(Item)


def _run(monkeypatch, capsys, cms: _Cms) -> tuple[dict, dict, list[dict]]:
    table = _Table()
    monkeypatch.setattr(h.tenant_config, "load_raw", lambda tid: {"segment": "fleet"})
    monkeypatch.setattr(h.tenant_config, "build_triage_config", lambda raw: TenantTriageConfig(
        thresholds=TenantThresholds(), escalations=(), predictive_weight="none"))
    monkeypatch.setattr(h.cms_client, "get_client", lambda: cms)
    monkeypatch.setattr(h, "_decisions_table", lambda: table)
    resp = h.handler({"tenantId": "meridian", "vin": VIN, "sessionId": "vsa-sess-test"}, None)
    emf = []
    for line in capsys.readouterr().out.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and "_aws" in doc:
            emf.append(doc)
    assert len(table.items) == 1
    return resp, table.items[0], emf


def test_failed_read_is_recorded_in_the_decision_and_counted(monkeypatch, capsys):
    cms = _Cms((), {"status": "failed", "pages": 0, "rows": 0})
    resp, item, emf = _run(monkeypatch, capsys, cms)
    # Classification still happens: triage never blocks on a failed read.
    assert resp["classification"] == "P3"
    assert item["inputsSnapshot"]["dtcRead"] == {"status": "failed", "pages": 0, "rows": 0}
    assert len(emf) == 1
    metric = emf[0]["_aws"]["CloudWatchMetrics"][0]
    assert metric["Namespace"] == "vsa/triage-classifier"
    assert metric["Metrics"] == [{"Name": "dtc_read_degraded", "Unit": "Count"}]
    assert emf[0]["dtc_read_status"] == "failed"
    assert emf[0]["dtc_read_degraded"] == 1


def test_partial_read_is_recorded_and_counted(monkeypatch, capsys):
    cms = _Cms((ActiveDtc(code="P0217", first_seen_at="1"),),
               {"status": "partial", "pages": 1, "rows": 2})
    resp, item, emf = _run(monkeypatch, capsys, cms)
    assert resp["classification"] == "P0"
    assert item["inputsSnapshot"]["dtcRead"]["status"] == "partial"
    assert [e["dtc_read_status"] for e in emf] == ["partial"]


def test_complete_read_is_recorded_and_not_counted(monkeypatch, capsys):
    cms = _Cms((), {"status": "complete", "pages": 1, "rows": 3})
    _, item, emf = _run(monkeypatch, capsys, cms)
    assert item["inputsSnapshot"]["dtcRead"] == {"status": "complete", "pages": 1, "rows": 3}
    assert emf == []


def test_vehicle_lookup_failure_is_counted(monkeypatch, capsys):
    """W5: a vehicles-table outage is the first thing a CMS access failure breaks."""
    cms = _Cms((), {"status": "vehicle_lookup_failed", "pages": 0, "rows": 0})
    _, item, emf = _run(monkeypatch, capsys, cms)
    assert item["inputsSnapshot"]["dtcRead"]["status"] == "vehicle_lookup_failed"
    assert [e["dtc_read_status"] for e in emf] == ["vehicle_lookup_failed"]


def test_vin_not_in_cms_is_recorded_but_not_counted(monkeypatch, capsys):
    cms = _Cms((), {"status": "vehicle_unresolved", "pages": 0, "rows": 0})
    _, item, emf = _run(monkeypatch, capsys, cms)
    assert item["inputsSnapshot"]["dtcRead"]["status"] == "vehicle_unresolved"
    assert emf == []


def test_client_that_does_not_report_is_recorded_as_unreported(monkeypatch, capsys):
    """The fake client has no last_dtc_read. Absence must not read as complete."""
    _, item, emf = _run(monkeypatch, capsys, _Cms((), report=False))
    assert item["inputsSnapshot"]["dtcRead"] == {"status": "unreported"}
    assert emf == []


@pytest.mark.parametrize("bad", [None, {}, {"pages": 1}, "complete"])
def test_malformed_report_is_unreported(monkeypatch, capsys, bad):
    _, item, _ = _run(monkeypatch, capsys, _Cms((), bad))
    assert item["inputsSnapshot"]["dtcRead"] == {"status": "unreported"}
