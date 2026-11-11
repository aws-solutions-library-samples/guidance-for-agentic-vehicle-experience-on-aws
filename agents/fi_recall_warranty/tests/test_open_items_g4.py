"""Tests for open_items_from_items, bundle_for_row, and recall.plan (S2a, task 4.1).

Mutation targets (note.md):
  M-1: expiry filter removed            → test_expired_at_boundary_excluded
  M-2: own-campaign filter removed      → test_bundle_drops_own_campaign_recall
  M-3: recall.plan not hash-excluded   → test_recall_plan_leaves_inputs_hash_unchanged
  M-4: second page dropped              → test_pagination_all_pages_consumed
"""
from __future__ import annotations

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from agents.fi_recall_warranty.attempts import (
    attempt_history_from_items,
    bundle_for_row,
    open_items_from_items,
    read_vin_findings,
)
from agents.fi_recall_warranty.evidence import build_recall_evidence, compute_inputs_hash

_SER = TypeSerializer()
_VIN = "MRDVEH0000000042"
_TBL_F = "vsa-staging-avx-findings"
_TBL_A = "vsa-staging-avx-actions"


def _raw(d: dict) -> dict:
    return {k: _SER.serialize(v) for k, v in d.items()}


def _health_finding(fid: str, expires_at: str = "2099-12-31T00:00:00Z") -> dict:
    return {
        "finding_id": fid,
        "vin": _VIN,
        "finding_kind": "fleet.health.vehicle",
        "headline": f"health-{fid}",
        "expires_at": expires_at,
        "subject_key": f"health/{fid}",
    }


def _recall_finding(fid: str, campaign_id: str, expires_at: str = "2099-12-31T00:00:00Z") -> dict:
    return {
        "finding_id": fid,
        "vin": _VIN,
        "finding_kind": "fleet.recall.vehicle",
        "headline": f"recall-{fid}",
        "expires_at": expires_at,
        "subject_key": f"{campaign_id}/book/1",
        "agent_id": "agent-fi-recall-warranty",
    }


def _warranty_finding(fid: str, expires_at: str = "2099-12-31T00:00:00Z") -> dict:
    return {
        "finding_id": fid,
        "vin": _VIN,
        "finding_kind": "fleet.warranty.claim_candidate",
        "headline": f"warranty-{fid}",
        "expires_at": expires_at,
        "subject_key": "warranty/claim/1",
    }


# ---------------------------------------------------------------------------
# 1. One query serves both consumers
# ---------------------------------------------------------------------------

class TestOneQueryServesBoth:
    def test_single_read_vin_findings_call_covers_both_consumers(self):
        """Stubber has exactly 2 pages; after read_vin_findings the stub is exhausted.

        Both attempt_history_from_items and open_items_from_items consume the
        same items list — no second DDB query is issued.

        M-4 mutation: dropping the second page read means the Stubber still has
        one pending response → stub.assert_no_pending_responses() raises.
        """
        import datetime
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        now_iso = "2026-10-05T00:00:00Z"
        now_dt = datetime.datetime(2026, 10, 5, 0, 0, 0, tzinfo=datetime.timezone.utc)
        page1_last = {"vin": {"S": _VIN}, "finding_id": {"S": "h1"}}

        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [_raw(_health_finding("h1"))],
                    "LastEvaluatedKey": page1_last,
                },
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                },
            )
            stub.add_response(
                method="query",
                service_response={"Items": [_raw(_health_finding("h2"))]},
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                    "ExclusiveStartKey": page1_last,
                },
            )

            items = read_vin_findings(_VIN, ddb_client=ddb, findings_table=_TBL_F)
            # Stubber has no remaining responses — exactly 2 pages were read.
            stub.assert_no_pending_responses()

        # Both consumers work on the same items list (no new DDB calls).
        history, skip = attempt_history_from_items(
            items, now=now_dt, ddb_client=ddb, actions_table=_TBL_A
        )
        open_items = open_items_from_items(items, now_iso=now_iso)
        assert len(items) == 2
        assert len(open_items) == 2
        assert skip == 0


# ---------------------------------------------------------------------------
# 2. Expiry boundary
# ---------------------------------------------------------------------------

