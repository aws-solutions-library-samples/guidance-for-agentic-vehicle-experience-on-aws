"""Stage F1 multi-day loop tests (Fix Group G3-1).

Coverage
--------
- Day 1 write: no history → attempt 1 written.
- Day 2 refresh: undecided (queue_status=none) → write_current, not skip.
- Day 4: item still written after natural expiry window.
- Operator decline (route real shape: audit_trail=[]): safety recall not re-raised
  before 14 days from updated_at, re-raised as attempt 2 after 14 days.
- Non-safety decline: re-raised after 30 days.
- refresh_on_noop=True reaches every write_finding call (mutation guard).

Each "must not" assertion has a control leg that proves the writes do work.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from agents.fi_recall_warranty import recall as rw_recall
from agents.fi_recall_warranty.attempts import (
    _AGENT_ID,
    _FINDING_KIND,
    _extract_diagnosis_source,
    _map_action_to_queue_status,
    fi_decision_id,
)

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_VIN = "MRDTST00000000099"
_FLEET_ID = "fleet-f1"
_LOCATION_ID = "loc-f1"


_STOCK_ROWS = [
    {
        "dealer_id": "dealer-f1",
        "part_number": "PART-F1",
        "qty_on_hand": "3",
        "latitude": "37.33",
        "longitude": "-122.03",
        "stock_as_of": "2026-10-07",
    }
]


def _row(campaign: str = "25V-F1A", recall_class: str = "safety") -> dict:
    return {
        "vin": _VIN,
        "campaign_id": campaign,
        "recall_class": recall_class,
        "do_not_drive": False,
        "match_basis": "POPULATION",
        "remedy_available_from": None,
        "remedy_part_numbers": [],
        "summary": "Brake caliper may seize.",
        "consequence": "Reduced braking.",
        "remedy": "Replace caliper.",
        "received_date": "2026-01-01",
        "catalog_version": "1",
        "rule_version": "1",
        "models": None,
        "model_year_min": None,
        "model_year_max": None,
        "plants": None,
        "build_from": None,
        "build_to": None,
        "park_outside": False,
        "confirming_code": None,
        "confirming_seen_on": None,
        "confirming_source": None,
    }


def _decline_action(finding_id: str, updated_at: str) -> dict:
    """Route's real Item shape: audit_trail=[], decided by operator (C2)."""
    return {
        "action_id": fi_decision_id(finding_id),
        "finding_id": finding_id,
        "created_by": "owner-sub-001",
        "created_at": updated_at,
        "updated_at": updated_at,
        "target_system": "dms",
        "action_kind": "book_recall_service",
        "status": "declined",
        "decision": "declined",
        "decided_by": {"kind": "operator", "id": "sub-001"},
        "parameters": {},
        "evidence": [],
        "audit_trail": [],
    }


# ---------------------------------------------------------------------------
# Stateful fake
# ---------------------------------------------------------------------------

class _StandingFake:
    def __init__(self, vin: str):
        self.vin = vin
        self.fleet_id = _FLEET_ID


