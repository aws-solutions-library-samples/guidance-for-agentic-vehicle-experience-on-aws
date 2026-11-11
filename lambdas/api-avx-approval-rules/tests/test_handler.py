"""Red-test skeletons: approval-rules API handler — spec D9.

Expected interface (Group 2 creates lambdas/api-avx-approval-rules/handler.py):

    Lambda handler: handler(event: dict, context) -> dict
    Routes:
        GET  /approval-rules/fleet/{fleet_id}
             → 200 {"rules": [...]}
        PUT  /approval-rules/fleet/{fleet_id}/{rule_id}
             body: {"action_kind": str, "limit": <number|None>,
                    "enabled": bool (client can enable/disable after creation)}
             On creation (no existing row): 400 if body sets enabled=True
               (rules_are_created_disabled); stores enabled=False otherwise.
             On update (existing row): honours the enabled field.
             → 200 {"rule_id": ..., "enabled": <stored value>, ...}
        DELETE /approval-rules/fleet/{fleet_id}/{rule_id}
             → 204

    Fleet check (contract § 6):
        fleet-operator group + trusted custom:fleetIds containing fleet_id
        OR platform-admin group + trusted custom:fleetIds containing fleet_id.
        There is NO platform-admin bypass of the fleet membership check.

    Guarded kinds (D9: refuse with 400):
        "exempt_recall", "ground_vehicle", "submit_warranty_claim", "complete_recall"
        (literal names; never imported from policy.py — D5 principle)

    New rules are stored enabled=False (create with enabled=True → 400). (D9)
    Updates can set enabled=True on an existing rule.

Tests are RED until lambdas/api-avx-approval-rules/handler.py exists
(ModuleNotFoundError on import inside each test function).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# xfail mark removed — T2.7 implementation is present (lambdas/api-avx-approval-rules/handler.py)

# ── sys.path setup ────────────────────────────────────────────────────────────
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Event / claims helpers
# ---------------------------------------------------------------------------

# W8: custom:fleetIds is sent as a CSV string, never a JSON array.
def _claims(sub="op-sub-mrdn-01", groups=None, fleet_ids=None):
    c: dict = {"sub": sub}
    c["cognito:groups"] = json.dumps(groups) if groups else "fleet-operator"
    if fleet_ids is not None:
        c["custom:fleetIds"] = (
            fleet_ids if isinstance(fleet_ids, str) else ",".join(fleet_ids)
        )
    return c


def _event(method, resource, path_params=None, claims=None, body=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
        "body": json.dumps(body) if body else None,
    }


def _decode_body(resp: dict) -> dict:
    return json.loads(resp["body"])


@pytest.fixture(autouse=True)
def _trusted_fleet_claim(monkeypatch):
    """The deployed staging value (security review Cycle 1 Critical 1). Without it a
    correct handler, which reads `custom:fleetIds` only through the trust gate in
    `_lib/avx_scope.py`, would refuse every positive test below, which would push an
    implementer toward reading the claim ungated. `TestTrustGate` turns it off."""
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")


@pytest.mark.parametrize("trust", [None, "false", "1", "yes"],
                         ids=["unset", "false", "one", "yes"])
@pytest.mark.parametrize("method,resource,path_params,body", [
    ("GET", "/approval-rules/fleet/{fleet_id}", {"fleet_id": "fleet-mrdn-01"}, None),
    ("PUT", "/approval-rules/fleet/{fleet_id}/{rule_id}",
     {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"}, {"action_kind": "acknowledge_only"}),
    # Security review Cycle 2 S3: the trust gate refuses before body validation, so a
    # guarded kind gets 403 here, not the 400 an in-scope caller would get.
    ("PUT", "/approval-rules/fleet/{fleet_id}/{rule_id}",
     {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-002"}, {"action_kind": "ground_vehicle"}),
    ("DELETE", "/approval-rules/fleet/{fleet_id}/{rule_id}",
     {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"}, None),
], ids=["get", "put", "put-guarded-kind", "delete"])
class TestTrustGate:
    """With the trust flag off, `custom:fleetIds` gives no fleet standing: a
    fleet-operator naming the fleet is refused and nothing is read or written."""

    def test_untrusted_fleet_claim_is_refused(self, monkeypatch, trust, method, resource,
                                              path_params, body):
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        if trust is None:
            monkeypatch.delenv("AVX_FLEET_IDS_CLAIM_TRUSTED", raising=False)
        else:
            monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", trust)
        table = MagicMock()
        table.query.return_value = {"Items": []}
        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(method, resource, path_params=path_params,
                       claims=_claims(fleet_ids="fleet-mrdn-01"), body=body),
                None,
            )
        assert resp["statusCode"] == 403
        for op in ("put_item", "update_item", "delete_item", "query", "get_item", "scan"):
            getattr(table, op).assert_not_called()


# ---------------------------------------------------------------------------
# Fleet check
# ---------------------------------------------------------------------------

class TestFleetCheck:
    """A fleet-B operator cannot read or write fleet-A rules."""

    def test_get_rules_denied_for_wrong_fleet(self):
        """Fleet-B operator cannot read fleet-A rules."""
        import handler as h  # noqa: PLC0415 — RED: ImportError until handler.py exists
        resp = h.handler(
            _event(
                "GET",
                "/approval-rules/fleet/{fleet_id}",
                path_params={"fleet_id": "fleet-mrdn-01"},
                claims=_claims(fleet_ids="fleet-mrdn-WRONG"),
            ),
            None,
        )
        # RED: handler.py absent → ImportError before this point.
        assert resp["statusCode"] == 403, "Fleet-B operator must not read fleet-A rules"

    def test_put_rule_denied_for_wrong_fleet(self):
        """Fleet-B operator cannot create a rule for fleet-A."""
        import handler as h  # noqa: PLC0415
        resp = h.handler(
            _event(
                "PUT",
                "/approval-rules/fleet/{fleet_id}/{rule_id}",
                path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                claims=_claims(fleet_ids="fleet-mrdn-WRONG"),
                body={"action_kind": "acknowledge_only"},
            ),
            None,
        )
        # RED.
        assert resp["statusCode"] == 403

    def test_platform_admin_still_needs_fleet_membership(self):
        """W3: platform-admin WITHOUT fleet membership cannot read/write fleet rules.

        Contract § 6: no platform-admin bypass of the membership check.
        """
        import handler as h  # noqa: PLC0415
        resp = h.handler(
            _event(
                "GET",
                "/approval-rules/fleet/{fleet_id}",
                path_params={"fleet_id": "fleet-mrdn-01"},
                claims=_claims(sub="admin", groups=["platform-admin"]),
                # no fleet_ids
            ),
            None,
        )
        # RED.
        assert resp["statusCode"] == 403

    def test_platform_admin_with_fleet_membership_passes(self):
        """W3: platform-admin WITH fleet membership in custom:fleetIds can read fleet rules."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "GET",
                    "/approval-rules/fleet/{fleet_id}",
                    path_params={"fleet_id": "fleet-mrdn-01"},
                    # W8: CSV string; platform-admin + fleet membership → 200
                    claims=_claims(
                        sub="admin",
                        groups=["platform-admin"],
                        fleet_ids="fleet-mrdn-01",
                    ),
                ),
                None,
            )

        # RED: handler.py absent.
        assert resp["statusCode"] == 200, (
            "platform-admin with fleet membership must be able to read fleet rules "
            "(contract § 6: no bypass, but still admitted when membership is present)"
        )

    def test_multi_fleet_operator_can_access_owned_fleet(self):
        """W8: an operator with two fleets in a CSV custom:fleetIds can access the first fleet."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "GET",
                    "/approval-rules/fleet/{fleet_id}",
                    path_params={"fleet_id": "fleet-mrdn-01"},
                    # W8: two fleets as a single CSV string
                    claims=_claims(fleet_ids="fleet-mrdn-01,fleet-mrdn-02"),
                ),
                None,
            )

        # RED: handler.py absent.
        assert resp["statusCode"] == 200, (
            "An operator with 'fleet-mrdn-01,fleet-mrdn-02' in CSV custom:fleetIds "
            "must be able to access fleet-mrdn-01 rules"
        )


# ---------------------------------------------------------------------------
# New rule defaults — contract § 6
# ---------------------------------------------------------------------------

class TestNewRuleDefaults:
    """Create behaviour: enabled=True is rejected; omitting enabled stores False."""

    def test_create_with_enabled_true_gets_400(self):
        """W3: PUT with enabled=True on creation → 400 rules_are_created_disabled (contract § 6).

        Create is always disabled; enabling is always a second, deliberate PUT.
        """
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no existing row → creation path

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"action_kind": "acknowledge_only", "enabled": True},
                ),
                None,
            )

        # RED: handler.py absent.
        assert resp["statusCode"] == 400, (
            "A create with enabled=True must return 400 rules_are_created_disabled (§ 6)"
        )
        body = _decode_body(resp)
        code = body.get("code") or body.get("error") or ""
        assert "rules_are_created_disabled" in code, (
            f"Error code must be 'rules_are_created_disabled', got {code!r}"
        )

    def test_create_without_enabled_stores_false(self):
        """W3: PUT without enabled on creation → 200 with enabled=False stored (contract § 6)."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        mock_table = MagicMock()
        mock_table.get_item.return_value = {}  # no existing row → creation path
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"action_kind": "acknowledge_only"},  # no enabled field
                ),
                None,
            )

        # RED: handler.py absent.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("enabled") is False, (
            f"A create without enabled must store and return enabled=False (§ 6), "
            f"got enabled={body.get('enabled')!r}"
        )

    def test_existing_rule_can_be_enabled(self):
        """W3: an existing rule can be enabled via a PUT with enabled=True (contract § 6)."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        existing_rule = {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": existing_rule}
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        # RED: handler.py absent.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body.get("enabled") is True


# ---------------------------------------------------------------------------
# Guarded kinds: refused 400 (D9 + D5)
# ---------------------------------------------------------------------------

class TestGuardedKindsRefused:
    """Creating a rule for a guarded kind is refused with 400.

    Literal kind names — never imported from policy.py (D5 principle).
    """

    @pytest.mark.parametrize("guarded_kind", [
        "exempt_recall",          # G1
        "ground_vehicle",         # G2
        "submit_warranty_claim",  # G3
        "complete_recall",        # G4
    ])
    def test_guarded_kind_returns_400(self, guarded_kind: str):
        """A rule for a guarded kind returns 400 (these kinds need the operator, D9)."""
        import handler as h  # noqa: PLC0415  RED

        resp = h.handler(
            _event(
                "PUT",
                "/approval-rules/fleet/{fleet_id}/{rule_id}",
                path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-guarded"},
                claims=_claims(fleet_ids="fleet-mrdn-01"),
                body={"action_kind": guarded_kind},
            ),
            None,
        )

        # RED: handler.py absent → ImportError before this point.
        assert resp["statusCode"] == 400, (
            f"Creating a rule for guarded kind {guarded_kind!r} must return 400, "
            f"got {resp['statusCode']}"
        )


# ---------------------------------------------------------------------------
# GET: list rules
# ---------------------------------------------------------------------------

class TestGetRules:
    """GET /approval-rules/fleet/{fleet_id} returns the fleet's rules."""

    def test_get_returns_rules_list(self):
        """GET returns a JSON body with a 'rules' key."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
            Table=lambda _: mock_table
        )):
            resp = h.handler(
                _event(
                    "GET",
                    "/approval-rules/fleet/{fleet_id}",
                    path_params={"fleet_id": "fleet-mrdn-01"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                ),
                None,
            )

        # RED.
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert "rules" in body, f"Response must carry a 'rules' list, got keys: {list(body)}"


# ---------------------------------------------------------------------------
# Non-auto-approvable kinds refused
# ---------------------------------------------------------------------------

class TestNonAutoApprovableRefused:
    """A rule for a kind that is not auto_approvable is refused with 400."""

    def test_book_recall_service_not_auto_approvable(self):
        """book_recall_service requires a caller token (DMS executor); can't auto-approve."""
        import handler as h  # noqa: PLC0415  RED

        resp = h.handler(
            _event(
                "PUT",
                "/approval-rules/fleet/{fleet_id}/{rule_id}",
                path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-brs"},
                claims=_claims(fleet_ids="fleet-mrdn-01"),
                body={"action_kind": "book_recall_service"},
            ),
            None,
        )

        # RED: handler.py absent.
        assert resp["statusCode"] == 400, (
            "book_recall_service is not auto_approvable; creating a rule must return 400"
        )



