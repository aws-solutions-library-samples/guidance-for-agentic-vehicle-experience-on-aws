"""G4 S4: run_recall / run_warranty model path tests.

Drives a real boto3.client('bedrock-runtime') through botocore.stub.Stubber.
DDB, Athena, ArtifactWriter, and lookup_live are monkeypatched at module seams.
"""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from agents.tier2 import runner

_VIN = "VIN-G4-001"
_DEALER_ID = "dealer-seattle"
_CAMPAIGN_ID = "CAMP-G4-001"

# Minimal non_safety+CONFIRMED recall row → severity=soon, items=[book]
_RECALL_ROW: dict[str, Any] = {
    "vin": _VIN, "campaign_id": _CAMPAIGN_ID,
    "recall_class": "non_safety", "do_not_drive": False,
    "park_outside": False, "match_basis": "CONFIRMED",
    "remedy_available_from": None, "remedy_part_numbers": [],
    "received_date": "2024-01-01", "catalog_version": "1",
    "rule_version": "1", "models": ["G4 Model"], "model_year_min": 2022,
    "model_year_max": 2024, "plants": [], "build_from": None, "build_to": None,
}

# Minimal warranty row for one eligible item — DMS-shaped (vehicle_vin, odometer_km,
# in_service_date, dealer_id) so N2 (re-merge raw row) would inject vehicle_vin into context.
_WARRANTY_ROW: dict[str, Any] = {
    "vin": _VIN, "vehicle_vin": _VIN, "ro_id": "RO-G4-001",
    "filing_deadline": "2099-12-31",
    "verdict": "approved", "verdict_status": "approved",
    "coverage_group": "powertrain", "recoverable_usd": 1200,
    "terms_version": "v1", "subject_key": "powertrain/file/1",
    "odometer_km": 40000, "in_service_date": "2022-01-01", "dealer_id": _DEALER_ID,
}

_STOCK_ROW: dict[str, Any] = {
    "dealer_id": _DEALER_ID, "part_number": "", "qty_on_hand": 1,
    "latitude": 47.6, "longitude": -122.3,
}

_WARRANTY_TERMS = {
    "powertrain": {
        "coverage_group": "powertrain", "term_years": 5, "term_miles": 60000,
        "labor_rate_usd": 150, "terms_version": "v1",
    },
}

_MODEL_DETAIL = "Service at dealer immediately — model recommendation."
_WARRANTY_NARRATIVE = "Complaint: noise. Cause: bearing. Correction: replace bearing."

_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
_GID = "guardrail-g4"
_GVER = "1"


def _make_bedrock_client() -> Any:
    return boto3.client(
        "bedrock-runtime", region_name="us-west-2",
        aws_access_key_id="x", aws_secret_access_key="x",
    )


def _converse_recall_resp(detail: str, dealer_id: str = _DEALER_ID) -> dict:
    return {
        "output": {"message": {
            "role": "assistant",
            "content": [{"toolUse": {
                "toolUseId": "tid-recall",
                "name": "record_recall_plan",
                "input": {
                    "dealer_id": dealer_id, "combine_with": [], "urgency": "this_week",
                    "detail": detail, "evidence_refs": ["recall.campaign"], "confidence": "high",
                },
            }}],
        }},
        "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        "metrics": {"latencyMs": 100},
        "stopReason": "tool_use",
    }


def _converse_warranty_resp(narrative: str) -> dict:
    return {
        "output": {"message": {
            "role": "assistant",
            "content": [{"toolUse": {
                "toolUseId": "tid-warranty",
                "name": "record_claim_draft",
                "input": {
                    "narrative": narrative, "proceed": "file",
                    "detail": "Submit the claim with warranty evidence.",
                    "evidence_refs": [], "confidence": "high",
                },
            }}],
        }},
        "usage": {"inputTokens": 80, "outputTokens": 40, "totalTokens": 120},
        "metrics": {"latencyMs": 80},
        "stopReason": "tool_use",
    }


def _guardrail_none() -> dict:
    return {
        "action": "NONE",
        "outputs": [],
        "assessments": [],
        "usage": {
            "topicPolicyUnits": 0, "contentPolicyUnits": 0, "wordPolicyUnits": 0,
            "sensitiveInformationPolicyUnits": 0, "sensitiveInformationPolicyFreeUnits": 0,
            "contextualGroundingPolicyUnits": 0,
        },
    }


def _patch_recall_seams(monkeypatch, *, vin_findings=None):
    """Patch all DDB/Athena seams for a one-VIN recall run."""
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.fi_recall_warranty import attempts as rw_att
    from agents.tier2 import writer as tier2_writer
    from agents.tier2 import artifact_writer as aw_mod

    monkeypatch.setattr(rw_sel, "select_unremedied_recalls",
                        lambda *a, **kw: ([_RECALL_ROW], 0))
    monkeypatch.setattr(rw_sel, "select_parts_stock",
                        lambda *a, **kw: [_STOCK_ROW])
    monkeypatch.setattr(rw_sel, "lookup_vehicle_cms_ids",
                        lambda vin, **kw: ("VEH001", "FLEET001", "LOC001"))
    monkeypatch.setattr(rw_sel, "lookup_fleet_locations",
                        lambda fid, **kw: {"LOC001": (47.5, -122.4)})
    monkeypatch.setattr(rw_sel, "check_grounded",
                        lambda vid, **kw: (False, 0))
    monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: None)

    calls = {"read_vin_findings": 0}
    items = vin_findings if vin_findings is not None else []

    def fake_read_vin_findings(vin, **kw):
        calls["read_vin_findings"] += 1
        return items

    monkeypatch.setattr(rw_att, "read_vin_findings", fake_read_vin_findings)
    monkeypatch.setattr(rw_att, "attempt_history_from_items",
                        lambda items, **kw: ({}, 0))
    return calls


