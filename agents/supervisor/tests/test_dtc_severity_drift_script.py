"""`scripts/check-dtc-severity-drift.py` must not pass while comparing nothing.

Review Cycle 1 W3 (issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/): with a
faked catalog, string severities and lowercase hints were skipped but still counted, so
the script printed "OK" and exited 0; and a weak hint masked a strong severity.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check-dtc-severity-drift.py"
_spec = importlib.util.spec_from_file_location("check_dtc_severity_drift", _PATH)
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)


@pytest.mark.parametrize("item,grade", [
    ({"severity_hint": "P0"}, "P0"),
    ({"severity_hint": "p1"}, "P1"),
    ({"severity": Decimal("4")}, "P0"),
    ({"severity": Decimal("3.0")}, "P1"),
    ({"severity": "CRITICAL"}, "P0"),
    ({"severity": "high"}, "P1"),
    ({"severity_hint": "P2", "severity": Decimal("4")}, "P0"),   # the more severe wins
    ({"severity_hint": "P0", "severity": Decimal("1")}, "P0"),
    ({}, None),
    ({"severity_hint": "urgent", "severity": "bad"}, None),
    ({"severity_hint": "P2", "severity": "weird"}, None),        # an unreadable field is not ignored
    ({"severity_hint": "urgent", "severity": Decimal("4")}, None),
])
def test_cms_grade(item, grade):
    assert drift.cms_grade(item) == grade


def _run(items):
    table = MagicMock()
    table.scan.return_value = {"Items": items}
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.object(drift.boto3, "resource", return_value=resource), \
         patch("sys.argv", ["check-dtc-severity-drift.py"]):
        return drift.main()


def test_a_clean_catalog_passes():
    assert _run([{"event_id": "e1", "dtc_code": "C1234", "severity_hint": "P0"}]) == 0


def test_drift_fails():
    assert _run([{"event_id": "e1", "dtc_code": "ZZZZ9", "severity_hint": "P0"}]) == 1


def test_an_ungradable_coded_entry_fails_even_beside_a_clean_one():
    assert _run([
        {"event_id": "e1", "dtc_code": "C1234", "severity_hint": "P0"},
        {"event_id": "e2", "dtc_code": "C1234", "severity": "weird"},
    ]) == 1


def test_all_ungradable_fails():
    assert _run([{"event_id": "e1", "dtc_code": "C1234", "severity": "weird"}]) == 1


@pytest.mark.parametrize("items", [[], [{"event_id": "e1", "severity_hint": "P0"}]])
def test_nothing_to_compare_fails(items):
    assert _run(items) == 1


def test_every_page_is_read():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"event_id": "e1", "dtc_code": "C1234", "severity_hint": "P0"}], "LastEvaluatedKey": {"event_id": "e1"}},
        {"Items": [{"event_id": "e2", "dtc_code": "ZZZZ9", "severity_hint": "P0"}]},
    ]
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.object(drift.boto3, "resource", return_value=resource), \
         patch("sys.argv", ["check-dtc-severity-drift.py"]):
        assert drift.main() == 1  # the drift is on the second page
    assert table.scan.call_count == 2
