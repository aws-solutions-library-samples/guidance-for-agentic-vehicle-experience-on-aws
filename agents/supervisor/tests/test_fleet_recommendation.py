"""Unit tests for the fleet_recommendation supervisor tool.

Spec: `.kiro/specs/2026-10-02-fi-cost-stage-b/spec.md` § D12.
Contract: `docs/fi-action-queue-contract.md` § 3, § 7.

Key invariants tested here:
- No model call (pure DynamoDB read).
- Fleet standing check: refuses when caller does not hold the finding's fleet.
- Browser allowlist: server-side fields not returned (scope, finding_key,
  inputs_hash, subject_key, tokens_used, decided_by.id, storage-only top-level vin).
- scope.vin → top-level "vin" in the filtered finding.
- evidence[] entries filtered to allowlist.
- decided_by.kind returned; decided_by.id not returned.
- parameters returned only on decision Actions (those with "decision" field).
- audit_trail entries filtered to allowlist.
- Queue status computed correctly per contract § 3 cases.
- No-finding returns available=False.
- Missing fleet_id returns available=False (fail-closed).
- DynamoDB error returns available=False.
- Action query failure is non-fatal: finding is returned without action.

Mutation check (fleet standing):
- With the standing check dropped, test_refuses_caller_outside_fleet FAILS:
  the response returns available=True instead of available=False.
  See /tmp/2026-10-02-fi-cost-stage-b-g3/3.4-mut/ for the mutant run.
"""

from __future__ import annotations

import base64
import pathlib
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents.supervisor.tools import fleet_recommendation as fr_mod
from agents.supervisor.tools.fleet_recommendation import (
    _AUDIT_ENTRY_ALLOWLIST,
    _EVIDENCE_ENTRY_ALLOWLIST,
    _FINDING_ALLOWLIST,
    _derive_decision_action_id,
    _filter_action,
    _filter_finding,
    check_fleet_standing,
    compute_queue_status,
    make_fleet_recommendation_tool,
)

# ---------------------------------------------------------------------------
# JWT helpers for tests
# ---------------------------------------------------------------------------


def _make_jwt(
    groups: list[str] | None = None,
    fleet_ids: list[str] | None = None,
    *,
    trust_fleet_ids: bool = True,
) -> tuple[str, dict]:
    """Build a minimal decodable JWT for testing.

    Returns (jwt_str, monkeypatch_env) where monkeypatch_env is a dict of
    env vars needed to make fleet_ids trusted.
    """
    claims: dict = {
        "sub": "user-abc-123",
        "cognito:groups": json.dumps(groups or []),
    }
    if fleet_ids is not None:
        claims["custom:fleetIds"] = json.dumps(fleet_ids)

    payload_bytes = json.dumps(claims).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    jwt = f"eyJhbGciOiJSUzI1NiJ9.{payload_b64}.fake-sig"

    env: dict = {}
    if trust_fleet_ids:
        env["AVX_FLEET_IDS_CLAIM_TRUSTED"] = "true"
    return jwt, env


# ---------------------------------------------------------------------------
# DynamoDB stub helpers
# ---------------------------------------------------------------------------


def _future_expires() -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=30)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _past_expires() -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=5)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_finding(
    finding_id: str = "FIND-aabbccdd-1234-5678-abcd-ef1234567890",
    fleet_id: str = "FLEET-001",
    *,
    expires_at: str | None = None,
    with_scope_vin: bool = True,
    with_recommended_action: bool = True,
) -> dict:
    """Build a Finding DynamoDB item (plain Python, not DDB attribute-value format).

    Includes every storage-only and server-side attribute the real writer stores,
    so that allowlist tests are not vacuously satisfied by a sparse fixture.
    """
    f: dict = {
        "finding_id": finding_id,
        "fleet_id": fleet_id,           # storage-only — filtered out of Finding return
        "owner_id": "owner-mrd-00001",  # storage-only VIN-scoped — must NOT appear
        "vehicle_id": "vehicle-mrd-00001",  # storage-only VIN-scoped — must NOT appear
        "finding_key": "a" * 64,        # storage-only — must NOT appear in response
        "inputs_hash": "b" * 64,        # server-side only
        "subject_key": "2026-10",       # server-side only
        "tokens_used": 3500,            # server-side only
        "cost_usd": 0.0105,             # server-side only
        "agent_id": "agent-fi-cost",
        "finding_kind": "fleet.cost.anomaly",
        "severity": "soon",
        "confidence": "high",
        "headline": "Vehicle MRD-00001 energy costs 45% above fleet",
        "detail": "Monthly energy cost $315 vs fleet median $217",
        "computed_at": "2026-10-10T04:02:00Z",
        "expires_at": expires_at if expires_at is not None else _future_expires(),
        "agent_version": "1.0.0",
        "audit_trail": [
            {"at": "2026-10-10T04:02:01Z", "inputs_hash": "b" * 64}
        ],
    }
    if with_scope_vin:
        f["scope"] = {"vin": "MRD-00001"}
        # Storage-only top-level vin uses a DIFFERENT value from scope.vin
        # so the test can distinguish which one the filter extracted.
        f["vin"] = "MRD-STORAGE"    # must NOT appear in response
    if with_recommended_action:
        f["recommended_action"] = {
            "action_kind": "apply_cost_action",
            "target_system": "cms",
            "parameters": {
                "cost_action": "shift_to_depot_off_peak",
                "vehicle_vin": "MRD-00001",
                "month": "2026-09",
                "cause": "charging_rate",
            },
            "estimated_value_usd": 42.50,
            "estimated_value_basis": "monthly kWh moved to depot off-peak x price diff, 2026-09",
        }
    f["evidence"] = [
        {
            "kind": "cost.cost_per_mile",
            "source": "adp",
            "value": "1.85",
            "computed_at": "2026-10-10T04:00:00Z",
            "provenance": "live",
            "vin": "MRD-00001",
            "source_ref": "adp://curated/cost-metrics/2026-09",  # evidence storage field — must NOT appear
            "_internal_tag": "MUST_NOT_APPEAR",   # not in allowlist
        },
        {
            "kind": "cost.method",
            "source": "adp",
            "value": "own 6-month mean and same-month fleet-model median",
            "computed_at": "2026-10-10T04:00:00Z",
            "provenance": "live",
        },
    ]
    return f