class _StatefulFake:
    """Fakes that build history from written findings — mirrors read_attempt_history."""

    def __init__(self):
        self._writes: list[tuple[dict, dict]] = []   # (finding, kwargs)
        self._store: dict[str, dict] = {}            # finding_key → finding
        self._actions: dict[str, dict] = {}          # action_id → Action

    # --- loop callables ---

    def vehicle_ids_fn(self, vin: str) -> Optional[tuple]:
        return (f"vid-{vin}", _FLEET_ID, _LOCATION_ID)

    def locations_fn(self, fleet_id: str) -> dict:
        return {_LOCATION_ID: (37.7749, -122.4194)}

    def grounded_fn(self, vehicle_id: str) -> tuple[bool, int]:
        return False, 0

    def history_fn(self, vin: str) -> tuple[dict, int]:
        now = datetime.datetime.now(datetime.timezone.utc)
        best: dict[tuple, dict] = {}
        for f in self._store.values():
            if f.get("agent_id") != _AGENT_ID:
                continue
            if f.get("finding_kind") != _FINDING_KIND:
                continue
            f_vin = f.get("vin") or (f.get("scope") or {}).get("vin")
            if f_vin != vin:
                continue
            parts = (f.get("subject_key") or "").split("/")
            if len(parts) != 3:
                continue
            cam, act, n_str = parts
            try:
                attempt_n = int(n_str)
            except ValueError:
                continue
            key = (cam, act)
            existing = best.get(key)
            newer = (
                existing is None
                or attempt_n > existing["attempt_number"]
                or (attempt_n == existing["attempt_number"]
                    and f.get("computed_at", "") > existing.get("computed_at", ""))
            )
            if newer:
                best[key] = {
                    "attempt_number": attempt_n,
                    "finding_id": f.get("finding_id", ""),
                    "inputs_hash": f.get("inputs_hash", ""),
                    "expires_at": f.get("expires_at", ""),
                    "computed_at": f.get("computed_at", ""),
                    "diagnosis_source": _extract_diagnosis_source(f),
                }
        history: dict[tuple, dict] = {}
        for key, entry in best.items():
            action_id = fi_decision_id(entry["finding_id"])
            action_obj = self._actions.get(action_id)
            qs, decided_at = _map_action_to_queue_status(action_obj, now)
            history[key] = {**entry, "queue_status": qs, "decided_at": decided_at}
        return history, 0

    def standing_fn(self, vin: str) -> _StandingFake:
        return _StandingFake(vin)

    def lookup_live(self, finding_key: str) -> Optional[dict]:
        return self._store.get(finding_key)

    def write_finding(self, finding: dict, standing: Any, refresh_on_noop: bool = False) -> dict:
        self._writes.append((finding, {"refresh_on_noop": refresh_on_noop}))
        fk = finding.get("finding_key", "")
        self._store[fk] = dict(finding)
        return {"action": "put"}

    def seed_action(self, finding_id: str, action: dict) -> None:
        self._actions[fi_decision_id(finding_id)] = action

    # --- assertions helpers ---

    @property
    def total_writes(self) -> int:
        return len(self._writes)

    def refresh_flags(self) -> list[bool]:
        return [kw["refresh_on_noop"] for _, kw in self._writes]

    def last_attempt(self) -> int:
        if not self._writes:
            return 0
        f = self._writes[-1][0]
        parts = (f.get("subject_key") or "").split("/")
        return int(parts[2]) if len(parts) == 3 else 0


def _run(fake: _StatefulFake, rows: list[dict],
         today: datetime.date, computed_at: str) -> dict:
    return rw_recall.run_recall_loop(
        recall_rows=rows, stock_rows=_STOCK_ROWS,
        today=today, computed_at=computed_at,
        vehicle_ids_fn=fake.vehicle_ids_fn, locations_fn=fake.locations_fn,
        grounded_fn=fake.grounded_fn, history_fn=fake.history_fn,
        standing_fn=fake.standing_fn, lookup_live=fake.lookup_live,
        write_finding=fake.write_finding, plan_fn=None,
    )