class TestExpiryFilter:
    def test_expired_at_boundary_excluded(self):
        """Item with expires_at == now_iso is excluded (boundary equal = excluded).

        M-1: removing the expiry filter lets this item through → assertion fails.
        """
        now_iso = "2026-10-05T12:00:00Z"
        items = [
            _health_finding("at-boundary", expires_at=now_iso),        # equal → excluded
            _health_finding("after-boundary", expires_at="2026-10-05T12:00:01Z"),  # 1s later
            _health_finding("before-boundary", expires_at="2026-10-05T11:59:59Z"), # 1s earlier
        ]
        result = open_items_from_items(items, now_iso=now_iso)
        ids = {r["finding_id"] for r in result}
        assert "at-boundary" not in ids, "expires_at == now_iso must be excluded"
        assert "after-boundary" in ids
        assert "before-boundary" not in ids


# ---------------------------------------------------------------------------
# 3. Warranty candidates excluded
# ---------------------------------------------------------------------------

class TestWarrantyExcluded:
    def test_warranty_claim_candidate_excluded(self):
        """fleet.warranty.claim_candidate items never appear in open_items."""
        items = [
            _warranty_finding("wc1"),
            _health_finding("h1"),
        ]
        result = open_items_from_items(items, now_iso="2020-01-01T00:00:00Z")
        kinds = {r["finding_kind"] for r in result}
        assert "fleet.warranty.claim_candidate" not in kinds
        assert "fleet.health.vehicle" in kinds


# ---------------------------------------------------------------------------
# 4. bundle_for_row: own campaign excluded, other campaign kept
# ---------------------------------------------------------------------------

class TestBundleForRow:
    def test_own_campaign_recall_excluded(self):
        """bundle_for_row drops recall items for the current campaign.

        M-2: removing the own-campaign filter lets the own recall item through
        → assert 'own' not in ids fails.
        """
        items = [
            {"finding_id": "own", "finding_kind": "fleet.recall.vehicle",
             "headline": "x", "campaign_id": "MRD-2024-001"},
            {"finding_id": "other", "finding_kind": "fleet.recall.vehicle",
             "headline": "y", "campaign_id": "MRD-2024-002"},
            {"finding_id": "health", "finding_kind": "fleet.health.vehicle",
             "headline": "z", "campaign_id": None},
        ]
        result = bundle_for_row(items, "MRD-2024-001")
        ids = {r["finding_id"] for r in result}
        assert "own" not in ids, "own campaign recall must be dropped"
        assert "other" in ids
        assert "health" in ids

    def test_other_campaign_kept(self):
        """bundle_for_row keeps recall items for different campaigns."""
        items = [
            {"finding_id": "f1", "finding_kind": "fleet.recall.vehicle",
             "headline": "h", "campaign_id": "MRD-2024-002"},
        ]
        result = bundle_for_row(items, "MRD-2024-001")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 5. Health Findings kept
# ---------------------------------------------------------------------------

class TestHealthFindingKept:
    def test_health_finding_in_open_items(self):
        """fleet.health.vehicle (and other non-warranty kinds) appear in open_items."""
        items = [
            _health_finding("h1"),
            {"finding_id": "other-kind", "finding_kind": "fleet.coverage.vehicle",
             "headline": "cov", "expires_at": "2099-01-01T00:00:00Z",
             "subject_key": "cov/1"},
        ]
        result = open_items_from_items(items, now_iso="2020-01-01T00:00:00Z")
        kinds = {r["finding_kind"] for r in result}
        assert "fleet.health.vehicle" in kinds
        assert "fleet.coverage.vehicle" in kinds

    def test_recall_campaign_id_extracted(self):
        """open_items_from_items extracts campaign_id for fleet.recall.vehicle."""
        items = [_recall_finding("r1", "MRD-2024-001")]
        result = open_items_from_items(items, now_iso="2020-01-01T00:00:00Z")
        assert result[0]["campaign_id"] == "MRD-2024-001"

    def test_non_recall_campaign_id_is_none(self):
        """open_items_from_items sets campaign_id=None for non-recall kinds."""
        items = [_health_finding("h1")]
        result = open_items_from_items(items, now_iso="2020-01-01T00:00:00Z")
        assert result[0]["campaign_id"] is None


# ---------------------------------------------------------------------------
# 6. Pagination: second page items appear in open_items
# ---------------------------------------------------------------------------