def _make_action(
    finding_id: str = "FIND-aabbccdd-1234-5678-abcd-ef1234567890",
    *,
    status: str = "pending_execution",
    decision: str = "approved",
    with_decided_by_id: bool = True,
    with_parameters: bool = True,
) -> dict:
    """Build a decision Action DDB item.

    Includes every audit attribute the real writer stores (including
    storage-only ``note`` and ``by``) so allowlist tests are not vacuous.
    """
    action: dict = {
        # The real decision Action's key: derived from finding_id (contract § 4).
        "action_id": _derive_decision_action_id(finding_id),
        "finding_id": finding_id,
        "created_by": "owner-0000aaaa-bbbb-cccc-dddd-eeeeffff0000",  # storage-only (§ 7)
        "evidence": [{"kind": "cost.month", "value": "2026-09"}],      # storage-only copy
        "fleet_id": "FLEET-001",        # storage-only
        "action_kind": "apply_cost_action",
        "target_system": "cms",
        "status": status,
        "decision": decision,
        "decided_by": {
            "kind": "operator",
            "id": "user-abc-123",       # MUST NOT appear in filtered response
        },
        "created_at": "2026-10-10T10:00:00Z",
        "updated_at": "2026-10-10T10:00:05Z",
        "target_ref": None,
        "audit_trail": [
            {
                "at": "2026-10-10T10:00:05Z",
                "from_status": "pending",
                "to_status": "pending_execution",
                "failure_kind": None,    # should be None, not appear in filtered
                "note": "CmsExecutor: POST /api/v1/cost-actions dispatched",  # must NOT appear
                "by": "user-abc-123",    # must NOT appear (contract § 7 "Not returned")
                "secret_note": "MUST_NOT_APPEAR",  # not in allowlist
            }
        ],
    }
    if with_parameters:
        action["parameters"] = {
            "cost_action": "shift_to_depot_off_peak",
            "vehicle_vin": "MRD-00001",
            "month": "2026-09",
            "cause": "charging_rate",
        }
    return action


def _make_ctx(jwt: str = "", fleet_ids: list[str] | None = None) -> SimpleNamespace:
    jwt_str, _ = _make_jwt(
        groups=["fleet-operator"],
        fleet_ids=fleet_ids or ["FLEET-001"],
    )
    return SimpleNamespace(jwt=jwt if jwt else jwt_str)


def _make_cfg() -> SimpleNamespace:
    return SimpleNamespace()


def _ddb_stubs(
    finding: dict | None = None,
    action: dict | None = None,
    *,
    finding_error: Exception | None = None,
    action_error: Exception | None = None,
) -> MagicMock:
    """Return a mock boto3 DynamoDB resource that stubs Table.get_item."""
    findings_tbl = MagicMock()
    actions_tbl = MagicMock()

    # Key-aware stubs, like the real tables (review cycle 2 W1): an item is returned only
    # for its own key, so reading the Action by the wrong key finds nothing.
    def _finding_get(Key=None, **_kw):
        if finding and Key == {"finding_id": finding.get("finding_id")}:
            return {"Item": finding}
        return {}

    def _action_get(Key=None, **_kw):
        if action is not None and Key == {"action_id": action.get("action_id")}:
            return {"Item": action}
        return {}

    if finding_error:
        findings_tbl.get_item.side_effect = finding_error
    else:
        findings_tbl.get_item.side_effect = _finding_get

    # Actions are looked up via GetItem on the derived decision action_id (contract § 4).
    if action_error:
        actions_tbl.get_item.side_effect = action_error
    else:
        actions_tbl.get_item.side_effect = _action_get

    resource = MagicMock()

    def _table_factory(name: str) -> MagicMock:
        # Match by default table name patterns
        if "action" in name:
            return actions_tbl
        return findings_tbl

    resource.Table.side_effect = _table_factory
    return resource


# ---------------------------------------------------------------------------
# Helpers to call the tool
# ---------------------------------------------------------------------------


