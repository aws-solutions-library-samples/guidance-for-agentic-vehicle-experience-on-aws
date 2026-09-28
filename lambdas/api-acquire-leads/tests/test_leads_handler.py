"""Test skeletons for lambdas/api-acquire-leads/handler.py

These tests FAIL (red) because handler.py does not exist yet.
They MUST collect cleanly — no ImportError, no AttributeError on collection.

Run:
    python3 -m pytest lambdas/api-acquire-leads/ -v

Contract coverage
-----------------
* 200/201 happy path — POST /acquire/leads creates a lead (feature-flagged OFF by default)
* GET /acquire/leads/{leadId} returns a lead record
* 400 on malformed input (missing required fields, missing path param)
* 403 when the path/body tenantId does not match the caller's Cognito claim
* 404 on unknown leadId (GET path)
* Feature-flag OFF → POST returns 404 (endpoint not available) not 500

Loading pattern: importlib.util.spec_from_file_location (no __init__.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Attempt to load the handler.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_handler_mod = None

if _HANDLER_PATH.exists():
    _spec = importlib.util.spec_from_file_location(
        "api_acquire_leads_handler", _HANDLER_PATH
    )
    _handler_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_handler_mod)


def _skip_if_no_handler():
    if _handler_mod is None:
        pytest.fail(
            "lambdas/api-acquire-leads/handler.py does not exist yet. "
            "These tests are expected to fail (red) until Group 2 implements it."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_event(
    body: dict | None = None,
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "POST",
        "pathParameters": None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
    }


def _get_event(
    lead_id: str = "LEAD-20260731-abc12345",
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"leadId": lead_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "queryStringParameters": None,
        "body": None,
    }


_VALID_POST_BODY = {
    "tenantId": "meridian-demo",
    "sessionId": "session-abc123",
    "contactInfo": {"email": "visitor@example.com"},
}

_LEAD_ITEM = {
    "leadId": "LEAD-20260731-abc12345",
    "tenantId": "meridian-demo",
    "sessionId": "session-abc123",
    "capturedAt": "2026-07-31T11:00:00+00:00",
    "contactInfo": {"email": "visitor@example.com"},
}


def _make_ddb_write_ok():
    table = MagicMock()
    table.put_item.return_value = {}
    table.get_item.return_value = {"Item": _LEAD_ITEM}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _make_ddb_not_found():
    table = MagicMock()
    table.get_item.return_value = {}  # No "Item" key
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# ---------------------------------------------------------------------------
# POST — feature-flag OFF (default)
# ---------------------------------------------------------------------------

class TestLeadsFeatureFlagOff:
    def test_post_returns_404_when_flag_off(self):
        """VSA_ACQUIRE_LEAD_CAPTURE_ENABLED defaults to OFF.
        POST must return a 4xx (not 500) to signal endpoint unavailable."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "false"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert resp["statusCode"] in (403, 404, 503), (
            f"Expected 4xx when lead capture is disabled, got {resp['statusCode']}"
        )


# ---------------------------------------------------------------------------
# POST — feature-flag ON
# ---------------------------------------------------------------------------

