"""Tests for the RSA dispatch consumer (Task 2.2).

Mutation guards mandated by Task 2.2:
  (a) Remove ConditionExpression → attempt ARRIVED→DISPATCHED must succeed.
  (b) Replace error path with swallowed except:pass → FAILED is never written.
  (c) Process same record twice → second provider call must be made.

Fix Group 4 additions (F4.1, F4.2, F4.3, F4.5, F4.8):
  F4.1 — moto-backed tests that actually exercise DynamoDB validation;
          fixes the ValidationException (mixed Attr+EAV idiom).
  F4.2 — fail-closed on absent lat/lon; producer must write serviceType +
          callbackContact; moto-backed end-to-end producer→consumer test.
  F4.3 — handler returns batchItemFailures for failed records.
  F4.5 — ConsistentRead=True on idempotency pre-check.
  F4.8 — moto-backed test that DynamoDB rejects ARRIVED→DISPATCHED.

F4.10 — module-name collision fix (W8):
  Both lambdas/rsa-dispatch-consumer/tests/ and lambdas/api-escalate/tests/
  contain handler.py + __init__.py. In one pytest process the bare
  `import handler` binds sys.modules["handler"] to whichever lambda loaded
  first, causing fixture errors in the other suite. Fix: load this handler
  under the unique module name "rsa_consumer_handler" using
  importlib.util.spec_from_file_location, and use patch.object on the loaded
  module reference rather than patching by dotted import name.

Run with:
  .venv/bin/python3 -m pytest lambdas/rsa-dispatch-consumer/ -v
  # Combined run (F4.10 target):
  .venv/bin/python3 -m pytest lambdas/rsa-dispatch-consumer/ lambdas/api-escalate/ -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — lambdas/ must be on sys.path so `from shared.rsa_client import`
# resolves, but we load the handler itself under a unique module name to avoid
# the sys.modules["handler"] collision with lambdas/api-escalate/tests/.
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_DIR = _LAMBDA_ROOT.parent

if str(_LAMBDAS_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDAS_DIR))

# Inject env vars before module-level os.environ reads fire.
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("VSA_RSA_CLIENT", "fake")


def _load_handler():
    """Load rsa-dispatch-consumer/handler.py under the unique name 'rsa_consumer_handler'.

    spec_from_file_location lets multiple lambda suites run in one process
    without the second import returning the first lambda's cached module object.
    The module is cached under 'rsa_consumer_handler' in sys.modules so
    repeated calls within a session are free.
    """
    mod_name = "rsa_consumer_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    handler_path = _LAMBDA_ROOT / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load once at collection time; test bodies reference as _h.
_h = _load_handler()


# ---------------------------------------------------------------------------
# Helpers — build DynamoDB stream records
# ---------------------------------------------------------------------------

def _make_stream_record(
    dispatch_id: str = "DISP-TEST-0001",
    status: str = "REQUESTED",
    vin: str = "1FMCU0F70MUA12345",
    lat: str = "37.7749",
    lon: str = "-122.4194",
    service_type: str = "tow",
    tenant_id: str = "meridian",
    callback_contact: str = "+15551234567",
    event_name: str = "INSERT",
) -> dict[str, Any]:
    """Build a minimal DynamoDB stream record shaped like a real INSERT event."""
    return {
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "dispatchId": {"S": dispatch_id},
                "status": {"S": status},
                "vin": {"S": vin},
                "lat": {"S": lat},
                "lon": {"S": lon},
                "serviceType": {"S": service_type},
                "tenantId": {"S": tenant_id},
                "callbackContact": {"S": callback_contact},
            }
        },
    }


def _make_event(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"Records": records}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_table_env(monkeypatch):
    """Inject RSA_DISPATCH_TABLE so the module-level os.environ read succeeds."""
    monkeypatch.setenv("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
    monkeypatch.setenv("VSA_RSA_CLIENT", "fake")


@pytest.fixture()
def mock_ddb_table():
    """Return a MagicMock that behaves like a DynamoDB Table resource.

    get_item returns {"Item": {}} by default (no existing item — row is new,
    status not yet set, so idempotency check proceeds to provider call).
    """
    table = MagicMock()
    table.update_item = MagicMock()
    # Default: item not found / no status → idempotency check passes through
    table.get_item = MagicMock(return_value={"Item": {}})
    return table


@pytest.fixture()
def patched_ddb(mock_ddb_table):
    """Patch _h._get_ddb() to return a resource whose Table() is the mock.

    Uses patch.object on the pre-loaded module reference (_h) rather than
    patching by dotted string name — the dotted form triggers importlib which
    would try to find 'rsa_consumer_handler' as a file on sys.path, but the
    module lives only in sys.modules under that name.
    patch.object goes directly to the object's attribute dict, bypassing the
    import machinery. (F4.10 / W8)
    """
    ddb_resource = MagicMock()
    ddb_resource.Table.return_value = mock_ddb_table
    with patch.object(_h, "_get_ddb", return_value=ddb_resource):
        yield mock_ddb_table


# ---------------------------------------------------------------------------
# Accept 1: Successful dispatch path
# ---------------------------------------------------------------------------

class TestSuccessfulDispatch:
    """Accept 1: INSERT of REQUESTED → provider called → DISPATCHED written."""

    def test_requested_insert_calls_provider(self, patched_ddb):
        """On REQUESTED INSERT, the RSA provider is called once."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABCDEF123456",
            eta_minutes=25,
            simulated=True,
            status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        provider_mock.request_dispatch.assert_called_once()

    def test_dispatched_written_with_provider_ref(self, patched_ddb):
        """DISPATCHED status is written with providerRef, etaMinutes, simulated."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABCDEF123456",
            eta_minutes=25,
            simulated=True,
            status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record(dispatch_id="DISP-0001")]), None)

        call_kwargs = patched_ddb.update_item.call_args.kwargs
        assert call_kwargs["Key"] == {"dispatchId": "DISP-0001"}
        ev = call_kwargs["ExpressionAttributeValues"]
        assert ev[":dispatched"] == "DISPATCHED", (
            f"Expected ':dispatched' = 'DISPATCHED', got {ev[':dispatched']!r}. "
            "The consumer must write DISPATCHED, not any other status."
        )
        assert ev[":ref"] == "SIM-ABCDEF123456"
        assert ev[":eta"] == 25
        assert ev[":sim"] is True

    def test_dispatched_status_value_not_just_presence(self, patched_ddb):
        """Assert the actual value written, not just that update_item was called."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-XYZ",
            eta_minutes=19,
            simulated=True,
            status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev[":dispatched"] == "DISPATCHED"

    def test_simulated_flag_written_as_true(self, patched_ddb):
        """spec R4: simulated must be written to the row."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABC",
            eta_minutes=20,
            simulated=True,
            status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev[":sim"] is True

    def test_skip_non_insert_events(self, patched_ddb):
        """MODIFY and REMOVE events are ignored — only INSERT triggers dispatch."""
        with patch.object(_h, "get_client") as mock_get_client:
            _h.handler(_make_event([_make_stream_record(event_name="MODIFY")]), None)
            mock_get_client.assert_not_called()
        patched_ddb.update_item.assert_not_called()

    def test_skip_non_requested_insert(self, patched_ddb):
        """INSERT of a row already in DISPATCHED state is skipped."""
        with patch.object(_h, "get_client") as mock_get_client:
            _h.handler(_make_event([_make_stream_record(status="DISPATCHED")]), None)
            mock_get_client.assert_not_called()
        patched_ddb.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Accept 2: Error path writes FAILED — never leaves row in REQUESTED
# ---------------------------------------------------------------------------

class TestErrorPathWritesFailed:
    """Accept 2: Provider error → FAILED written with reason."""

    def test_provider_exception_writes_failed(self, patched_ddb):
        """Provider raising RuntimeError → FAILED with reason written."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = RuntimeError("provider unavailable")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        patched_ddb.update_item.assert_called_once()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev[":failed"] == "FAILED", (
            f"Expected ':failed' = 'FAILED', got {ev.get(':failed')!r}. "
            "The error path MUST write FAILED. A swallowed exception leaves the row "
            "in REQUESTED — the exact dead-letterbox defect this spec exists to close."
        )
        assert ":reason" in ev
        assert ev[":reason"]

    def test_failed_reason_contains_exception_info(self, patched_ddb):
        """The failure reason must reference the exception type."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = ConnectionError("timeout")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        reason = ev.get(":reason", "")
        assert reason
        assert "ConnectionError" in reason or "timeout" in reason.lower()

    def test_row_never_left_in_requested_on_error(self, patched_ddb):
        """After a provider error, the row is FAILED not REQUESTED."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = Exception("anything")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record(dispatch_id="DISP-ERR-0001")]), None)

        assert patched_ddb.update_item.call_count == 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED"

    def test_swallowed_exception_guard(self, patched_ddb):
        """Explicit guard: a silent except:pass leaves the row in REQUESTED."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = ValueError("bad input")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        patched_ddb.update_item.assert_called_once()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED"


# ---------------------------------------------------------------------------
# Accept 3: ConditionExpression — terminal state enforcement (spec R7)
# ---------------------------------------------------------------------------

class TestTerminalStateConditionExpression:
    """Accept 3: Every write carries a ConditionExpression refusing terminal overwrites."""

    def test_condition_expression_present_on_dispatched_write(self, patched_ddb):
        """ConditionExpression must be present in the DISPATCHED update_item call."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABC", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        call_kwargs = patched_ddb.update_item.call_args.kwargs
        assert "ConditionExpression" in call_kwargs

    def test_condition_expression_present_on_failed_write(self, patched_ddb):
        """ConditionExpression must be present on the FAILED writeback too."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = RuntimeError("boom")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        call_kwargs = patched_ddb.update_item.call_args.kwargs
        assert "ConditionExpression" in call_kwargs

    def test_terminal_placeholders_built_from_sorted_terminals(self):
        """Terminal-state placeholders are built from sorted() — deterministic ordering."""
        from shared.rsa_client import LIFECYCLE_TERMINAL_STATES

        expected_sorted = sorted(LIFECYCLE_TERMINAL_STATES)
        assert _h._SORTED_TERMINALS == expected_sorted

    def test_arrived_to_dispatched_blocked_by_condition(self, patched_ddb):
        """Simulate CE rejection when row is already ARRIVED."""
        from botocore.exceptions import ClientError

        patched_ddb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
            "UpdateItem",
        )

        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABC", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        assert patched_ddb.update_item.call_count == 1


# ---------------------------------------------------------------------------
# Accept 4: Idempotency — same record processed twice → one provider call
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Accept 4: Same stream record processed twice → exactly one provider call."""

    def test_same_record_twice_calls_provider_once(self, patched_ddb):
        """Processing the same REQUESTED INSERT twice → provider called once."""
        get_item_call_count = {"n": 0}

        def fake_get_item(**kwargs):
            get_item_call_count["n"] += 1
            if get_item_call_count["n"] == 1:
                return {"Item": {}}
            return {"Item": {"status": "DISPATCHED"}}

        patched_ddb.get_item.side_effect = fake_get_item

        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-IDEMPOTENT", eta_minutes=22, simulated=True, status="DISPATCHED",
        )

        record = _make_stream_record(dispatch_id="DISP-IDEM-0001")
        event = _make_event([record, record])

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(event, None)

        assert provider_mock.request_dispatch.call_count == 1

    def test_idempotent_replay_does_not_write_failed(self, patched_ddb):
        """A CE failure on DISPATCHED write must NOT trigger a FAILED write."""
        from botocore.exceptions import ClientError

        patched_ddb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
            "UpdateItem",
        )

        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-ABC", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record()]), None)

        assert patched_ddb.update_item.call_count == 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ":dispatched" in ev


