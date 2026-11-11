"""Tests for agents/fi_sim_operator/select.py (task 11.2b).

Covers all five classes, every skip reason, and the mutants S1, S2, S7, S8,
S9, S10.
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from agents.fi_sim_operator.select import (
    CLASS_FH_BOOK,
    CLASS_FH_GROUND_BOOK,
    CLASS_RECALL_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    CLASS_WARRANTY_FILE,
    SKIP_DECIDED,
    SKIP_EXPIRED,
    SKIP_HELD,
    SKIP_NEEDS_REVIEW,
    SKIP_NOT_IN_SCOPE_KIND,
    SKIP_NOT_SAFETY_RECALL,
    SKIP_P0_NOT_GROUNDED,
    SKIP_STALE,
    SKIP_TOO_NEW,
    SKIP_UNCLASSIFIED,
    classify_finding,
    FH_AGENT,
    RW_AGENT,
)

# ── time helpers ───────────────────────────────────────────────────────────────
_NOW = "2026-11-06T07:10:00Z"
_RECENT = "2026-11-06T05:00:00Z"    # 2h 10m ago — within 26 h
_STALE_AT = "2026-11-04T05:00:00Z"  # > 26 h ago

_EXPIRES_FUTURE = "2026-11-09T07:10:00Z"
_EXPIRES_PAST = "2026-11-05T07:09:00Z"  # before _NOW

_VIN = "MRDTEST0000000001"  # 17 chars
_VIN2 = "MRDTEST0000000002"  # 17 chars
_FLEET = "demo-lastmile-delivery"
_CAMPAIGN = "MRD-2026-004"


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _base_finding(
    *,
    finding_id: str | None = None,
    agent_id: str = FH_AGENT,
    action_kind: str = "ground_vehicle",
    target_system: str = "cms",
    vin: str = _VIN,
    fleet_id: str = _FLEET,
    severity: str = "p0",
    computed_at: str = _RECENT,
    expires_at: str = _EXPIRES_FUTURE,
    parameters: dict | None = None,
    evidence: list | None = None,
) -> dict:
    rec_params = parameters if parameters is not None else {
        "vehicle_vin": vin,
        "fault_codes": ["P0001"],
    }
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": agent_id,
        "finding_kind": "fleet.health.service",
        "scope": {"vin": vin},
        "fleet_id": fleet_id,
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": target_system,
            "parameters": rec_params,
        },
        "disclosure_txt": "Stop-driving fault P0001: ground until the repair is recorded",
        "evidence": evidence if evidence is not None else [],
    }


def _classify(finding, *, decided=None, hold_vins=None, grounded=None, pending=None):
    return classify_finding(
        finding=finding,
        decided_ids=decided or set(),
        now_iso=_NOW,
        hold_vins=frozenset(hold_vins or []),
        # 11.3b item 7: a list of VINs means "grounded for the fixtures' code P0001";
        # a dict is passed through as {vin: codes}.
        grounded_codes=(grounded if isinstance(grounded, dict)
                        else {v: frozenset({"P0001"}) for v in (grounded or [])}),
        recall_pending_groundings=set(pending or []),
    )


# ── Class fh_ground_book ───────────────────────────────────────────────────────

class TestFhGroundBook:
    def test_fh_ground_vehicle_with_fault_codes_is_fh_ground_book(self):
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            parameters={"vehicle_vin": _VIN, "fault_codes": ["P0001"]},
        )
        cf = _classify(f)
        assert cf.class_name == CLASS_FH_GROUND_BOOK
        assert cf.skip_reason is None

    def test_fh_ground_vehicle_empty_fault_codes_is_unclassified(self):
        """Empty fault_codes list: no structural match → unclassified."""
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            parameters={"vehicle_vin": _VIN, "fault_codes": []},
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_UNCLASSIFIED


# ── Class fh_book ──────────────────────────────────────────────────────────────

class TestFhBook:
    def test_fh_book_non_p0_is_fh_book(self):
        """book_fleet_service with severity != p0 → fh_book."""
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="book_fleet_service",
            target_system="dms",
            severity="attention",
            parameters={
                "vehicle_vin": _VIN,
                "needs": [{"need_id": "fault:P0001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_month",
            },
        )
        cf = _classify(f)
        assert cf.class_name == CLASS_FH_BOOK
        assert cf.skip_reason is None

    def test_fh_book_p0_with_grounded_vin_is_fh_book(self):
        """p0 booking with VIN in grounded_vins → fh_book (already grounded)."""
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="book_fleet_service",
            target_system="dms",
            severity="p0",
            parameters={
                "vehicle_vin": _VIN,
                "needs": [{"need_id": "fault:P0001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_week",
            },
        )
        cf = _classify(f, grounded=[_VIN])
        assert cf.class_name == CLASS_FH_BOOK
        assert cf.skip_reason is None

    def test_fh_book_p0_not_grounded_skip(self):
        """p0 booking without grounding → p0_not_grounded.

        Mutant guard: if grounded check were dropped, a p0 car would be booked
        before it's grounded (spec D10, seam: grounding before booking).
        """
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="book_fleet_service",
            target_system="dms",
            severity="p0",
            parameters={
                "vehicle_vin": _VIN,
                "needs": [{"need_id": "fault:P0001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_week",
            },
        )
        cf = _classify(f)  # grounded_vins empty
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_P0_NOT_GROUNDED


# ── Class recall_ground_book ───────────────────────────────────────────────────

def _recall_campaign_entry(vin: str, campaign_id: str, recall_class: str = "safety") -> dict:
    return {
        # Persisted shape (the Finding writer's kind/value), as read from DynamoDB.
        "vin": vin,
        "provenance": "live",
        "kind": "recall.campaign",
        "value": {
            "campaign_id": campaign_id,
            "recall_class": recall_class,
            "do_not_drive": True,
        },
    }


class TestRecallGroundBook:
    def test_rw_ground_vehicle_safety_recall_is_recall_ground_book(self):
        """RW agent, ground_vehicle with recall_id + safety campaign → recall_ground_book."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            severity="p0",
            parameters={"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN},
            evidence=[_recall_campaign_entry(_VIN2, _CAMPAIGN, "safety")],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name == CLASS_RECALL_GROUND_BOOK

    def test_rw_ground_vehicle_non_safety_recall_skip(self):
        """Non-safety recall campaign → not_safety_recall.

        Mutant guard S9: if safety check is dropped, non-safety recalls are booked.
        """
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            severity="attention",
            parameters={"vehicle_vin": _VIN2, "recall_id": "MRD-2026-NONSAFE"},
            evidence=[_recall_campaign_entry(_VIN2, "MRD-2026-NONSAFE", "information")],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL

    def test_rw_ground_vehicle_no_evidence_skip(self):
        """No recall.campaign evidence → not_safety_recall."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            parameters={"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN},
            evidence=[],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL

    def test_rw_ground_vehicle_campaign_mismatch_skip(self):
        """Evidence campaign_id differs from parameters recall_id → not_safety_recall."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="ground_vehicle",
            target_system="cms",
            parameters={"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN},
            evidence=[_recall_campaign_entry(_VIN2, "MRD-OTHER", "safety")],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL


# ── Class recall_book ──────────────────────────────────────────────────────────

class TestRecallBook:
    def test_recall_book_safety_no_pending_grounding(self):
        """book_recall_service safety recall with no pending grounding → recall_book."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="book_recall_service",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN, "dealer_id": "DLR-002"},
            evidence=[_recall_campaign_entry(_VIN2, _CAMPAIGN, "safety")],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name == CLASS_RECALL_BOOK

    def test_recall_book_non_safety_skip(self):
        """Non-safety recall booking → not_safety_recall (mutant guard S9)."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="book_recall_service",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN2, "recall_id": "MRD-2026-NONSAFE"},
            evidence=[_recall_campaign_entry(_VIN2, "MRD-2026-NONSAFE", "information")],
            vin=_VIN2,
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL

    def test_recall_book_with_pending_grounding_skip(self):
        """A pending recall grounding exists for same VIN+campaign → paired (S8).

        This booking will be the pair for the grounding; it's not a standalone book.
        S8: counts as 'paired' so skip counts read correctly, not 'unclassified'.
        """
        from agents.fi_sim_operator.select import SKIP_PAIRED
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="book_recall_service",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN, "dealer_id": "DLR-002"},
            evidence=[_recall_campaign_entry(_VIN2, _CAMPAIGN, "safety")],
            vin=_VIN2,
        )
        cf = _classify(f, pending=[(_VIN2, _CAMPAIGN)])
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_PAIRED