def _patch_warranty_seams(monkeypatch):
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.tier2 import writer as tier2_writer

    monkeypatch.setattr(rw_sel, "select_warranty_candidates",
                        lambda *a, **kw: [_WARRANTY_ROW])
    monkeypatch.setattr(rw_sel, "select_warranty_terms",
                        lambda *a, **kw: _WARRANTY_TERMS)
    monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: None)


class _FakeWriter:
    """Minimal ArtifactWriter stand-in."""

    def __init__(self):
        self.table_name = "vsa-staging-avx-findings"
        self.dynamodb_client = MagicMock()
        self.calls: list = []

    def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                      update_on_same_hash=False):
        self.calls.append(finding.copy())
        return {"action": "put", "finding_id": finding.get("finding_id", "FID")}


# ---------------------------------------------------------------------------
# run_recall end-to-end with model
# ---------------------------------------------------------------------------

class TestRunRecallModel:
    def test_written_finding_carries_model_detail_and_source(self, monkeypatch):
        calls = _patch_recall_seams(monkeypatch)
        client = _make_bedrock_client()
        writer = _FakeWriter()
        records: list = []

        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            runner.run_recall(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                vehicles_table="t", fleet_locations_table="t2",
                groundings_table="t3", actions_table="t4",
                avx_writer=writer, bedrock_client=client,
                guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                run_record_writer=lambda **kw: records.append(kw),
                owner_resolver=lambda vin: None,
            )
            stub.assert_no_pending_responses()

        assert writer.calls, "run_recall must write at least one Finding"
        finding = writer.calls[-1]
        ev = {e["kind"]: e["value"] for e in finding.get("evidence", [])}
        # recall.diagnosis_source is written as {kind: "recall.diagnosis_source", value: "model"}
        diag_src = ev.get("recall.diagnosis_source")
        assert diag_src == "model", (
            f"recall.diagnosis_source evidence entry must be 'model', got {diag_src!r}"
        )
        # The model's detail lands on the recall.plan evidence entry or the finding's detail.
        assert finding.get("detail") == _MODEL_DETAIL or ev.get("recall.detail") == _MODEL_DETAIL or any(
            isinstance(v, dict) and v.get("detail") == _MODEL_DETAIL
            for v in ev.values()
        ), f"Finding must carry the model detail '{_MODEL_DETAIL}'"

    def test_run_record_carries_model_counters(self, monkeypatch):
        _patch_recall_seams(monkeypatch)
        client = _make_bedrock_client()
        writer = _FakeWriter()
        records: list = []

        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            runner.run_recall(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                vehicles_table="t", fleet_locations_table="t2",
                groundings_table="t3", actions_table="t4",
                avx_writer=writer, bedrock_client=client,
                guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                run_record_writer=lambda **kw: records.append(kw),
                owner_resolver=lambda vin: None,
            )

        (rec,) = records
        assert rec["model_calls"] >= 1, "model_calls must be ≥ 1"
        assert rec["tokens_used"] > 0, "tokens_used must be > 0"
        assert rec["cost_usd"] > 0.0, "cost_usd must be > 0"
        for key in ("rules_fallbacks", "guardrail_interventions", "deferred"):
            assert key in rec, f"run record must carry {key!r}"


# ---------------------------------------------------------------------------
# run_warranty end-to-end with model
# ---------------------------------------------------------------------------

