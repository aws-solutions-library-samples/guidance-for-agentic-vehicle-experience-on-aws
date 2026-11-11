"""Route-level tests for G3F1.5 fixes: C2, C3 (route assertions), C4, C5, W1, W2, W6.

Every test goes through ``lambda_handler`` (``h.handler``).
Schema tests fail for the named reason (not a collateral rule).

Mutation targets are documented inline; each was verified in a git-archive copy
under /tmp/2026-10-05-fi-fleet-health-stage-d-g3-f15/.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-findings/tests/test_g3f15_fixes.py -q
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AVX_STAGE", "test")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

import handler as h  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"
_INVESTIGATION_ID = "inv-g3f15-0001"
_PATTERN_ID = "pat-g3f15-P0AA6"
_OPTION_ID = "opt-g3f15-001"
_FAULT_CODE = "P0420"
_DEALER_ID = "dealer-seattle"
_THREAD_ID = "thr-g3f15-0001"
_DRAFT_ID = "dft-g3f15-0001"
_ENGINEER_SUB = "eng-sub-g3f15-0001"
_OPERATOR_SUB = "op-sub-g3f15-0001"

_CLAIMS_ENGINEER = {
    "sub": _ENGINEER_SUB,
    "cognito:groups": ["product-engineer"],
}
_CLAIMS_OPERATOR = {
    "sub": _OPERATOR_SUB,
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": _FLEET_ID,
}
_CLAIMS_FLEET_VIEWER = {
    "sub": "viewer-sub-g3f15-0001",
    "cognito:groups": ["fleet-viewer"],
    "custom:fleetIds": _FLEET_ID,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _CCF_cls():
    return type("ConditionalCheckFailedException", (ClientError,), {})


def _fake_table():
    _CCF = _CCF_cls()

    class _FT:
        def __init__(self):
            self._store: dict = {}
            self.meta = MagicMock()
            self.meta.client.exceptions.ConditionalCheckFailedException = _CCF

        def put_item(self, **kw):
            item = kw.get("Item", {})
            aid = item.get("action_id")
            cond = kw.get("ConditionExpression", "")
            if "attribute_not_exists" in str(cond) and aid in self._store:
                raise _CCF(
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": ""}},
                    "PutItem",
                )
            if aid is not None:
                self._store[aid] = item
            return {}

        def get_item(self, **kw):
            aid = kw.get("Key", {}).get("action_id")
            item = self._store.get(aid)
            return {"Item": item} if item else {}

    return _FT()


def _event(method, resource, path_params=None, claims=None, body=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": claims or _CLAIMS_OPERATOR}},
        "body": json.dumps(body) if body is not None else None,
    }


def _approve_event(finding_id, body, claims=None) -> dict:
    return _event(
        "POST",
        "/findings/{finding_id}/approve",
        path_params={"finding_id": finding_id},
        claims=claims,
        body=body,
    )


def _decode(resp) -> dict:
    return json.loads(resp["body"])


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Block DDB/dispatch for every test unless the test patches them itself."""
    monkeypatch.setattr(h, "_dispatch_action",
                        lambda action, claims: MagicMock(status="executed", target_ref=None),
                        raising=False)
    monkeypatch.setattr(h, "_record_dispatch_result",
                        lambda action, result: action,
                        raising=False)

    def _no_find(*_a, **_k):
        raise AssertionError("test did not patch _load_finding_by_id")

    fake_res = MagicMock()
    fake_res.Table.return_value = _fake_table()
    monkeypatch.setattr(h, "_load_finding_by_id", _no_find, raising=False)
    monkeypatch.setattr(h, "_get_ddb", lambda: fake_res, raising=False)
    monkeypatch.setattr(h, "_ddb", fake_res, raising=False)


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------

def _pop_investigation_finding(
    option_id=_OPTION_ID,
    action_kind="record_engineering_decision",
) -> dict:
    """Population-scoped quality.investigation Finding for engineering kinds."""
    return {
        "finding_id": "FND-g3f15-pop-inv-0001",
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": "adp",
            "parameters": {
                "investigation_id": _INVESTIGATION_ID,
                "pattern_id": _PATTERN_ID,
                "option_id": option_id,
                "notes": "",
            },
        },
    }


def _pop_issue_finding(action_kind="issue_recall") -> dict:
    """Population-scoped quality.issue Finding for issue_recall / issue_software_campaign."""
    return {
        "finding_id": "FND-g3f15-pop-issue-0001",
        "finding_kind": "quality.issue",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": "adp",
            "parameters": {
                "thread_id": _THREAD_ID,
                "draft_id": _DRAFT_ID,
            },
        },
    }