class TestLeadsPostEnabled:
    def test_post_returns_201_when_flag_on(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert resp["statusCode"] == 201

    def test_post_response_contains_lead_id(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            body = json.loads(
                _handler_mod.handler(_post_event(_VALID_POST_BODY), None)["body"]
            )
        assert "leadId" in body

    def test_post_missing_tenant_id_returns_400(self):
        _skip_if_no_handler()
        bad_body = {k: v for k, v in _VALID_POST_BODY.items() if k != "tenantId"}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(bad_body), None)
        assert resp["statusCode"] == 400

    def test_post_invalid_json_returns_400(self):
        _skip_if_no_handler()
        evt = _post_event()
        evt["body"] = "not-json"
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# POST 403 — tenant clamp
# ---------------------------------------------------------------------------

class TestLeadsTenantClamp:
    def test_body_tenant_mismatch_returns_403(self):
        """Body has tenantId=other-tenant but claim says meridian-demo → 403."""
        _skip_if_no_handler()
        mismatched = {**_VALID_POST_BODY, "tenantId": "other-tenant"}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(
                _post_event(mismatched, claim_tenant="meridian-demo"), None
            )
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# GET /acquire/leads/{leadId}
# ---------------------------------------------------------------------------

class TestLeadsGet:
    def test_get_returns_200(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            resp = _handler_mod.handler(_get_event(), None)
        assert resp["statusCode"] == 200

    def test_get_returns_lead_id(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            body = json.loads(_handler_mod.handler(_get_event(), None)["body"])
        assert "leadId" in body

    def test_get_missing_lead_id_param_returns_400(self):
        _skip_if_no_handler()
        bad_event = _get_event()
        bad_event["pathParameters"] = {}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400

    def test_get_unknown_lead_returns_404(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_not_found()):
            resp = _handler_mod.handler(_get_event(lead_id="LEAD-unknown"), None)
        assert resp["statusCode"] == 404

    def test_get_lead_tenant_mismatch_returns_403(self):
        """Lead belongs to other-tenant; claim is meridian-demo → 403."""
        _skip_if_no_handler()
        other_item = {**_LEAD_ITEM, "tenantId": "other-tenant"}
        table = MagicMock()
        table.get_item.return_value = {"Item": other_item}
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(_handler_mod, "_get_ddb", return_value=resource):
            resp = _handler_mod.handler(
                _get_event(claim_tenant="meridian-demo"), None
            )
        assert resp["statusCode"] == 403



# ---------------------------------------------------------------------------
# Idempotency — issues/2026-09-20-acquire-lead-id-not-idempotent/
#
# leadId used to be seeded on wall-clock seconds, giving a 1-second idempotency
# window while RetryPolicy.backoff_base_ms is 1000 — so the first retry landed in
# the next second every time and minted a duplicate lead. A lead is a commercial
# signal to a dealer, so a duplicate has a real cost.
#
# MUTATION-VERIFIED: reintroducing captured_at into the _make_lead_id seed fails
# test_lead_id_is_independent_of_wall_clock and
# test_same_request_twice_yields_same_lead_id; removing ConditionExpression fails
# test_put_item_uses_conditional_expression; returning 500 on the conditional
# failure fails test_conditional_check_failure_is_success_not_error.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402


def _digest_for(body, tenant="meridian-demo"):
    """The digest the handler will compute for `body` under `tenant`.

    Deliberately assembles the seed from its parts rather than calling a single
    helper, so it mirrors the handler's inputs without hiding them.
    """
    return _handler_mod._seed_digest(
        tenant, body.get("sessionId") or "", _handler_mod._idempotency_basis(body))


def _conditional_failure_ddb():
    """A table whose put_item rejects the write because the leadId already exists."""
    table = MagicMock()
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )
    table.get_item.return_value = {"Item": _LEAD_ITEM}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


class TestLeadIdIdempotency:
    def test_lead_id_is_independent_of_wall_clock(self):
        """The property the defect violated: same inputs, different second, same id.

        Compares within one UTC day so the human-readable date prefix is constant;
        the prefix is deliberately still time-derived (see _make_lead_id docstring).
        """
        _skip_if_no_handler()
        t1 = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 20, 16, 0, 1, tzinfo=timezone.utc)
        basis = _handler_mod._idempotency_basis(_VALID_POST_BODY)
        a = _handler_mod._make_lead_id("t", "s", t1, basis)
        b = _handler_mod._make_lead_id("t", "s", t2, basis)
        assert a == b, (
            f"leadId changed across a 1-second gap ({a} != {b}). The wall clock is "
            "back in the hash seed, which is the original defect: RetryPolicy's "
            "1000ms base backoff guarantees the retry lands in the next second."
        )

    def test_different_basis_yields_different_lead_id(self):
        """Anti-vacuity for the test above — proves the id is not simply constant."""
        _skip_if_no_handler()
        t = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
        a = _handler_mod._make_lead_id(
            "t", "s", t, _handler_mod._idempotency_basis({"modelId": "model-a"}))
        b = _handler_mod._make_lead_id(
            "t", "s", t, _handler_mod._idempotency_basis({"modelId": "model-b"}))
        assert a != b, (
            "Two different payloads produced the same leadId — the basis is being "
            "ignored, so the idempotency test above proves nothing."
        )

    def test_same_request_twice_yields_same_lead_id(self):
        """End-to-end: the retry shape, with the clock advanced between attempts.

        The two calls MUST be made to observe different wall-clock seconds. An
        earlier version of this test called the handler twice in rapid succession,
        so both landed in the same second and it passed even with the wall clock
        back in the hash seed — it proved nothing, and the mutation run caught it.
        RetryPolicy.backoff_base_ms is 1000, so a real retry is ~1s later; that is
        what is simulated here.
        """
        _skip_if_no_handler()
        t1 = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 20, 16, 0, 1, tzinfo=timezone.utc)
        clock = MagicMock(wraps=datetime)
        clock.now.side_effect = [t1, t2]
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.object(_handler_mod, "datetime", clock), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            first = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
            second = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert clock.now.call_count == 2, (
            "The handler did not read the clock once per request, so this test is "
            "not exercising the retry shape it claims to."
        )
        id_one = json.loads(first["body"])["leadId"]
        id_two = json.loads(second["body"])["leadId"]
        assert id_one == id_two, (
            f"Identical requests one second apart produced two leadIds ({id_one}, "
            f"{id_two}) — a retried dispatch would create a duplicate lead in a "
            "dealer's queue."
        )

    def test_put_item_uses_conditional_expression(self):
        """Without the condition, an id collision silently REPLACES the stored lead."""
        _skip_if_no_handler()
        ddb = _make_ddb_write_ok()
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        kwargs = ddb.Table.return_value.put_item.call_args.kwargs
        assert "ConditionExpression" in kwargs, (
            "put_item was called without a ConditionExpression, so a duplicate id "
            f"would overwrite the existing lead rather than be rejected. Got: {kwargs}"
        )
        assert "attribute_not_exists" in str(kwargs["ConditionExpression"])

    def test_conditional_check_failure_is_success_not_error(self):
        """A CONFIRMED replay is a SUCCESS. Reporting it as an error would make
        RetailExecutor treat a correct outcome as a failure and escalate.

        Cycle 2 Warning 2: this test used to reach the replay path with a stored item
        carrying no digest, so when the digest comparison landed it silently migrated
        onto the unverifiable branch — the suite's canonical "a replay succeeds" test
        was no longer exercising a verified replay at all. It now stores a matching
        digest, so it asserts what its name claims.
        """
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.return_value = {
            "Item": dict(_LEAD_ITEM, idempotencyDigest=_digest_for(_VALID_POST_BODY))
        }
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert 200 <= resp["statusCode"] < 300, (
            f"Confirmed replay returned {resp['statusCode']}; RetailExecutor treats "
            "only 2xx as success, so this would be recorded as a failed action."
        )
        body = json.loads(resp["body"])
        assert body.get("idempotentReplay") is True
        assert "leadId" in body, "A 2xx without a leadId is unreconcilable for the caller."

    def test_unverifiable_replay_fails_closed(self):
        """If the read-back fails we CANNOT prove this is a replay — so we must not
        claim one.

        This inverts an earlier test named `test_replay_read_back_failure_still_
        succeeds`, whose docstring opened "The read-back is cosmetic". That was true
        when the read only sourced a capturedAt for the response; the digest
        comparison made the read the sole evidence for a correctness gate, and
        Cycle 2 Warning 2 caught that the old assertion had become a fail-open on
        unverifiable state. A false "your lead was captured" is worse than an
        operator re-submitting one.
        """
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.side_effect = RuntimeError("ddb read down")
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert resp["statusCode"] >= 500, (
            f"unverifiable replay returned {resp['statusCode']} — a 2xx here claims a "
            "replay that was never confirmed."
        )
        assert json.loads(resp["body"]).get("idempotentReplay") is not True

    def test_explicit_idempotency_key_overrides_payload(self):
        """An explicit key lets a caller collapse two differing payloads on purpose."""
        _skip_if_no_handler()
        t = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
        a = _handler_mod._make_lead_id("t", "s", t, _handler_mod._idempotency_basis(
            {"modelId": "model-a", "idempotencyKey": "same-intent"}))
        b = _handler_mod._make_lead_id("t", "s", t, _handler_mod._idempotency_basis(
            {"modelId": "model-b", "idempotencyKey": "same-intent"}))
        assert a == b