def _call(
    finding: dict | None,
    action: dict | None = None,
    finding_id: str = "FIND-aabbccdd-1234-5678-abcd-ef1234567890",
    *,
    fleet_ids: list[str] | None = None,
    groups: list[str] | None = None,
    trust_fleet_ids: bool = True,
    finding_error: Exception | None = None,
    action_error: Exception | None = None,
) -> dict:
    """Call make_fleet_recommendation_tool → fleet_recommendation(finding_id)."""
    jwt_str, env = _make_jwt(
        groups=groups if groups is not None else ["fleet-operator"],
        fleet_ids=fleet_ids if fleet_ids is not None else ["FLEET-001"],
        trust_fleet_ids=trust_fleet_ids,
    )
    ctx = SimpleNamespace(jwt=jwt_str)
    cfg = SimpleNamespace()
    resource_mock = _ddb_stubs(
        finding,
        action,
        finding_error=finding_error,
        action_error=action_error,
    )
    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
        tool = make_fleet_recommendation_tool(ctx, cfg)
        return tool(finding_id)


# ===========================================================================
# Fleet standing check
# ===========================================================================

class TestCheckFleetStanding:
    """Tests for the JWT-based fleet standing check (mutation target)."""

    def test_operator_with_fleet_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["FLEET-001", "FLEET-002"],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is True

    def test_admin_with_fleet_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["platform-admin"],
            fleet_ids=["FLEET-001"],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is True

    def test_operator_wrong_fleet_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Caller in fleet-operator group but fleet_id not in their list."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["FLEET-999"],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is False

    def test_admin_wrong_fleet_fails_fleet_asymmetry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fleet asymmetry: even platform-admin needs membership (contract § 4)."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["platform-admin"],
            fleet_ids=["FLEET-999"],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is False

    def test_wrong_group_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither platform-admin nor fleet-operator → denied regardless of fleet."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["fleet-driver"],
            fleet_ids=["FLEET-001"],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is False

    def test_untrusted_fleet_ids_claim_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When AVX_FLEET_IDS_CLAIM_TRUSTED is not 'true', deny."""
        monkeypatch.delenv("AVX_FLEET_IDS_CLAIM_TRUSTED", raising=False)
        jwt_str, _ = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["FLEET-001"],
            trust_fleet_ids=False,
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is False

    def test_malformed_jwt_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A JWT that cannot be decoded → deny (fail closed)."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        assert check_fleet_standing("not-a-jwt", "FLEET-001") is False
        assert check_fleet_standing("", "FLEET-001") is False

    def test_csv_fleet_ids_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CSV fleet_ids (Stage A D13) are supported."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        claims = {
            "sub": "user-x",
            "cognito:groups": '["fleet-operator"]',
            "custom:fleetIds": "FLEET-001,FLEET-002",
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).rstrip(b"=").decode()
        jwt_str = f"hdr.{payload_b64}.sig"
        assert check_fleet_standing(jwt_str, "FLEET-002") is True

    def test_blank_fleet_id_member_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank fleet-id members (after splitting) do not grant access."""
        monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
        jwt_str, _ = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["", "   "],
        )
        assert check_fleet_standing(jwt_str, "FLEET-001") is False


# ===========================================================================
# Happy path
# ===========================================================================