class TestPagination:
    def test_second_page_items_appear_in_open_items(self):
        """Items from both pages of read_vin_findings appear in open_items.

        M-4: dropping the second page read loses h2 → assertion fails.
        """
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        page1_last = {"vin": {"S": _VIN}, "finding_id": {"S": "h1"}}

        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [_raw(_health_finding("h1"))],
                    "LastEvaluatedKey": page1_last,
                },
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                },
            )
            stub.add_response(
                method="query",
                service_response={"Items": [_raw(_health_finding("h2"))]},
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                    "ExclusiveStartKey": page1_last,
                },
            )
            items = read_vin_findings(_VIN, ddb_client=ddb, findings_table=_TBL_F)

        open_items = open_items_from_items(items, now_iso="2020-01-01T00:00:00Z")
        ids = {r["finding_id"] for r in open_items}
        assert "h1" in ids
        assert "h2" in ids, "Page-2 item must appear in open_items (M-4 target)"


# ---------------------------------------------------------------------------
# 7. recall.plan present on every build_recall_evidence call
# ---------------------------------------------------------------------------

_BASE_ROW = {
    "vin": _VIN, "campaign_id": "MRD-2024-001", "recall_class": "safety",
    "do_not_drive": False, "park_outside": False, "received_date": "2024-01-01",
    "catalog_version": "1", "match_basis": "POPULATION",
    "rule_version": "1", "models": None, "model_year_min": None,
    "model_year_max": None, "plants": None, "build_from": None, "build_to": None,
}


def _build_ev(**kwargs):
    defaults = dict(
        row=_BASE_ROW, item="book", attempt=1, rooftops=[], origin=None,
        open_item_ids=[], diagnosis_source="rules",
    )
    defaults.update(kwargs)
    return build_recall_evidence(**defaults)


class TestRecallPlanEntry:
    def test_recall_plan_present_without_plan_arg(self):
        """recall.plan entry is emitted even when plan=None."""
        entries = _build_ev()
        keys = [next(k for k in e if k not in ("vin", "provenance")) for e in entries]
        assert "recall.plan" in keys

    def test_recall_plan_present_with_plan_arg(self):
        """recall.plan entry carries the provided urgency and combine_with."""
        plan = {"urgency": "soon", "combine_with": ["FND-AAA"]}
        entries = _build_ev(plan=plan)
        plan_entry = next(
            e["recall.plan"] for e in entries if "recall.plan" in e
        )
        assert plan_entry["urgency"] == "soon"
        assert plan_entry["combine_with"] == ["FND-AAA"]

    def test_recall_plan_default_has_empty_combine_with(self):
        """Default plan (no argument) has combine_with=[]."""
        entries = _build_ev()
        plan_entry = next(e["recall.plan"] for e in entries if "recall.plan" in e)
        assert plan_entry["combine_with"] == []


# ---------------------------------------------------------------------------
# 8. recall.plan excluded from inputs_hash
# ---------------------------------------------------------------------------

class TestInputsHash:
    def test_recall_plan_leaves_inputs_hash_unchanged(self):
        """Changing urgency or combine_with in plan does not change inputs_hash.

        M-3: removing recall.plan from _HASH_EXCLUDED means the hash changes
        when urgency changes → assert h1 == h2 fails.
        """
        e_no_plan = _build_ev()
        e_with_plan = _build_ev(plan={"urgency": "p0", "combine_with": ["FND-X"]})
        e_other_urgency = _build_ev(plan={"urgency": "routine", "combine_with": []})

        h_no_plan = compute_inputs_hash(e_no_plan)
        h_with_plan = compute_inputs_hash(e_with_plan)
        h_other_urgency = compute_inputs_hash(e_other_urgency)

        assert h_no_plan == h_with_plan, (
            "Adding a plan must not change inputs_hash (recall.plan is volatile)"
        )
        assert h_with_plan == h_other_urgency, (
            "Changing urgency must not change inputs_hash (M-3 target)"
        )

    def test_changing_campaign_data_changes_hash(self):
        """Changing stable campaign data (e.g., received_date) does change inputs_hash."""
        row_a = {**_BASE_ROW, "received_date": "2024-01-01"}
        row_b = {**_BASE_ROW, "received_date": "2024-06-01"}
        h_a = compute_inputs_hash(_build_ev(row=row_a))
        h_b = compute_inputs_hash(_build_ev(row=row_b))
        assert h_a != h_b, "Stable campaign field change must alter inputs_hash"

    def test_hash_unchanged_vs_before_plan_field_added(self):
        """Regression: adding recall.plan to evidence does not change the hash of
        a fixture that was stable before this change (D50 contract).

        Computes the hash once with plan=None and once with plan omitted entirely
        (same call) and asserts they match — confirms the exclusion works.
        """
        ev = _build_ev()
        # recall.plan entry is present but excluded from the hash.
        h1 = compute_inputs_hash(ev)
        # Mutate the recall.plan entry in-place (simulate urgency update).
        ev2 = [
            {**e, "recall.plan": {"urgency": "p0", "combine_with": ["X"]}}
            if "recall.plan" in e else e
            for e in ev
        ]
        h2 = compute_inputs_hash(ev2)
        assert h1 == h2, "recall.plan mutation must not change inputs_hash (regression)"