class TestRunWarrantyModel:
    def test_written_finding_carries_model_detail_and_source(self, monkeypatch):
        _patch_warranty_seams(monkeypatch)
        client = _make_bedrock_client()
        writer = _FakeWriter()

        with Stubber(client) as stub:
            stub.add_response("converse", _converse_warranty_resp(_WARRANTY_NARRATIVE))
            stub.add_response("apply_guardrail", _guardrail_none())
            runner.run_warranty(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", avx_writer=writer,
                bedrock_client=client, guardrail_id=_GID, guardrail_version=_GVER,
                model_id=_MODEL_ID, owner_resolver=lambda vin: None,
                run_record_writer=lambda **kw: None,
            )
            stub.assert_no_pending_responses()

        assert writer.calls, "run_warranty must write at least one Finding"
        finding = writer.calls[-1]
        # The model's detail is carried directly on the finding.
        assert finding.get("detail") == "Submit the claim with warranty evidence.", (
            f"Finding must carry the model's detail, got {finding.get('detail')!r}"
        )
        # Confidence reflects the model call ("high"), not the rules default ("low").
        assert finding.get("confidence") == "high", (
            f"Finding confidence must come from the model call, got {finding.get('confidence')!r}"
        )
        # C2: warranty.diagnosis_source evidence entry must be "model", not "rules".
        ev = {e["kind"]: e["value"] for e in finding.get("evidence", [])}
        diag_src = ev.get("warranty.diagnosis_source")
        assert diag_src == "model", (
            f"warranty.diagnosis_source evidence entry must be 'model', got {diag_src!r}"
        )

    def test_item_context_uses_evidence_keys_not_raw_row(self, monkeypatch):
        """item_context must be built from evidence values, not raw row fields (W2).

        The converse request's JSON must carry warranty.* evidence kinds as keys and
        must NOT carry vehicle_vin or ro_id (which come from the raw DMS row).
        """
        _patch_warranty_seams(monkeypatch)
        from agents.fi_recall_warranty import llm as rw_llm

        captured: list = []

        def fake_draft_claim(*, bedrock_client, item_context, evidence_kinds,
                              rules_fn, stats, model_id, guardrail_id, guardrail_version):
            captured.append(dict(item_context))
            return rules_fn(Exception("spy"))

        monkeypatch.setattr(rw_llm, "draft_claim", fake_draft_claim)

        client = _make_bedrock_client()
        writer = _FakeWriter()
        runner.run_warranty(
            "staging", athena_client=MagicMock(), workgroup="wg",
            results_s3_path="s3://b/p/", avx_writer=writer,
            bedrock_client=client, guardrail_id=_GID, guardrail_version=_GVER,
            model_id=_MODEL_ID, owner_resolver=lambda vin: None,
            run_record_writer=lambda **kw: None,
        )

        assert captured, "draft_claim must be called"
        ctx = captured[0]
        _EXPECTED_CTX_KEYS = {
            "warranty.verdict", "warranty.ro_lines", "warranty.coverage",
            "warranty.recovery", "evidence_kinds",
        }
        assert set(ctx) == _EXPECTED_CTX_KEYS, (
            f"item_context exact key set must be {_EXPECTED_CTX_KEYS}; got {sorted(ctx)}. W2"
        )
        # Assert one carried value: labor_rate from warranty terms (150 USD).
        assert ctx["warranty.recovery"]["labor_rate"] == 150, (
            f"warranty.recovery.labor_rate must be 150; got "
            f"{ctx['warranty.recovery'].get('labor_rate')!r}. W2"
        )


# ---------------------------------------------------------------------------
# bedrock_client=None → no converse
# ---------------------------------------------------------------------------

class TestNoBedrockClient:
    def test_no_converse_when_bedrock_client_none(self, monkeypatch):
        _patch_recall_seams(monkeypatch)
        writer = _FakeWriter()
        converse_calls: list = []

        def fake_converse(*a, **kw):
            converse_calls.append(kw)
            return {}

        runner.run_recall(
            "staging", athena_client=MagicMock(), workgroup="wg",
            results_s3_path="s3://b/p/", ddb_client=MagicMock(),
            vehicles_table="t", fleet_locations_table="t2",
            groundings_table="t3", actions_table="t4",
            avx_writer=writer, bedrock_client=None,
            run_record_writer=lambda **kw: None,
            owner_resolver=lambda vin: None,
        )
        assert not converse_calls, "No converse call when bedrock_client=None"


# ---------------------------------------------------------------------------
# One by_vin query per VIN (cache)
# ---------------------------------------------------------------------------

class TestVinCache:
    def test_by_vin_queried_once_per_vin(self, monkeypatch):
        """history_fn and open_items_fn share the per-run cache: one read_vin_findings per VIN."""
        calls = _patch_recall_seams(monkeypatch)
        client = _make_bedrock_client()
        writer = _FakeWriter()

        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            runner.run_recall(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                vehicles_table="t", fleet_locations_table="t2",
                groundings_table="t3", actions_table="t4",
                avx_writer=writer, bedrock_client=client,
                guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                run_record_writer=lambda **kw: None,
                owner_resolver=lambda vin: None,
            )

        assert calls["read_vin_findings"] == 1, (
            f"Expected exactly 1 read_vin_findings call for VIN '{_VIN}', "
            f"got {calls['read_vin_findings']}"
        )


# ---------------------------------------------------------------------------
# item_context allowed_urgencies for p0
# ---------------------------------------------------------------------------

