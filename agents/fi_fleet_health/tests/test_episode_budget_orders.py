"""Episode budget after exhaustions, in every by_vin row order (review cycle 5 a W1, W2).

Ported from the cycle-5 reviewer's ``probe_a5.py``. Drives the REAL ``run_health_loop`` and
``read_attempt_history_health`` over a fake DynamoDB that stores serialized Finding rows (an
expired row is left and a new row Put, as ``agents/tier2/writer.py`` does). Each run reads the
history in 16 row orders x 4 page sizes and requires one answer, then runs the loop and records
the new base attempt's Action status.

- W1: an expired undecided ``service/6`` and its failed re-Put at the same attempt must resolve
  to the re-Put in every order (scenario D). Mutant ``a-tiebreak-dropped`` FAILS.
- W2: after an exhaustion the next item stores ``started_at_attempt = n+1``, and a second
  exhaustion still gives the next episode 5 attempts (A). Mutants ``a-exh-restart-carries`` and
  ``a-both-defences`` FAIL.
"""
import datetime
import json
import random
from decimal import Decimal

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from agents.fi_fleet_health.loop import read_attempt_history_health, run_health_loop
from agents.fi_recall_warranty.attempts import fi_decision_id

SER = TypeSerializer().serialize
VIN = "MRDVEH00000000093"


def _ddbify(obj):
    return json.loads(json.dumps(obj), parse_float=Decimal)


class FakeDDB:
    def __init__(self):
        self.rows = []          # serialized rows
        self.actions = {}       # action_id -> status
        self.order = "fwd"
        self.page = None

    def _ordered(self, order):
        rows = list(self.rows)
        if order == "fwd":
            return rows
        if order == "rev":
            return rows[::-1]
        if order == "sk":
            return sorted(rows, key=lambda r: r["subject_key"]["S"])
        if order == "sk-desc":
            return sorted(rows, key=lambda r: r["subject_key"]["S"], reverse=True)
        if order.startswith("rnd"):
            random.Random(int(order[3:])).shuffle(rows)
            return rows
        raise ValueError(order)

    def query(self, **kw):
        rows = self._ordered(self.order)
        if not self.page:
            return {"Items": rows}
        start = int((kw.get("ExclusiveStartKey") or {}).get("i", {"N": "0"})["N"])
        chunk = rows[start:start + self.page]
        out = {"Items": chunk}
        if start + self.page < len(rows):
            out["LastEvaluatedKey"] = {"i": {"N": str(start + self.page)}}
        return out

    def get_item(self, TableName, Key, **_kw):
        aid = Key["action_id"]["S"]
        st = self.actions.get(aid)
        if st is None:
            return {}
        return {"Item": {"action_id": SER(aid), "status": SER(st), "audit_trail": SER([]),
                         "updated_at": SER(self.now_iso), "created_at": SER(self.now_iso)}}

    def write(self, f):
        sk = f["subject_key"]
        row = {
            "finding_id": SER(f["finding_id"]), "agent_id": SER(f["agent_id"]),
            "finding_kind": SER(f["finding_kind"]), "subject_key": SER(sk),
            "severity": SER(f.get("severity", "informational")),
            "computed_at": SER(f["computed_at"]), "expires_at": SER(f["expires_at"]),
            "inputs_hash": SER(f.get("inputs_hash", "")), "vin": SER(VIN),
            "evidence": SER(_ddbify(f.get("evidence") or [])),
        }
        for i, r in enumerate(self.rows):
            # agents/tier2/writer.py: a LIVE row with the same key is updated in place
            # (finding_id kept); an expired one is left and a new row is Put.
            if r["subject_key"]["S"] == sk and r["expires_at"]["S"] > self.now_iso:
                row["finding_id"] = r["finding_id"]
                self.rows[i] = row
                return row["finding_id"]["S"]
        self.rows.append(row)
        return f["finding_id"]

    def expire_all(self):
        for r in self.rows:
            r["expires_at"] = SER("2000-01-01T00:00:00Z")


ORDERS = ["fwd", "rev", "sk", "sk-desc"] + [f"rnd{i}" for i in range(12)]
PAGES = [None, 1, 2, 3]


def _hist_key(h):
    s = h.get("service")
    if s is None:
        return None
    return (s["attempt_number"], s["queue_status"], s.get("episode_start"),
            tuple(sorted(s.get("closing_needs") or ())))


def check_orders(ddb):
    seen = {}
    for o in ORDERS:
        for p in PAGES:
            ddb.order, ddb.page = o, p
            h, _ = read_attempt_history_health(VIN, ddb_client=ddb, findings_table="f",
                                              actions_table="a")
            seen.setdefault(_hist_key(h), []).append(f"{o}/p{p}")
    ddb.order, ddb.page = "fwd", None
    return seen


def vrow(codes):
    return {
        "vin": VIN, "fleet_id": "fleet-1", "vehicle_id": "veh-1",
        "open_faults": [{"code": c, "band": "attention", "last_seen_at": "2026-10-01T00:00:00Z"}
                        for c in codes],
        "agent2_findings": [], "pm_tasks": [], "position": None, "utilisation": None,
        "catalog_entries": {},
    }


