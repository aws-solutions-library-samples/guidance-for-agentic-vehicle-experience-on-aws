"""11.3c RED tests — FI bookings carry description and evidence.dtcs.

spec § 11.3c: every FI fleet-service booking sent to DMS carries a `description`
built from `parameters.needs` and, when at least one fault need is present, an
`evidence` dict whose `dtcs` key holds the fault codes in need order, de-duped,
max 50.  `book_recall_service` carries only a `description`; no `evidence`.

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_booking_description.py -v
"""
from __future__ import annotations

import json
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from agents.executors.base import RetryPolicy
from agents.executors.dms import DmsExecutor, _fleet_service_body, _request_body
from agents.executors.simulated_operator import DECIDER_ID, DECIDER_KIND, DEMO_FLEET_IDS

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

_Creds = namedtuple("_Creds", "access_key secret_key token")
_CREDS = _Creds("TESTEXAMPLEACCESSKEY", "secret/TEST+KEY", "session-EXAMPLE")
_DEMO_FLEET = next(iter(sorted(DEMO_FLEET_IDS)))
_INTERNAL_BASE = "https://dmsi0.execute-api.us-west-2.amazonaws.com/staging"
_DMS_BASE = "https://dms-staging-desc.test"
_TOKEN = "op-bearer-desc-tests"


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if body is not None:
        r.json.return_value = body
    else:
        r.json.return_value = {}
    return r


def _executor() -> DmsExecutor:
    return DmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0),
        credentials_fn=lambda: _CREDS,
    )


def _sim_decider() -> dict:
    return {"kind": DECIDER_KIND, "id": DECIDER_ID}


def _op_decider() -> dict:
    return {"kind": "operator", "id": "op-sub-9001"}


def _fleet_action(needs: list, decider: dict | None = None) -> dict:
    return {
        "action_id": "AVX-ACT-aa000000-0000-4000-8000-000000000001",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "fleet_id": _DEMO_FLEET,
        "decided_by": decider or _sim_decider(),
        "parameters": {
            "vehicle_vin": "MRDVEH00000000099",
            "dealer_id": "DLR-DESC-01",
            "needs": needs,
        },
    }


def _recall_action(recall_id: str = "MRD-2026-004", decider: dict | None = None) -> dict:
    return {
        "action_id": "AVX-ACT-bb000000-0000-4000-8000-000000000001",
        "action_kind": "book_recall_service",
        "target_system": "dms",
        "fleet_id": _DEMO_FLEET,
        "decided_by": decider or _sim_decider(),
        "parameters": {
            "vehicle_vin": "MRDVEH00000000099",
            "dealer_id": "DLR-DESC-01",
            "recall_id": recall_id,
        },
    }


def _posted_body(action: dict, claims: dict) -> dict:
    """Dispatch the action and return the JSON body that was posted."""
    ex = _executor()
    with patch("agents.executors.dms.requests.post",
               return_value=_resp(201, {"ro_id": "DMS-RO-T1"})) as post_mock:
        ex.dispatch(action, claims)
    assert post_mock.called, "no POST was made"
    call = post_mock.call_args
    # signed path uses `data=`, token path uses `json=`
    if call[1].get("data") is not None:
        return json.loads(call[1]["data"])
    return call[1]["json"]