class TestAllowedUrgencies:
    def test_p0_allowed_urgencies_exactly_this_week(self, monkeypatch):
        """p0 band (safety+do_not_drive) → item_context allowed_urgencies = ['this_week']."""
        p0_row = {**_RECALL_ROW, "recall_class": "safety", "do_not_drive": True}

        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_recall_warranty import attempts as rw_att
        from agents.tier2 import writer as tier2_writer

        monkeypatch.setattr(rw_sel, "select_unremedied_recalls",
                            lambda *a, **kw: ([p0_row], 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **kw: [_STOCK_ROW])
        monkeypatch.setattr(rw_sel, "lookup_vehicle_cms_ids",
                            lambda vin, **kw: ("VEH001", "FLEET001", "LOC001"))
        monkeypatch.setattr(rw_sel, "lookup_fleet_locations",
                            lambda fid, **kw: {"LOC001": (47.5, -122.4)})
        monkeypatch.setattr(rw_sel, "check_grounded", lambda vid, **kw: (False, 0))
        monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: None)
        monkeypatch.setattr(rw_att, "read_vin_findings", lambda vin, **kw: [])
        monkeypatch.setattr(rw_att, "attempt_history_from_items",
                            lambda items, **kw: ({}, 0))

        captured: list = []

        def spy_plan_fn(*, row, item_type, band_result, rooftops, origin, bundle, **kw):
            from agents.tier2.runner import _allowed_urgencies_for_severity
            ctx = {
                "allowed_urgencies": _allowed_urgencies_for_severity(band_result.severity),
            }
            captured.append(ctx)
            # Return rules fallback so writing succeeds
            from agents.fi_recall_warranty.runner import build_rules_plan
            return build_rules_plan(rooftops, band_result) if rooftops else {
                "dealer_id": None, "combine_with": [], "urgency": band_result.urgency,
                "detail": "fallback", "confidence": "low", "diagnosis_source": "rules",
            }

        # Patch the plan_fn by intercepting llm.plan_booking
        from agents.fi_recall_warranty import llm as rw_llm

        def fake_plan_booking(*, bedrock_client, item_context, candidate_dealer_ids,
                               bundle_ids, severity, recall_class, evidence_kinds,
                               rules_fn, stats, model_id, guardrail_id, guardrail_version):
            captured.append({"allowed_urgencies": item_context.get("allowed_urgencies")})
            return rules_fn(Exception("spy"))

        monkeypatch.setattr(rw_llm, "plan_booking", fake_plan_booking)

        client = _make_bedrock_client()
        writer = _FakeWriter()
        runner.run_recall(
            "staging", athena_client=MagicMock(), workgroup="wg",
            results_s3_path="s3://b/p/", ddb_client=MagicMock(),
            vehicles_table="t", fleet_locations_table="t2",
            groundings_table="t3", actions_table="t4",
            avx_writer=writer, bedrock_client=client,
            guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
            run_record_writer=lambda **kw: None,
            owner_resolver=lambda vin: None,
        )

        # At least one plan_booking call for a p0 item
        p0_calls = [c for c in captured if c.get("allowed_urgencies") is not None]
        assert p0_calls, "No plan_booking calls captured for p0 row"
        assert p0_calls[-1]["allowed_urgencies"] == ["this_week"], (
            f"p0 allowed_urgencies must be exactly ['this_week'], "
            f"got {p0_calls[-1]['allowed_urgencies']!r}"
        )


# ---------------------------------------------------------------------------
# _from_env raises before building any client when guardrail vars unset
# ---------------------------------------------------------------------------