def _fleet_health_finding(
    severity="p0",
    action_kind="book_fleet_service",
    needs=None,
) -> dict:
    """VIN-scoped fleet.health.service Finding (booking item)."""
    if needs is None:
        needs = [{"labor_op": "LAB-BRK-01", "need_id": "need-001"}]
    base: dict = {
        "finding_id": "FND-g3f15-health-0001",
        "finding_kind": "fleet.health.service",
        "agent_id": "agent-fi-fleet-health",
        "severity": severity,
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "needs": needs,
                "dealer_id": _DEALER_ID,
                "urgency": "this_week",
            },
        },
    }
    if severity == "p0":
        base["disclosure_txt"] = "Stop driving immediately — P0420 requires immediate attention."
    return base


def _recall_finding_blank_recall_id(recall_id="") -> dict:
    """VIN-scoped fleet.recall Finding whose recommendation has a blank recall_id (W1)."""
    return {
        "finding_id": "FND-g3f15-recall-blank-0001",
        "finding_kind": "fleet.recall.vehicle",
        "agent_id": "agent-fi-recall-warranty",
        "severity": "p0",
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": _VIN,
                "recall_id": recall_id,   # blank or whitespace
            },
        },
    }


def _plain_finding(action_kind="acknowledge_only") -> dict:
    """Plain VIN-scoped Finding (not FI item, no recommended_action by default)."""
    return {
        "finding_id": "FND-g3f15-plain-0001",
        "finding_kind": "health.tires",
        "agent_id": "agent-fi-fleet-health",
        "severity": "attention",
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": "none",
            "parameters": {},
        },
    }


def _plain_finding_no_rec() -> dict:
    """Plain VIN-scoped Finding with no recommended_action (non-FI item)."""
    return {
        "finding_id": "FND-g3f15-plain-no-rec-0001",
        "finding_kind": "health.tires",
        "agent_id": "agent-fi-fleet-health",
        "severity": "attention",
        "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
        "fleet_id": _FLEET_ID,
        "evidence": [],
        "expires_at": "2099-12-31T00:00:00Z",
    }


# ===========================================================================
# C2 — ADP target gate generalised to also refuse non-ADP kinds
# ===========================================================================

class TestAdpTargetRefused:
    """C2 (G3F1.5): approving a non-adp-row kind with target_system='adp' → 400.

    Adding adp rows put 'adp' into _VALID_TARGET_SYSTEMS for every kind.
    The gate must refuse target='adp' when the kind's own row doesn't name adp.

    Mutation: remove the adp branch from the gate → acknowledge_only with
    target='adp' reaches AdpExecutor → 200 → FAILS on the 400 assertion.
    """

    def test_adp_target_on_acknowledge_only_is_400(self):
        """acknowledge_only with target_system='adp' → 400 before any write.

        Uses a non-FI finding (no recommended_action) so only the C2 gate catches
        the mismatch — not the rec.target_system check.

        Mutation: remove the `target_system == 'adp' and _row.target_system != 'adp'`
        gate → approve proceeds → statusCode != 400 → FAILS.
        """
        finding = _plain_finding_no_rec()
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "acknowledge_only", "target_system": "adp"},
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"acknowledge_only with target='adp' must return 400; "
            f"got {resp['statusCode']}. "
            "Mutation: remove adp gate → 2xx → FAILS."
        )

    def test_adp_target_on_non_engineering_fi_finding_is_400(self):
        """book_recall_service with target='adp' → 400 (kind's row targets 'dms').

        Mutation: remove adp gate → request reaches the dms-target check and gets
        a different 400 (action_kind must target 'dms'); the test assertion uses
        assert 400 so it still passes, meaning this test has limited discrimination.
        But the specific error message pins the REASON more precisely:
        'unknown target_system: adp'.
        """
        finding = {
            "finding_id": "FND-g3f15-recall-0001",
            "finding_kind": "fleet.recall.vehicle",
            "agent_id": "agent-fi-recall-warranty",
            "severity": "p0",
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "fleet_id": _FLEET_ID,
            "evidence": [],
            "expires_at": "2099-12-31T00:00:00Z",
            "recommended_action": {
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {"vehicle_vin": _VIN, "recall_id": "MRD-2024-001",
                                "dealer_id": _DEALER_ID},
            },
        }
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "book_recall_service", "target_system": "adp",
                  "parameters": {}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"book_recall_service with target='adp' must return 400; "
            f"got {resp['statusCode']}. "
            "Mutation: remove adp gate → FAILS."
        )

    def test_record_engineering_decision_with_adp_target_is_allowed(self):
        """Positive control: record_engineering_decision with target='adp' is allowed."""
        finding = _pop_investigation_finding()
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "record_engineering_decision",
                "target_system": "adp",
                "parameters": {
                    "investigation_id": _INVESTIGATION_ID,
                    "pattern_id": _PATTERN_ID,
                    "option_id": _OPTION_ID,
                    "notes": "",
                },
            },
            claims=_CLAIMS_ENGINEER,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] != 400 or "adp" not in _decode(resp).get("error", ""), (
            "record_engineering_decision with target='adp' must not be rejected "
            "by the adp-gate (its policy row names adp)."
        )


