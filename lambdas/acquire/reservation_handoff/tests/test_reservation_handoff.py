"""Tests for lambdas/acquire/reservation_handoff/handler.py

Run:
    python3 -m pytest lambdas/acquire/reservation_handoff/ -v
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Load handler by file path to avoid module-name collisions with sibling Lambdas.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("reservation_handoff_handler", _HANDLER_PATH)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER_SUB = "a1b2c3d4-0000-0000-0000-000000000001"


def _event(body: dict, sub: str = _OWNER_SUB) -> dict:
    """Build an API-Gateway-proxy-shaped event with a Cognito authorizer claim.

    Per decisions.md Q8, ownerSub is derived from the claim (claims.sub),
    not from the request body.  The _BASE fixture no longer includes ownerSub.
    """
    return {
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": sub,
                    "custom:tenantId": body.get("tenantId", ""),
                }
            }
        },
    }


_CONFIGURATION = {
    "categoryId": "commuter",
    "modelId": "meridian-300",
    "variantId": "standard",
    "colorId": "racing-red",
    "accessoryIds": ["top-box-45l"],
}

# ownerSub is intentionally absent here — derived from the claim (decisions.md Q8).
_BASE = {
    "tenantId": "genericmoto-demo",
    "ownerContact": {"email": "buyer@example.com", "displayName": "A. Buyer"},
    "configuration": _CONFIGURATION,
    "dealerRef": "dealer-springfield-01",
    "financing": {"tier": "prime", "approvedAmount": 10000.0},
    "payment": {"transactionRef": "TXN-abc", "status": "captured"},
}


def _mock_ddb_ok():
    """Return a DynamoDB resource mock that succeeds on all calls."""
    table = MagicMock()
    table.put_item.return_value = {}
    # No catalog lookups return anything by default; tests that need catalog
    # data should set up get_item / query individually.
    table.get_item.return_value = {"Item": None}
    table.query.return_value = {"Items": []}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _client_error(code: str = "ConditionalCheckFailedException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "msg"}},
        "PutItem",
    )


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestMakeOrderId:
    def test_determinism(self):
        dt = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)
        a = rh._make_order_id("t1", "s1", dt)
        b = rh._make_order_id("t1", "s1", dt)
        assert a == b

    def test_format(self):
        dt = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)
        order_id = rh._make_order_id("t1", "s1", dt)
        assert order_id.startswith("ORD-20260721-")
        assert len(order_id) == len("ORD-20260721-") + 6

    def test_different_tenant_different_id(self):
        dt = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)
        a = rh._make_order_id("t1", "s1", dt)
        b = rh._make_order_id("t2", "s1", dt)
        assert a != b

    def test_different_sub_different_id(self):
        dt = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)
        a = rh._make_order_id("t1", "s1", dt)
        b = rh._make_order_id("t1", "s2", dt)
        assert a != b


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

class TestHandler:
    def test_happy_path_returns_201(self):
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            resp = rh.handler(_event(_BASE), None)
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["stage"] == "orderPlaced"
        assert body["orderId"].startswith("ORD-")
        assert "nextTransitionAt" in body
        assert "correlation" in body

    def test_response_includes_order_number(self):
        """orderNumber must be present in response (decisions.md Q3).

        Swift ReservationResponse.orderNumber is non-optional — its absence
        makes every reservation fail to decode client-side.
        """
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            body = json.loads(rh.handler(_event(_BASE), None)["body"])
        assert "orderNumber" in body, (
            "response must include 'orderNumber' — "
            "Swift ReservationResponse.orderNumber is non-optional"
        )
        assert body["orderNumber"] == body["orderId"], (
            "orderNumber should match orderId (human-readable alias)"
        )

    def test_star_envelope_has_handover_type(self):
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            body = json.loads(rh.handler(_event(_BASE), None)["body"])
        assert body["correlation"]["starEnvelope"]["appointment_type"] == "handover"

    def test_missing_tenant_id_rejected(self):
        bad = {k: v for k, v in _BASE.items() if k != "tenantId"}
        resp = rh.handler(_event(bad, sub=_OWNER_SUB), None)
        assert resp["statusCode"] == 400
        assert "tenantId" in json.loads(resp["body"])["error"]

    def test_missing_configuration_fields_rejected(self):
        bad = {**_BASE, "configuration": {"categoryId": "commuter"}}
        resp = rh.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_invalid_json_body(self):
        # Supply a valid sub so we reach the JSON-parse step.
        evt = {
            "body": "bad-json",
            "requestContext": {
                "authorizer": {"claims": {"sub": _OWNER_SUB}}
            },
        }
        resp = rh.handler(evt, None)
        assert resp["statusCode"] == 400

    def test_missing_claim_sub_rejected(self):
        """A missing sub claim must return 401, not 500."""
        evt = _event(_BASE, sub="")
        resp = rh.handler(evt, None)
        assert resp["statusCode"] == 401

    def test_idempotent_duplicate_returns_201(self):
        """ConditionalCheckFailed on PutItem → same 201 response (idempotent)."""
        table = MagicMock()
        table.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(_BASE), None)
        assert resp["statusCode"] == 201

    def test_ddb_fatal_error_returns_500(self):
        table = MagicMock()
        table.put_item.side_effect = _client_error("InternalServerError")
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(_BASE), None)
        assert resp["statusCode"] == 500

    def test_owner_sub_derived_from_claim(self):
        """ownerSub on the stored item must come from the JWT claim, not the body.

        decisions.md Q8: the kiosk device's own Cognito subject, not a shared
        sentinel or a client-supplied value.
        """
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        device_sub = "device-sub-uuid-9999"
        with patch.object(rh, "_get_ddb", return_value=resource):
            rh.handler(_event(_BASE, sub=device_sub), None)

        assert captured_item, "put_item was never called"
        assert captured_item[0].get("ownerSub") == device_sub, (
            f"ownerSub must be the claim sub '{device_sub}', "
            f"got {captured_item[0].get('ownerSub')!r}"
        )

    def test_tenant_owner_key_uses_claim_sub(self):
        """tenantOwnerKey must be `<tenantId>#<claim.sub>` (not body.ownerSub)."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        device_sub = "device-sub-uuid-9999"
        with patch.object(rh, "_get_ddb", return_value=resource):
            rh.handler(_event(_BASE, sub=device_sub), None)

        assert captured_item, "put_item was never called"
        key = captured_item[0].get("tenantOwnerKey")
        assert key == f"{_BASE['tenantId']}#{device_sub}", (
            f"tenantOwnerKey must use claim sub, got {key!r}"
        )

    def test_stage_history_populated(self):
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(rh, "_get_ddb", return_value=resource):
            rh.handler(_event(_BASE), None)

        assert captured_item, "put_item was never called"
        history = captured_item[0].get("stageHistory", [])
        assert len(history) == 1
        assert history[0]["stage"] == "orderPlaced"

    def test_stamps_discover_lead_id_when_present(self):
        """discoverLeadId in request body is stamped onto the order item."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        payload_with_lead = {**_BASE, "discoverLeadId": "LEAD-20260728-abcd1234"}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload_with_lead), None)

        assert resp["statusCode"] == 201
        assert captured_item, "put_item was never called"
        assert captured_item[0].get("discoverLeadId") == "LEAD-20260728-abcd1234"

    def test_no_discover_lead_id_field_when_absent(self):
        """When discoverLeadId is not in the request body, the field is absent from the item."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(rh, "_get_ddb", return_value=resource):
            rh.handler(_event(_BASE), None)

        assert captured_item, "put_item was never called"
        assert "discoverLeadId" not in captured_item[0]