class TestFromEnvGuardrail:
    @pytest.mark.parametrize("fn", ["_run_recall_from_env", "_run_warranty_from_env"])
    @pytest.mark.parametrize("missing", ["FI_COST_GUARDRAIL_ID", "FI_COST_GUARDRAIL_VERSION"])
    def test_raises_guardrail_var_unset_before_any_client(self, fn, missing, monkeypatch):
        """_from_env raises RuntimeError before building any boto3 client when guardrail unset.

        Hang-safe: also patches loops so if the guardrail check is bypassed the function
        completes immediately and the 'assert not built' assertion catches the mutant.
        """
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "gid")
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")
        monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket")
        monkeypatch.delenv(missing, raising=False)

        built: list = []

        def fake_client(service, *a, **kw):
            built.append(service)
            return MagicMock()

        monkeypatch.setattr(boto3, "client", fake_client)

        # Also patch loops so a bypass of the guardrail check doesn't hang.
        from agents.fi_recall_warranty import recall as rw_recall, warranty as rw_w
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.tier2 import artifact_writer as aw_mod

        _empty_recall = {"rows": 0, "items_written": 0, "items_skipped_unchanged": 0,
                         "rows_refused": 0, "grounding_read_errors": 0, "by_item_type": {},
                         "book_items_without_rooftops": 0, "model_items": 0, "deferred": 0,
                         "plans_refused": 0, "items_refreshed_from_live": 0}
        _empty_warranty = {"rows": 0, "items_written": 0, "items_skipped_unchanged": 0,
                           "rows_refused": 0, "rows_without_terms": 0, "model_items": 0,
                           "deferred": 0, "items_refreshed_from_live": 0}
        monkeypatch.setattr(rw_sel, "select_unremedied_recalls", lambda *a, **kw: ([], 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_candidates", lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_terms", lambda *a, **kw: {})
        monkeypatch.setattr(rw_recall, "run_recall_loop", lambda **kw: _empty_recall)
        monkeypatch.setattr(rw_w, "run_warranty_loop", lambda **kw: _empty_warranty)
        monkeypatch.setattr(aw_mod, "ArtifactWriter",
                            lambda table_name, dynamodb_client: MagicMock(
                                table_name=table_name, dynamodb_client=dynamodb_client))
        monkeypatch.setattr(runner, "_assigned_driver_directory", lambda s, **kw: object())
        monkeypatch.setattr(runner, "resolve_standing_facts", lambda vin, **kw: None)

        with pytest.raises(RuntimeError, match="FI_COST_GUARDRAIL"):
            getattr(runner, fn)("staging", {}, None)

        assert not built, (
            f"No boto3 client must be built before the guardrail check; built={built}"
        )


# ---------------------------------------------------------------------------
# Malformed cap env vars raise
# ---------------------------------------------------------------------------

class TestCapParsing:
    @pytest.mark.parametrize("bad_value", ["abc", "-1", "501", "true"])
    @pytest.mark.parametrize("env_var", ["FI_RECALL_MAX_MODEL_CALLS",
                                          "FI_WARRANTY_MAX_MODEL_CALLS"])
    def test_malformed_cap_raises(self, bad_value, env_var, monkeypatch):
        """Malformed cap env var raises before building any client (hang-safe: loops also mocked)."""
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "gid")
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")
        monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket")
        monkeypatch.setenv(env_var, bad_value)

        from agents.fi_recall_warranty import recall as rw_recall, warranty as rw_w
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.tier2 import artifact_writer as aw_mod

        monkeypatch.setattr(rw_sel, "select_unremedied_recalls", lambda *a, **kw: ([], 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_candidates", lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_terms", lambda *a, **kw: {})
        monkeypatch.setattr(rw_recall, "run_recall_loop",
                            lambda **kw: {"rows": 0, "items_written": 0,
                                          "items_skipped_unchanged": 0, "rows_refused": 0,
                                          "grounding_read_errors": 0, "by_item_type": {},
                                          "book_items_without_rooftops": 0, "model_items": 0,
                                          "deferred": 0, "plans_refused": 0,
                                          "items_refreshed_from_live": 0})
        monkeypatch.setattr(rw_w, "run_warranty_loop",
                            lambda **kw: {"rows": 0, "items_written": 0,
                                          "items_skipped_unchanged": 0, "rows_refused": 0,
                                          "rows_without_terms": 0, "model_items": 0,
                                          "deferred": 0, "items_refreshed_from_live": 0})
        monkeypatch.setattr(aw_mod, "ArtifactWriter",
                            lambda table_name, dynamodb_client: MagicMock(
                                table_name=table_name, dynamodb_client=dynamodb_client))
        monkeypatch.setattr(runner, "_assigned_driver_directory", lambda s, **kw: object())
        monkeypatch.setattr(runner, "resolve_standing_facts", lambda vin, **kw: None)
        monkeypatch.setattr(boto3, "client", MagicMock())

        fn = "_run_recall_from_env" if "RECALL" in env_var else "_run_warranty_from_env"
        with pytest.raises((ValueError, RuntimeError)):
            getattr(runner, fn)("staging", {}, None)


# ---------------------------------------------------------------------------
# Bedrock client built with cost's Config
# ---------------------------------------------------------------------------

class TestBedrockClientConfig:
    @pytest.mark.parametrize("fn", ["_run_recall_from_env", "_run_warranty_from_env"])
    def test_bedrock_runtime_built_once_with_cost_config(self, fn, monkeypatch):
        """_from_env builds exactly one bedrock-runtime client with cost's timeouts."""
        monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "gid")
        monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")
        monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket")

        from agents.fi_recall_warranty import recall as rw_recall
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_recall_warranty import warranty as rw_w
        from agents.tier2 import artifact_writer as aw_mod

        monkeypatch.setattr(rw_sel, "select_unremedied_recalls",
                            lambda *a, **kw: ([], 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_candidates",
                            lambda *a, **kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_terms", lambda *a, **kw: {})
        monkeypatch.setattr(rw_recall, "run_recall_loop",
                            lambda **kw: {"rows": 0, "items_written": 0,
                                          "items_skipped_unchanged": 0,
                                          "rows_refused": 0, "grounding_read_errors": 0,
                                          "by_item_type": {}, "book_items_without_rooftops": 0,
                                          "model_items": 0, "deferred": 0,
                                          "plans_refused": 0, "items_refreshed_from_live": 0})
        monkeypatch.setattr(rw_w, "run_warranty_loop",
                            lambda **kw: {"rows": 0, "items_written": 0,
                                          "items_skipped_unchanged": 0,
                                          "rows_refused": 0, "rows_without_terms": 0,
                                          "model_items": 0, "deferred": 0,
                                          "items_refreshed_from_live": 0})

        class _FakeAW:
            def __init__(self, table_name, dynamodb_client):
                self.table_name = table_name
                self.dynamodb_client = dynamodb_client
            def write_finding(self, f, **kw): return {}

        monkeypatch.setattr(aw_mod, "ArtifactWriter", _FakeAW)

        bedrock_clients: list = []
        orig_boto_client = boto3.client

        def recording_client(service, *a, **kw):
            if service == "bedrock-runtime":
                bedrock_clients.append(kw.get("config"))
                return MagicMock(name="bedrock-runtime")
            return MagicMock(name=service)

        monkeypatch.setattr(boto3, "client", recording_client)
        monkeypatch.setattr(runner, "_assigned_driver_directory", lambda s, **kw: object())
        monkeypatch.setattr(runner, "resolve_standing_facts", lambda vin, **kw: None)

        getattr(runner, fn)("staging", {}, None)

        assert len(bedrock_clients) == 1, (
            f"Exactly one bedrock-runtime client must be built, got {len(bedrock_clients)}")
        cfg = bedrock_clients[0]
        assert isinstance(cfg, Config), "Must pass a botocore.config.Config"
        assert cfg.read_timeout == 30, f"read_timeout must be 30s, got {cfg.read_timeout}"
        assert cfg.connect_timeout == 5, f"connect_timeout must be 5s, got {cfg.connect_timeout}"
        retries = cfg.retries or {}
        assert retries.get("total_max_attempts") == 1, (
            f"total_max_attempts must be 1, got {retries}")


# ---------------------------------------------------------------------------
# Heartbeat is put
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_heartbeat_run_records_written_emitted(self, monkeypatch):
        """run_recall emits RunRecordsWritten heartbeat after a successful run."""
        _patch_recall_seams(monkeypatch)
        client = _make_bedrock_client()
        writer = _FakeWriter()
        cw = MagicMock()
        heartbeat_calls: list = []

        # Patch at the runner module level — imported name, not the observability module.
        monkeypatch.setattr(runner, "emit_run_record_heartbeat",
                            lambda cw_client, stage, component: heartbeat_calls.append((stage, component)))

        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            runner.run_recall(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                vehicles_table="t", fleet_locations_table="t2",
                groundings_table="t3", actions_table="t4",
                avx_writer=writer, bedrock_client=client,
                guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                run_record_writer=lambda **kw: True,
                owner_resolver=lambda vin: None,
                cloudwatch_client=cw,
            )

        assert heartbeat_calls, "RunRecordsWritten heartbeat must be emitted after a successful run"
        assert any(stage == "staging" for stage, _ in heartbeat_calls)



# ---------------------------------------------------------------------------
# C1: write wrappers must forward update_on_same_hash (runner-level, day 1→2)
# ---------------------------------------------------------------------------

class TestDayTwoUpgrade:
    """C1: run_recall / run_warranty write closures must forward update_on_same_hash."""

    def _kw_writer(self):
        kwlog: list = []
        class _K(_FakeWriter):
            def write_finding(self, f, *, standing=None, refresh_on_noop=False,
                              update_on_same_hash=False):
                kwlog.append(update_on_same_hash)
                return super().write_finding(f, standing=standing,
                                             refresh_on_noop=refresh_on_noop,
                                             update_on_same_hash=update_on_same_hash)
        return _K(), kwlog

    def test_recall_day2_update_on_same_hash(self, monkeypatch):
        from agents.tier2 import writer as tier2_writer
        _patch_recall_seams(monkeypatch)
        w1 = _FakeWriter()
        runner.run_recall("staging", athena_client=MagicMock(), workgroup="wg",
                          results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                          vehicles_table="t", fleet_locations_table="t2",
                          groundings_table="t3", actions_table="t4",
                          avx_writer=w1, bedrock_client=None,
                          run_record_writer=lambda **kw: None,
                          owner_resolver=lambda vin: None)
        assert w1.calls, "day 1 must write a finding"
        live = dict(w1.calls[-1])
        monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: live)
        w2, kwlog = self._kw_writer()
        client = _make_bedrock_client()
        completed = False
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            try:
                runner.run_recall(
                    "staging", athena_client=MagicMock(), workgroup="wg",
                    results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                    vehicles_table="t", fleet_locations_table="t2",
                    groundings_table="t3", actions_table="t4",
                    avx_writer=w2, bedrock_client=client,
                    guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                    run_record_writer=lambda **kw: None, owner_resolver=lambda vin: None)
                completed = True
            except TypeError:
                pass
        assert completed, "run_recall day-2 must complete without TypeError (C1)"
        assert any(kwlog), (
            "avx_writer.write_finding must be called with update_on_same_hash=True (C1)"
        )

    def test_warranty_day2_update_on_same_hash(self, monkeypatch):
        from agents.tier2 import writer as tier2_writer
        _patch_warranty_seams(monkeypatch)
        w1 = _FakeWriter()
        runner.run_warranty("staging", athena_client=MagicMock(), workgroup="wg",
                            results_s3_path="s3://b/p/", avx_writer=w1, bedrock_client=None,
                            run_record_writer=lambda **kw: None, owner_resolver=lambda vin: None)
        assert w1.calls, "day 1 must write a finding"
        live = dict(w1.calls[-1])
        monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: live)
        w2, kwlog = self._kw_writer()
        client = _make_bedrock_client()
        completed = False
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_warranty_resp(_WARRANTY_NARRATIVE))
            stub.add_response("apply_guardrail", _guardrail_none())
            try:
                runner.run_warranty(
                    "staging", athena_client=MagicMock(), workgroup="wg",
                    results_s3_path="s3://b/p/", avx_writer=w2, bedrock_client=client,
                    guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                    run_record_writer=lambda **kw: None, owner_resolver=lambda vin: None)
                completed = True
            except TypeError:
                pass
        assert completed, "run_warranty day-2 must complete without TypeError (C1)"
        assert any(kwlog), (
            "avx_writer.write_finding must be called with update_on_same_hash=True (C1)"
        )