# ---------------------------------------------------------------------------
# F4.5: ConsistentRead=True on the idempotency pre-check
# ---------------------------------------------------------------------------

class TestConsistentRead:
    """F4.5 (W3): The get_item pre-check must use ConsistentRead=True.

    Mutation guard: drop ConsistentRead → test MUST fail.
    """

    def test_get_item_uses_consistent_read(self, patched_ddb):
        """The idempotency get_item call must use ConsistentRead=True.

        Without it, an eventually-consistent read may see a stale REQUESTED
        on a row that is already DISPATCHED, causing a duplicate provider call.
        """
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-CR", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([_make_stream_record(dispatch_id="DISP-CR-001")]), None)

        # get_item must have been called with ConsistentRead=True
        get_item_call_kwargs = patched_ddb.get_item.call_args.kwargs
        assert get_item_call_kwargs.get("ConsistentRead") is True, (
            "get_item pre-check must use ConsistentRead=True (F4.5 / W3). "
            "Mutation guard: remove ConsistentRead from the call and this test fails."
        )


# ---------------------------------------------------------------------------
# F4.3: batchItemFailures return value
# ---------------------------------------------------------------------------

class TestBatchItemFailures:
    """F4.3 (W1): Handler returns batchItemFailures for records that failed.

    reportBatchItemFailures is declared on the event source. A handler
    returning None means complete batch success — Lambda advances the
    checkpoint and the DLQ never engages. Any failed record must be reported
    in batchItemFailures so Lambda retries only that record.

    Mutation guard: return None instead of batchItemFailures → test MUST fail.
    """

    def test_failed_record_reported_in_batch_item_failures(self, patched_ddb):
        """A record whose processing failed must be in batchItemFailures."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = RuntimeError("boom")

        # The write also fails (simulates DynamoDB unavailability on the FAILED path)
        from botocore.exceptions import ClientError
        patched_ddb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )

        record = _make_stream_record(dispatch_id="DISP-FAIL-001")
        # Add a kinesis/DynamoDB sequence number so the handler can include it
        record["dynamodb"]["SequenceNumber"] = "000001"

        result = _h.handler(_make_event([record]), None)

        assert result is not None, (
            "Handler must return a response object (not None) when records fail. "
            "Lambda treats None as 'complete success' and advances the checkpoint. "
            "Mutation guard: return None → this assertion fails."
        )
        assert "batchItemFailures" in result, (
            "Response must contain 'batchItemFailures'. "
            "Without it, the failed record is silently discarded."
        )
        failures = result["batchItemFailures"]
        assert len(failures) >= 1, "At least one failure must be reported."
        assert any("itemIdentifier" in f for f in failures), (
            "Each batchItemFailure must have an 'itemIdentifier'."
        )

    def test_successful_batch_returns_empty_batch_item_failures(self, patched_ddb):
        """A fully successful batch returns empty batchItemFailures (or None).

        Lambda interprets both an empty list and None as complete success.
        The important thing is that a successful record is NOT reported as failed.
        """
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-OK", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            result = _h.handler(_make_event([_make_stream_record()]), None)

        # Either None or {"batchItemFailures": []} is acceptable for a clean batch
        if result is not None:
            failures = result.get("batchItemFailures", [])
            assert failures == [], (
                f"A successful batch must not report any failures, got {failures!r}."
            )


# ---------------------------------------------------------------------------
# F4.1: moto-backed tests — write path succeeds against real DynamoDB
# ---------------------------------------------------------------------------

class TestMotoWritePath:
    """F4.1 (C1): Write path must succeed against a real (moto) DynamoDB table.

    A MagicMock accepts any request including ones DynamoDB rejects. The bug
    fixed by F4.1 was that each update_item passed a boto3 Attr(...) condition
    *and* hand-built ExpressionAttributeValues; boto3 generates its own
    placeholders, so the manual ones were unused and DynamoDB returned:
      ValidationException: Value provided in ExpressionAttributeValues
        unused in expressions

    Because the mock happily accepts this, all 24 prior tests passed while
    every real write was silently failing with ValidationException.

    Mutation guard: reintroduce the unused EAVs alongside the Attr condition
    → moto returns ValidationException → these tests FAIL.
    (A MagicMock-only test would still pass. That's the point.)
    """

    def test_happy_path_reaches_dispatched_with_moto(self, monkeypatch):
        """Real moto table: REQUESTED INSERT → DISPATCHED with providerRef/eta/simulated.

        This test uses moto instead of a MagicMock. It is the guard that would
        have caught the ValidationException before this spec shipped.
        """
        import boto3
        from moto import mock_aws

        # Use the same table name the module-level _RSA_DISPATCH_TABLE was set to
        # at import time (inject_table_env autouse sets RSA_DISPATCH_TABLE to
        # "vsa-test-rsa-dispatch" before the module loads).
        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        _h._ddb = None  # reset before mock context

        with mock_aws():
            # Create the table moto will validate against.
            # Use no explicit region so this matches _get_ddb()'s boto3.resource().
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            # Seed the REQUESTED row (simulating what /escalate would write)
            table.put_item(Item={
                "dispatchId": "DISP-MOTO-001",
                "status": "REQUESTED",
                "vin": "1FMCU0F70MUA12345",
                "lat": "37.7749",
                "lon": "-122.4194",
                "serviceType": "tow",
                "tenantId": "meridian",
                "callbackContact": "+15551234567",
            })

            # Reset the lazy DDB singleton so it re-creates inside the mock context
            _h._ddb = None

            provider_mock = MagicMock()
            provider_mock.request_dispatch.return_value = MagicMock(
                provider_ref="SIM-MOTO-ABCDEF",
                eta_minutes=19,
                simulated=True,
                status="DISPATCHED",
            )

            record = _make_stream_record(dispatch_id="DISP-MOTO-001")
            with patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler(_make_event([record]), None)

            # Verify the row actually reached DISPATCHED in the real table
            item = table.get_item(Key={"dispatchId": "DISP-MOTO-001"})["Item"]
            assert item["status"] == "DISPATCHED", (
                f"Row must reach DISPATCHED, got {item['status']!r}. "
                "Mutation guard: reintroduce unused EAVs → DynamoDB raises "
                "ValidationException → status stays REQUESTED → this fails."
            )
            assert "providerRef" in item, "providerRef must be written to the row."
            assert "etaMinutes" in item, "etaMinutes must be written to the row."
            assert item.get("simulated") is True, "simulated must be written as True."

        # Reset singleton after mock context
        _h._ddb = None

    def test_provider_error_reaches_failed_with_moto(self, monkeypatch):
        """Real moto table: provider raises → row reaches FAILED with a reason.

        This is Accept 2 verified against moto. The old _write_failed had the same
        unused-EAV bug and only logged, so the DISPATCHED write's failure was
        swallowed and the row stayed REQUESTED — the dead letterbox reintroduced
        one layer up.
        """
        import boto3
        from moto import mock_aws

        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        # Ensure singleton is reset before entering mock context
        _h._ddb = None

        with mock_aws():
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.put_item(Item={
                "dispatchId": "DISP-MOTO-FAIL-001",
                "status": "REQUESTED",
                "vin": "1FMCU0F70MUA12345",
                "lat": "37.7749",
                "lon": "-122.4194",
                "serviceType": "tow",
                "tenantId": "meridian",
                "callbackContact": "+15551234567",
            })

            _h._ddb = None

            provider_mock = MagicMock()
            provider_mock.request_dispatch.side_effect = RuntimeError("provider down")

            record = _make_stream_record(dispatch_id="DISP-MOTO-FAIL-001")
            with patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler(_make_event([record]), None)

            item = table.get_item(Key={"dispatchId": "DISP-MOTO-FAIL-001"})["Item"]
            assert item["status"] == "FAILED", (
                f"Row must reach FAILED after provider error, got {item['status']!r}. "
                "The old _write_failed had the same EAV bug and only logged, "
                "leaving the row in REQUESTED — the dead letterbox."
            )
            assert "failureReason" in item, "failureReason must be written on FAILED."

        _h._ddb = None


# ---------------------------------------------------------------------------
# F4.2: Producer/consumer field contract + fail closed on absent lat/lon
# ---------------------------------------------------------------------------

class TestProducerConsumerContract:
    """F4.2 (C2): Producer must write serviceType and callbackContact.
    Consumer must fail closed on absent/invalid lat/lon.

    The Critical: _queue_rsa_action wrote dispatchId/status/vin/reason/etc
    but NOT lat, lon, serviceType, or callbackContact. The consumer's
    float(row.get("lat", 0)) turned absence into valid-looking 0.0, so a
    dispatch silently went to lat=0, lon=0 (Gulf of Guinea).

    Mutation guards:
      - Drop serviceType from producer → a test MUST fail.
      - Set lat/lon absent → fail-closed test MUST show FAILED, not dispatch.
    """

    def test_consumer_fails_closed_on_absent_lat(self, patched_ddb):
        """When lat is absent from the row, consumer writes FAILED, not dispatch.

        Dispatching to lat=0, lon=0 is worse than a refusal — it sends a truck
        to the Gulf of Guinea at the driver's expense.
        """
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        # Row missing lat/lon (exactly what _queue_rsa_action was writing)
        record = {
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "dispatchId": {"S": "DISP-NO-LAT"},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                    # lat and lon intentionally absent
                }
            },
        }

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        # Provider must NOT have been called — we should fail closed
        provider_mock.request_dispatch.assert_not_called()

        # FAILED must have been written
        assert patched_ddb.update_item.call_count >= 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", (
            f"Consumer must write FAILED when lat/lon is absent, not dispatch to (0,0). "
            f"Got status {ev.get(':failed')!r}. "
            "Mutation guard: drop this fail-closed check → provider is called with "
            "lat=0.0, lon=0.0 and status becomes DISPATCHED → this assertion fails."
        )
        reason = ev.get(":reason", "")
        assert reason, "FAILED write must include a reason for the refusal."

    def test_consumer_fails_closed_on_absent_lon(self, patched_ddb):
        """When lon is absent from the row, consumer writes FAILED, not dispatch."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        record = {
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "dispatchId": {"S": "DISP-NO-LON"},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "lat": {"S": "37.7749"},
                    # lon absent
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                }
            },
        }

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        assert patched_ddb.update_item.call_count >= 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED"

    def test_producer_writes_service_type(self, monkeypatch):
        """_queue_rsa_action must write serviceType to the dispatch row.

        The consumer reads serviceType; if the producer omits it, the provider
        receives service_type='' and cannot book the correct service.

        Mutation guard: drop serviceType from the put_item item dict →
        assert 'serviceType' in item fails.
        """
        import importlib.util as _iu

        escalate_path = Path(__file__).resolve().parent.parent.parent / "api-escalate" / "handler.py"

        # api-escalate requires TENANT_CONFIG_TABLE, RSA_DISPATCH_TABLE, and
        # LIVE_STATE_FN_NAME at import time.
        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("CMS_STAGE", "test")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")

        spec = _iu.spec_from_file_location("_escalate_for_f42_svc", escalate_path)
        eh = _iu.module_from_spec(spec)
        sys.modules["_escalate_for_f42_svc"] = eh
        try:
            spec.loader.exec_module(eh)

            ddb_mock = MagicMock()
            table_mock = MagicMock()
            ddb_mock.Table.return_value = table_mock
            table_mock.put_item.return_value = {}
            table_mock.get_item.return_value = {}  # no driver phone

            with patch.object(eh, "_get_ddb", return_value=ddb_mock):
                eh._queue_rsa_action(
                    tenant_id="meridian",
                    vin="1FMCU0F70MUA12345",
                    vehicle_id="VEH-001",
                    driver_id=None,
                    display_name=None,
                    reason="flat tire",
                    summary=None,
                    triage_session_id=None,
                    contact_id="CTX-001",
                    voice_session_id=None,
                )

            assert table_mock.put_item.called, "_queue_rsa_action must call put_item"
            call_args = table_mock.put_item.call_args
            item_written = call_args.kwargs.get("Item") or call_args[1].get("Item") or {}
            assert "serviceType" in item_written, (
                "Producer must write 'serviceType' to the dispatch row. "
                "Mutation guard: drop serviceType from the item dict → this fails."
            )
        finally:
            del sys.modules["_escalate_for_f42_svc"]

    def test_producer_writes_callback_contact(self, monkeypatch):
        """_queue_rsa_action must write callbackContact when driver phone is available."""
        import importlib.util as _iu

        escalate_path = Path(__file__).resolve().parent.parent.parent / "api-escalate" / "handler.py"

        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("CMS_STAGE", "test")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")

        spec = _iu.spec_from_file_location("_escalate_for_f42_cb", escalate_path)
        eh = _iu.module_from_spec(spec)
        sys.modules["_escalate_for_f42_cb"] = eh
        try:
            spec.loader.exec_module(eh)

            ddb_mock = MagicMock()
            table_mock = MagicMock()
            ddb_mock.Table.return_value = table_mock
            table_mock.put_item.return_value = {}
            # Simulate driver phone lookup succeeding. The driver must be
            # assigned to the vehicle: /escalate only uses the phone of the
            # vehicle's assigned driver (escalation spec, security W1 callback fix).
            table_mock.get_item.return_value = {
                "Item": {"phone": "+15559876543", "assignedVehicleId": "VEH-001"}
            }

            with patch.object(eh, "_get_ddb", return_value=ddb_mock):
                eh._queue_rsa_action(
                    tenant_id="meridian",
                    vin="1FMCU0F70MUA12345",
                    vehicle_id="VEH-001",
                    driver_id="DRV-001",
                    display_name="Samantha",
                    reason="flat tire",
                    summary=None,
                    triage_session_id=None,
                    contact_id="CTX-001",
                    voice_session_id=None,
                )

            # put_item may be called twice if drivers table is also used for other things
            # Find the call where dispatchId is in the item (the RSA dispatch row)
            put_items = [
                c.kwargs.get("Item") or (c.args[0] if c.args else {})
                for c in table_mock.put_item.call_args_list
            ]
            dispatch_items = [i for i in put_items if "dispatchId" in i]
            assert dispatch_items, "_queue_rsa_action must call put_item with a dispatchId"
            item_written = dispatch_items[0]
            assert "callbackContact" in item_written, (
                "Producer must write 'callbackContact' when a driver phone is resolved. "
                "Mutation guard: drop callbackContact from item → this fails."
            )
        finally:
            del sys.modules["_escalate_for_f42_cb"]