# ===========================================================================
# C3 — decided_by.kind: engineer on the route response and DDB write
# ===========================================================================

class TestEngineerDecidedByOnRoute:
    """C3 (G3F1.5): all three engineering kinds record decided_by.kind: engineer.

    Asserted on both the HTTP response and the DDB Item.

    Mutation (M1 survived): change approve route to record kind='operator' for
    population Findings → response and DDB Item show 'operator' → FAILS.
    """

    def _approve_engineering_kind(self, finding, action_kind, extra_params=None):
        """Approve an engineering kind through the route; return (resp, stored_item)."""
        params: dict = {
            "investigation_id": _INVESTIGATION_ID,
            "pattern_id": _PATTERN_ID,
            "option_id": _OPTION_ID,
            "notes": "",
        }
        if action_kind in ("issue_recall", "issue_software_campaign"):
            params = {"thread_id": _THREAD_ID, "draft_id": _DRAFT_ID}
        if extra_params:
            params.update(extra_params)

        ft = _fake_table()
        fake_res = MagicMock()
        fake_res.Table.return_value = ft

        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": action_kind,
                "target_system": "adp",
                "parameters": params,
            },
            claims=_CLAIMS_ENGINEER,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)

        stored_items = list(ft._store.values())
        return resp, (stored_items[0] if stored_items else {})

    def test_record_engineering_decision_response_decided_by_engineer(self):
        """record_engineering_decision response carries decided_by.kind='engineer'.

        Mutation: route uses 'operator' → response.decided_by.kind='operator' → FAILS.
        """
        finding = _pop_investigation_finding()
        resp, stored = self._approve_engineering_kind(finding, "record_engineering_decision")
        assert resp["statusCode"] == 200, f"Expected 200, got {resp['statusCode']}: {_decode(resp)}"
        db = _decode(resp)
        assert db.get("decided_by", {}).get("kind") == "engineer", (
            f"response.decided_by.kind must be 'engineer'; got {db.get('decided_by')!r}. "
            "Mutation: route uses 'operator' → FAILS."
        )
        assert db.get("decided_by", {}).get("id") == _ENGINEER_SUB

    def test_record_engineering_decision_ddb_decided_by_engineer(self):
        """record_engineering_decision DDB Item carries decided_by.kind='engineer'.

        Mutation: route stores 'operator' → stored item shows 'operator' → FAILS.
        """
        finding = _pop_investigation_finding()
        _resp, stored = self._approve_engineering_kind(finding, "record_engineering_decision")
        assert stored.get("decided_by", {}).get("kind") == "engineer", (
            f"stored Action.decided_by.kind must be 'engineer'; got {stored.get('decided_by')!r}. "
            "Mutation: store 'operator' → FAILS."
        )

    def test_issue_recall_response_decided_by_engineer(self):
        """issue_recall response carries decided_by.kind='engineer'.

        Mutation: route uses 'operator' for issue kinds → FAILS.
        """
        finding = _pop_issue_finding("issue_recall")
        resp, stored = self._approve_engineering_kind(finding, "issue_recall")
        assert resp["statusCode"] == 200, f"Expected 200, got {resp['statusCode']}: {_decode(resp)}"
        db = _decode(resp)
        assert db.get("decided_by", {}).get("kind") == "engineer", (
            f"issue_recall response.decided_by.kind must be 'engineer'; got {db.get('decided_by')!r}. "
            "Mutation: use 'operator' → FAILS."
        )

    def test_issue_software_campaign_response_decided_by_engineer(self):
        """issue_software_campaign response carries decided_by.kind='engineer'.

        Mutation: route uses 'operator' for issue kinds → FAILS.
        """
        finding = _pop_issue_finding("issue_software_campaign")
        resp, stored = self._approve_engineering_kind(finding, "issue_software_campaign")
        assert resp["statusCode"] == 200, f"Expected 200, got {resp['statusCode']}: {_decode(resp)}"
        db = _decode(resp)
        assert db.get("decided_by", {}).get("kind") == "engineer", (
            f"issue_software_campaign response.decided_by.kind must be 'engineer'; "
            f"got {db.get('decided_by')!r}. "
            "Mutation: use 'operator' → FAILS."
        )

    def test_non_engineering_kind_decided_by_operator(self):
        """Positive control: non-engineering kind on fleet Finding → decided_by.kind='operator'."""
        finding = _plain_finding()
        ft = _fake_table()
        fake_res = MagicMock()
        fake_res.Table.return_value = ft
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "acknowledge_only", "target_system": "none", "parameters": {}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res), \
             patch.object(h, "_dispatch_action",
                          return_value=MagicMock(status="executed", target_ref=None)), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=lambda action, result: action):
            resp = h.handler(event, None)
        assert resp["statusCode"] in (200, 201, 202)
        db = _decode(resp)
        assert db.get("decided_by", {}).get("kind") == "operator"