# ---------------------------------------------------------------------------
# C2: warranty loop must record diagnosis_source=model and enable day-2 skip
# ---------------------------------------------------------------------------

class TestWarrantyDiagnosisSource:
    """C2: model-written Finding must carry warranty.diagnosis_source=model so
    the day-2 run takes the refresh_from_live path (D8 step 4)."""

    _ROW = {
        "vehicle_vin": "VIN-C2", "ro_id": "RO-C2-001", "line_no": 1,
        "verdict_status": "approved", "verdict_reason": "in term",
        "matched_rule_id": "R1", "terms_version": "v1",
        "coverage_group": "powertrain", "recoverable_usd": 800.0,
        "labor_op": "LOP-1", "dealer_id": "D1", "fault_code": "P0420",
        "filing_deadline": "2099-12-31", "in_service_date": "2024-01-01",
        "odometer_km": 10000, "repair_date": "2026-09-01",
    }
    _TERMS = {"powertrain": {"coverage_group": "powertrain", "term_years": 5,
                              "term_miles": 60000, "labor_rate_usd": 150,
                              "terms_version": "v1"}}

    def test_day1_model_source_and_day2_skip(self):
        from agents.fi_recall_warranty.warranty import run_warranty_loop
        store: dict = {}
        plan_calls = [0]

        def plan_fn(*, row, evidence):
            plan_calls[0] += 1
            return {"narrative": "N.", "proceed": "file", "detail": "D",
                    "confidence": "high", "diagnosis_source": "model"}

        def write(finding, standing, **kw):
            store[finding["finding_key"]] = finding

        def run():
            return run_warranty_loop(
                candidate_rows=[dict(self._ROW)],
                computed_at="2026-10-08T00:00:00Z",
                standing_fn=lambda vin: None,
                lookup_live=lambda k: store.get(k),
                write_finding=write,
                plan_fn=plan_fn,
                warranty_terms=self._TERMS,
            )

        run()
        assert store, "day 1 must write a finding"
        ev = {e["kind"]: e["value"] for e in list(store.values())[0].get("evidence", [])}
        assert ev.get("warranty.diagnosis_source") == "model", (
            f"day1 warranty.diagnosis_source must be 'model'; got {ev.get('warranty.diagnosis_source')!r} (C2)"
        )
        plan_calls[0] = 0
        s2 = run()
        assert plan_calls[0] == 0, (
            f"day2 must make 0 plan_fn calls (D8 step 4); got {plan_calls[0]} (C2)"
        )
        assert s2["items_refreshed_from_live"] == 1, (
            f"day2 must refresh from live; items_refreshed_from_live={s2['items_refreshed_from_live']} (C2)"
        )

    def test_no_diagnosis_source_plan_defaults_to_rules_no_update_on_same_hash(self):
        """plan with no diagnosis_source on same-hash rules row: defaults to rules, no update_on_same_hash. N4."""
        from agents.fi_recall_warranty.warranty import run_warranty_loop
        store: dict = {}
        write_kwargs: list = []

        def write(finding, standing, **kw):
            store[finding["finding_key"]] = finding
            write_kwargs.append(kw)
            return {"action": "put"}

        # Day 1: rules path (plan_fn=None) → rules finding with same inputs_hash
        run_warranty_loop(
            candidate_rows=[dict(self._ROW)], computed_at="2026-10-08T00:00:00Z",
            standing_fn=lambda vin: None, lookup_live=lambda k: None,
            write_finding=write, plan_fn=None, warranty_terms=self._TERMS,
        )
        assert store, "day 1 must write a rules finding"

        # Day 2: plan with no diagnosis_source key → default must be 'rules'
        write_kwargs.clear()
        run_warranty_loop(
            candidate_rows=[dict(self._ROW)], computed_at="2026-10-08T00:00:00Z",
            standing_fn=lambda vin: None,
            lookup_live=lambda k: store.get(k),
            write_finding=write,
            plan_fn=lambda *, row, evidence: {
                "narrative": "N.", "proceed": "file", "detail": "D", "confidence": "high"
            },
            warranty_terms=self._TERMS,
        )
        assert write_kwargs, "day 2 must write"
        assert not write_kwargs[-1].get("update_on_same_hash"), (
            f"no-diagnosis_source plan must NOT use update_on_same_hash; got {write_kwargs[-1]}. N4"
        )
        ev = {e["kind"]: e["value"] for e in store[list(store)[0]].get("evidence", [])}
        assert ev.get("warranty.diagnosis_source") == "rules", (
            f"no-diagnosis_source plan must default to 'rules'; "
            f"got {ev.get('warranty.diagnosis_source')!r}. N4"
        )