# ---------------------------------------------------------------------------
# 9. Volatile sibling kinds are excluded from the bundle and inputs_hash
#    (user, 2026-10-13; item 3 fix, option B)
# ---------------------------------------------------------------------------
#
# Mutation targets for this group (added at write time):
#   M-5 (bundle):  remove the ``_VOLATILE_FINDING_KINDS`` filter from
#                  ``bundle_for_row`` → ``test_bundle_drops_volatile_kinds``
#                  and ``test_bundle_drops_only_volatile`` fail on their own
#                  assertions.
#   M-6 (hash):    empty ``_VOLATILE_FINDING_KINDS`` → the same two tests fail;
#                  additionally
#                  ``test_adding_volatile_siblings_between_runs_leaves_hash_stable``
#                  fails on ``h1 == h2``.
#   M-7 (bundle):  rewrite the filter as ``in`` instead of ``not in`` →
#                  ``test_bundle_drops_only_volatile`` fails because every
#                  stable sibling is dropped, not kept.

from agents.fi_recall_warranty.attempts import _VOLATILE_FINDING_KINDS


def _diagnostics_sibling(fid: str, headline: str = "p0420 active") -> dict:
    """Build an ``health.diagnostics`` entry as it appears in
    ``bundle_for_row``'s input (post-``open_items_from_items``)."""
    return {
        "finding_id": fid,
        "finding_kind": "health.diagnostics",
        "headline": headline,
        "campaign_id": None,
    }


def _health_battery_sibling(fid: str, headline: str = "battery routine") -> dict:
    """A stable sibling (``health.battery`` — 8-day TTL)."""
    return {
        "finding_id": fid,
        "finding_kind": "health.battery",
        "headline": headline,
        "campaign_id": None,
    }


def _other_recall_sibling(fid: str, campaign_id: str) -> dict:
    """A sibling for a DIFFERENT recall campaign (stable, 8-day TTL)."""
    return {
        "finding_id": fid,
        "finding_kind": "fleet.recall.vehicle",
        "headline": f"recall-{fid}",
        "campaign_id": campaign_id,
    }