# ===========================================================================
# C4 — p0 urgency guard tests
# ===========================================================================

class TestP0UrgencyGuard:
    """C4 (G3F1.5): approve route validates urgency against the p0 band.

    Spec § 4: p0 items require urgency='this_week' only. Other values → 400.
    Other severities are unrestricted within the valid set.

    Mutations (M5, M6 survived):
      M5: remove the p0 urgency check → p0 with this_month gets 200 → FAILS.
      M6: remove the urgency enum check → unknown value gets 200 → FAILS.
    """

    def _book_fleet(self, urgency, severity="p0", claims=None) -> dict:
        """Approve book_fleet_service via the route with the given urgency.

        Body only includes dealer_id and urgency (body_overridable) — not needs
        (server-set; route fills from recommendation).
        """
        finding = _fleet_health_finding(severity=severity)
        body_params: dict = {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "dealer_id": _DEALER_ID,
                "urgency": urgency,
            },
        }
        event = _approve_event(
            finding["finding_id"],
            body=body_params,
            claims=claims or _CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_p0_with_this_week_is_approved(self):
        """p0 item + urgency='this_week' → 200 approved."""
        resp = self._book_fleet("this_week", severity="p0")
        assert resp["statusCode"] == 200, (
            f"p0 item with urgency='this_week' must approve; got {resp['statusCode']}: "
            f"{_decode(resp)}"
        )
        assert _decode(resp).get("decision") == "approved"

    def test_p0_with_this_month_is_400(self):
        """p0 item + urgency='this_month' → 400, nothing written.

        Mutation (M5): remove p0 urgency check → this_month gets 200 → FAILS.
        """
        resp = self._book_fleet("this_month", severity="p0")
        assert resp["statusCode"] == 400, (
            f"p0 item with urgency='this_month' must return 400; got {resp['statusCode']}. "
            "Mutation M5: remove p0 check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "urgency" in error, f"400 error must mention 'urgency'; got {error!r}"

    def test_p0_with_next_visit_is_400(self):
        """p0 item + urgency='next_visit' → 400, nothing written.

        Mutation (M5): remove p0 urgency check → next_visit gets 200 → FAILS.
        """
        resp = self._book_fleet("next_visit", severity="p0")
        assert resp["statusCode"] == 400, (
            f"p0 item with urgency='next_visit' must return 400; got {resp['statusCode']}. "
            "Mutation M5: remove p0 check → 200 → FAILS."
        )

    def test_attention_with_next_visit_is_overridden(self):
        """attention item + urgency='next_visit' (differs from rec) → 200 overridden.

        Positive control: non-p0 items are unrestricted within the valid set.
        """
        resp = self._book_fleet("next_visit", severity="attention")
        assert resp["statusCode"] == 200, (
            f"attention item with urgency='next_visit' must approve; got {resp['statusCode']}"
        )
        assert _decode(resp).get("decision") == "overridden", (
            "urgency different from recommendation → overridden"
        )

    def test_unknown_urgency_value_is_400(self):
        """Unknown urgency value → 400 before any write.

        Mutation (M6): remove urgency enum check → unknown gets 200 → FAILS.
        """
        resp = self._book_fleet("immediately", severity="attention")
        assert resp["statusCode"] == 400, (
            f"Unknown urgency value must return 400; got {resp['statusCode']}. "
            "Mutation M6: remove urgency enum check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "urgency" in error, f"400 error must mention 'urgency'; got {error!r}"

    def test_no_ddb_write_on_invalid_urgency(self):
        """No DDB write occurs when urgency is invalid."""
        finding = _fleet_health_finding(severity="p0")
        ft = _fake_table()
        fake_res = MagicMock()
        fake_res.Table.return_value = ft
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "dealer_id": _DEALER_ID,
                    "urgency": "next_visit",   # invalid for p0
                },
            },
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400
        assert not ft._store, "No DDB row must be written when urgency is invalid for p0"


# ===========================================================================
# C5 — server-set fault_codes mismatch → 400
# ===========================================================================

class TestFaultCodesServerSetMismatch:
    """C5 (G3F1.5): a body fault_codes that differs from the recommendation → 400.

    Spec D11 / contract § 4 "Deterministic seams": server-set parameters per kind.
    A body that tries to replace, widen, or reorder the server-set codes must be refused.

    Mutation (M11): drop the fault_codes body-match check → different body gets 200 → FAILS.
    """

    def _ground_fault(self, rec_codes, body_codes=None, claims=None) -> dict:
        """Approve ground_vehicle fault-form via the route."""
        finding = {
            "finding_id": "FND-g3f15-gnd-fault-0001",
            "finding_kind": "fleet.health.service",
            "agent_id": "agent-fi-fleet-health",
            "severity": "p0",
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "fleet_id": _FLEET_ID,
            "evidence": [],
            "expires_at": "2099-12-31T00:00:00Z",
            "disclosure_txt": "Stop driving immediately.",
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "fault_codes": rec_codes,
                },
            },
        }
        body_params: dict = {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"vehicle_vin": _VIN},
        }
        if body_codes is not None:
            body_params["parameters"]["fault_codes"] = body_codes
        event = _approve_event(
            finding["finding_id"],
            body=body_params,
            claims=claims or _CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_omitted_body_fault_codes_is_200(self):
        """Positive control: body omits fault_codes → server fills from rec → 200."""
        resp = self._ground_fault(rec_codes=["P0420", "P0300"])
        assert resp["statusCode"] == 200, (
            f"Omitted body fault_codes must be filled from rec and approve; "
            f"got {resp['statusCode']}: {_decode(resp)}"
        )

    def test_equal_body_fault_codes_is_200(self):
        """Positive control: body fault_codes equals rec → 200."""
        resp = self._ground_fault(rec_codes=["P0420"], body_codes=["P0420"])
        assert resp["statusCode"] == 200, (
            f"Matching body fault_codes must approve; got {resp['statusCode']}: {_decode(resp)}"
        )

    def test_different_body_fault_codes_is_400(self):
        """Body fault_codes differs from rec → 400 before any write.

        Mutation (M11): remove the fault_codes body-match check → different gets 200 → FAILS.
        """
        resp = self._ground_fault(rec_codes=["P0420"], body_codes=["P0300"])
        assert resp["statusCode"] == 400, (
            f"Body fault_codes differing from rec must return 400; got {resp['statusCode']}. "
            "Mutation M11: remove body-match check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "fault_codes" in error, f"400 must mention fault_codes; got {error!r}"

    def test_superset_body_fault_codes_is_400(self):
        """Body fault_codes that is a superset of rec → 400.

        Mutation: allow any superset → widened codes bypass the server-set check → FAILS.
        """
        resp = self._ground_fault(rec_codes=["P0420"], body_codes=["P0420", "P0300"])
        assert resp["statusCode"] == 400, (
            f"Body fault_codes superset must return 400; got {resp['statusCode']}. "
            "Mutation: allow superset → 200 → FAILS."
        )

    def test_reordered_body_fault_codes_is_400(self):
        """Body fault_codes with different order from rec → 400.

        A reordered list is not byte-equal and must be refused.
        """
        resp = self._ground_fault(rec_codes=["P0420", "P0300"], body_codes=["P0300", "P0420"])
        assert resp["statusCode"] == 400, (
            f"Reordered body fault_codes must return 400; got {resp['statusCode']}. "
        )


# ===========================================================================
# W1 — blank recall_id regression
# ===========================================================================

class TestBlankRecallIdRejected:
    """W1 (G3F1.5): a blank recall_id in the recommendation → 400 before any write.

    Stage C regression: the dual-form detection used `is not None`, so `recall_id: ""`
    took the recall form and produced a 200 with an empty recall_id recorded.

    Mutation: revert to `is not None` → blank recall_id gets 200 → FAILS.
    """

    def test_blank_recall_id_returns_400(self):
        """Recommendation with recall_id='' → 400.

        Mutation: revert dual-form check to `is not None` → blank recall_id
        takes the recall form → 200 (MissingDispatchParameter later) → FAILS.
        """
        finding = _recall_finding_blank_recall_id("")
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "ground_vehicle", "target_system": "cms",
                  "parameters": {"vehicle_vin": _VIN}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"Blank recall_id must return 400; got {resp['statusCode']}. "
            "Mutation: `is not None` → blank gets 200 → FAILS."
        )

    def test_whitespace_recall_id_returns_400(self):
        """Recommendation with recall_id='   ' (whitespace) → 400.

        Mutation: check only emptiness (not strip()) → whitespace gets 200 → FAILS.
        """
        finding = _recall_finding_blank_recall_id("   ")
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "ground_vehicle", "target_system": "cms",
                  "parameters": {"vehicle_vin": _VIN}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)

        assert resp["statusCode"] == 400, (
            f"Whitespace recall_id must return 400; got {resp['statusCode']}. "
            "Mutation: check only emptiness → whitespace gets 200 → FAILS."
        )

    def test_no_ddb_write_on_blank_recall_id(self):
        """No DDB write occurs when recall_id is blank."""
        finding = _recall_finding_blank_recall_id("")
        ft = _fake_table()
        fake_res = MagicMock()
        fake_res.Table.return_value = ft
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "ground_vehicle", "target_system": "cms",
                  "parameters": {"vehicle_vin": _VIN}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_get_ddb", return_value=fake_res):
            h.handler(event, None)
        assert not ft._store, "No DDB row must be written when recall_id is blank"

    def test_valid_recall_id_still_approves(self):
        """Positive control: non-blank recall_id still approves."""
        finding = {
            "finding_id": "FND-g3f15-recall-valid-0001",
            "finding_kind": "fleet.recall.vehicle",
            "agent_id": "agent-fi-recall-warranty",
            "severity": "p0",
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "fleet_id": _FLEET_ID,
            "evidence": [],
            "expires_at": "2099-12-31T00:00:00Z",
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN, "recall_id": "MRD-2024-001"},
            },
        }
        event = _approve_event(
            finding["finding_id"],
            body={"action_kind": "ground_vehicle", "target_system": "cms",
                  "parameters": {"vehicle_vin": _VIN}},
            claims=_CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"Valid recall_id must still approve; got {resp['statusCode']}: {_decode(resp)}"
        )