# ── Class warranty_file ────────────────────────────────────────────────────────

def _warranty_verdict_entry(vin: str, verdict: str) -> dict:
    return {
        # Persisted shape (the Finding writer's kind/value), as read from DynamoDB.
        "vin": vin,
        "provenance": "live",
        "kind": "warranty.verdict",
        "value": {
            "verdict": verdict,
            "reason": None,
            "matched_rule_id": "R001",
            "terms_version": "1.0",
        },
    }


class TestWarrantyFile:
    def test_warranty_approved_is_warranty_file(self):
        """submit_warranty_claim with one approved verdict → warranty_file."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="submit_warranty_claim",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN},
            evidence=[_warranty_verdict_entry(_VIN, "approved")],
        )
        cf = _classify(f)
        assert cf.class_name == CLASS_WARRANTY_FILE

    def test_warranty_requires_review_skip(self):
        """verdict == 'requires_review' → needs_review (mutant guard S8).

        If this were to pass, a claim needing DMS review would be filed
        automatically — wrong.
        """
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="submit_warranty_claim",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN},
            evidence=[_warranty_verdict_entry(_VIN, "requires_review")],
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NEEDS_REVIEW

    def test_warranty_denied_skip(self):
        """verdict == 'denied' → needs_review (not approvable by stand-in)."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="submit_warranty_claim",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN},
            evidence=[_warranty_verdict_entry(_VIN, "denied")],
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NEEDS_REVIEW

    def test_warranty_no_verdict_entry_skip(self):
        """No warranty.verdict evidence → needs_review."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="submit_warranty_claim",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN},
            evidence=[],
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NEEDS_REVIEW

    def test_warranty_two_verdict_entries_skip(self):
        """More than one warranty.verdict entry → needs_review (design: exactly one)."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="submit_warranty_claim",
            target_system="dms",
            severity="attention",
            parameters={"vehicle_vin": _VIN},
            evidence=[
                _warranty_verdict_entry(_VIN, "approved"),
                _warranty_verdict_entry(_VIN, "approved"),
            ],
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NEEDS_REVIEW