class TestKioskAwareReservation:
    """Tests specific to the Group 2 kiosk-aware changes."""

    def test_selected_accessory_ids_alias_accepted(self):
        """selectedAccessoryIds is accepted as an alias for accessoryIds (Q2)."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        config_with_alias = {
            "categoryId": "commuter",
            "modelId": "meridian-300",
            "variantId": "standard",
            "colorId": "racing-red",
            "selectedAccessoryIds": ["top-box-45l"],  # alias field
        }
        payload = {**_BASE, "configuration": config_with_alias}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload), None)

        assert resp["statusCode"] == 201
        assert captured_item, "put_item was never called"
        config = captured_item[0].get("configuration", {})
        # Should be stored as accessoryIds, not selectedAccessoryIds
        assert "accessoryIds" in config, (
            "selectedAccessoryIds alias should be normalised to accessoryIds"
        )
        assert config["accessoryIds"] == ["top-box-45l"]
        assert "selectedAccessoryIds" not in config, (
            "selectedAccessoryIds alias field should not persist to the stored item"
        )

    def test_category_id_optional_when_model_lookup_succeeds(self):
        """categoryId is derived from the model row when absent in request (Q2)."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        # Simulate a catalog table that returns the model row.
        model_item = {
            "tenantId": "genericmoto-demo",
            "itemKey": "MODEL#commuter#meridian-300",
            "categoryKey": "CAT#commuter",
        }

        def fake_table(table_name):
            tbl = MagicMock()
            tbl.put_item.side_effect = fake_put_item
            tbl.get_item.return_value = {"Item": None}  # META#tenantConfig absent
            tbl.query.return_value = {"Items": [model_item]}
            return tbl

        resource = MagicMock()
        resource.Table.side_effect = fake_table

        config_no_category = {
            "modelId": "meridian-300",
            "variantId": "standard",
            "colorId": "racing-red",
        }
        payload = {**_BASE, "configuration": config_no_category}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload), None)

        assert resp["statusCode"] == 201, json.loads(resp["body"])
        assert captured_item, "put_item was never called"
        config = captured_item[0].get("configuration", {})
        assert config.get("categoryId") == "commuter", (
            f"categoryId should be derived from model row, got {config.get('categoryId')!r}"
        )

    def test_category_id_optional_when_model_lookup_fails(self):
        """Missing categoryId + catalog lookup failure still returns 201 (no hard error)."""
        table = MagicMock()
        table.put_item.return_value = {}
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        config_no_category = {
            "modelId": "meridian-300",
            "variantId": "standard",
            "colorId": "racing-red",
        }
        payload = {**_BASE, "configuration": config_no_category}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload), None)

        assert resp["statusCode"] == 201

    def test_dealer_ref_falls_back_to_tenant_config(self):
        """When dealerRef is absent from the body, it is taken from tenant config."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        tenant_config_item = {
            "tenantId": "genericmoto-demo",
            "itemKey": "META#tenantConfig",
            "dealerRef": "dealer-from-tenant-config",
        }

        def fake_table(table_name):
            tbl = MagicMock()
            tbl.put_item.side_effect = fake_put_item
            tbl.get_item.return_value = {"Item": tenant_config_item}
            tbl.query.return_value = {"Items": []}
            return tbl

        resource = MagicMock()
        resource.Table.side_effect = fake_table

        payload_no_dealer = {k: v for k, v in _BASE.items() if k != "dealerRef"}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload_no_dealer), None)

        assert resp["statusCode"] == 201
        assert captured_item, "put_item was never called"
        assert captured_item[0].get("dealerRef") == "dealer-from-tenant-config"

    def test_interior_id_and_ive_package_persisted(self):
        """interiorId and ivePackage in configuration are stored on the item."""
        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        table = MagicMock()
        table.put_item.side_effect = fake_put_item
        table.get_item.return_value = {"Item": None}
        table.query.return_value = {"Items": []}
        resource = MagicMock()
        resource.Table.return_value = table

        config_with_extras = {
            **_CONFIGURATION,
            "interiorId": "luxury-black",
            "ivePackage": "MERIDIAN-IVE-PREMIUM",
        }
        payload = {**_BASE, "configuration": config_with_extras}

        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(payload), None)

        assert resp["statusCode"] == 201
        assert captured_item, "put_item was never called"
        config = captured_item[0].get("configuration", {})
        assert config.get("interiorId") == "luxury-black"
        assert config.get("ivePackage") == "MERIDIAN-IVE-PREMIUM"

    def test_cadence_from_tenant_config_used_for_next_transition(self):
        """nextTransitionAt is derived from deliveryCadence when available."""
        from decimal import Decimal

        captured_item: list[dict] = []

        def fake_put_item(Item, **kwargs):  # noqa: N803
            captured_item.append(Item)
            return {}

        # Very short cadence (near-zero hours) so the transition is near now.
        tenant_config_item = {
            "tenantId": "genericmoto-demo",
            "itemKey": "META#tenantConfig",
            "deliveryCadence": {
                "orderPlacedToPayment": {
                    "minHours": Decimal("0.00001"),
                    "maxHours": Decimal("0.00001"),
                }
            },
        }

        def fake_table(table_name):
            tbl = MagicMock()
            tbl.put_item.side_effect = fake_put_item
            tbl.get_item.return_value = {"Item": tenant_config_item}
            tbl.query.return_value = {"Items": []}
            return tbl

        resource = MagicMock()
        resource.Table.side_effect = fake_table

        before = datetime.now(timezone.utc)
        with patch.object(rh, "_get_ddb", return_value=resource):
            resp = rh.handler(_event(_BASE), None)
        after = datetime.now(timezone.utc)

        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        from datetime import datetime as dt
        next_ts = dt.fromisoformat(body["nextTransitionAt"])
        # With near-zero cadence, nextTransitionAt should be very close to now
        delta = (next_ts - before).total_seconds()
        assert 0 <= delta < 5, (
            f"With near-zero cadence, nextTransitionAt should be near now; "
            f"delta={delta:.3f}s"
        )

    def test_eventbridge_publish_called_when_flag_set(self):
        """order.placed is published to EventBridge when flag is enabled."""
        import importlib
        import os

        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_events = MagicMock()
            mock_events.put_events.return_value = {}
            with (
                patch.object(rh, "_get_events", return_value=mock_events),
                patch.object(rh, "_PUBLISH_ORDER_PLACED", True),
                patch.object(rh, "_EVENT_BUS_ARN", "arn:aws:events:us-east-1:123:event-bus/test"),
            ):
                resp = rh.handler(_event(_BASE), None)

        assert resp["statusCode"] == 201
        mock_events.put_events.assert_called_once()
        call_kwargs = mock_events.put_events.call_args
        entries = call_kwargs[1].get("Entries") or call_kwargs[0][0].get("Entries", [])
        assert len(entries) == 1
        assert entries[0]["DetailType"] == "order.placed"

    def test_eventbridge_publish_not_called_when_flag_off(self):
        """order.placed is NOT published when VSA_ACQUIRE_PUBLISH_ORDER_PLACED is false."""
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_events = MagicMock()
            with (
                patch.object(rh, "_get_events", return_value=mock_events),
                patch.object(rh, "_PUBLISH_ORDER_PLACED", False),
            ):
                resp = rh.handler(_event(_BASE), None)

        assert resp["statusCode"] == 201
        mock_events.put_events.assert_not_called()

    def test_eventbridge_failure_does_not_fail_request(self):
        """A publish failure must not propagate to the caller (fire-and-forget)."""
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_events = MagicMock()
            mock_events.put_events.side_effect = RuntimeError("bus unavailable")
            with (
                patch.object(rh, "_get_events", return_value=mock_events),
                patch.object(rh, "_PUBLISH_ORDER_PLACED", True),
                patch.object(rh, "_EVENT_BUS_ARN", "arn:aws:events:us-east-1:123:event-bus/test"),
            ):
                resp = rh.handler(_event(_BASE), None)

        # Despite EventBridge failure, the reservation succeeds.
        assert resp["statusCode"] == 201

    def test_sfn_start_called_after_write(self):
        """Step Functions execution is started after the DDB write succeeds."""
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_sfn = MagicMock()
            mock_sfn.start_execution.return_value = {}
            with (
                patch.object(rh, "_get_sfn", return_value=mock_sfn),
                patch.object(rh, "_SFN_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:test"),
            ):
                resp = rh.handler(_event(_BASE), None)

        assert resp["statusCode"] == 201
        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args
        # Execution name should be the orderId.
        name_arg = call_kwargs[1].get("name") or call_kwargs[0][1]
        body = json.loads(resp["body"])
        assert name_arg == body["orderId"]

    def test_sfn_not_called_when_arn_missing(self):
        """No Step Functions call when VSA_ACQUIRE_PROGRESSION_SFN_ARN is empty."""
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_sfn = MagicMock()
            with (
                patch.object(rh, "_get_sfn", return_value=mock_sfn),
                patch.object(rh, "_SFN_STATE_MACHINE_ARN", ""),
            ):
                resp = rh.handler(_event(_BASE), None)

        assert resp["statusCode"] == 201
        mock_sfn.start_execution.assert_not_called()

    def test_sfn_failure_does_not_fail_request(self):
        """An SFN start failure must not propagate to the caller."""
        with patch.object(rh, "_get_ddb", return_value=_mock_ddb_ok()):
            mock_sfn = MagicMock()
            mock_sfn.start_execution.side_effect = RuntimeError("sfn unavailable")
            with (
                patch.object(rh, "_get_sfn", return_value=mock_sfn),
                patch.object(rh, "_SFN_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:test"),
            ):
                resp = rh.handler(_event(_BASE), None)

        # SFN failure must not fail the reservation.
        assert resp["statusCode"] == 201
