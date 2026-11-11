"""select.py side of 11.3b: item 7 (grounded codes) and the evidence shape.

The evidence fixtures here are copied from staging Findings (read-only, 11-06):
persisted entries are ``{"kind": ..., "value": {...}, "vin", "provenance", ...}``.
The first pass read the in-memory ``{"recall.campaign": {...}}`` shape, so on real
data every recall booking skipped as ``not_safety_recall`` and every claim as
``needs_review`` while its tests (built on the in-memory shape) stayed green.

Mutants (records/11/mutants-11.3b-s2.md): H7 a VIN counts as grounded without its
codes; E1 the in-memory evidence shape read again.
"""
from __future__ import annotations

from agents.fi_sim_operator.select import (
    CLASS_FH_BOOK,
    CLASS_RECALL_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    CLASS_WARRANTY_FILE,
    FH_AGENT,
    RW_AGENT,
    SKIP_NEEDS_REVIEW,
    SKIP_NOT_SAFETY_RECALL,
    SKIP_P0_NOT_GROUNDED,
    classify_finding,
)

_VIN = "MRDAZ01S0PC00HZG4"
_NOW = "2026-11-06T07:10:00Z"


def _finding(agent, kind, target, params, evidence, severity="p0"):
    return {
        "finding_id": "FIND-11-3b", "agent_id": agent, "fleet_id": "demo-rental-carshare",
        "scope": {"vin": _VIN}, "severity": severity,
        "computed_at": "2026-11-06T05:40:00Z", "expires_at": "2026-11-09T05:40:00Z",
        "recommended_action": {"action_kind": kind, "target_system": target, "parameters": params},
        "evidence": evidence,
    }


def _ev(kind, value):
    return {"computed_at": "2026-11-06T05:00:00Z", "kind": kind, "provenance": "live",
            "source": "agent-fi-recall-warranty", "source_ref": "run", "value": value, "vin": _VIN}


_CAMPAIGN = _ev("recall.campaign", {"campaign_id": "MRD-2026-004", "catalog_version": "1.0.0",
                                    "do_not_drive": True, "park_outside": "true",
                                    "recall_class": "safety", "received_date": "2026-09-30"})
_VERDICT = _ev("warranty.verdict", {"verdict": "approved", "matched_rule_id": "RULE-TERM-HV_BATTERY",
                                    "reason": "covered under hv_battery (8yr/100000mi)",
                                    "terms_version": "1.0.0"})


def _classify(f, *, grounded=None, pending=None):
    return classify_finding(finding=f, decided_ids=set(), now_iso=_NOW, hold_vins=frozenset(),
                            grounded_codes=grounded or {}, recall_pending_groundings=pending or set())


class TestPersistedEvidenceShape:
    def test_safety_recall_booking_from_a_real_shaped_finding(self):
        f = _finding(RW_AGENT, "book_recall_service", "dms",
                     {"dealer_id": "dealer-seattle", "recall_id": "MRD-2026-004", "vehicle_vin": _VIN},
                     [_CAMPAIGN, _ev("recall.rule", {"x": 1})])
        assert _classify(f).class_name == CLASS_RECALL_BOOK

    def test_recall_grounding_from_a_real_shaped_finding(self):
        f = _finding(RW_AGENT, "ground_vehicle", "cms",
                     {"recall_id": "MRD-2026-004", "vehicle_vin": _VIN}, [_CAMPAIGN])
        assert _classify(f).class_name == CLASS_RECALL_GROUND_BOOK

    def test_approved_claim_from_a_real_shaped_finding(self):
        f = _finding(RW_AGENT, "submit_warranty_claim", "dms",
                     {"vehicle_vin": _VIN, "fault_code": "P1E00"}, [_VERDICT], severity="attention")
        assert _classify(f).class_name == CLASS_WARRANTY_FILE

    def test_in_memory_shape_is_not_read(self):
        # The agents' pre-writer shape never reaches DynamoDB; reading it would mean
        # the reader matches something other than what the writer stores.
        f = _finding(RW_AGENT, "submit_warranty_claim", "dms", {"vehicle_vin": _VIN},
                     [{"vin": _VIN, "provenance": "live", "warranty.verdict": {"verdict": "approved"}}],
                     severity="attention")
        assert _classify(f).skip_reason == SKIP_NEEDS_REVIEW

    def test_non_safety_recall_skips(self):
        camp = _ev("recall.campaign", {**_CAMPAIGN["value"], "recall_class": "compliance"})
        f = _finding(RW_AGENT, "book_recall_service", "dms",
                     {"dealer_id": "d", "recall_id": "MRD-2026-004", "vehicle_vin": _VIN}, [camp])
        assert _classify(f).skip_reason == SKIP_NOT_SAFETY_RECALL


def _p0_booking(codes):
    return _finding(FH_AGENT, "book_fleet_service", "dms",
                    {"vehicle_vin": _VIN, "dealer_id": "dealer-austin", "urgency": "this_week",
                     "needs": [{"need_id": f"fault:{c}", "labor_op": "L"} for c in codes]
                     + [{"need_id": "pm:TIRE_ROTATION", "labor_op": "T"}]}, [])


class TestGroundedCodes:
    def test_grounded_for_every_code_books(self):
        f = _p0_booking(["P0A0D", "P0A7F"])
        assert _classify(f, grounded={_VIN: frozenset({"P0A0D", "P0A7F", "P0001"})}).class_name == CLASS_FH_BOOK

    def test_grounded_for_another_code_waits(self):
        f = _p0_booking(["P0A0D", "P0A7F"])
        cf = _classify(f, grounded={_VIN: frozenset({"P0A0D"})})
        assert cf.skip_reason == SKIP_P0_NOT_GROUNDED

    def test_another_vin_grounded_waits(self):
        f = _p0_booking(["P0A0D"])
        assert _classify(f, grounded={"MRDOTHER000000001": frozenset({"P0A0D"})}).skip_reason == SKIP_P0_NOT_GROUNDED

    def test_p0_booking_without_a_fault_need_waits(self):
        f = _p0_booking([])
        assert _classify(f, grounded={_VIN: frozenset({"P0A0D"})}).skip_reason == SKIP_P0_NOT_GROUNDED