def _d(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMultiDayLoop:

    def test_day1_writes_attempt1(self):
        fake = _StatefulFake()
        s = _run(fake, [_row()], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert s["items_written"] >= 1, "day 1 must write (control)"
        assert fake.total_writes >= 1

    def test_day2_rewrites_undecided(self):
        """queue_status=none must not skip — write_current (C1)."""
        fake = _StatefulFake()
        _run(fake, [_row()], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert fake.total_writes >= 1, "control: day 1 must write"
        before = fake.total_writes
        s2 = _run(fake, [_row()], _d("2026-10-08"), "2026-10-08T05:00:00Z")
        assert s2["items_written"] >= 1, (
            "day 2 must rewrite undecided attempt; "
            "plan_attempt must return write_current for none, not skip"
        )
        assert fake.total_writes > before

    def test_day4_still_written(self):
        """Item expired but run still rewrites (not abandoned after day 3)."""
        fake = _StatefulFake()
        for iso in ("2026-10-07", "2026-10-08", "2026-10-09"):
            _run(fake, [_row()], _d(iso), f"{iso}T05:00:00Z")
        s4 = _run(fake, [_row()], _d("2026-10-10"), "2026-10-10T05:00:00Z")
        assert s4["items_written"] >= 1, "day 4 must still write, not abandon"

    def test_decline_safety_no_reraise_before_14_days(self):
        """Safety decline: no re-raise before 14 days from updated_at (C2)."""
        fake = _StatefulFake()
        # Control: day 1 writes
        s1 = _run(fake, [_row("25V-F1B", "safety")], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert s1["items_written"] >= 1, "control: day 1 must write"
        fid = fake._writes[0][0]["finding_id"]
        fake.seed_action(fid, _decline_action(fid, "2026-10-08T00:00:00Z"))
        # Day 20 = 12 days after Oct 8: < 14 → no reraise
        s_early = _run(fake, [_row("25V-F1B", "safety")], _d("2026-10-20"), "2026-10-20T05:00:00Z")
        assert s_early["items_written"] == 0, (
            "safety recall must not re-raise before 14 days from updated_at (C2); "
            f"items_written={s_early['items_written']}"
        )

    def test_decline_safety_reraise_after_14_days(self):
        """Safety decline: attempt n+1 written after 14 days (C2 + control)."""
        fake = _StatefulFake()
        s1 = _run(fake, [_row("25V-F1C", "safety")], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert s1["items_written"] >= 1, "control: day 1 must write"
        fid = fake._writes[0][0]["finding_id"]
        fake.seed_action(fid, _decline_action(fid, "2026-10-08T00:00:00Z"))
        # Oct 23 = 15 days after Oct 8: >= 14
        s_late = _run(fake, [_row("25V-F1C", "safety")], _d("2026-10-23"), "2026-10-23T05:00:00Z")
        assert s_late["items_written"] >= 1, "safety recall must re-raise after 14 days"
        assert fake.last_attempt() >= 2, "re-raise must be attempt >= 2"

    def test_decline_non_safety_reraise_after_30_days(self):
        """Non-safety decline: attempt n+1 written after 30 days."""
        fake = _StatefulFake()
        s1 = _run(fake, [_row("25V-F1D", "non_safety")], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert s1["items_written"] >= 1, "control: day 1 must write"
        fid = fake._writes[0][0]["finding_id"]
        fake.seed_action(fid, _decline_action(fid, "2026-10-08T00:00:00Z"))
        # Nov 8 = 31 days after Oct 8: >= 30
        s_late = _run(fake, [_row("25V-F1D", "non_safety")], _d("2026-11-08"), "2026-11-08T05:00:00Z")
        assert s_late["items_written"] >= 1, "non-safety must re-raise after 30 days"
        assert fake.last_attempt() >= 2, "re-raise must be attempt >= 2"

    def test_refresh_on_noop_true_on_all_writes(self):
        """refresh_on_noop=True must reach every write_finding call (mutation guard)."""
        fake = _StatefulFake()
        _run(fake, [_row()], _d("2026-10-07"), "2026-10-07T05:00:00Z")
        assert fake.total_writes >= 1, "control: must write"
        flags = fake.refresh_flags()
        assert all(flags), (
            f"all write_finding calls must have refresh_on_noop=True; got flags={flags}"
        )



class TestHistoryNewestByComputedAt:
    """Unit-level test for computed_at tie-break in history_fn (mutant d guard).

    The DDB data model allows only ONE row per finding_key (PK), so the
    loop tests can't manufacture two rows with the same subject_key.  This
    test seeds _store directly with two distinct fake keys to cover the
    comparison path.
    """

    def test_prefers_newer_computed_at_same_attempt(self):
        """history_fn picks the newest finding when two rows share the same attempt."""
        fake = _StatefulFake()
        old_fid = "FIND-MUT-D-OLD-00000001"
        new_fid = "FIND-MUT-D-NEW-00000001"
        subj = "25V-FH/book/1"
        fake._store["fk-history-old"] = {
            "agent_id": _AGENT_ID, "finding_kind": _FINDING_KIND,
            "scope": {"vin": _VIN}, "subject_key": subj,
            "finding_id": old_fid, "computed_at": "2026-10-07T05:00:00Z",
            "inputs_hash": "aaa", "expires_at": "2026-10-10T05:00:00Z", "evidence": [],
        }
        fake._store["fk-history-new"] = {
            "agent_id": _AGENT_ID, "finding_kind": _FINDING_KIND,
            "scope": {"vin": _VIN}, "subject_key": subj,
            "finding_id": new_fid, "computed_at": "2026-10-08T05:00:00Z",
            "inputs_hash": "bbb", "expires_at": "2026-10-11T05:00:00Z", "evidence": [],
        }
        # Seed a decline on the NEWER finding_id only
        fake.seed_action(new_fid, _decline_action(new_fid, "2026-10-08T00:00:00Z"))

        history, _ = fake.history_fn(_VIN)
        key = ("25V-FH", "book")
        assert key in history, "history must have an entry for the seeded subject_key"
        qs = history[key]["queue_status"]
        assert qs == "rejected", (
            f"history_fn must pick the newest finding (action on new_fid); "
            f"got queue_status={qs!r}. "
            "Mutant d: if oldest is picked, fi_decision_id(old_fid) finds no action → 'none'."
        )



class TestReadAttemptHistoryNewestByComputedAt:
    """Direct DDB-Stubber test for the computed_at tie-break in read_attempt_history.

    Two DDB rows share the same subject_key but different computed_at and finding_id.
    The history must keep the one with the LARGER computed_at (newer).

    Mutant d: inverting > to < selects the older row → history["finding_id"] is
    FIND_OLD, failing the assertion below.
    """

    _VIN = "MRDFXD00000000099"
    _TBL_F = "vsa-test-findings"
    _TBL_A = "vsa-test-actions"
    _CAMPAIGN = "25V-FXD"
    _FIND_OLD = "FIND-FXD-OLD-0000-0000-0000-000000000001"
    _FIND_NEW = "FIND-FXD-NEW-0000-0000-0000-000000000001"

    def _ddb_finding(self, finding_id: str, computed_at: str) -> dict:
        from boto3.dynamodb.types import TypeSerializer
        _s = TypeSerializer().serialize
        return {k: _s(v) for k, v in {
            "finding_id": finding_id,
            "vin": self._VIN,
            "agent_id": _AGENT_ID,
            "finding_kind": _FINDING_KIND,
            "subject_key": f"{self._CAMPAIGN}/book/1",
            "computed_at": computed_at,
            "inputs_hash": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "evidence": [],
        }.items()}

    def test_newer_computed_at_wins(self):
        import boto3
        from botocore.stub import Stubber
        from agents.fi_recall_warranty.attempts import fi_decision_id, read_attempt_history

        ddb = boto3.client("dynamodb", region_name="us-west-2")
        find_old_item = self._ddb_finding(self._FIND_OLD, "2026-10-07T05:00:00Z")
        find_new_item = self._ddb_finding(self._FIND_NEW, "2026-10-08T05:00:00Z")
        # GetItem for whichever finding is selected — returns empty (no action needed)
        act_id_selected = fi_decision_id(self._FIND_NEW)   # expected to be selected

        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={"Items": [find_old_item, find_new_item]},
                expected_params={
                    "TableName": self._TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": self._VIN}},
                },
            )
            # One GetItem for whichever finding_id is selected (no action, queue_status=none)
            stub.add_response("get_item", {})

            history, skip = read_attempt_history(
                self._VIN,
                ddb_client=ddb,
                findings_table=self._TBL_F,
                actions_table=self._TBL_A,
            )

        key = (self._CAMPAIGN, "book")
        assert key in history, "history must have an entry"
        got_fid = history[key]["finding_id"]
        assert got_fid == self._FIND_NEW, (
            f"read_attempt_history must keep the NEWER finding (computed_at comparison); "
            f"got finding_id={got_fid!r}, expected {self._FIND_NEW!r}. "
            "Mutant d: inverting > to < picks FIND_OLD instead."
        )