# ---------------------------------------------------------------------------
# F4.8: moto-backed terminal-state condition semantics (W6)
# ---------------------------------------------------------------------------

class TestTerminalStateConditionSemantics:
    """F4.8 (W6): DynamoDB actually rejects ARRIVED→DISPATCHED due to ConditionExpression.

    The prior tests (TestTerminalStateConditionExpression) asserted:
      - 'ConditionExpression' is in the call kwargs (presence)
      - The handler respects a hand-raised ConditionalCheckFailedException (mock)

    These pass even if the ConditionExpression is 'Attr("status").ne("BANANA")',
    because neither test evaluates the condition against real data.

    F4.8 uses moto: seed an ARRIVED row, run the handler, and assert DynamoDB
    rejects the DISPATCHED write (the row stays ARRIVED).

    Mutation guard: replace the condition with Attr("status").ne("BANANA") →
    moto accepts the ARRIVED→DISPATCHED write → item["status"] == "DISPATCHED"
    → this test FAILS.
    Depends on F4.1 (the condition must first be well-formed for DDB to accept
    the request at all).
    """

    def test_moto_rejects_arrived_to_dispatched(self, monkeypatch):
        """DynamoDB (moto) rejects writing DISPATCHED onto an ARRIVED row.

        F4.8: the ConditionExpression must semantically refuse terminal→DISPATCHED,
        not just be present. Substituting Attr("status").ne("BANANA") passes
        the presence test but allows the write — moto catches it.

        This test directly calls update_item with the handler's condition against
        a real moto table, bypassing the stream-level REQUESTED check that would
        otherwise skip the ARRIVED row before the write.

        Mutation guard: replace _build_dispatched_write_condition() with
        Attr("status").ne("BANANA") → moto accepts the write → status becomes
        DISPATCHED → this test FAILS.
        """
        import boto3
        from botocore.exceptions import ClientError
        from moto import mock_aws

        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        _h._ddb = None  # reset before mock context

        with mock_aws():
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            # Seed an ARRIVED row (terminal state)
            table.put_item(Item={
                "dispatchId": "DISP-ARRIVED-COND-001",
                "status": "ARRIVED",
            })

            _h._ddb = None

            # Directly attempt to write DISPATCHED onto the ARRIVED row using
            # the handler's own condition. This bypasses the stream-level
            # REQUESTED-check that would skip the row before the write happens.
            from boto3.dynamodb.conditions import Attr
            condition = _h._build_dispatched_write_condition()
            try:
                _h._get_ddb().Table(table_name).update_item(
                    Key={"dispatchId": "DISP-ARRIVED-COND-001"},
                    UpdateExpression="SET #st = :dispatched",
                    ConditionExpression=condition,
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={":dispatched": "DISPATCHED"},
                )
                # If we get here, DynamoDB accepted the write — the condition is wrong
                item = table.get_item(Key={"dispatchId": "DISP-ARRIVED-COND-001"})["Item"]
                assert False, (
                    f"DynamoDB accepted ARRIVED→DISPATCHED (status is now {item['status']!r}). "
                    "The ConditionExpression must refuse this write. "
                    "Mutation guard: replace condition with Attr('status').ne('BANANA') "
                    "→ DynamoDB accepts the write → status becomes DISPATCHED → FAIL."
                )
            except ClientError as exc:
                assert exc.response["Error"]["Code"] == "ConditionalCheckFailedException", (
                    f"Expected ConditionalCheckFailedException, got {exc.response['Error']['Code']!r}. "
                    "The condition must specifically fail because the row is in a terminal state."
                )

            # Verify the row is still ARRIVED
            item = table.get_item(Key={"dispatchId": "DISP-ARRIVED-COND-001"})["Item"]
            assert item["status"] == "ARRIVED", (
                f"Row must still be ARRIVED after the condition blocked the write, "
                f"got {item['status']!r}."
            )

        _h._ddb = None

    def test_moto_accepts_requested_to_dispatched(self, monkeypatch):
        """DynamoDB (moto) accepts writing DISPATCHED onto a REQUESTED row."""
        import boto3
        from moto import mock_aws

        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        _h._ddb = None  # reset before mock context

        with mock_aws():
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.put_item(Item={
                "dispatchId": "DISP-REQD-001",
                "status": "REQUESTED",
                "vin": "1FMCU0F70MUA12345",
                "lat": "37.7749",
                "lon": "-122.4194",
                "serviceType": "tow",
                "tenantId": "meridian",
                "callbackContact": "+15551234567",
            })

            _h._ddb = None

            provider_mock = MagicMock()
            provider_mock.request_dispatch.return_value = MagicMock(
                provider_ref="SIM-REQD-ANTI",
                eta_minutes=19,
                simulated=True,
                status="DISPATCHED",
            )

            record = _make_stream_record(dispatch_id="DISP-REQD-001")
            with patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler(_make_event([record]), None)

            item = table.get_item(Key={"dispatchId": "DISP-REQD-001"})["Item"]
            assert item["status"] == "DISPATCHED", (
                f"DynamoDB must accept REQUESTED→DISPATCHED transition, "
                f"but status is {item['status']!r}. "
                "Anti-vacuity: the terminal guard must not block legitimate writes."
            )

        _h._ddb = None