@pytest.fixture(autouse=True)
def _envs(monkeypatch):
    monkeypatch.setenv("VSA_DMS_API_ENDPOINT", _DMS_BASE)
    monkeypatch.setenv("VSA_DMS_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)


# ===========================================================================
# Pure helper: _fleet_service_body description and evidence
# ===========================================================================

class TestFleetServiceBodyDescription:
    """_fleet_service_body populates `description` from needs."""

    def test_fault_codes_in_description(self):
        action = _fleet_action([
            {"need_id": "fault:P0A0D", "labor_op": "OP1"},
            {"need_id": "fault:P0A7F", "labor_op": "OP2"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        desc = body.get("description", "")
        assert "fault codes" in desc
        assert "P0A0D" in desc
        assert "P0A7F" in desc

    def test_description_starts_with_fi_fleet_service(self):
        action = _fleet_action([{"need_id": "fault:P0A0D", "labor_op": "OP1"}])
        body = _fleet_service_body(action)
        assert body is not None
        assert body["description"].startswith("FI fleet service")

    def test_non_fault_need_appears_in_description(self):
        action = _fleet_action([
            {"need_id": "fault:P0A0D", "labor_op": "OP1"},
            {"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        desc = body["description"]
        assert "TIRE_ROTATION" in desc

    def test_exact_description_names_need_kinds_in_dealer_words(self):
        # Exact string: fault codes first (de-duplicated), then maintenance, then an
        # agent finding by its kind alone. "pm"/"a2" are internal prefixes a dealer
        # shouldn't read (architect, prompt 287).
        action = _fleet_action([
            {"need_id": "fault:P0A0D", "labor_op": "OP1"},
            {"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"},
            {"need_id": "fault:P0A7F", "labor_op": "OP2"},
            {"need_id": "fault:P0A0D", "labor_op": "OP3"},
            {"need_id": "a2:health.battery", "labor_op": "BATT"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        assert body["description"] == (
            "FI fleet service: fault codes P0A0D, P0A7F; maintenance TIRE_ROTATION; health.battery"
        )
        assert body["evidence"] == {"dtcs": ["P0A0D", "P0A7F"]}
    def test_no_fault_needs_still_has_description(self):
        action = _fleet_action([{"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"}])
        body = _fleet_service_body(action)
        assert body is not None
        assert "description" in body
        assert "TIRE_ROTATION" in body["description"]

    def test_description_max_200_chars(self):
        # 60 distinct fault codes, each 6 chars → would exceed 200 chars untruncated
        needs = [{"need_id": f"fault:P{i:04d}A", "labor_op": "OP"} for i in range(60)]
        action = _fleet_action(needs)
        body = _fleet_service_body(action)
        assert body is not None
        assert len(body["description"]) <= 200

    def test_description_truncation_names_count_omitted(self):
        needs = [{"need_id": f"fault:P{i:04d}A", "labor_op": "OP"} for i in range(60)]
        action = _fleet_action(needs)
        body = _fleet_service_body(action)
        assert body is not None
        assert "more" in body["description"], "truncated description must say '…and N more'"

    def test_description_no_fault_codes_no_evidence_key(self):
        action = _fleet_action([{"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"}])
        body = _fleet_service_body(action)
        assert body is not None
        assert "evidence" not in body, "evidence must be absent when no fault needs"


class TestFleetServiceBodyEvidence:
    """_fleet_service_body populates `evidence.dtcs` from fault needs."""

    def test_evidence_present_when_fault_needs_exist(self):
        action = _fleet_action([{"need_id": "fault:P0A0D", "labor_op": "OP1"}])
        body = _fleet_service_body(action)
        assert body is not None
        assert "evidence" in body
        assert "dtcs" in body["evidence"]

    def test_evidence_dtcs_contains_fault_codes_in_need_order(self):
        action = _fleet_action([
            {"need_id": "fault:P0A7F", "labor_op": "OP1"},
            {"need_id": "fault:P0A0D", "labor_op": "OP2"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        assert body["evidence"]["dtcs"] == ["P0A7F", "P0A0D"]

    def test_evidence_dtcs_deduplicates_fault_codes(self):
        action = _fleet_action([
            {"need_id": "fault:P0A0D", "labor_op": "OP1"},
            {"need_id": "fault:P0A0D", "labor_op": "OP2"},  # duplicate
            {"need_id": "fault:P0A7F", "labor_op": "OP3"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        dtcs = body["evidence"]["dtcs"]
        assert dtcs == ["P0A0D", "P0A7F"], f"expected deduped list, got {dtcs}"

    def test_evidence_dtcs_capped_at_50(self):
        needs = [{"need_id": f"fault:P{i:04d}A", "labor_op": "OP"} for i in range(60)]
        action = _fleet_action(needs)
        body = _fleet_service_body(action)
        assert body is not None
        assert len(body["evidence"]["dtcs"]) <= 50

    def test_evidence_absent_when_no_fault_needs(self):
        action = _fleet_action([
            {"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"},
            {"need_id": "a2:health.battery", "labor_op": "BATT"},
        ])
        body = _fleet_service_body(action)
        assert body is not None
        assert "evidence" not in body

    def test_evidence_absent_when_needs_empty_after_filtering(self):
        # needs list with a need that has no fault: prefix
        action = _fleet_action([{"need_id": "NID-001", "labor_op": "OP1"}])
        body = _fleet_service_body(action)
        assert body is not None
        assert "evidence" not in body


class TestFleetServiceBodyExistingFieldsUnchanged:
    """Adding description/evidence must not disturb vehicle_vin, dealer_id, lines, urgency."""

    def test_existing_fields_preserved(self):
        action = _fleet_action([
            {"need_id": "fault:P0A0D", "labor_op": "HV_OP", "need_id": "fault:P0A0D"},
        ])
        action["parameters"]["urgency"] = "this_week"
        body = _fleet_service_body(action)
        assert body is not None
        assert body["vehicle_vin"] == "MRDVEH00000000099"
        assert body["dealer_id"] == "DLR-DESC-01"
        assert body["urgency"] == "this_week"
        assert "lines" in body


# ===========================================================================
# Pure helper: _request_body description for book_recall_service
# ===========================================================================

class TestRequestBodyRecallDescription:
    """_request_body adds `description` for book_recall_service with recall_id."""

    def test_description_names_recall_id(self):
        action = _recall_action("MRD-2026-004")
        body = _request_body(action)
        assert body is not None
        desc = body.get("description", "")
        assert "MRD-2026-004" in desc
        assert "recall" in desc.lower()

    def test_description_absent_when_no_recall_id(self):
        action = _recall_action()
        del action["parameters"]["recall_id"]
        body = _request_body(action)
        assert body is not None
        assert "description" not in body, "no description without recall_id"

    def test_description_absent_for_non_recall_kind(self):
        """_request_body for book_combined_visit must NOT add a description."""
        action = {
            "action_kind": "book_combined_visit",
            "parameters": {"vehicle_vin": "VIN0000001", "dealer_id": "DLR-1"},
        }
        body = _request_body(action)
        assert body is not None
        assert "description" not in body

    def test_recall_description_no_evidence(self):
        """book_recall_service must not carry an evidence key."""
        action = _recall_action("MRD-2026-004")
        body = _request_body(action)
        assert body is not None
        assert "evidence" not in body


# ===========================================================================
# Integration: signed path (simulated decider, book_fleet_service)
# ===========================================================================

class TestSignedPathBodyHasDescriptionAndEvidence:
    """The signed (SigV4) path for the simulated decider sends description+evidence."""

    def test_signed_fleet_service_body_has_description(self):
        needs = [{"need_id": "fault:P0A0D", "labor_op": "OP1"}]
        body = _posted_body(_fleet_action(needs, _sim_decider()), {})
        assert "description" in body, f"body keys: {list(body)}"

    def test_signed_fleet_service_body_has_evidence(self):
        needs = [{"need_id": "fault:P0A0D", "labor_op": "OP1"}]
        body = _posted_body(_fleet_action(needs, _sim_decider()), {})
        assert "evidence" in body
        assert body["evidence"]["dtcs"] == ["P0A0D"]

    def test_signed_fleet_service_no_evidence_when_no_faults(self):
        needs = [{"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"}]
        body = _posted_body(_fleet_action(needs, _sim_decider()), {})
        assert "evidence" not in body


# ===========================================================================
# Integration: token path (operator decider, book_fleet_service)
# ===========================================================================

class TestTokenPathBodyHasDescriptionAndEvidence:
    """The bearer-token path for operator decider also sends description+evidence."""

    def test_token_fleet_service_body_has_description(self):
        needs = [{"need_id": "fault:P0A0D", "labor_op": "OP1"}]
        body = _posted_body(_fleet_action(needs, _op_decider()), {"token": _TOKEN})
        assert "description" in body

    def test_token_fleet_service_body_has_evidence(self):
        needs = [{"need_id": "fault:P0A0D", "labor_op": "OP1"}]
        body = _posted_body(_fleet_action(needs, _op_decider()), {"token": _TOKEN})
        assert "evidence" in body
        assert body["evidence"]["dtcs"] == ["P0A0D"]

    def test_token_fleet_service_no_evidence_when_no_faults(self):
        needs = [{"need_id": "pm:TIRE_ROTATION", "labor_op": "TIRE"}]
        body = _posted_body(_fleet_action(needs, _op_decider()), {"token": _TOKEN})
        assert "evidence" not in body


# ===========================================================================
# Integration: book_recall_service — description sent, no evidence
# ===========================================================================

class TestRecallServiceBodyIntegration:
    """book_recall_service carries description; no evidence on any path."""

    def test_signed_recall_body_has_description(self):
        body = _posted_body(_recall_action("MRD-2026-004", _sim_decider()), {})
        assert "description" in body
        assert "MRD-2026-004" in body["description"]

    def test_signed_recall_body_no_evidence(self):
        body = _posted_body(_recall_action("MRD-2026-004", _sim_decider()), {})
        assert "evidence" not in body