class TestFleetRecommendationHappyPath:
    """The tool returns allowlisted fields when the caller has fleet standing."""

    def test_returns_available_true(self) -> None:
        out = _call(finding=_make_finding(), action=_make_action())
        assert out["available"] is True

    def test_returns_finding_and_action(self) -> None:
        out = _call(finding=_make_finding(), action=_make_action())
        assert "finding" in out
        assert "action" in out
        assert "queue_status" in out

    def test_queue_status_approved_for_pending_execution(self) -> None:
        # pending_execution decision=approved → queue_status == "approved"
        # The clock is pinned 5 minutes after the fixture Action's updated_at
        # (2026-10-10T10:00:05Z). Unpinned, this read the real time and turned "stalled"
        # (contract § 3 row 9: pending_execution for more than 15 minutes) once that
        # fixed timestamp was 15 minutes in the past.
        with patch.object(fr_mod, "_utc_now_str", return_value="2026-10-10T10:05:05Z"):
            out = _call(
                finding=_make_finding(),
                action=_make_action(status="pending_execution", decision="approved"),
            )
        assert out["queue_status"] == "approved"

    def test_queue_status_pending_when_no_action(self) -> None:
        out = _call(finding=_make_finding(), action=None)
        assert out["queue_status"] == "pending"
        assert "action" not in out

    def test_scope_vin_exposed_as_top_level_vin(self) -> None:
        """scope.vin must appear as top-level vin in the finding (contract § 7)."""
        out = _call(finding=_make_finding(with_scope_vin=True))
        assert out["finding"].get("vin") == "MRD-00001"

    def test_storage_vin_not_returned(self) -> None:
        """The storage-only top-level vin attribute must NOT be returned.

        _make_finding sets storage vin = "MRD-STORAGE" (≠ scope.vin = "MRD-00001")
        so this test distinguishes which value the filter extracted.
        """
        # The finding has both scope.vin ("MRD-00001") and a storage-only vin
        # ("MRD-STORAGE").  The returned vin must come from scope.vin only.
        finding = _make_finding(with_scope_vin=True)
        out = _call(finding=finding)
        finding_fields = out["finding"]
        # vin should equal scope.vin, not the storage-only "MRD-STORAGE"
        assert finding_fields.get("vin") == "MRD-00001"
        assert finding_fields.get("vin") != "MRD-STORAGE"
        # storage-only fleet_id must not appear in the filtered finding
        assert "fleet_id" not in finding_fields

    def test_finding_headline_and_detail_returned(self) -> None:
        out = _call(finding=_make_finding())
        assert out["finding"]["headline"] == "Vehicle MRD-00001 energy costs 45% above fleet"
        assert "detail" in out["finding"]

    def test_recommended_action_returned(self) -> None:
        out = _call(finding=_make_finding(with_recommended_action=True))
        assert "recommended_action" in out["finding"]
        assert out["finding"]["recommended_action"]["action_kind"] == "apply_cost_action"

    def test_evidence_entries_filtered(self) -> None:
        """evidence[] entries only carry the contract § 7 allowlisted fields."""
        out = _call(finding=_make_finding())
        evidence = out["finding"]["evidence"]
        assert len(evidence) == 2
        for entry in evidence:
            # Every key must be in the evidence allowlist
            assert all(k in {"kind", "source", "value", "computed_at", "provenance", "vin"} for k in entry)
            # Internal tag must not appear
            assert "_internal_tag" not in entry

    def test_action_decided_by_kind_returned_id_not(self) -> None:
        """decided_by.kind is returned; decided_by.id (a Cognito sub) is not."""
        out = _call(finding=_make_finding(), action=_make_action(with_decided_by_id=True))
        action = out["action"]
        assert "decided_by" in action
        assert action["decided_by"] == {"kind": "operator"}
        # decided_by.id must be absent
        assert "id" not in action["decided_by"]

    def test_action_parameters_on_decision_action(self) -> None:
        """parameters must appear on a decision Action (one carrying 'decision')."""
        out = _call(finding=_make_finding(), action=_make_action(with_parameters=True))
        assert "parameters" in out["action"]

    def test_audit_trail_filtered(self) -> None:
        """audit_trail entries only carry the contract § 7 allowlisted fields."""
        out = _call(finding=_make_finding(), action=_make_action())
        audit = out["action"]["audit_trail"]
        assert len(audit) == 1
        for entry in audit:
            assert all(
                k in {"at", "from_status", "to_status", "failure_kind",
                      "failure_reason", "error_status", "guardrail"}
                for k in entry
            )
            assert "secret_note" not in entry

    def test_server_side_fields_not_in_finding(self) -> None:
        """finding_key, inputs_hash, subject_key, tokens_used, cost_usd not returned."""
        out = _call(finding=_make_finding())
        f = out["finding"]
        for forbidden in ("finding_key", "inputs_hash", "subject_key", "tokens_used", "cost_usd"):
            assert forbidden not in f, f"server-side field {forbidden!r} must not appear"

    def test_scope_not_in_finding(self) -> None:
        """scope dict must not appear (scope.vin is extracted as top-level vin)."""
        out = _call(finding=_make_finding())
        assert "scope" not in out["finding"]

    def test_finding_audit_trail_not_returned(self) -> None:
        """The Finding's own audit_trail must not be returned (only Action's is)."""
        out = _call(finding=_make_finding(), action=None)
        assert "audit_trail" not in out["finding"]

    def test_action_fleet_id_not_returned(self) -> None:
        """The Action's storage-only fleet_id must not appear in the filtered action."""
        out = _call(finding=_make_finding(), action=_make_action())
        assert "fleet_id" not in out["action"]


# ===========================================================================
# Fleet standing refusal (the mutation-critical test)
# ===========================================================================

class TestFleetStandingRefusal:
    """The tool refuses callers who lack standing on the finding's fleet.

    Mutation check: dropping check_fleet_standing (always returning True) causes
    test_refuses_caller_outside_fleet to FAIL.  See hand-back for mutant record.
    """

    def test_refuses_caller_outside_fleet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller is fleet-operator but for FLEET-999, not the finding's FLEET-001."""
        finding = _make_finding(fleet_id="FLEET-001")
        out = _call(
            finding=finding,
            fleet_ids=["FLEET-999"],  # caller cannot access FLEET-001
        )
        assert out["available"] is False
        assert "fleet" in out["reason"].lower()

    def test_refuses_wrong_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Caller is fleet-driver (not fleet-operator or platform-admin)."""
        finding = _make_finding(fleet_id="FLEET-001")
        out = _call(
            finding=finding,
            groups=["fleet-driver"],
            fleet_ids=["FLEET-001"],
        )
        assert out["available"] is False

    def test_refuses_when_finding_has_no_fleet_id(self) -> None:
        """VIN-scoped finding with no resolved fleet → fail closed (contract § 7)."""
        finding = _make_finding()
        del finding["fleet_id"]  # simulate unresolved fleet
        out = _call(finding=finding)
        assert out["available"] is False

    def test_refuses_when_fleet_ids_untrusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even operator group fails when AVX_FLEET_IDS_CLAIM_TRUSTED is off."""
        jwt_str, _ = _make_jwt(
            groups=["fleet-operator"],
            fleet_ids=["FLEET-001"],
            trust_fleet_ids=False,  # claim not trusted
        )
        ctx = SimpleNamespace(jwt=jwt_str)
        cfg = SimpleNamespace()
        finding = _make_finding(fleet_id="FLEET-001")
        resource_mock = _ddb_stubs(finding)
        # AVX_FLEET_IDS_CLAIM_TRUSTED is NOT set
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
            if "AVX_FLEET_IDS_CLAIM_TRUSTED" in __import__("os").environ:
                __import__("os").environ.pop("AVX_FLEET_IDS_CLAIM_TRUSTED")
            tool = make_fleet_recommendation_tool(ctx, cfg)
            out = tool("FIND-aabbccdd-1234-5678-abcd-ef1234567890")
        assert out["available"] is False