# ---------------------------------------------------------------------------
# Mutation-guard: guarded_kind error code (added for mutation (c) coverage)
# ---------------------------------------------------------------------------

class TestGuardedKindErrorCode:
    """The guarded-kind path returns 'guarded_kind' — not 'kind_not_auto_approvable'.

    This distinguishes the explicit D5 guardrail from the auto_approvable check.
    Removing a kind from _GUARDED_KINDS changes the code to 'kind_not_auto_approvable'
    (since all guarded kinds are also non-auto_approvable), which fails this test.
    """

    @pytest.mark.parametrize("guarded_kind", [
        "exempt_recall",
        "ground_vehicle",
        "submit_warranty_claim",
        "complete_recall",
    ])
    def test_guarded_kind_error_code_is_guarded_kind(self, guarded_kind: str):
        """The error code is 'guarded_kind', not 'kind_not_auto_approvable'."""
        import handler as h  # noqa: PLC0415

        resp = h.handler(
            _event(
                "PUT",
                "/approval-rules/fleet/{fleet_id}/{rule_id}",
                path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-guarded"},
                claims=_claims(fleet_ids="fleet-mrdn-01"),
                body={"action_kind": guarded_kind},
            ),
            None,
        )
        assert resp["statusCode"] == 400
        body = _decode_body(resp)
        code = body.get("code", "")
        assert code == "guarded_kind", (
            f"Expected code='guarded_kind' for {guarded_kind!r}; got {code!r}. "
            "The D5 guardrail path must fire before the auto_approvable check."
        )