# ---------------------------------------------------------------------------
# W1: aborted run must carry model counters in its run record
# ---------------------------------------------------------------------------

class TestAbortedRunModelCounters:
    """W1: except branch must record model stats before emitting the run record."""

    def _abort_writer(self):
        """Raises RuntimeError on update_on_same_hash writes to abort after model call."""
        class _A(_FakeWriter):
            def write_finding(self, f, *, standing=None, refresh_on_noop=False,
                              update_on_same_hash=False):
                if update_on_same_hash:
                    raise RuntimeError("abort-for-w1-test")
                return super().write_finding(f, standing=standing,
                                             refresh_on_noop=refresh_on_noop)
        return _A()

    def test_recall_abort_carries_tokens(self, monkeypatch):
        from agents.tier2 import writer as tier2_writer
        from agents.tier2 import run_records as rr
        _patch_recall_seams(monkeypatch)
        w1 = _FakeWriter()
        runner.run_recall("staging", athena_client=MagicMock(), workgroup="wg",
                          results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                          vehicles_table="t", fleet_locations_table="t2",
                          groundings_table="t3", actions_table="t4",
                          avx_writer=w1, bedrock_client=None,
                          run_record_writer=lambda **kw: None, owner_resolver=lambda vin: None)
        assert w1.calls
        live = dict(w1.calls[-1])
        monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: live)
        records: list = []
        monkeypatch.setattr(rr, "get_runs_table_name", lambda: "t")
        monkeypatch.setattr(rr, "write_run_record", lambda **kw: records.append(kw) or True)
        client = _make_bedrock_client()
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_response("apply_guardrail", _guardrail_none())
            try:
                runner.run_recall(
                    "staging", athena_client=MagicMock(), workgroup="wg",
                    results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                    vehicles_table="t", fleet_locations_table="t2",
                    groundings_table="t3", actions_table="t4",
                    avx_writer=self._abort_writer(), bedrock_client=client,
                    guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                    run_record_writer=None, owner_resolver=lambda vin: None)
            except Exception:
                pass
        assert records, "run record must be emitted on abort (W1)"
        rec = records[0]
        assert rec.get("status") == "aborted", (
            f"abort run must have status='aborted'; got {rec.get('status')!r}"
        )
        assert rec.get("tokens_used", 0) == 150, (
            f"abort run must carry tokens_used=150, got {rec.get('tokens_used')!r} (W1)"
        )
        assert rec.get("model_calls", 0) == 1, (
            f"abort run must carry model_calls=1, got {rec.get('model_calls')!r} (W1)"
        )
        assert rec.get("cost_usd", 0.0) > 0.0, (
            f"abort run must carry cost_usd > 0.0, got {rec.get('cost_usd')!r} (W1)"
        )

    def test_warranty_abort_carries_tokens(self, monkeypatch):
        from agents.tier2 import writer as tier2_writer
        from agents.tier2 import run_records as rr
        _patch_warranty_seams(monkeypatch)
        w1 = _FakeWriter()
        runner.run_warranty("staging", athena_client=MagicMock(), workgroup="wg",
                            results_s3_path="s3://b/p/", avx_writer=w1, bedrock_client=None,
                            run_record_writer=lambda **kw: None, owner_resolver=lambda vin: None)
        assert w1.calls
        live = dict(w1.calls[-1])
        monkeypatch.setattr(tier2_writer, "_query_live_row", lambda **kw: live)
        records: list = []
        monkeypatch.setattr(rr, "get_runs_table_name", lambda: "t")
        monkeypatch.setattr(rr, "write_run_record", lambda **kw: records.append(kw) or True)
        client = _make_bedrock_client()
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_warranty_resp(_WARRANTY_NARRATIVE))
            stub.add_response("apply_guardrail", _guardrail_none())
            try:
                runner.run_warranty(
                    "staging", athena_client=MagicMock(), workgroup="wg",
                    results_s3_path="s3://b/p/", avx_writer=self._abort_writer(),
                    bedrock_client=client, guardrail_id=_GID, guardrail_version=_GVER,
                    model_id=_MODEL_ID, owner_resolver=lambda vin: None,
                    run_record_writer=None)
            except Exception:
                pass
        assert records, "run record must be emitted on abort (W1)"
        rec = records[0]
        assert rec.get("status") == "aborted", (
            f"abort run must have status='aborted'; got {rec.get('status')!r}"
        )
        assert rec.get("tokens_used", 0) == 120, (
            f"abort run must carry tokens_used=120, got {rec.get('tokens_used')!r} (W1)"
        )
        assert rec.get("model_calls", 0) == 1, (
            f"abort run must carry model_calls=1, got {rec.get('model_calls')!r} (W1)"
        )
        assert rec.get("cost_usd", 0.0) > 0.0, (
            f"abort run must carry cost_usd > 0.0, got {rec.get('cost_usd')!r} (W1)"
        )