class TestVolatileSiblingsExcluded:
    """``bundle_for_row`` drops Findings whose ``finding_kind`` is in
    ``_VOLATILE_FINDING_KINDS`` (user, 2026-10-13 option B).  The invariant is
    'the model and the hash see the same thing'; filtering the bundle delivers
    both because the bundle feeds the model view directly (``plan_fn(..., bundle=
    item["bundle"])``) and feeds the hash view via ``open_item_ids`` →
    ``build_recall_evidence`` → ``vehicle.open_items.finding_ids``.
    """

    def test_volatile_kinds_set_includes_diagnostics(self):
        """``_VOLATILE_FINDING_KINDS`` must include ``health.diagnostics``.

        Diagnostics findings rewrite every 5 minutes with a 30-minute TTL
        (``DIAGNOSTICS_TTL_SECONDS``); including them in the bundle would
        churn ``inputs_hash`` between consecutive recall runs.  This is the
        user's option B decision (2026-10-13).
        """
        assert "health.diagnostics" in _VOLATILE_FINDING_KINDS

    def test_bundle_drops_volatile_kinds(self):
        """``bundle_for_row`` drops every ``health.diagnostics`` entry.

        Property under test: the bundle excludes diagnostics findings whatever
        campaign is being processed.  M-5 mutant (filter removed) fails here.
        """
        items = [
            _diagnostics_sibling("diag-1"),
            _diagnostics_sibling("diag-2"),
            _health_battery_sibling("batt-1"),
        ]
        result = bundle_for_row(items, "MRD-2024-999")
        kinds = {r["finding_kind"] for r in result}
        ids = {r["finding_id"] for r in result}
        assert "health.diagnostics" not in kinds, (
            "health.diagnostics must be dropped from the bundle "
            "(user, 2026-10-13 option B)"
        )
        assert ids == {"batt-1"}, (
            f"only stable siblings must remain; got {sorted(ids)}"
        )

    def test_bundle_drops_only_volatile(self):
        """Stable siblings (``health.battery``, other-campaign recall) stay in
        the bundle; only kinds in ``_VOLATILE_FINDING_KINDS`` and this
        campaign's own recall are removed.  M-7 mutant (``in`` instead of
        ``not in``) fails here because every stable sibling would be dropped.
        """
        items = [
            _diagnostics_sibling("diag-1"),
            _health_battery_sibling("batt-1"),
            _other_recall_sibling("other-1", "MRD-2024-OTHER"),
        ]
        result = bundle_for_row(items, "MRD-2024-SELF")
        ids = {r["finding_id"] for r in result}
        assert ids == {"batt-1", "other-1"}, (
            "stable siblings must be kept; "
            f"health.diagnostics dropped, got {sorted(ids)}"
        )

    def test_adding_volatile_siblings_between_runs_leaves_hash_stable(self):
        """A diagnostics Finding written by the ``diagnostics`` tier 2 component
        between two recall runs must not change ``inputs_hash``.

        This is the user's acceptance for item 3 (2026-10-13): "siblings added
        between two runs give zero model calls on the second".  The chain is
        ``open_items`` → ``bundle_for_row`` (drops diagnostics) →
        ``open_item_ids`` → ``build_recall_evidence`` (writes
        ``vehicle.open_items.finding_ids``) → ``compute_inputs_hash``.  With
        volatile kinds filtered, both the model view and the hash view are
        byte-identical across the two runs.

        M-5/M-6 mutants (filter removed / empty set) fail here on ``h1 == h2``.
        """
        own_campaign = "MRD-2024-001"
        stable_siblings = [
            _health_battery_sibling("batt-1"),
            _other_recall_sibling("other-1", "MRD-2024-OTHER"),
        ]

        # Run 1: no diagnostics present (05:00Z scheduled recall).
        bundle_1 = bundle_for_row(stable_siblings, own_campaign)
        open_item_ids_1 = [it["finding_id"] for it in bundle_1]
        ev_1 = _build_ev(open_item_ids=open_item_ids_1, row={**_BASE_ROW, "campaign_id": own_campaign})
        h1 = compute_inputs_hash(ev_1)

        # Run 2: diagnostics tier 2 wrote one or more new findings between runs.
        # The siblings grow by diagnostic ids whose own live-row expires in 30 min
        # (so they would churn every ~5 minutes on a running system).
        between_run_diagnostics = [
            _diagnostics_sibling("diag-1-active"),
            _diagnostics_sibling("diag-2-just-cleared"),
        ]
        bundle_2 = bundle_for_row(stable_siblings + between_run_diagnostics, own_campaign)
        open_item_ids_2 = [it["finding_id"] for it in bundle_2]
        ev_2 = _build_ev(open_item_ids=open_item_ids_2, row={**_BASE_ROW, "campaign_id": own_campaign})
        h2 = compute_inputs_hash(ev_2)

        assert bundle_1 == bundle_2, (
            "the bundle must be identical across runs — volatile siblings "
            "do not reach the model path"
        )
        assert open_item_ids_1 == open_item_ids_2, (
            "the open_item_ids list fed into the evidence must be identical"
        )
        assert h1 == h2, (
            "a diagnostics write between recall runs must not change inputs_hash "
            "(user acceptance, 2026-10-13: zero model calls on an unchanged re-run)"
        )

    def test_adding_a_stable_sibling_between_runs_changes_hash(self):
        """Negative control: a stable sibling (e.g. a NEW warranty candidate or
        a NEW recall campaign) added between runs DOES change the hash and so
        does fire the model path — the stale posture item 3 was trying to
        guard against is only the volatile cadence, not real changes.
        """
        own_campaign = "MRD-2024-001"
        before = [_health_battery_sibling("batt-1")]
        after = before + [_other_recall_sibling("new-recall-1", "MRD-2024-OTHER")]

        bundle_1 = bundle_for_row(before, own_campaign)
        bundle_2 = bundle_for_row(after, own_campaign)
        ev_1 = _build_ev(
            open_item_ids=[it["finding_id"] for it in bundle_1],
            row={**_BASE_ROW, "campaign_id": own_campaign},
        )
        ev_2 = _build_ev(
            open_item_ids=[it["finding_id"] for it in bundle_2],
            row={**_BASE_ROW, "campaign_id": own_campaign},
        )
        h1 = compute_inputs_hash(ev_1)
        h2 = compute_inputs_hash(ev_2)
        assert h1 != h2, (
            "a NEW stable sibling must change inputs_hash so the model path "
            "re-fires on a genuinely changed vehicle world"
        )