# ---------------------------------------------------------------------------
# Group 2 architect review of T2.7: body shape and the create race.
# ---------------------------------------------------------------------------

class TestRuleBodyShape:
    def _put(self, body, existing=None):
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415

        table = MagicMock()
        table.get_item.return_value = {"Item": existing} if existing else {}
        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event("PUT", "/approval-rules/fleet/{fleet_id}/{rule_id}",
                       path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                       claims=_claims(fleet_ids="fleet-mrdn-01"), body=body),
                None,
            )
        return resp, table

    def test_create_without_action_kind_is_400(self):
        resp, table = self._put({"enabled": False})
        assert resp["statusCode"] == 400
        table.put_item.assert_not_called()

    @pytest.mark.parametrize("enabled", ["true", "false", 1, 0, "yes"])
    def test_enabled_must_be_a_boolean(self, enabled):
        """bool("false") is True: a string must never enable a standing approval."""
        existing = {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001",
                    "action_kind": "acknowledge_only", "enabled": False, "created_by": "op"}
        resp, table = self._put({"enabled": enabled}, existing=existing)
        assert resp["statusCode"] == 400
        table.put_item.assert_not_called()

    def test_unknown_field_is_400(self):
        resp, table = self._put({"action_kind": "acknowledge_only", "created_by": "someone-else"})
        assert resp["statusCode"] == 400
        table.put_item.assert_not_called()

    def test_create_is_a_conditional_put(self):
        """A create racing another create or an enable must not overwrite it."""
        resp, table = self._put({"action_kind": "acknowledge_only"})
        assert resp["statusCode"] == 200
        cond = table.put_item.call_args.kwargs.get("ConditionExpression", "")
        assert "attribute_not_exists" in cond