# ── Skip reasons: pre-filter ───────────────────────────────────────────────────

class TestPreFilterSkips:
    def test_no_recommended_action_skip(self):
        """A Finding with no recommended_action → not_in_scope_kind."""
        f = {
            "finding_id": _fid(),
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": _RECENT,
            "expires_at": _EXPIRES_FUTURE,
            "evidence": [],
        }
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND

    def test_cost_action_kind_skip(self):
        """apply_cost_action → not_in_scope_kind (mutant guard S1)."""
        f = _base_finding(action_kind="apply_cost_action", target_system="adp")
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND

    def test_move_vehicles_kind_skip(self):
        """move_vehicles → not_in_scope_kind (mutant guard S1)."""
        f = _base_finding(action_kind="move_vehicles", target_system="cms")
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND

    def test_unknown_kind_skip(self):
        """An unknown action_kind → not_in_scope_kind (mutant guard S10: allowlist)."""
        f = _base_finding(action_kind="some_future_kind", target_system="cms")
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND

    def test_expired_finding_skip(self):
        """expires_at <= now_iso → expired."""
        f = _base_finding(expires_at=_EXPIRES_PAST)
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_EXPIRED

    def test_decided_finding_skip(self):
        """finding_id in decided_ids → decided."""
        fid = _fid()
        f = _base_finding(finding_id=fid)
        cf = _classify(f, decided={fid})
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_DECIDED

    def test_stale_finding_skip(self):
        """computed_at > 26 h ago → stale (mutant guard S7).

        If the staleness check were dropped, findings based on data from the
        11-03 orphaned regeneration would be approved.
        """
        f = _base_finding(computed_at=_STALE_AT)
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_STALE

    def test_held_vin_skip(self):
        """VIN in hold_vins → held (mutant guard S2)."""
        f = _base_finding(vin=_VIN)
        cf = _classify(f, hold_vins=[_VIN])
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_HELD

    def test_non_17_char_vin_skip(self):
        """VIN shorter than 17 chars → unclassified."""
        f = _base_finding(vin="SHORTVIN")
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_UNCLASSIFIED

    def test_wrong_target_system_skip(self):
        """book_fleet_service with target_system 'cms' (wrong) → unclassified."""
        f = _base_finding(
            action_kind="book_fleet_service",
            target_system="cms",  # should be dms
            severity="attention",
            parameters={
                "vehicle_vin": _VIN,
                "needs": [{"need_id": "fault:P0001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_month",
            },
        )
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_UNCLASSIFIED


class TestMutantS1CostActionAdmitted:
    """S1: cost action or move admitted.

    The allowlist check must be the *only* gate on action_kind; a denylist
    implementation could forget to list new or future kinds.
    """

    def test_apply_cost_action_never_admitted(self):
        """apply_cost_action must always be not_in_scope_kind."""
        f = _base_finding(
            agent_id=RW_AGENT,
            action_kind="apply_cost_action",
            target_system="adp",
            parameters={"vehicle_vin": _VIN},
        )
        cf = _classify(f)
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND

    def test_move_vehicles_never_admitted(self):
        f = _base_finding(
            agent_id=FH_AGENT,
            action_kind="move_vehicles",
            target_system="cms",
            parameters={"vehicle_vin": _VIN},
        )
        cf = _classify(f)
        assert cf.skip_reason == SKIP_NOT_IN_SCOPE_KIND


class TestMutantS2HeldVin:
    """S2: held VIN or held vehicle id admitted."""

    def test_held_vin_never_admitted(self):
        """A finding whose VIN is in hold_vins must be skipped."""
        hold = "MRDHOLD000000015A"  # 17 chars
        f = _base_finding(vin=hold)
        cf = _classify(f, hold_vins=[hold])
        assert cf.skip_reason == SKIP_HELD
        assert cf.class_name is None