# ---------------------------------------------------------------------------
# savedConfig — F2 of spec 2026-09-20-avx-acquire-configurator-target
#
# The Strands tool (agents/supervisor/tools/lead_capture.py) has always written a
# nested savedConfig {preferredCategory, preferredModelId}; this endpoint accepted
# only a flat modelId and no savedConfig at all. Two writers, one table, two shapes
# for one concept — a reader crossing both cannot reconcile them.
#
# MUTATION-VERIFIED: deleting the `if body.get("savedConfig")` block fails
# test_saved_config_is_persisted.
# ---------------------------------------------------------------------------

class TestSavedConfig:
    _SAVED_CONFIG = {"preferredCategory": "suv", "preferredModelId": "model-x"}

    def _post_with(self, ddb, body):
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            return _handler_mod.handler(_post_event(body), None)

    def test_saved_config_is_persisted(self):
        _skip_if_no_handler()
        ddb = _make_ddb_write_ok()
        body = dict(_VALID_POST_BODY, savedConfig=self._SAVED_CONFIG)
        resp = self._post_with(ddb, body)
        assert 200 <= resp["statusCode"] < 300
        item = ddb.Table.return_value.put_item.call_args.kwargs["Item"]
        assert item.get("savedConfig") == self._SAVED_CONFIG, (
            f"savedConfig was not persisted; item keys were {sorted(item)}. The "
            "endpoint would silently drop the configuration the owner approved."
        )

    def test_saved_config_is_not_synthesised_from_model_id(self):
        """A modelId is not a configuration. Inventing one would fabricate a value
        the caller never sent — the failure R4 established the precedent against."""
        _skip_if_no_handler()
        ddb = _make_ddb_write_ok()
        self._post_with(ddb, dict(_VALID_POST_BODY, modelId="model-x"))
        item = ddb.Table.return_value.put_item.call_args.kwargs["Item"]
        assert "savedConfig" not in item, (
            f"savedConfig was synthesised from modelId: {item.get('savedConfig')!r}"
        )
        assert item.get("modelId") == "model-x", "flat modelId must still be honoured"

    def test_saved_config_changes_the_lead_id(self):
        """savedConfig is part of the idempotency basis, so two different approved
        configurations in one session are two different leads — not one collapsed."""
        _skip_if_no_handler()
        t = datetime(2026, 9, 20, 16, 0, 0, tzinfo=timezone.utc)
        a = _handler_mod._make_lead_id("t", "s", t, _handler_mod._idempotency_basis(
            {"savedConfig": {"preferredModelId": "model-a"}}))
        b = _handler_mod._make_lead_id("t", "s", t, _handler_mod._idempotency_basis(
            {"savedConfig": {"preferredModelId": "model-b"}}))
        assert a != b, (
            "Two different savedConfigs mapped to one leadId — the second approval "
            "would be swallowed as an idempotent replay of the first."
        )