# ===========================================================================
# Fix Group 2f fixes — review FG2-lambdas findings
# ===========================================================================

# ---------------------------------------------------------------------------
# Critical 2 (RM1): stored enabled=False, not just responded enabled=False
# ---------------------------------------------------------------------------

class TestStoredRuleEnabled:
    """Critical 2 / RM1: the create path must STORE enabled=False.

    The existing test_create_without_enabled_stores_false asserts the response
    body; a mutation that stores True but returns False passes it. These tests
    assert the actual put_item call's Item.

    Mutation to catch (RM1): change ``"enabled": False`` to ``"enabled": True``
    in the item dict while keeping the response literal as False.
    """

    def _put_create(self, body=None):
        from unittest.mock import MagicMock, patch
        import handler as h

        table = MagicMock()
        table.get_item.return_value = {}  # no existing row → creation path
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body=body or {"action_kind": "acknowledge_only"},
                ),
                None,
            )
        return resp, table

    def test_create_stores_enabled_false_in_dynamodb(self):
        """The stored Item's enabled field must be False (not just the response)."""
        resp, table = self._put_create()
        assert resp["statusCode"] == 200

        table.put_item.assert_called_once()
        stored = table.put_item.call_args.kwargs["Item"]
        assert stored["enabled"] is False, (
            f"create must STORE enabled=False; got enabled={stored['enabled']!r} in the "
            f"put_item call. Responding False while storing True would allow a rule to "
            f"fire the evaluator before a second deliberate enable (contract § 6)."
        )

    def test_create_with_enabled_false_body_stores_false(self):
        """Explicit enabled=False in body still stores False (not coerced to True)."""
        resp, table = self._put_create(body={"action_kind": "acknowledge_only", "enabled": False})
        assert resp["statusCode"] == 200

        stored = table.put_item.call_args.kwargs["Item"]
        assert stored["enabled"] is False