# ---------------------------------------------------------------------------
# S1: guardrail API exception must NOT count as an intervention
# ---------------------------------------------------------------------------

class TestGuardrailExceptionNotIntervention:
    """S1: apply_guardrail raising must increment guardrail_errors, not guardrail_interventions.
    N3 reverts this by counting exceptions as interventions."""

    def test_recall_guardrail_exception_interventions_zero(self, monkeypatch):
        """plan_booking path: guardrail ClientError → guardrail_interventions == 0. N3."""
        _patch_recall_seams(monkeypatch)
        records: list = []
        client = _make_bedrock_client()
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_recall_resp(_MODEL_DETAIL))
            stub.add_client_error("apply_guardrail", "ValidationException", "guardrail down")
            runner.run_recall(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", ddb_client=MagicMock(),
                vehicles_table="t", fleet_locations_table="t2",
                groundings_table="t3", actions_table="t4",
                avx_writer=_FakeWriter(), bedrock_client=client,
                guardrail_id=_GID, guardrail_version=_GVER, model_id=_MODEL_ID,
                run_record_writer=lambda **kw: records.append(kw),
                owner_resolver=lambda vin: None,
            )
        assert records, "run record must be emitted"
        assert records[0].get("guardrail_interventions") == 0, (
            f"guardrail exception must NOT count as intervention; "
            f"got {records[0].get('guardrail_interventions')}. N3"
        )

    def test_warranty_guardrail_exception_interventions_zero(self, monkeypatch):
        """draft_claim path: guardrail ClientError → guardrail_interventions == 0. N3."""
        _patch_warranty_seams(monkeypatch)
        records: list = []
        client = _make_bedrock_client()
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_warranty_resp(_WARRANTY_NARRATIVE))
            stub.add_client_error("apply_guardrail", "ValidationException", "guardrail down")
            runner.run_warranty(
                "staging", athena_client=MagicMock(), workgroup="wg",
                results_s3_path="s3://b/p/", avx_writer=_FakeWriter(),
                bedrock_client=client, guardrail_id=_GID, guardrail_version=_GVER,
                model_id=_MODEL_ID, owner_resolver=lambda vin: None,
                run_record_writer=lambda **kw: records.append(kw),
            )
        assert records, "run record must be emitted"
        assert records[0].get("guardrail_interventions") == 0, (
            f"guardrail exception must NOT count as intervention; "
            f"got {records[0].get('guardrail_interventions')}. N3"
        )