# ---------------------------------------------------------------------------
# Additional correctness
# ---------------------------------------------------------------------------

class TestProviderCallArguments:
    """The provider is called with the correct arguments from the DDB row."""

    def test_provider_receives_correct_dispatch_id(self, patched_ddb):
        """The dispatch_id passed to the provider is the table's PK."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(
                _make_event([_make_stream_record(dispatch_id="DISP-SPECIFIC-ID")]),
                None,
            )

        _, kwargs = provider_mock.request_dispatch.call_args
        assert kwargs.get("dispatch_id") == "DISP-SPECIFIC-ID"

    def test_empty_records_list_is_no_op(self, patched_ddb):
        """An event with no records does nothing."""
        with patch.object(_h, "get_client") as mock_get_client:
            _h.handler({"Records": []}, None)
            mock_get_client.assert_not_called()
        patched_ddb.update_item.assert_not_called()

    def test_multiple_distinct_records_processed(self, patched_ddb):
        """Multiple distinct REQUESTED records are all processed."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-MULTI", eta_minutes=20, simulated=True, status="DISPATCHED"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(
                _make_event([
                    _make_stream_record(dispatch_id="DISP-001"),
                    _make_stream_record(dispatch_id="DISP-002"),
                ]),
                None,
            )

        assert provider_mock.request_dispatch.call_count == 2
        assert patched_ddb.update_item.call_count == 2


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------

class TestEnvironmentValidation:
    """RSA_DISPATCH_TABLE must be set — no default (fail-closed)."""

    def test_missing_table_env_raises_at_init(self, monkeypatch):
        """If RSA_DISPATCH_TABLE is not set, module reload raises KeyError."""
        monkeypatch.delenv("RSA_DISPATCH_TABLE", raising=False)
        # Remove the cached module so spec_from_file_location re-executes the module body
        if "rsa_consumer_handler" in sys.modules:
            del sys.modules["rsa_consumer_handler"]

        with pytest.raises(KeyError):
            _load_handler()

        # Restore for subsequent tests
        os.environ["RSA_DISPATCH_TABLE"] = "vsa-test-rsa-dispatch"
        _load_handler()



# ---------------------------------------------------------------------------
# F5.D4: Producer→consumer round-trip test (Cycle 5 Critical 2)
# ---------------------------------------------------------------------------