# ---------------------------------------------------------------------------
# Warning 6: update must not resurrect a deleted rule
# ---------------------------------------------------------------------------

class TestUpdateDeletedRule:
    """Warning 6: a concurrent DELETE between get_item and put_item must return 409.

    The read now uses ConsistentRead=True and the write uses
    ConditionExpression="attribute_exists(rule_id)" so a race where the rule is
    deleted between the read and write is detected.

    Mutation to catch: remove the ConditionExpression from put_item.
    """

    def test_update_after_concurrent_delete_returns_409(self):
        """Simulates: get_item sees the rule (stale) then put_item fires CCF (deleted)."""
        from botocore.exceptions import ClientError
        from unittest.mock import MagicMock, patch
        import handler as h

        _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

        existing_rule = {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": existing_rule}
        # Simulate the rule being deleted between our read and the write:
        table.put_item.side_effect = _CCF(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "..."}},
            "PutItem",
        )
        table.meta.client.exceptions.ConditionalCheckFailedException = _CCF

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        assert resp["statusCode"] == 409, (
            "An update that races a concurrent DELETE must return 409 (rule_changed_concurrently), "
            f"not 200 (which would resurrect the rule as enabled=True). Got {resp['statusCode']}"
        )
        body = _decode_body(resp)
        assert body.get("code") == "rule_changed_concurrently"

    def test_update_uses_consistent_read(self):
        """The get_item on the update path passes ConsistentRead=True."""
        from unittest.mock import MagicMock, patch
        import handler as h

        existing_rule = {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": existing_rule}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        assert resp["statusCode"] == 200
        get_kwargs = table.get_item.call_args.kwargs
        assert get_kwargs.get("ConsistentRead") is True, (
            "get_item on the update path must use ConsistentRead=True to avoid "
            "racing a concurrent DELETE (Warning 6)"
        )

    def test_update_put_carries_condition_expression(self):
        """The put_item on the update path carries attribute_exists(rule_id)."""
        from unittest.mock import MagicMock, patch
        import handler as h

        existing_rule = {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": existing_rule}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        put_kwargs = table.put_item.call_args.kwargs
        cond = put_kwargs.get("ConditionExpression", "")
        assert "attribute_exists" in cond, (
            "put_item on the update path must carry ConditionExpression "
            "with attribute_exists(rule_id) to prevent resurrecting a deleted rule. "
            f"Got ConditionExpression={cond!r}"
        )


# ---------------------------------------------------------------------------
# Warning 8 (rules): RM2, RM3/RM3b, RM8, RM9
# ---------------------------------------------------------------------------

class TestStoredRuleShape:
    """Warning 8: assert on the stored item and key queried or deleted (not status codes)."""

    def test_limit_body_field_is_rejected_400(self):
        """RM2: a PUT body carrying 'limit' must return 400 (no auto-approvable kind has
        a bound in Stage A). Without this test, a mutation that accepts the limit survives.
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        table = MagicMock()
        table.get_item.return_value = {}  # creation path
        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"action_kind": "acknowledge_only", "limit": 100},
                ),
                None,
            )
        assert resp["statusCode"] == 400, (
            "A PUT body with 'limit' must return 400 (no kind has a bound in Stage A). "
            f"Got {resp['statusCode']}"
        )
        table.put_item.assert_not_called()

    def test_create_stores_created_by_from_token_sub(self):
        """RM3/RM3b: the create path must store created_by from the token sub.

        A mutation that hard-codes a different value or copies a body field would
        let a caller attribute the rule to another identity.
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        table = MagicMock()
        table.get_item.return_value = {}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(sub="unique-sub-from-token", fleet_ids="fleet-mrdn-01"),
                    body={"action_kind": "acknowledge_only"},
                ),
                None,
            )

        stored = table.put_item.call_args.kwargs["Item"]
        assert stored["created_by"] == "unique-sub-from-token", (
            f"created_by must be the verified token sub 'unique-sub-from-token'; "
            f"got {stored.get('created_by')!r}"
        )

    def test_delete_calls_delete_item_with_correct_key(self):
        """RM8: DELETE must call delete_item with (fleet_id, rule_id) as the key.

        A mutation that no-ops DELETE (removes the delete_item call) silently fails
        to remove the rule; it remains enabled and continues to auto-approve.
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        table = MagicMock()
        table.delete_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "DELETE",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-to-delete"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                ),
                None,
            )

        assert resp["statusCode"] == 204
        table.delete_item.assert_called_once_with(
            Key={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-to-delete"}
        )

    def test_get_queries_the_requested_fleet_partition(self):
        """RM9: GET must query the requested fleet_id, not a hardcoded partition.

        Uses two different fleet_ids; each call receives its own mock response via
        boto3's ConditionExpressionBuilder to extract the queried value.
        """
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        from unittest.mock import MagicMock, patch
        import handler as h

        queried_fleets: list = []

        def fake_query(**kwargs):
            key_cond = kwargs.get("KeyConditionExpression")
            builder = ConditionExpressionBuilder()
            expression = builder.build_expression(key_cond)
            # attribute_value_placeholders maps placeholder names to values
            queried_fleets.extend(expression.attribute_value_placeholders.values())
            return {"Items": []}

        table = MagicMock()
        table.query.side_effect = fake_query

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h.handler(
                _event(
                    "GET",
                    "/approval-rules/fleet/{fleet_id}",
                    path_params={"fleet_id": "fleet-mrdn-UNIQUE-A"},
                    claims=_claims(fleet_ids="fleet-mrdn-UNIQUE-A"),
                ),
                None,
            )

        assert "fleet-mrdn-UNIQUE-A" in queried_fleets, (
            f"GET must query the path-parameter fleet_id 'fleet-mrdn-UNIQUE-A'; "
            f"got queried values: {queried_fleets}"
        )



# ===========================================================================
# Fix Group 3d fixes — review Cycle 8b Suggestions (RW6c, ABA)
# ===========================================================================

# ---------------------------------------------------------------------------
# RW6c: update ConditionExpression must be an exact match, not a substring
# ---------------------------------------------------------------------------

class TestUpdateConditionExact:
    """RW6c: the update ConditionExpression must be asserted by exact value.

    Mutant RW6c: 'attribute_exists(rule_id) OR attribute_not_exists(rule_id)' always
    evaluates True, effectively disabling the delete-race guard.  A substring check
    ('attribute_exists' in cond) passes RW6c because the mutant still contains that
    substring.  Exact equality catches it.

    The ABA fix adds AND #u = :read_at to close the delete+recreate window;
    the correct exact string is 'attribute_exists(rule_id) AND #u = :read_at'.

    Mutation tests (fail against unfixed code):
      - unfixed handler: ConditionExpression='attribute_exists(rule_id)' → exact assert fails
      - RW6c mutant:     '...OR attribute_not_exists(rule_id)' → exact assert fails
      - ABA-missing:     no ExpressionAttributeValues → value assert fails
    """

    def _existing_with_updated_at(self):
        return {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-06-01T12:00:00Z",
        }

    def test_update_condition_expression_is_exact(self):
        """Assert the exact ConditionExpression, not a substring.

        Fails against:
          - unfixed code (no ABA clause): cond == 'attribute_exists(rule_id)'
          - RW6c mutant: cond contains OR branch
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        existing = self._existing_with_updated_at()
        table = MagicMock()
        table.get_item.return_value = {"Item": existing}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        assert resp["statusCode"] == 200
        cond = table.put_item.call_args.kwargs.get("ConditionExpression", "")
        assert cond == "attribute_exists(rule_id) AND #u = :read_at", (
            f"ConditionExpression must be exactly 'attribute_exists(rule_id) AND #u = :read_at'; "
            f"got {cond!r}. "
            "A substring check allows 'attribute_exists(rule_id) OR attribute_not_exists(rule_id)' "
            "to survive (RW6c mutant — always evaluates True, disabling the delete guard)."
        )

    def test_update_passes_read_updated_at_in_expression_values(self):
        """ExpressionAttributeValues must include :read_at = existing['updated_at'].

        Fails against unfixed code (no ExpressionAttributeValues at all).
        The ABA fix binds the write to the exact updated_at seen at read time.
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        existing = self._existing_with_updated_at()
        table = MagicMock()
        table.get_item.return_value = {"Item": existing}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        put_kw = table.put_item.call_args.kwargs
        expr_vals = put_kw.get("ExpressionAttributeValues", {})
        assert ":read_at" in expr_vals, (
            "put_item ExpressionAttributeValues must contain ':read_at' for the ABA guard; "
            f"got keys: {list(expr_vals)}"
        )
        assert expr_vals[":read_at"] == "2026-06-01T12:00:00Z", (
            f":read_at must equal the read item's updated_at '2026-06-01T12:00:00Z'; "
            f"got {expr_vals.get(':read_at')!r}"
        )


# ---------------------------------------------------------------------------
# ABA: update must not overwrite a rule deleted and re-created between read and write
# ---------------------------------------------------------------------------

class TestUpdateABAProtection:
    """ABA (delete-and-recreate race): optimistic lock on updated_at prevents overwrite.

    Scenario: Operator A reads rule (updated_at=T1).  Operator B deletes it.
    Operator C creates a new rule with the same rule_id (updated_at=T2, enabled=False).
    Without the ABA guard, Operator A's update fires attribute_exists (passes —
    new rule exists) and overwrites the new disabled rule with the old enabled=True row.

    With the fix, the condition includes #u = :read_at where :read_at = T1.
    The new rule has updated_at=T2 ≠ T1 → CCF → 409.

    Mutation to catch:
      - Remove AND #u = :read_at from the condition; the write succeeds and overwrites
        the new rule.  The test that asserts 409 on a read-vs-stored mismatch fails.
    """

    def _put_with_update(self, existing, put_side_effect=None):
        """Run an enable PUT for an existing rule; return (response, table mock)."""
        from unittest.mock import MagicMock, patch
        import handler as h

        table = MagicMock()
        table.get_item.return_value = {"Item": existing}
        if put_side_effect is not None:
            table.put_item.side_effect = put_side_effect
        else:
            table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )
        return resp, table

    def test_aba_race_returns_409(self):
        """CCF on the updated_at condition (ABA scenario) returns 409.

        The test simulates: get_item returns the old rule (T1), put_item fires CCF
        because another request changed the row (T2 in DDB).
        Without the ABA guard, CCF would only fire on DELETE, not on DELETE+CREATE.
        The exact-condition test above verifies the guard is in place; this test
        verifies that when the guard fires (CCF), the response is 409 (not 200).

        Mutation to catch: remove the try/except → unhandled exception (not 409).
        """
        from botocore.exceptions import ClientError
        from unittest.mock import MagicMock, patch
        import handler as h

        _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

        existing = {
            "fleet_id": "fleet-mrdn-01",
            "rule_id": "rule-001",
            "action_kind": "acknowledge_only",
            "enabled": False,
            "created_by": "op-sub-mrdn-01",
            "updated_at": "2026-06-01T12:00:00Z",  # T1 — what we read
        }

        def _ccf_side_effect(**kwargs):
            raise _CCF(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "The conditional request failed"}},
                "PutItem",
            )

        table = MagicMock()
        table.get_item.return_value = {"Item": existing}
        table.put_item.side_effect = _ccf_side_effect
        table.meta.client.exceptions.ConditionalCheckFailedException = _CCF

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            resp = h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        assert resp["statusCode"] == 409, (
            "A CCF on the update condition (ABA race: rule deleted and re-created "
            "between read and write) must return 409, not 200 (overwriting new rule). "
            f"Got {resp['statusCode']}"
        )
        body = _decode_body(resp)
        assert body.get("code") == "rule_changed_concurrently", (
            f"Error code must be 'rule_changed_concurrently' for ABA race; "
            f"got {body.get('code')!r}"
        )

    def test_aba_guard_uses_attribute_expression_names(self):
        """The put_item call must include ExpressionAttributeNames with '#u': 'updated_at'.

        Without ExpressionAttributeNames, #u would be treated as a literal attribute
        name starting with #, not an alias — the condition would fail at DDB level.
        Mutation: remove ExpressionAttributeNames → '#u' unresolved → DDB error.
        This test catches the mutation by asserting the key is present in the call.
        """
        from unittest.mock import MagicMock, patch
        import handler as h

        existing = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-001",
            "action_kind": "acknowledge_only", "enabled": False,
            "created_by": "op", "updated_at": "2026-06-01T12:00:00Z",
        }
        table = MagicMock()
        table.get_item.return_value = {"Item": existing}
        table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h.handler(
                _event(
                    "PUT",
                    "/approval-rules/fleet/{fleet_id}/{rule_id}",
                    path_params={"fleet_id": "fleet-mrdn-01", "rule_id": "rule-001"},
                    claims=_claims(fleet_ids="fleet-mrdn-01"),
                    body={"enabled": True},
                ),
                None,
            )

        put_kw = table.put_item.call_args.kwargs
        expr_names = put_kw.get("ExpressionAttributeNames", {})
        assert "#u" in expr_names, (
            f"ExpressionAttributeNames must include '#u' (alias for 'updated_at'); "
            f"got {expr_names!r}"
        )
        assert expr_names["#u"] == "updated_at", (
            f"'#u' must map to 'updated_at'; got {expr_names.get('#u')!r}"
        )