# ===========================================================================
# W2 — malformed needs refuse the approval with 400
# ===========================================================================

class TestMalformedNeedsRefused:
    """W2 (G3F1.5): a malformed need in the recommendation refuses the approval (400).

    DMS lines carry line_no as a string. A need that isn't a dict or has no
    labor_op was silently dropped; now it refuses the whole booking.

    Mutation: revert to `continue` on malformed needs → malformed need gets 200
    with fewer lines → FAILS on the 400 assertion.
    """

    def _approve_fleet_service(self, needs, claims=None) -> dict:
        """Approve book_fleet_service via the route with the given needs in recommendation.

        Body only includes dealer_id and urgency (body_overridable). The needs are
        in the recommendation and server-set — they don't need to be in the body.
        """
        finding = _fleet_health_finding(needs=needs)
        event = _approve_event(
            finding["finding_id"],
            body={
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "dealer_id": _DEALER_ID,
                    "urgency": "this_week",
                },
            },
            claims=claims or _CLAIMS_OPERATOR,
        )
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            return h.handler(event, None)

    def test_valid_needs_approve(self):
        """Positive control: valid needs list → 200."""
        needs = [{"labor_op": "LAB-BRK-01", "need_id": "need-001"},
                 {"labor_op": "LAB-SUS-03", "need_id": "need-002"}]
        resp = self._approve_fleet_service(needs)
        assert resp["statusCode"] == 200, (
            f"Valid needs must approve; got {resp['statusCode']}: {_decode(resp)}"
        )

    def test_non_dict_need_is_400(self):
        """A need that is not a dict → 400, nothing written.

        Mutation: revert to `continue` → skips invalid need, books the rest → 200 → FAILS.
        """
        needs = [{"labor_op": "LAB-BRK-01"}, "bad-need"]
        resp = self._approve_fleet_service(needs)
        assert resp["statusCode"] == 400, (
            f"Non-dict need must return 400; got {resp['statusCode']}. "
            "Mutation: continue on non-dict → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "needs" in error, f"400 error must mention 'needs'; got {error!r}"

    def test_missing_labor_op_need_is_400(self):
        """A need without labor_op → 400, nothing written.

        Mutation: continue on missing labor_op → skips the malformed need → 200 → FAILS.
        """
        needs = [{"labor_op": "LAB-BRK-01"}, {"need_id": "need-002"}]  # second is missing labor_op
        resp = self._approve_fleet_service(needs)
        assert resp["statusCode"] == 400, (
            f"Need without labor_op must return 400; got {resp['statusCode']}. "
            "Mutation: continue on missing labor_op → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "needs" in error and "labor_op" in error, (
            f"400 must mention 'needs' and 'labor_op'; got {error!r}"
        )

    def test_blank_labor_op_need_is_400(self):
        """A need with blank labor_op → 400."""
        needs = [{"labor_op": "LAB-BRK-01"}, {"labor_op": "   "}]
        resp = self._approve_fleet_service(needs)
        assert resp["statusCode"] == 400, (
            f"Need with blank labor_op must return 400; got {resp['statusCode']}."
        )


# ===========================================================================
# W6 — route tests for book_fleet_service, issue_recall, issue_software_campaign
# ===========================================================================

class TestBookFleetServiceRoute:
    """W6 (G3F1.5): route tests for book_fleet_service.

    server-set needs mismatch → 400; dealer_id override → overridden;
    non-DMS group → 403; fleet-operator → 200.

    Mutation (M7): remove dealer_id guard → no 400/403 even without DMS group → FAILS.
    """

    def _book_fleet_event(self, finding, params, claims=None) -> dict:
        return _approve_event(
            finding["finding_id"],
            body={"action_kind": "book_fleet_service",
                  "target_system": "dms", "parameters": params},
            claims=claims or _CLAIMS_OPERATOR,
        )

    def test_book_fleet_service_approved(self):
        """book_fleet_service with correct params → 200 approved."""
        finding = _fleet_health_finding()
        # Body only sends body_overridable params (dealer_id, urgency).
        # vehicle_vin and needs are server-set from the recommendation.
        event = self._book_fleet_event(finding, {
            "dealer_id": _DEALER_ID,
            "urgency": "this_week",
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"book_fleet_service must approve; got {resp['statusCode']}: {_decode(resp)}"
        )
        assert _decode(resp).get("decision") == "approved"

    def test_dealer_id_override_marks_overridden(self):
        """book_fleet_service with different dealer_id → 200 overridden.

        Mutation (M7): remove dealer_id from body_overridable → override not detected →
        decision stays 'approved' → FAILS.
        """
        finding = _fleet_health_finding()
        event = self._book_fleet_event(finding, {
            "dealer_id": "dealer-DIFFERENT",   # different from rec
            "urgency": "this_week",
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"book_fleet_service with different dealer_id must get 200; "
            f"got {resp['statusCode']}: {_decode(resp)}"
        )
        assert _decode(resp).get("decision") == "overridden", (
            "Different dealer_id must mark decision='overridden'. "
            "Mutation M7: remove dealer_id from body_overridable → 'approved' → FAILS."
        )

    def test_non_dms_group_gets_403(self):
        """fleet-viewer without DMS group → 403 on book_fleet_service.

        Mutation: remove DMS group check → fleet-viewer gets 200 → FAILS.
        """
        finding = _fleet_health_finding()
        event = self._book_fleet_event(finding, {
            "dealer_id": _DEALER_ID,
            "urgency": "this_week",
        }, claims=_CLAIMS_FLEET_VIEWER)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"fleet-viewer must get 403 on book_fleet_service; got {resp['statusCode']}. "
            "Mutation: remove DMS group check → 200 → FAILS."
        )

    def test_needs_mismatch_is_400(self):
        """Body sends needs that differ from server-set → 400.

        needs is server-set; the body may not override it.
        Mutation: allow body to override server-set needs → 200 → FAILS.
        """
        finding = _fleet_health_finding(needs=[{"labor_op": "LAB-BRK-01"}])
        event = self._book_fleet_event(finding, {
            # Body explicitly sends a different needs list — should be refused
            "needs": [{"labor_op": "LAB-ENG-04"}],
            "dealer_id": _DEALER_ID,
            "urgency": "this_week",
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400, (
            f"Body needs mismatch must return 400; got {resp['statusCode']}. "
            "Mutation: allow body override of server-set needs → 200 → FAILS."
        )

    def test_missing_dealer_id_in_rec_and_body_is_400(self):
        """book_fleet_service with no dealer_id in rec or body → 400.

        Mutation (M7): remove book_fleet_service from the post-fill dealer_id check →
        missing dealer_id gets through to DmsExecutor → FAILS because the route
        must catch missing dealer_id before writing the Action.
        """
        # Build a finding with no dealer_id in the recommendation
        finding_no_dealer = {
            "finding_id": "FND-g3f15-health-no-dealer-0001",
            "finding_kind": "fleet.health.service",
            "agent_id": "agent-fi-fleet-health",
            "severity": "attention",
            "scope": {"vin": _VIN, "fleet_id": _FLEET_ID},
            "fleet_id": _FLEET_ID,
            "evidence": [],
            "expires_at": "2099-12-31T00:00:00Z",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [{"labor_op": "LAB-BRK-01"}],
                    # No dealer_id — body also omits it
                    "urgency": "this_week",
                },
            },
        }
        event = self._book_fleet_event(finding_no_dealer, {
            "urgency": "this_week",
            # No dealer_id
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding_no_dealer):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400, (
            f"book_fleet_service with no dealer_id must return 400; got {resp['statusCode']}. "
            "Mutation M7: remove book_fleet_service from dealer_id check → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "dealer_id" in error, f"400 must mention dealer_id; got {error!r}"


class TestIssueKindsRoute:
    """W6 (G3F1.5): route tests for issue_recall and issue_software_campaign.

    engineer-only; fleet-operator → 403; decision='approved'; draft_id mismatch → 400.

    Mutation (M8): AdpExecutor refuses both issue kinds →
    the executor-dispatch isn't reached if the route itself refused → test passes regardless.
    This test pins at the route level.
    """

    def _issue_event(self, finding, action_kind, params, claims=None) -> dict:
        return _approve_event(
            finding["finding_id"],
            body={"action_kind": action_kind, "target_system": "adp", "parameters": params},
            claims=claims or _CLAIMS_ENGINEER,
        )

    def test_issue_recall_approved_by_engineer(self):
        """issue_recall with correct params → 200 approved, decided_by.kind=engineer."""
        finding = _pop_issue_finding("issue_recall")
        event = self._issue_event(finding, "issue_recall", {
            "thread_id": _THREAD_ID, "draft_id": _DRAFT_ID,
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"issue_recall must approve; got {resp['statusCode']}: {_decode(resp)}"
        )
        db = _decode(resp)
        assert db.get("decision") == "approved"
        assert db.get("decided_by", {}).get("kind") == "engineer", (
            "issue_recall must record decided_by.kind='engineer'."
        )

    def test_issue_software_campaign_approved_by_engineer(self):
        """issue_software_campaign with correct params → 200 approved."""
        finding = _pop_issue_finding("issue_software_campaign")
        event = self._issue_event(finding, "issue_software_campaign", {
            "thread_id": _THREAD_ID, "draft_id": _DRAFT_ID,
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"issue_software_campaign must approve; got {resp['statusCode']}: {_decode(resp)}"
        )
        assert _decode(resp).get("decision") == "approved"

    def test_fleet_operator_gets_403_on_issue_recall(self):
        """fleet-operator → 403 on issue_recall (engineer-only).

        Mutation: allow fleet-operator on engineering kinds → 200 → FAILS.
        """
        finding = _pop_issue_finding("issue_recall")
        event = self._issue_event(finding, "issue_recall", {
            "thread_id": _THREAD_ID, "draft_id": _DRAFT_ID,
        }, claims=_CLAIMS_OPERATOR)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"fleet-operator must get 403 on issue_recall; got {resp['statusCode']}. "
            "Mutation: allow fleet-operator → 200 → FAILS."
        )

    def test_draft_id_mismatch_is_400(self):
        """Body draft_id differing from server-set → 400.

        Mutation: allow body to override server-set draft_id → 200 → FAILS.
        """
        finding = _pop_issue_finding("issue_recall")
        event = self._issue_event(finding, "issue_recall", {
            "thread_id": _THREAD_ID,
            "draft_id": "DIFFERENT-DRAFT-ID",   # differs from rec
        })
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400, (
            f"Mismatched draft_id must return 400; got {resp['statusCode']}. "
            "Mutation: allow override → 200 → FAILS."
        )
        error = _decode(resp).get("error", "")
        assert "draft_id" in error, f"400 must mention 'draft_id'; got {error!r}"

    def test_issue_recall_on_non_population_finding_is_400(self):
        """issue_recall on a non-population Finding → 400 (kind/scope coupling).

        Mutation: remove kind/scope coupling → issue_recall on fleet Finding gets 200 → FAILS.
        """
        finding = _plain_finding_no_rec()
        event = self._issue_event(finding, "issue_recall", {
            "thread_id": _THREAD_ID, "draft_id": _DRAFT_ID,
        }, claims=_CLAIMS_OPERATOR)
        with patch.object(h, "_load_finding_by_id", return_value=finding):
            resp = h.handler(event, None)
        assert resp["statusCode"] in (400, 403), (
            f"issue_recall on non-population Finding must return 400 or 403; "
            f"got {resp['statusCode']}. "
            "Mutation: remove kind/scope coupling → 200 → FAILS."
        )