class TestProducerConsumerRoundTrip:
    """F5.D4 (C2 remainder): The real _queue_rsa_action → real consumer round-trip.

    F4.2 Accept 3 required "a moto-backed test writes a row via the real producer
    function and asserts the consumer's behaviour on it — not a hand-built fixture."
    It was not written, which is why the 'every dispatch ends in FAILED' defect
    survived 35 green tests. This is that test.

    Two cases:
      FRESH: producer resolves a fresh position → row has lat/lon → consumer dispatches
      STALE: producer resolves a stale position → row has no lat/lon → consumer fails closed

    Mutation guard: drop `lat` from the producer's write → DISPATCHED case MUST fail.
    Constraint: NO hand-built row — the real _queue_rsa_action must be the source.
    """

    def _load_escalate_handler(self):
        """Load api-escalate/handler.py under a unique module name for this round-trip."""
        mod_name = "_escalate_for_roundtrip"
        if mod_name in sys.modules:
            return sys.modules[mod_name]
        escalate_path = _LAMBDA_ROOT.parent / "api-escalate" / "handler.py"
        spec = importlib.util.spec_from_file_location(mod_name, escalate_path)
        eh = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = eh
        spec.loader.exec_module(eh)
        return eh

    def _make_stream_record_from_item(self, item: dict) -> dict:
        """Convert a put_item Item dict to a DynamoDB stream INSERT record shape."""
        def _typed(v):
            if isinstance(v, bool):
                return {"BOOL": v}
            if isinstance(v, (int, float)):
                return {"N": str(v)}
            return {"S": str(v)}

        return {
            "eventName": "INSERT",
            "dynamodb": {
                "SequenceNumber": "999001",
                "NewImage": {k: _typed(v) for k, v in item.items()},
            },
        }

    def test_fresh_position_round_trip_reaches_dispatched(self, monkeypatch):
        """Real producer (fresh live-state stub) → real moto table → real consumer → DISPATCHED.

        This is the test that would have caught the dead-letterbox defect:
        no hand-built fixture, no mock table for the producer's write.

        Mutation guard: drop `lat` from the producer's write (e.g. by removing
        the position resolution call entirely) → item has no lat/lon →
        consumer fails closed → status stays FAILED → assertion fails.
        """
        import io
        import boto3
        from moto import mock_aws

        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("CMS_STAGE", "test")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")

        table_name = _h._RSA_DISPATCH_TABLE

        # Stub live-state to return a fresh position.
        fresh_response_body = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 30,
            "redisReachable": True,
            "locationSource": "redis",   # F6.1: required for the escalate gate
            "vin": "1FMCU0F70MUA12345",  # F7.2: must match the vin passed to _queue_rsa_action
        }
        fresh_envelope = {"statusCode": 200, "body": json.dumps(fresh_response_body)}
        payload_bytes = json.dumps(fresh_envelope).encode("utf-8")

        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = {"Payload": io.BytesIO(payload_bytes)}

        eh = self._load_escalate_handler()

        _h._ddb = None
        eh._ddb = None
        _h._lambda_client = None
        eh._lambda_client = None

        with mock_aws():
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

            # Reset lazy singletons inside the mock context.
            _h._ddb = None
            eh._ddb = None

            # --- PRODUCER: call the real _queue_rsa_action (live-state stubbed) ---
            captured_items: list[dict] = []
            original_put_item = None

            def capturing_put_item(Item):
                captured_items.append(Item)
                # Also write to the real moto table so the consumer can read it.
                table.put_item(Item=Item)

            # We need the producer to write to the real moto table.
            # Use patch.object to intercept the DDB Table().put_item call, forward to moto.
            from unittest.mock import patch as _patch

            with _patch.object(eh, "_get_lambda", return_value=lambda_mock), \
                 _patch.object(eh, "_resolve_driver_phone", return_value=None):
                # Call with a real ddb resource that points at moto.
                with _patch.object(eh, "_get_ddb", return_value=ddb):
                    dispatch_id = eh._queue_rsa_action(
                        tenant_id="meridian",
                        vin="1FMCU0F70MUA12345",
                        vehicle_id="VEH-ROUNDTRIP-001",
                        driver_id=None,
                        display_name=None,
                        reason="flat tire",
                        summary=None,
                        triage_session_id=None,
                        contact_id="cnt-ROUNDTRIP",
                        voice_session_id=None,
                    )

            assert dispatch_id is not None, (
                "Producer must return a dispatchId. "
                "Check that _queue_rsa_action completed successfully."
            )

            # Verify the producer wrote lat/lon (proving live-state stub was used).
            produced_item = table.get_item(Key={"dispatchId": dispatch_id})["Item"]
            print(f"\nPRODUCER row keys : {sorted(produced_item.keys())}")
            print(f"  lat present? {('lat' in produced_item)} | lon present? {'lon' in produced_item}")

            assert "lat" in produced_item, (
                "Producer must write lat to the dispatch row when live-state returns a fresh position. "
                "Mutation guard: drop lat from producer write → this assertion fails and the round-trip "
                "test catches the defect that survived 35 tests in F4."
            )
            assert "lon" in produced_item, "Producer must write lon when position is fresh"

            # --- CONSUMER: feed the real producer row to the real consumer ---
            _h._ddb = None  # reset so consumer re-creates inside mock context
            stream_record = self._make_stream_record_from_item(produced_item)

            provider_mock = MagicMock()
            provider_mock.request_dispatch.return_value = MagicMock(
                provider_ref="SIM-ROUNDTRIP-ABCDEF",
                eta_minutes=19,
                simulated=True,
                status="DISPATCHED",
            )

            with _patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler({"Records": [stream_record]}, None)

            # Verify the row reached DISPATCHED in the real table.
            final_item = table.get_item(Key={"dispatchId": dispatch_id})["Item"]
            print(f"--- RESULT ---")
            print(f"provider called : {provider_mock.request_dispatch.called}")
            print(f"terminal status : {final_item['status']}")

            assert final_item["status"] == "DISPATCHED", (
                f"Round-trip must reach DISPATCHED, got {final_item['status']!r}. "
                f"failureReason: {final_item.get('failureReason', '(none)')}. "
                "This test is the guard that would have caught the dead-letterbox defect. "
                "Mutation guard: drop lat from the producer write → no lat/lon in row → "
                "consumer fails closed → status stays FAILED → this assertion fails."
            )
            assert "providerRef" in final_item, "providerRef must be written on DISPATCHED"
            assert "etaMinutes" in final_item, "etaMinutes must be written on DISPATCHED"
            assert final_item.get("simulated") is True, "simulated must be True for fake provider"
            assert provider_mock.request_dispatch.called, "Provider must have been called"

        _h._ddb = None
        eh._ddb = None

    def test_stale_position_round_trip_reaches_failed(self, monkeypatch):
        """Real producer (stale live-state stub) → real moto table → real consumer → FAILED.

        When the live-state position is stale, the producer writes no lat/lon.
        The consumer's fail-closed path then writes FAILED with the location reason.
        This proves the second case of F5.D4's Accept criteria.
        """
        import io
        import boto3
        from moto import mock_aws

        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("TENANT_CONFIG_TABLE", "test-tenant-config")
        monkeypatch.setenv("CMS_STAGE", "test")
        monkeypatch.setenv("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")

        table_name = _h._RSA_DISPATCH_TABLE

        # Stub live-state to return a STALE position (age > 900s threshold).
        stale_response_body = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "lastSeenAgoSeconds": 3600,  # 1 hour — exceeds 900s default
            "redisReachable": True,
            "locationSource": "redis",   # F6.1: locationSource present; stale by age
        }
        stale_envelope = {"statusCode": 200, "body": json.dumps(stale_response_body)}
        payload_bytes = json.dumps(stale_envelope).encode("utf-8")

        lambda_mock = MagicMock()
        lambda_mock.invoke.return_value = {"Payload": io.BytesIO(payload_bytes)}

        eh = self._load_escalate_handler()

        _h._ddb = None
        eh._ddb = None

        from unittest.mock import patch as _patch

        with mock_aws():
            ddb = boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

            _h._ddb = None
            eh._ddb = None

            with _patch.object(eh, "_get_lambda", return_value=lambda_mock), \
                 _patch.object(eh, "_resolve_driver_phone", return_value=None), \
                 _patch.object(eh, "_get_ddb", return_value=ddb):
                dispatch_id = eh._queue_rsa_action(
                    tenant_id="meridian",
                    vin="1FMCU0F70MUA12345",
                    vehicle_id="VEH-STALE-001",
                    driver_id=None,
                    display_name=None,
                    reason="flat tire",
                    summary=None,
                    triage_session_id=None,
                    contact_id="cnt-STALE",
                    voice_session_id=None,
                )

            assert dispatch_id is not None, "Producer must write the row even when position is stale"

            produced_item = table.get_item(Key={"dispatchId": dispatch_id})["Item"]
            assert "lat" not in produced_item, (
                "Stale position must not be written to the row"
            )

            # Consumer: feed the row (no lat/lon) to the consumer.
            _h._ddb = None
            stream_record = self._make_stream_record_from_item(produced_item)

            provider_mock = MagicMock()
            with _patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler({"Records": [stream_record]}, None)

            final_item = table.get_item(Key={"dispatchId": dispatch_id})["Item"]
            assert final_item["status"] == "FAILED", (
                f"Consumer must fail closed when lat/lon absent, got {final_item['status']!r}. "
                "A stale position must not result in a dispatch."
            )
            assert not provider_mock.request_dispatch.called, (
                "Provider must NOT be called when lat/lon is absent"
            )
            assert "failureReason" in final_item, "failureReason must be written on FAILED"

        _h._ddb = None
        eh._ddb = None