# ===========================================================================
# Not-found and error cases
# ===========================================================================

class TestFleetRecommendationErrorCases:

    def test_finding_not_found(self) -> None:
        """DynamoDB returns no Item → available=False with "not found" reason."""
        out = _call(finding=None)
        assert out["available"] is False
        assert "not found" in out["reason"].lower()

    def test_empty_finding_id(self) -> None:
        out = _call(finding=_make_finding(), finding_id="")
        assert out["available"] is False
        assert "required" in out["reason"].lower()

    def test_blank_finding_id(self) -> None:
        out = _call(finding=_make_finding(), finding_id="   ")
        assert out["available"] is False

    def test_ddb_get_error_returns_unavailable(self) -> None:
        """A DynamoDB GetItem error → available=False (not an exception)."""
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "DDB error"}},
            "GetItem",
        )
        out = _call(finding=_make_finding(), finding_error=err)
        assert out["available"] is False
        assert "unavailable" in out["reason"].lower()

    def test_action_read_error_returns_unavailable(self) -> None:
        """An Action GetItem error → available=False, never an invented 'pending'.

        The pre-fix bug: a Query AccessDeniedException (the runtime role has no
        /index/* grant) was swallowed, and compute_queue_status(finding, None)
        returned 'pending' — an operator who already approved saw 'pending'.

        Fix: a failed GetItem returns available=False.
        """
        from botocore.exceptions import ClientError

        err = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "User is not authorized to perform GetItem on /index/*",
                }
            },
            "GetItem",
        )
        out = _call(
            finding=_make_finding(),
            action=None,
            action_error=err,
        )
        assert out["available"] is False
        assert "unavailable" in out["reason"].lower()
        # queue_status must NOT be present (and certainly not "pending")
        assert "queue_status" not in out or out.get("queue_status") != "pending"


# ===========================================================================
# Queue status (contract § 3)
# ===========================================================================