# ---------------------------------------------------------------------------
# Review Cycle 1 fixes — W1 (read-back), W2 (collision vs replay), W4 (shape)
#
# MUTATION-VERIFIED, one per fix:
#   remove savedConfig from _CLIENT_FIELDS        -> test_get_returns_saved_config
#   drop the stored-digest comparison             -> test_collision_is_not_reported_as_replay
#   drop the isinstance(savedConfig, dict) check  -> test_non_object_saved_config_rejected
# ---------------------------------------------------------------------------

class TestReviewCycle1Fixes:
    _SAVED_CONFIG = {"preferredCategory": "suv", "preferredModelId": "model-x"}

    # ── W1: savedConfig must survive the read-back ────────────────────────
    def test_get_returns_saved_config(self):
        """POST accepted it and GET stripped it, so following target_ref=leadId
        through this API lost the configuration — the same outcome
        MissingSavedConfig guards against, one hop downstream."""
        _skip_if_no_handler()
        stored = dict(_LEAD_ITEM, savedConfig=self._SAVED_CONFIG)
        ddb = _make_ddb_write_ok()
        ddb.Table.return_value.get_item.return_value = {"Item": stored}
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb):
            resp = _handler_mod.handler(_get_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body.get("savedConfig") == self._SAVED_CONFIG, (
            f"GET stripped savedConfig; returned keys {sorted(body)}"
        )

    def test_internal_fields_are_never_client_visible(self):
        """The digest must not be readable — it would let a caller probe which basis
        produced an existing lead. Enforced by the allowlist; asserted here so a
        later edit cannot quietly move a field across."""
        _skip_if_no_handler()
        assert not (_handler_mod._CLIENT_FIELDS & _handler_mod._INTERNAL_FIELDS), (
            "an internal field appears in the client allowlist: "
            f"{_handler_mod._CLIENT_FIELDS & _handler_mod._INTERNAL_FIELDS}"
        )
        out = _handler_mod._public_lead(dict(_LEAD_ITEM, idempotencyDigest="SECRET"))
        assert "idempotencyDigest" not in out

    # ── W2: a collision is not a replay ──────────────────────────────────
    def _post(self, ddb, body=None):
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            return _handler_mod.handler(_post_event(body or _VALID_POST_BODY), None)

    def test_true_replay_is_still_a_success(self):
        """Positive control: matching digest means the same request, retried."""
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.return_value = {
            "Item": dict(_LEAD_ITEM, idempotencyDigest=_digest_for(_VALID_POST_BODY))
        }
        resp = self._post(ddb)
        assert 200 <= resp["statusCode"] < 300
        assert json.loads(resp["body"])["idempotentReplay"] is True

    def test_collision_is_not_reported_as_replay(self):
        """A DIFFERENT request colliding on the truncated 32-bit leadId suffix must
        NOT be reported as a successful replay: this caller's lead was never stored,
        and the leadId they would receive belongs to another lead."""
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.return_value = {
            "Item": dict(_LEAD_ITEM, idempotencyDigest="a-different-requests-digest")
        }
        resp = self._post(ddb)
        assert resp["statusCode"] >= 500, (
            f"collision returned {resp['statusCode']}; a 2xx would hand the caller a "
            "target_ref for a lead that is not theirs while silently dropping theirs."
        )
        body = json.loads(resp["body"])
        assert body.get("idempotentReplay") is not True, (
            "a collision must never claim idempotentReplay"
        )

    def test_collision_returns_5xx_not_4xx(self):
        """5xx is deliberate: a 4xx outside {401,403,404,405,415} classifies as
        `refused` in agents/executors/base.py, which would tell the owner the DEALER
        declined — a false statement about a third party for our own id collision.
        5xx is transient, so the executor exhausts and escalates to an operator."""
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.return_value = {
            "Item": dict(_LEAD_ITEM, idempotencyDigest="mismatch")
        }
        code = self._post(ddb)["statusCode"]
        assert not (400 <= code < 500), (
            f"collision returned {code}; a 4xx would be narrated to the owner as a "
            "dealer refusal rather than escalated as our own fault."
        )

    def test_digest_is_persisted(self):
        _skip_if_no_handler()
        ddb = _make_ddb_write_ok()
        self._post(ddb)
        item = ddb.Table.return_value.put_item.call_args.kwargs["Item"]
        expected = _digest_for(_VALID_POST_BODY)
        assert item.get("idempotencyDigest") == expected, (
            "without a stored digest a collision is indistinguishable from a replay"
        )

    # ── W4: savedConfig shape ────────────────────────────────────────────
    @pytest.mark.parametrize("bad", ["model-x", ["model-x"], 42, True])
    def test_non_object_saved_config_rejected(self, bad):
        """This endpoint is the field's only trust boundary — RetailExecutor forwards
        it opaquely by design and must not inspect it."""
        _skip_if_no_handler()
        resp = self._post(_make_ddb_write_ok(), dict(_VALID_POST_BODY, savedConfig=bad))
        assert resp["statusCode"] == 400, (
            f"savedConfig={bad!r} ({type(bad).__name__}) was accepted; it would "
            "persist a shape no reader expects."
        )

    def test_object_saved_config_accepted(self):
        """Anti-vacuity for the test above — proves the check is not rejecting all."""
        _skip_if_no_handler()
        resp = self._post(
            _make_ddb_write_ok(), dict(_VALID_POST_BODY, savedConfig=self._SAVED_CONFIG))
        assert 200 <= resp["statusCode"] < 300



# ---------------------------------------------------------------------------
# Review Cycle 2 fixes — W1 (digest must discriminate), W2 (read is load-bearing)
#
# Cycle 2 W1 is the sharpest finding in this spec, and it landed on my own fix for
# the same class of error: replacing _seed_digest's body with sha256(b"") — ignoring
# its inputs entirely — left all 32 tests GREEN. Every digest test derived its
# expected value by calling the function under test, so a constant digest compared
# equal on every collision and silently reopened W2 in full. The leadId had
# test_different_basis_yields_different_lead_id; the digest had no equivalent.
#
# MUTATION-VERIFIED:
#   _seed_digest -> a constant          -> test_seed_digest_discriminates_on_every_input
#   ConsistentRead=True removed         -> test_replay_read_is_strongly_consistent
# ---------------------------------------------------------------------------

class TestReviewCycle2Fixes:
    def test_seed_digest_discriminates_on_every_input(self):
        """Each of tenant, session and basis must change the digest.

        Asserts against literals computed here from DIFFERING inputs rather than by
        re-calling the function on the same input — a self-referential expectation is
        exactly what let a constant digest pass.
        """
        _skip_if_no_handler()
        d = _handler_mod._seed_digest
        base = d("tenant-a", "sess-1", "basis-1")
        assert d("tenant-b", "sess-1", "basis-1") != base, "tenant does not affect the digest"
        assert d("tenant-a", "sess-2", "basis-1") != base, "session does not affect the digest"
        assert d("tenant-a", "sess-1", "basis-2") != base, "basis does not affect the digest"
        assert d("tenant-a", "sess-1", "basis-1") == base, "digest is not deterministic"
        assert len(base) == 64, (
            f"expected an untruncated sha256 hex digest, got {len(base)} chars — "
            "truncation reintroduces the collision class the digest exists to resolve"
        )

    def test_digest_covers_the_same_inputs_as_the_lead_id(self):
        """Regression for the Cycle 2 Suggestion: digesting only `basis` left a
        collision class invisible — two requests from different tenants or sessions
        whose basis matched would collide in the truncated leadId and then compare
        EQUAL on the digest, which is the false replay all of this exists to stop."""
        _skip_if_no_handler()
        same_basis = _handler_mod._idempotency_basis({"modelId": "model-x"})
        a = _handler_mod._seed_digest("tenant-a", "sess-1", same_basis)
        b = _handler_mod._seed_digest("tenant-b", "sess-1", same_basis)
        assert a != b, (
            "two tenants sharing a basis produced one digest — a cross-tenant leadId "
            "collision would be confirmed as a replay"
        )

    def test_replay_read_is_strongly_consistent(self):
        """The read was cosmetic when it only sourced a capturedAt; the digest
        comparison made it the sole evidence for a correctness gate. A stale read can
        now confirm a replay that never happened."""
        _skip_if_no_handler()
        ddb = _conditional_failure_ddb()
        ddb.Table.return_value.get_item.return_value = {
            "Item": dict(_LEAD_ITEM, idempotencyDigest=_digest_for(_VALID_POST_BODY))
        }
        with patch.object(_handler_mod, "_get_ddb", return_value=ddb), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        kwargs = ddb.Table.return_value.get_item.call_args.kwargs
        assert kwargs.get("ConsistentRead") is True, (
            f"replay read-back was not strongly consistent (kwargs={kwargs}); an "
            "eventually-consistent read is not evidence for a correctness gate"
        )



# ---------------------------------------------------------------------------
# Cycle 3 Suggestion with teeth — seed injectivity
#
# Removing the "|" separators from _seed_digest left all 35 tests green. There is no
# live defect: the separators are present, and `sessionId` (caller-supplied and
# unvalidated) cannot forge a field split because _idempotency_basis prefix-tags every
# basis with "key:" or "payload:", so the seed's last field is always identifiable and
# the parse is unambiguous.
#
# That argument is the reason the code is safe and it was written down nowhere. A
# refactor touching EITHER the separator or those prefixes would break injectivity
# silently, which is why the invariant is asserted rather than trusted.
# ---------------------------------------------------------------------------

class TestSeedInjectivity:
    def test_basis_is_always_prefix_tagged(self):
        """The load-bearing invariant: every basis starts with a known tag, so a
        caller-supplied sessionId cannot impersonate the basis field."""
        _skip_if_no_handler()
        cases = [
            {},
            {"modelId": "m"},
            {"idempotencyKey": "k"},
            {"savedConfig": {"a": 1}},
            # Adversarial: a sessionId shaped like a field boundary.
            {"sessionId": "a|payload:{}", "modelId": "m"},
        ]
        for body in cases:
            basis = _handler_mod._idempotency_basis(body)
            assert basis.startswith(("key:", "payload:")), (
                f"basis {basis!r} for body {body!r} is not prefix-tagged; the seed "
                "becomes ambiguous and two different requests could digest equal"
            )

    def test_seed_fields_are_separated(self):
        """A shifted field boundary must change the digest."""
        _skip_if_no_handler()
        d = _handler_mod._seed_digest
        assert d("tenant", "ab", "payload:x") != d("tenanta", "b", "payload:x"), (
            "moving a character across the tenant/session boundary did not change the "
            "digest — the fields are being concatenated without separation, so two "
            "different requests can share one digest and a collision would be "
            "confirmed as a replay"
        )