# ---------------------------------------------------------------------------
# F5.5: batchItemFailures when SequenceNumber is absent
# ---------------------------------------------------------------------------

class TestBatchItemFailuresWithoutSequenceNumber:
    """F5.5 (W): A failing record with no SequenceNumber must not produce None.

    Today (before fix): `if seq:` gates the append, so absent SequenceNumber →
    handler logs and returns None → Lambda treats batch as fully succeeded →
    checkpoint advances → row stays REQUESTED permanently.

    After fix: when SequenceNumber is absent, raise RuntimeError so Lambda
    retries the whole batch rather than silently discarding the record.

    Mutation guard: restore the None return → test MUST fail.
    """

    def test_no_sequence_number_raises_not_returns_none(self):
        """A failing record with no SequenceNumber raises, not returns None.

        DynamoDB Streams always sets SequenceNumber, so this path is unreachable
        with well-formed input. But if it fires, raising is strictly safer than
        returning None (batch success) — a retry is better than a silent drop.

        Mutation guard: restore `return None` after the no-seq branch →
        result would be None → the None assertion below would FAIL.
        """
        from botocore.exceptions import ClientError

        # Build a record that will fail processing (provider raises) but has no SequenceNumber.
        record_no_seq = {
            "eventName": "INSERT",
            "dynamodb": {
                # No SequenceNumber key
                "NewImage": {
                    "dispatchId": {"S": "DISP-NOSEQ-001"},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "lat": {"S": "37.7749"},
                    "lon": {"S": "-122.4194"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                },
            },
        }

        mock_ddb_table = MagicMock()
        # Both the provider call and the FAILED write will raise.
        mock_ddb_table.get_item.return_value = {"Item": {}}
        mock_ddb_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )
        mock_ddb_resource = MagicMock()
        mock_ddb_resource.Table.return_value = mock_ddb_table

        provider_mock = MagicMock()
        provider_mock.request_dispatch.side_effect = RuntimeError("boom")

        with patch.object(_h, "_get_ddb", return_value=mock_ddb_resource), \
             patch.object(_h, "get_client", return_value=provider_mock):
            # The handler should raise (not return None) when SequenceNumber is absent
            # and the record processing failed.
            with pytest.raises(RuntimeError) as exc_info:
                _h.handler({"Records": [record_no_seq]}, None)

        assert "SequenceNumber" in str(exc_info.value) or "retry" in str(exc_info.value).lower(), (
            "The raised error should explain why we're forcing a retry. "
            "Mutation guard: restore `return None` → handler returns None, "
            "no exception raised → pytest.raises fails → this test fails."
        )


# ---------------------------------------------------------------------------
# F5.7b: Mixed-batch test (one success, one failure)
# ---------------------------------------------------------------------------

class TestMixedBatch:
    """F5.7b: A batch with one succeeding and one failing record.

    Accept: the failure is in batchItemFailures and the success is absent.
    Behaviour was verified by hand in Cycle 5 but was never pinned.
    """

    def test_mixed_batch_only_failing_record_in_failures(self):
        """One good record + one bad record → only the bad one is in batchItemFailures.

        The good record must NOT appear in batchItemFailures — Lambda would retry
        it unnecessarily and could produce a duplicate provider call.
        """
        from botocore.exceptions import ClientError

        good_seq = "SEQ-GOOD-001"
        bad_seq = "SEQ-BAD-002"

        good_record = _make_stream_record(
            dispatch_id="DISP-GOOD-001",
            status="REQUESTED",
        )
        good_record["dynamodb"]["SequenceNumber"] = good_seq

        bad_record = _make_stream_record(
            dispatch_id="DISP-BAD-001",
            status="REQUESTED",
        )
        bad_record["dynamodb"]["SequenceNumber"] = bad_seq

        call_count = {"n": 0}

        mock_ddb_table = MagicMock()

        def side_effect_update_item(**kwargs):
            """Succeed for the good record, fail for the bad one."""
            dispatch_id = kwargs.get("Key", {}).get("dispatchId", "")
            if dispatch_id == "DISP-BAD-001":
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
                    "UpdateItem",
                )
            return {}

        mock_ddb_table.get_item.return_value = {"Item": {}}
        mock_ddb_table.update_item.side_effect = side_effect_update_item
        mock_ddb_resource = MagicMock()
        mock_ddb_resource.Table.return_value = mock_ddb_table

        good_provider = MagicMock()
        good_provider.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-GOOD-ABC",
            eta_minutes=20,
            simulated=True,
            status="DISPATCHED",
        )

        bad_provider = MagicMock()
        bad_provider.request_dispatch.side_effect = RuntimeError("provider down for bad")

        provider_call_count = {"n": 0}

        def side_effect_get_client():
            # Both records call get_client; we need to differentiate by which
            # record is being processed. Since we can't easily intercept at the
            # record level, we return a provider that succeeds on first call and
            # fails on second.
            provider_call_count["n"] += 1
            if provider_call_count["n"] <= 1:
                return good_provider
            return bad_provider

        with patch.object(_h, "_get_ddb", return_value=mock_ddb_resource), \
             patch.object(_h, "get_client", side_effect=side_effect_get_client):
            result = _h.handler({"Records": [good_record, bad_record]}, None)

        # The bad record must be reported in batchItemFailures.
        assert result is not None, (
            "Handler must return a response when at least one record fails. "
            "Mutation guard: return None instead of batchItemFailures → this assertion fails."
        )
        failures = result.get("batchItemFailures", [])
        failure_seqs = {f["itemIdentifier"] for f in failures}

        assert bad_seq in failure_seqs, (
            f"The failing record (seq={bad_seq!r}) must be in batchItemFailures. "
            f"Got: {failure_seqs!r}"
        )
        assert good_seq not in failure_seqs, (
            f"The succeeding record (seq={good_seq!r}) must NOT be in batchItemFailures. "
            "Lambda would retry it unnecessarily, risking a duplicate provider call. "
            f"Got: {failure_seqs!r}"
        )



# ---------------------------------------------------------------------------
# F6.4: Consumer-side coordinate range validation
# ---------------------------------------------------------------------------