class TestComputeQueueStatus:
    """Contract § 3 queue status rules."""

    _BASE_FINDING = {
        "expires_at": "2099-12-31T00:00:00Z",
    }

    def _now(self) -> str:
        return "2026-10-10T12:00:00Z"

    def test_no_action_pending(self) -> None:
        assert compute_queue_status(self._BASE_FINDING, None, utc_now=self._now()) == "pending"

    def test_no_action_expired(self) -> None:
        finding = {"expires_at": "2026-01-01T00:00:00Z"}
        assert compute_queue_status(finding, None, utc_now=self._now()) == "expired"

    def test_declined_decision_declined_is_rejected(self) -> None:
        action = {"status": "declined", "decision": "declined", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "rejected"

    def test_declined_guardrail_is_blocked(self) -> None:
        action = {
            "status": "declined",
            "decision": "approved",
            "audit_trail": [{"guardrail": "G1", "failure_kind": "target_refused"}],
        }
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "blocked"

    def test_declined_target_refused_no_guardrail_is_refused(self) -> None:
        action = {
            "status": "declined",
            "decision": "approved",
            "audit_trail": [{"failure_kind": "target_refused"}],
        }
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "refused"

    def test_failed_is_failed(self) -> None:
        action = {"status": "failed", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "failed"

    def test_cancelled_is_cancelled(self) -> None:
        action = {"status": "cancelled", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "cancelled"

    def test_executed_is_executed(self) -> None:
        action = {"status": "executed", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "executed"

    def test_completed_is_executed(self) -> None:
        action = {"status": "completed", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "executed"

    def test_pending_execution_within_15min_is_approved(self) -> None:
        action = {
            "status": "pending_execution",
            "decision": "approved",
            "updated_at": "2026-10-10T11:59:00Z",  # 1 min ago
            "audit_trail": [],
        }
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "approved"

    def test_pending_execution_stale_is_stalled(self) -> None:
        action = {
            "status": "pending_execution",
            "decision": "approved",
            "updated_at": "2026-10-10T11:30:00Z",  # 30 min ago > 15
            "audit_trail": [],
        }
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "stalled"

    def test_in_progress_overridden(self) -> None:
        action = {"status": "in_progress", "decision": "overridden", "audit_trail": []}
        assert compute_queue_status(self._BASE_FINDING, action, utc_now=self._now()) == "overridden"


# ===========================================================================
# Allowlist filter units
# ===========================================================================

class TestFilterFinding:
    """_filter_finding strips server-side and storage-only fields."""

    def test_strips_finding_key(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "finding_key" not in result

    def test_strips_inputs_hash(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "inputs_hash" not in result

    def test_strips_subject_key(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "subject_key" not in result

    def test_strips_tokens_used(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "tokens_used" not in result

    def test_strips_cost_usd(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "cost_usd" not in result

    def test_strips_scope(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "scope" not in result

    def test_strips_storage_only_vin(self) -> None:
        """The denormalised top-level vin storage attribute must NOT appear.
        scope.vin is extracted as top-level vin instead.

        _make_finding sets storage vin = "MRD-STORAGE" (different from
        scope.vin = "MRD-00001") so the test distinguishes which was used.
        """
        f = _make_finding(with_scope_vin=True)
        result = _filter_finding(f)
        # vin should equal scope.vin
        assert result["vin"] == "MRD-00001"
        # not the storage-only value
        assert result["vin"] != "MRD-STORAGE"
        # Finding has BOTH top-level vin (storage) and scope.vin —
        # the filter should read scope.vin and produce exactly one "vin" entry
        assert list(k for k in result if k == "vin") == ["vin"]

    def test_strips_fleet_id(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "fleet_id" not in result

    def test_strips_finding_audit_trail(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        assert "audit_trail" not in result

    def test_keeps_allowlisted_fields(self) -> None:
        f = _make_finding()
        result = _filter_finding(f)
        for field in ("finding_id", "agent_id", "finding_kind", "severity",
                      "confidence", "headline", "detail", "computed_at",
                      "expires_at", "agent_version"):
            assert field in result, f"allowed field {field!r} was stripped"


class TestFilterAction:
    """_filter_action strips decided_by.id and non-decision parameters."""

    def test_decided_by_id_stripped(self) -> None:
        a = _make_action(with_decided_by_id=True)
        result = _filter_action(a)
        assert result["decided_by"] == {"kind": "operator"}
        assert "id" not in result.get("decided_by", {})

    def test_parameters_present_on_decision_action(self) -> None:
        a = _make_action(with_parameters=True)
        assert "decision" in a
        result = _filter_action(a)
        assert "parameters" in result

    def test_parameters_absent_on_non_decision_action(self) -> None:
        a = _make_action(with_parameters=True)
        del a["decision"]  # make it non-decision
        result = _filter_action(a)
        assert "parameters" not in result

    def test_fleet_id_stripped(self) -> None:
        a = _make_action()
        result = _filter_action(a)
        assert "fleet_id" not in result

    def test_audit_entry_secret_note_stripped(self) -> None:
        a = _make_action()
        result = _filter_action(a)
        for entry in result["audit_trail"]:
            assert "secret_note" not in entry


# ===========================================================================
# Decision Action id derivation (contract § 4 literal pin)
# ===========================================================================

def test_derived_decision_action_id_literal() -> None:
    """The derived decision Action id matches the contract § 4 literal.

    Both Lambdas (approve, decline) use the same derivation pinned by a
    literal test.  This test pins it for the supervisor tool so that
    all three agree.
    """
    import hashlib as _hl

    fid = "FIND-aabbccdd-1234-5678-abcd-ef1234567890"
    h = _hl.sha256(f"{fid}|fi-decision".encode()).hexdigest()[:32]
    expected = f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    assert _derive_decision_action_id(fid) == expected


def test_non_decision_action_ignored() -> None:
    """An Action without 'decision' is treated as no action (contract § 3, § 4).

    An informational health Finding may have a non-decision Action in the
    table (e.g. an 'acknowledge_only' from another consumer).  GetItem
    returns it; it must be ignored so queue_status is still 'pending' or
    'expired', not 'executed'.
    """
    action_no_decision = _make_action()
    del action_no_decision["decision"]  # make it a non-decision Action
    out = _call(finding=_make_finding(), action=action_no_decision)
    assert out["available"] is True
    # No decision → pending (finding has future expires_at)
    assert out["queue_status"] == "pending"
    # Non-decision action must not appear
    assert "action" not in out


# ===========================================================================
# Allowlist: storage-only fields must not survive the filter
# ===========================================================================

class TestAllowlistSubsetAssertions:
    """Subset assertions against a hardcoded expected key set.

    Checking against a runtime constant (e.g. _FINDING_ALLOWLIST) would not
    catch a mutant that adds owner_id/vehicle_id to the constant — both the
    production code and the test would use the widened set.  These tests use
    a hardcoded expected set derived from the contract § 7 text so that any
    addition to the allowlist constants requires a corresponding change here.

    Mutation coverage:
      M4 (note/by added to _AUDIT_ENTRY_ALLOWLIST)  → CAUGHT by test_audit_entry_keys_hardcoded
      M5 (owner_id/vehicle_id to _FINDING_ALLOWLIST) → CAUGHT by test_finding_storage_only_fields_not_returned
      M6 (source_ref to _EVIDENCE_ENTRY_ALLOWLIST)  → CAUGHT by test_evidence_entries_filtered (existing)
    """

    # Hardcoded expected key sets from contract § 7 (not the runtime constants).
    _EXPECTED_FINDING_KEYS = frozenset({
        "finding_id", "agent_id", "finding_kind", "severity", "confidence",
        "headline", "detail", "computed_at", "expires_at", "recommended_action",
        "location", "disclosure_txt", "agent_version",
        "vin",       # extracted from scope.vin
        "evidence",  # filtered separately
    })
    _EXPECTED_AUDIT_ENTRY_KEYS = frozenset({
        "at", "from_status", "to_status", "failure_kind",
        "failure_reason", "error_status", "guardrail",
    })

    def test_finding_output_keys_are_hardcoded_subset(self) -> None:
        """All keys in a filtered Finding are in the contract § 7 hardcoded set."""
        f = _make_finding()
        result = _filter_finding(f)
        unexpected = set(result.keys()) - self._EXPECTED_FINDING_KEYS
        assert not unexpected, (
            f"filtered finding has unexpected keys: {unexpected!r}; "
            "check _FINDING_ALLOWLIST against contract § 7"
        )

    def test_finding_storage_only_fields_not_returned(self) -> None:
        """owner_id and vehicle_id (storage-only VIN-scoped) never reach the browser."""
        # Fixture now includes these fields so the test is not vacuous.
        f = _make_finding()
        assert "owner_id" in f, "fixture must carry owner_id"
        assert "vehicle_id" in f, "fixture must carry vehicle_id"
        result = _filter_finding(f)
        assert "owner_id" not in result, "owner_id must be stripped (storage-only)"
        assert "vehicle_id" not in result, "vehicle_id must be stripped (storage-only)"

    def test_audit_entry_keys_hardcoded(self) -> None:
        """Filtered audit entries carry only the contract § 7 hardcoded key set."""
        a = _make_action()
        # Fixture must have note and by so the test is not vacuous.
        for entry in a["audit_trail"]:
            assert "note" in entry, "fixture must carry audit note"
            assert "by" in entry, "fixture must carry audit by"
        result = _filter_action(a)
        for entry in result.get("audit_trail", []):
            unexpected = set(entry.keys()) - self._EXPECTED_AUDIT_ENTRY_KEYS
            assert not unexpected, (
                f"audit entry has unexpected keys: {unexpected!r}; "
                "check _AUDIT_ENTRY_ALLOWLIST against contract § 7"
            )

    def test_evidence_source_ref_not_returned(self) -> None:
        """evidence source_ref (storage-only) must not reach the browser."""
        f = _make_finding()
        # Fixture must have source_ref so the test is not vacuous.
        assert any(
            "source_ref" in e for e in f.get("evidence", [])
        ), "fixture must carry evidence source_ref"
        result = _filter_finding(f)
        for entry in result.get("evidence", []):
            assert "source_ref" not in entry, "evidence source_ref must be stripped"


# ===========================================================================
# No model call and p95 latency
# ===========================================================================

def test_no_model_call() -> None:
    """No boto3.client call is ever made — only boto3.resource for DynamoDB.

    A version that called Bedrock or Athena inside the tool would fail this.
    Spec D12: "No model call. No Athena call. Fast DynamoDB read (p95 < 300 ms)."
    """
    jwt_str, env = _make_jwt(groups=["fleet-operator"], fleet_ids=["FLEET-001"])
    ctx = SimpleNamespace(jwt=jwt_str)
    cfg = SimpleNamespace()
    resource_mock = _ddb_stubs(_make_finding(), _make_action())
    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock), \
         patch.object(fr_mod.boto3, "client") as mock_client:
        tool_fn = make_fleet_recommendation_tool(ctx, cfg)
        out = tool_fn("FIND-aabbccdd-1234-5678-abcd-ef1234567890")
    assert out["available"] is True
    mock_client.assert_not_called()


def test_p95_latency_under_300ms() -> None:
    """p95 latency under 300 ms over 200 calls against a stubbed table.

    Spec D12 accept criteria: "p95 under 300 ms against a stubbed table".
    This is a pure Python/mock call — wall-clock p95 must stay well under
    300 ms even with test framework overhead.
    """
    import time

    jwt_str, env = _make_jwt(groups=["fleet-operator"], fleet_ids=["FLEET-001"])
    ctx = SimpleNamespace(jwt=jwt_str)
    cfg = SimpleNamespace()
    finding = _make_finding()
    action = _make_action()
    resource_mock = _ddb_stubs(finding, action)

    latencies: list[float] = []
    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock):
        tool_fn = make_fleet_recommendation_tool(ctx, cfg)
        for _ in range(200):
            t0 = time.perf_counter()
            tool_fn("FIND-aabbccdd-1234-5678-abcd-ef1234567890")
            latencies.append(time.perf_counter() - t0)

    latencies.sort()
    p95_s = latencies[int(0.95 * len(latencies))]
    assert p95_s < 0.3, (
        f"p95={p95_s * 1000:.1f} ms >= 300 ms; "
        "the tool must be a fast DynamoDB read with no model call"
    )


# ===========================================================================
# Registration in PERSONA_TOOL_FACTORIES
# ===========================================================================

def test_fleet_recommendation_registered_for_fleet_manager_only() -> None:
    """make_fleet_recommendation_tool is in the fleet_manager persona list.

    Spec D12: tool is for "the fleet operator … the fleet assistant".
    NOT for driver/consumer/owner (FI queue is an operator surface).
    """
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES
    from agents.supervisor.tools.fleet_recommendation import make_fleet_recommendation_tool as mfrt

    fm = PERSONA_TOOL_FACTORIES["fleet_manager"]
    assert mfrt in fm, (
        "make_fleet_recommendation_tool must be in fleet_manager persona; "
        f"current fleet_manager tools: {[f.__name__ for f in fm]}"
    )

    # Not on consumer-facing personas
    for persona in ("fleet_driver", "consumer", "vehicle_owner", "service_advisor"):
        assert mfrt not in PERSONA_TOOL_FACTORIES.get(persona, []), (
            f"make_fleet_recommendation_tool must not be on {persona!r} "
            "(FI cost recommendation queue is an operator surface)"
        )




# ===========================================================================
# Group 3 review cycle 2 (tier1 W1–W3)
# ===========================================================================

#: Contract § 7 "Action" allowlist, written out here (not imported) so widening the module's
#: constant fails this test.
_ACTION_FIELDS_ALLOWED = frozenset({
    "action_id", "finding_id", "action_kind", "target_system", "status", "decision",
    "decided_by", "created_at", "updated_at", "target_ref", "audit_trail", "parameters",
})


def test_decision_action_read_by_its_derived_key() -> None:
    """W1 (M7): with key-aware table stubs, a completed decision shows `executed` only when the
    tool reads the Action by the id derived from finding_id (contract § 4)."""
    fid = "FIND-aabbccdd-1234-5678-abcd-ef1234567890"
    out = _call(_make_finding(finding_id=fid), _make_action(finding_id=fid, status="completed"))
    assert out["available"] is True
    assert out["action"]["action_id"] == _derive_decision_action_id(fid)
    assert out["queue_status"] == "executed"


def test_action_output_fields_are_the_contract_allowlist() -> None:
    """W2: `created_by` and the Action's stored `evidence` copy never reach the output."""
    out = _call(_make_finding(), _make_action())
    action = out["action"]
    assert set(action) <= _ACTION_FIELDS_ALLOWED, set(action) - _ACTION_FIELDS_ALLOWED
    assert "created_by" not in action
    assert "evidence" not in action


def test_no_bedrock_client_by_any_route() -> None:
    """W3 (M8): no Bedrock client through boto3.client, a boto3 Session, or botocore."""
    import boto3.session
    import botocore.session

    jwt_str, env = _make_jwt(groups=["fleet-operator"], fleet_ids=["FLEET-001"])
    ctx = SimpleNamespace(jwt=jwt_str)
    resource_mock = _ddb_stubs(_make_finding(), _make_action())

    def _refuse(*args, **kwargs):
        raise AssertionError(f"fleet_recommendation created a client: {args} {kwargs}")

    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock), \
         patch.object(fr_mod.boto3, "client", side_effect=_refuse), \
         patch.object(boto3.session.Session, "client", _refuse), \
         patch.object(botocore.session.Session, "create_client", _refuse):
        out = make_fleet_recommendation_tool(ctx, SimpleNamespace())(
            "FIND-aabbccdd-1234-5678-abcd-ef1234567890"
        )
    assert out["available"] is True
    src = pathlib.Path(fr_mod.__file__).read_text()
    for banned in ("bedrock", "BedrockModel", "converse", "strands.models"):
        assert banned not in src, f"fleet_recommendation.py mentions {banned!r}"




# ===========================================================================
# Group 3 review cycle 3 (tier1 W1, W2)
# ===========================================================================

def test_no_client_created_even_if_the_tool_swallows_errors() -> None:
    """W1 (N3): a recording mock, checked after the call, so a model client created inside a
    try/except is still seen."""
    import botocore.session

    jwt_str, env = _make_jwt(groups=["fleet-operator"], fleet_ids=["FLEET-001"])
    ctx = SimpleNamespace(jwt=jwt_str)
    resource_mock = _ddb_stubs(_make_finding(), _make_action())
    recorder = MagicMock(side_effect=RuntimeError("no clients in this test"))
    with patch.dict("os.environ", env), \
         patch.object(fr_mod.boto3, "resource", return_value=resource_mock), \
         patch.object(botocore.session.Session, "create_client", recorder):
        out = make_fleet_recommendation_tool(ctx, SimpleNamespace())(
            "FIND-aabbccdd-1234-5678-abcd-ef1234567890"
        )
    assert out["available"] is True
    assert recorder.call_count == 0, f"a botocore client was created: {recorder.call_args_list}"


@pytest.mark.parametrize("caller_fleets", [["FLEET-0011"], ["FLEET-00"], ["XFLEET-001"], ["fleet-001"]])
def test_fleet_membership_is_exact(caller_fleets) -> None:
    """W2 (N4): standing needs the exact fleet id; an overlapping or differently-cased id is
    refused (a substring match would let FLEET-0011 read FLEET-001)."""
    out = _call(_make_finding(fleet_id="FLEET-001"), _make_action(), fleet_ids=caller_fleets)
    assert out["available"] is False