def run_scenario(name, steps):
    """steps: list of (codes, status_for_new_base_attempt). Returns (ok, log)."""
    ddb = FakeDDB()
    t0 = datetime.datetime(2026, 11, 1, 5, 40, tzinfo=datetime.timezone.utc)
    log, ok = [], True
    for run, step in enumerate(steps):
        codes, status = step[0], step[1]
        now = t0 + datetime.timedelta(days=run)
        ddb.now_iso = now.isoformat().replace("+00:00", "Z")
        if len(step) > 2 and step[2] == "expire":
            ddb.expire_all()   # e.g. the vehicle was cut by the service cap for 3+ nights
        seen = check_orders(ddb)
        if len(seen) != 1:
            ok = False
            log.append(f"  run {run}: ORDER-DEPENDENT history {dict((k, v[:3]) for k, v in seen.items())}")
        ddb.order = ORDERS[run % len(ORDERS)]
        ddb.page = PAGES[run % len(PAGES)]
        written = []
        run_health_loop(
            vehicle_rows=[vrow(codes)],
            stock_rows=[{"dealer_id": "D-1", "part_number": "X", "qty_on_hand": 1,
                         "latitude": 0.0, "longitude": 0.0}],
            computed_at=ddb.now_iso,
            grounded_fn=lambda vid: (False, 0),
            history_fn=lambda vin: read_attempt_history_health(
                vin, ddb_client=ddb, findings_table="f", actions_table="a"),
            standing_fn=lambda vin: object(),
            lookup_live=lambda fk: None,
            write_finding=lambda f, st, refresh_on_noop=True: written.append(f),
            recall_visit_fn=lambda vin: {"recall_id": "", "rooftop_dealer_id": "D-1"},
            fault_grounding_enabled=False, now=now,
        )
        svc = [f for f in written if (f.get("subject_key") or "").startswith("service")]
        for f in svc:
            fid = ddb.write(f)
            if not f["subject_key"].startswith("service_exhausted") and status is not None:
                ddb.actions[fi_decision_id(fid)] = status
        ep = [e["value"].get("started_at_attempt") for f in svc for e in f.get("evidence") or []
              if e.get("kind") == "health.episode"]
        log.append(f"  run {run:2d} codes={','.join(codes):17s} wrote={[f['subject_key'] for f in svc]} ep={ep}"
                   f" -> {status if svc and 'exhausted' not in svc[0]['subject_key'] else '-'}")
    return ok, log, ddb


def episodes(ddb):
    """Per-episode base-attempt counts from the stored rows' health.episode evidence."""
    by_ep = {}
    exh = []
    for r in ddb.rows:
        sk = r["subject_key"]["S"]
        seg, n = sk.split("/")
        if seg == "service_exhausted":
            exh.append(int(n))
            continue
        evid = TypeDeserializer().deserialize(r["evidence"])
        st = [e["value"]["started_at_attempt"] for e in evid if e.get("kind") == "health.episode"]
        by_ep.setdefault(int(st[0]) if st else None, []).append(int(n))
    return {k: sorted(v) for k, v in sorted(by_ep.items(), key=lambda kv: (kv[0] is None, kv[0]))}, sorted(exh)


N1, N2, N3 = ["P0A80"], ["P0A80", "P1B00"], ["P0A80", "P1B00", "P2C00"]
F = "failed"
SCENARIOS = {
    # Two exhaustions in a row; each later episode must run 5 attempts.
    "A-exh-exh": ([(N1, F)] * 6 + [(N2, F)] * 6 + [(N3, F)] * 6,
                  {1: [1, 2, 3, 4, 5], 6: [6, 7, 8, 9, 10], 11: [11, 12, 13, 14, 15]},
                  [5, 10, 15]),
    # Exhaustion, then an episode closed by a completed booking, then a full episode.
    "B-exh-completed": ([(N1, F)] * 6 + [(N2, F), (N2, "completed"), (N2, F)] + [(N3, F)] * 6,
                        {1: [1, 2, 3, 4, 5], 6: [6, 7], 8: [8, 9, 10, 11, 12]},
                        [5, 12]),
    # Exhaustion, then an episode dismissed at its first attempt, then a full episode.
    "C-exh-dismissed": ([(N1, F)] * 6 + [(N2, "dismissed"), (N2, F)] + [(N3, F)] * 6,
                        {1: [1, 2, 3, 4, 5], 6: [6], 7: [7, 8, 9, 10, 11]},
                        [5, 11]),
    # Exhaustion; episode 2's first item goes undecided, expires (vehicle cut by the cap
    # for 3+ nights) and is re-Put under the same subject_key with a new finding_id; the
    # new row fails. Episode 2 must still run 5 attempts (6..10), never re-read the old
    # undecided row in some order and rewrite service/6 instead of moving on.
    "D-exh-expired-repost": ([(N1, F)] * 6 + [(N2, None), (N2, F, "expire")] + [(N2, F)] * 5,
                             {1: [1, 2, 3, 4, 5], 6: [6, 6, 7, 8, 9, 10]},
                             [5, 10]),
}




@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_episode_gets_its_budget_in_every_row_order(name):
    steps, want_eps, want_exh = SCENARIOS[name]
    ok, log, ddb = run_scenario(name, steps)
    eps, exh = episodes(ddb)
    assert ok, "order-dependent history:\n" + "\n".join(log)
    assert eps == want_eps, f"episodes {eps} != {want_eps}\n" + "\n".join(log)
    assert exh == want_exh, f"exhausted_at {exh} != {want_exh}"