class TestConsumerCoordinateRangeValidation:
    """F6.4 (consumer side): abs(lat) <= 90 and abs(lon) <= 180 enforced in consumer.

    The consumer is the second line of defence. The producer should refuse out-of-range
    coordinates before writing them, but the consumer must independently validate to
    protect against bugs in the producer or direct DynamoDB writes.

    Mutation guard: remove the producer-side range check in api-escalate →
    a consumer-side test MUST still catch it. This class provides those consumer tests.
    """

    def _make_record(self, lat: str, lon: str, dispatch_id: str = "DISP-RANGE-001") -> dict:
        return {
            "eventName": "INSERT",
            "dynamodb": {
                "SequenceNumber": f"seq-{dispatch_id}",
                "NewImage": {
                    "dispatchId": {"S": dispatch_id},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                    "lat": {"S": lat},
                    "lon": {"S": lon},
                },
            },
        }

    def test_consumer_fails_closed_on_out_of_range_lat(self, patched_ddb):
        """lat=999.0 must be refused by the consumer (write FAILED, not dispatch).

        Mutation guard: remove the consumer-side range check → provider is called
        with lat=999, this test fails because provider WOULD be called.
        """
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        record = self._make_record(lat="999.0", lon="-122.4194", dispatch_id="DISP-OOB-LAT")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        assert patched_ddb.update_item.call_count >= 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", (
            f"Consumer must write FAILED when lat=999.0 (out of range). "
            f"Got status {ev.get(':failed')!r}. "
            "Mutation guard: remove range check → provider is called, status becomes DISPATCHED."
        )
        reason = ev.get(":reason", "")
        assert reason, "FAILED write must include a reason mentioning range"

    def test_consumer_fails_closed_on_out_of_range_lon(self, patched_ddb):
        """lon=-999.0 must be refused by the consumer (write FAILED, not dispatch)."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        record = self._make_record(lat="37.7749", lon="-999.0", dispatch_id="DISP-OOB-LON")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", (
            "Consumer must write FAILED when lon=-999.0 (out of range)."
        )

    def test_consumer_accepts_boundary_coordinates(self, patched_ddb):
        """lat=90.0, lon=180.0 are valid boundary values; consumer must accept them."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-BOUNDARY", eta_minutes=15, simulated=True, status="DISPATCHED",
        )

        record = self._make_record(lat="90.0", lon="180.0", dispatch_id="DISP-BOUNDARY")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_called_once()
        assert provider_mock.request_dispatch.call_count == 1, (
            "Provider must be called for boundary coordinates lat=90, lon=180 (valid range)"
        )

    def test_consumer_zero_zero_still_refused(self, patched_ddb):
        """(0, 0) is still refused (existing guard; gains explicit test per F6.4)."""
        provider_mock = MagicMock()
        provider_mock.request_dispatch.return_value = MagicMock(
            provider_ref="SIM-X", eta_minutes=20, simulated=True, status="DISPATCHED",
        )

        record = self._make_record(lat="0.0", lon="0.0", dispatch_id="DISP-ZERO")

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", (
            "Consumer must write FAILED for lat=0.0/lon=0.0 (Gulf of Guinea). "
            "This is the existing (0,0) guard gaining a named test per F6.4."
        )


# ---------------------------------------------------------------------------
# F7.3: Unparseable coordinate branch — consumer side
# ---------------------------------------------------------------------------

