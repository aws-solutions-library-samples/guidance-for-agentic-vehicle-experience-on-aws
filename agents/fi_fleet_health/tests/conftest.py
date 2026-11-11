"""conftest.py — shared fixtures for fi_fleet_health tests.

Provides autouse freshness stub so tests that call run_service
don't need real Athena after the G3F6F1.5 snapshot-freshness wiring.
"""
from __future__ import annotations

import datetime
import pytest

from agents.tier2 import runner
from agents.tier2.snapshot_freshness import ADP_KEYS, SNAPSHOT_TABLES, SnapshotState


@pytest.fixture(autouse=True)
def _fresh_snapshot_dates(monkeypatch):
    """Patch _read_snapshot_dates to return today's SnapshotStates for all snapshot keys."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    fresh = {
        k: SnapshotState(
            date=today,
            row_count=(1 if k in ADP_KEYS else None),
            table_date=today,
        )
        for k in SNAPSHOT_TABLES
    }
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: fresh)