class TestConsumerUnparseableCoordinates:
    """F7.3 (W6-5 remainder): float()-raises branch in the consumer is exercised.

    F6.4 added the range check and a (0,0) test.  The ValueError branch
    (non-numeric lat or lon in the row) was still uncovered on the consumer side.

    Mutation guard: swallow the parse error and continue (return lat=0.0) →
    the provider would be called (then refused by the (0,0) guard), but the
    fail reason would be different, so the assertion on the FAILED reason MUST fail.
    """

    def _make_record_non_numeric(self, lat: str, lon: str, dispatch_id: str) -> dict:
        return {
            "eventName": "INSERT",
            "dynamodb": {
                "SequenceNumber": f"seq-{dispatch_id}",
                "NewImage": {
                    "dispatchId": {"S": dispatch_id},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                    "lat": {"S": lat},
                    "lon": {"S": lon},
                },
            },
        }

    def test_consumer_fails_closed_on_non_numeric_lat(self, patched_ddb):
        """Consumer must write FAILED when lat is non-numeric (float() raises ValueError).

        Mutation guard: swallow ValueError and continue →
        (0,0) guard may catch it but with a different reason, this assertion fails.
        """
        provider_mock = MagicMock()

        record = self._make_record_non_numeric(
            lat="not-a-number", lon="-122.4194", dispatch_id="DISP-BAD-LAT"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        assert patched_ddb.update_item.call_count >= 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", (
            "Consumer must write FAILED when lat is non-numeric. "
            "Mutation guard: swallow ValueError → wrong reason or wrong path, this FAILS."
        )
        reason = ev.get(":reason", "")
        # The reason must mention invalid/parse — not (0,0) or range — so we know
        # the ValueError branch was actually reached.
        assert "Invalid" in reason or "invalid" in reason or "lat/lon" in reason.lower(), (
            f"FAILED reason must reference parse failure (ValueError branch); "
            f"got {reason!r}. Mutation guard: swallow ValueError → (0,0) reason instead."
        )

    def test_consumer_fails_closed_on_non_numeric_lon(self, patched_ddb):
        """Consumer must write FAILED when lon is non-numeric (float() raises ValueError)."""
        provider_mock = MagicMock()

        record = self._make_record_non_numeric(
            lat="37.7749", lon="not-a-number", dispatch_id="DISP-BAD-LON"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        assert patched_ddb.update_item.call_count >= 1
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED", "Consumer must write FAILED when lon is non-numeric"
        reason = ev.get(":reason", "")
        assert "Invalid" in reason or "invalid" in reason or "lat/lon" in reason.lower(), (
            f"FAILED reason must reference parse failure; got {reason!r}"
        )



# ---------------------------------------------------------------------------
# F8.3: Consumer refusal reasons must not contain coordinate values
# ---------------------------------------------------------------------------

class TestConsumerRefusalReasonsNoCoords:
    """F8.3 (S1): Consumer refusal reason strings must not interpolate coordinate values.

    The reason string is echoed verbatim to CloudWatch by _write_failed's
    log.info("dispatchId=%s written to FAILED: %s", dispatch_id, reason).
    lat/lon values in the reason give any principal with logs:GetLogEvents on
    this group a second route to the coordinate data.

    The fix:
      - Parse failure reason: "Invalid lat/lon in row: <type_name>" (no raw value)
      - Range-violation reason: plain text without lat=.../lon=... interpolation

    Constraints:
      - Both reasons must remain distinguishable from each other (F7.3 tests depend on this).
      - Parse reason must still contain 'Invalid', 'invalid', or 'lat/lon' (F7.3 asserts that).

    MUTATION-VERIFIED (F8.3):
      Mutant: restore f"Invalid lat/lon in row: {exc}" (raw exc string) to the parse-failure reason.
      test_parse_failure_reason_has_no_raw_value → FAILED.
    """

    def _make_record_non_numeric(self, lat: str, lon: str, dispatch_id: str) -> dict:
        return {
            "eventName": "INSERT",
            "dynamodb": {
                "SequenceNumber": f"seq-{dispatch_id}",
                "NewImage": {
                    "dispatchId": {"S": dispatch_id},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                    "lat": {"S": lat},
                    "lon": {"S": lon},
                },
            },
        }

    def _make_record_numeric(self, lat: str, lon: str, dispatch_id: str) -> dict:
        return {
            "eventName": "INSERT",
            "dynamodb": {
                "SequenceNumber": f"seq-{dispatch_id}",
                "NewImage": {
                    "dispatchId": {"S": dispatch_id},
                    "status": {"S": "REQUESTED"},
                    "vin": {"S": "1FMCU0F70MUA12345"},
                    "serviceType": {"S": "tow"},
                    "tenantId": {"S": "meridian"},
                    "callbackContact": {"S": "+15551234567"},
                    "lat": {"S": lat},
                    "lon": {"S": lon},
                },
            },
        }

    def test_parse_failure_reason_has_no_raw_value(self, patched_ddb):
        """The FAILED reason for a parse error must not contain the raw coordinate string.

        The raw string from float("47.6062_EXTRA") that appears in str(exc) must not
        reach CloudWatch via the reason field.

        Mutation guard: restore f"Invalid lat/lon in row: {exc}" (raw exc string)
        → the malformed value appears in the reason, this FAILS.
        """
        MALFORMED_LAT = "47.6062_MALFORMED"
        provider_mock = MagicMock()
        record = self._make_record_non_numeric(
            lat=MALFORMED_LAT, lon="-122.4194", dispatch_id="DISP-NORAW-LAT"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED"
        reason = ev.get(":reason", "")
        # The raw malformed value must NOT appear in the reason
        assert MALFORMED_LAT not in reason, (
            f"Malformed lat value {MALFORMED_LAT!r} must NOT appear in the FAILED reason. "
            f"Got reason: {reason!r}. "
            "Mutation guard: restore f'Invalid lat/lon in row: {exc}' → raw value in reason, "
            "this FAILS."
        )
        # But the reason must still be distinguishable as a parse error (F7.3 contract)
        assert "Invalid" in reason or "invalid" in reason or "lat/lon" in reason.lower(), (
            f"Reason must still reference parse failure (lat/lon or Invalid). "
            f"Got: {reason!r}"
        )

    def test_range_violation_reason_has_no_coordinate_values(self, patched_ddb):
        """The FAILED reason for out-of-range coordinates must not contain the actual values.

        lat=999.0 and lon=-999.0 must not appear in the reason echoed to CloudWatch.

        Mutation guard: restore f"Coordinates out of valid range: lat={lat}, lon={lon}"
        → the numeric values appear in the reason, this FAILS.
        """
        provider_mock = MagicMock()
        record = self._make_record_numeric(
            lat="999.0", lon="-999.0", dispatch_id="DISP-NOCOORDS-RANGE"
        )

        with patch.object(_h, "get_client", return_value=provider_mock):
            _h.handler(_make_event([record]), None)

        provider_mock.request_dispatch.assert_not_called()
        ev = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert ev.get(":failed") == "FAILED"
        reason = ev.get(":reason", "")
        # The actual out-of-range values must NOT appear in the reason
        assert "999.0" not in reason, (
            f"Coordinate value '999.0' must NOT appear in the FAILED reason. "
            f"Got reason: {reason!r}. "
            "Mutation guard: restore f'lat={lat}, lon={lon}' → values in reason, this FAILS."
        )
        assert "-999.0" not in reason, (
            f"Coordinate value '-999.0' must NOT appear in the FAILED reason. "
            f"Got reason: {reason!r}."
        )
        # The reason must still be non-empty and mention range (not collapse to parse error)
        assert reason, "FAILED reason must be non-empty"
        # Must be distinguishable from the parse-failure reason
        assert (
            "Invalid" not in reason and "invalid" not in reason
        ) or "range" in reason.lower(), (
            "Range-violation reason must be distinguishable from parse-failure reason. "
            f"Got: {reason!r}"
        )

    def test_parse_and_range_reasons_are_distinguishable(self, patched_ddb):
        """Parse-failure and range-violation reasons must remain distinguishable.

        F7.3's tests assert that the parse path produces a reason containing
        'Invalid', 'invalid', or 'lat/lon'. This test confirms the range path
        does not accidentally produce the same markers, keeping the two refusals
        identifiable in logs and in the DDB row.
        """
        provider_mock = MagicMock()

        # --- Parse failure ---
        record_parse = self._make_record_non_numeric(
            lat="not-a-number", lon="-122.4194", dispatch_id="DISP-DIST-PARSE"
        )
        with patch.object(_h, "get_client", return_value=provider_mock):
            patched_ddb.reset_mock()
            _h.handler(_make_event([record_parse]), None)
        ev_parse = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        parse_reason = ev_parse.get(":reason", "")

        # --- Range violation ---
        record_range = self._make_record_numeric(
            lat="999.0", lon="-122.4194", dispatch_id="DISP-DIST-RANGE"
        )
        with patch.object(_h, "get_client", return_value=provider_mock):
            patched_ddb.reset_mock()
            _h.handler(_make_event([record_range]), None)
        ev_range = patched_ddb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        range_reason = ev_range.get(":reason", "")

        assert parse_reason != range_reason, (
            "Parse-failure and range-violation FAILED reasons must be distinguishable. "
            f"Both returned: {parse_reason!r}"
        )



# ---------------------------------------------------------------------------
# F10.11 (W7): Consumer timestamp writes tested against moto
# ---------------------------------------------------------------------------

class TestConsumerTimestampWrites:
    """F10.11 (W7): DISPATCHED and FAILED writebacks must include G3-D3 timestamps.

    Task 3.1 reads dispatchedAt / failedAt / updatedAt and returns null when
    they are absent, so a regression here passes silently through both tasks.

    Mutation guard: remove dispatchedAt/updatedAt from the DISPATCHED write,
    or remove failedAt/updatedAt from _write_failed → moto table row lacks the
    attribute → assertion fails.
    """

    def _make_moto_table(self, monkeypatch, table_name: str):
        """Create a minimal moto-backed table and reset the lazy DDB singleton."""
        import boto3 as _boto3
        from moto import mock_aws as _mock_aws
        return _boto3, _mock_aws

    def test_dispatched_writeback_includes_timestamps_with_moto(self, monkeypatch):
        """DISPATCHED write must set dispatchedAt and updatedAt in G3-D3 format.

        Mutation guard: remove either attribute from the UpdateExpression
        → attribute absent from moto row → assertion fails.
        """
        import boto3 as _boto3
        from moto import mock_aws

        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        _h._ddb = None

        with mock_aws():
            ddb = _boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.put_item(Item={
                "dispatchId": "DISP-TS-DISPATCH-001",
                "status": "REQUESTED",
                "vin": "1FMCU0F70MUA12345",
                "lat": "37.7749",
                "lon": "-122.4194",
                "serviceType": "tow",
                "tenantId": "meridian",
                "callbackContact": "+15551234567",
            })

            _h._ddb = None
            provider_mock = MagicMock()
            provider_mock.request_dispatch.return_value = MagicMock(
                provider_ref="SIM-TS-ABCDEF",
                eta_minutes=19,
                simulated=True,
                status="DISPATCHED",
            )

            from unittest.mock import patch as _patch
            with _patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler({"Records": [{
                    "eventName": "INSERT",
                    "dynamodb": {"NewImage": {
                        "dispatchId": {"S": "DISP-TS-DISPATCH-001"},
                        "status": {"S": "REQUESTED"},
                        "vin": {"S": "1FMCU0F70MUA12345"},
                        "lat": {"S": "37.7749"},
                        "lon": {"S": "-122.4194"},
                        "serviceType": {"S": "tow"},
                        "tenantId": {"S": "meridian"},
                        "callbackContact": {"S": "+15551234567"},
                    }},
                }]}, None)

            item = table.get_item(Key={"dispatchId": "DISP-TS-DISPATCH-001"})["Item"]
            assert item["status"] == "DISPATCHED"

            # dispatchedAt must be present and in G3-D3 format
            assert "dispatchedAt" in item, (
                "DISPATCHED write must set dispatchedAt (G3-D3). "
                "Mutation: remove dispatchedAt from UpdateExpression → absent → FAIL."
            )
            import datetime as _dt
            _dt.datetime.strptime(item["dispatchedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")
            assert item["dispatchedAt"].endswith("Z"), "dispatchedAt must end with Z."

            # updatedAt must be present and in G3-D3 format
            assert "updatedAt" in item, (
                "DISPATCHED write must set updatedAt (G3-D3). "
                "Mutation: remove updatedAt from UpdateExpression → absent → FAIL."
            )
            _dt.datetime.strptime(item["updatedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")

        _h._ddb = None

    def test_failed_writeback_includes_timestamps_with_moto(self, monkeypatch):
        """FAILED write (_write_failed) must set failedAt and updatedAt in G3-D3 format.

        Mutation guard: remove either attribute from the UpdateExpression
        in _write_failed → attribute absent from moto row → assertion fails.
        """
        import boto3 as _boto3
        from moto import mock_aws

        table_name = _h._RSA_DISPATCH_TABLE
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        _h._ddb = None

        with mock_aws():
            ddb = _boto3.resource("dynamodb")
            table = ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "dispatchId", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.put_item(Item={
                "dispatchId": "DISP-TS-FAILED-001",
                "status": "REQUESTED",
                "vin": "1FMCU0F70MUA12345",
                "lat": "37.7749",
                "lon": "-122.4194",
                "serviceType": "tow",
                "tenantId": "meridian",
                "callbackContact": "+15551234567",
            })

            _h._ddb = None
            provider_mock = MagicMock()
            provider_mock.request_dispatch.side_effect = RuntimeError("provider down")

            from unittest.mock import patch as _patch
            with _patch.object(_h, "get_client", return_value=provider_mock):
                _h.handler({"Records": [{
                    "eventName": "INSERT",
                    "dynamodb": {"NewImage": {
                        "dispatchId": {"S": "DISP-TS-FAILED-001"},
                        "status": {"S": "REQUESTED"},
                        "vin": {"S": "1FMCU0F70MUA12345"},
                        "lat": {"S": "37.7749"},
                        "lon": {"S": "-122.4194"},
                        "serviceType": {"S": "tow"},
                        "tenantId": {"S": "meridian"},
                        "callbackContact": {"S": "+15551234567"},
                    }},
                }]}, None)

            item = table.get_item(Key={"dispatchId": "DISP-TS-FAILED-001"})["Item"]
            assert item["status"] == "FAILED"

            # failedAt must be present and in G3-D3 format
            assert "failedAt" in item, (
                "FAILED write must set failedAt (G3-D3). "
                "Mutation: remove failedAt from _write_failed UpdateExpression → absent → FAIL."
            )
            import datetime as _dt
            _dt.datetime.strptime(item["failedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")
            assert item["failedAt"].endswith("Z"), "failedAt must end with Z."

            # updatedAt must be present and in G3-D3 format
            assert "updatedAt" in item, (
                "FAILED write must set updatedAt (G3-D3). "
                "Mutation: remove updatedAt from _write_failed UpdateExpression → absent → FAIL."
            )
            _dt.datetime.strptime(item["updatedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")

        _h._ddb = None
