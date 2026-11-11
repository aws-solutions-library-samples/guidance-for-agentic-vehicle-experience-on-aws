"""Red tests for T1.5: run-record hardening — TTL, millisecond started_at,
conditional put + retry.

All tests here are RED until the following are implemented:
  - ``timeToLiveAttribute`` added to the stack (TTL attribute name ``ttl``).
  - ``run_records.write_run_record`` adds a ``ttl`` item field.
  - ``observability._utc_now`` produces millisecond precision.
  - ``lambdas/avx-auto-approver/handler._now_iso`` produces millisecond precision.
  - ``write_run_record`` uses a conditional put (``attribute_not_exists(agent_component)``).
  - On ConditionalCheckFailedException, retries once with a new timestamp.
  - On a second CCF, returns False (never overwrites).

Spec: .kiro/specs/2026-10-26-fi-sweep-avx-batch/spec.md (T1.5)
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# CMS _ISO_RE — must match every started_at we write.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"
)
# Millisecond pattern: the fractional part must have at least 1 digit; seconds must be present.
_MILLISECOND_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"
)

_AGENT_ID = "agent-2-vehicle-health"
_TABLE = "vsa-staging-avx-agent-runs"
_TTL_DAYS = 400


# ── helper ───────────────────────────────────────────────────────────────────


def _import_run_records():
    from agents.tier2 import run_records  # noqa: PLC0415
    return run_records


def _make_call(rr, **overrides):
    mock_ddb = MagicMock()
    defaults = dict(
        agent_id=_AGENT_ID,
        component="battery",
        started_at="2026-10-02T02:00:00.000Z",
        finished_at="2026-10-02T02:05:00.000Z",
        status="completed",
        artifacts_written=1,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name=_TABLE,
    )
    defaults.update(overrides)
    return rr.write_run_record(**defaults), mock_ddb


# ── TTL field ─────────────────────────────────────────────────────────────────


class TestTtlField:
    """write_run_record must add a ``ttl`` field (epoch seconds, ~400 days)."""

    def _item_from_call(self, mock_ddb) -> dict[str, Any]:
        put_calls = mock_ddb.Table.return_value.put_item.call_args_list
        assert put_calls, "put_item was never called"
        return put_calls[0].kwargs.get("Item", put_calls[0].args[0] if put_calls[0].args else {})

    def test_ttl_field_present(self):
        """Item must contain a ``ttl`` field (RED until TTL is implemented)."""
        rr = _import_run_records()
        _, mock_ddb = _make_call(rr)
        item = self._item_from_call(mock_ddb)
        assert "ttl" in item, (
            "write_run_record must add a 'ttl' field (T1.5 — epoch-seconds TTL). "
            f"Item keys: {sorted(item)}"
        )

    def test_ttl_is_integer_or_decimal(self):
        """``ttl`` must be a number (int or Decimal) — DynamoDB rejects float."""
        rr = _import_run_records()
        _, mock_ddb = _make_call(rr)
        item = self._item_from_call(mock_ddb)
        ttl = item.get("ttl")
        assert isinstance(ttl, (int, Decimal)), (
            f"'ttl' must be int or Decimal, got {type(ttl)}: {ttl}"
        )

    def test_ttl_is_approximately_finished_at_plus_400_days(self):
        """TTL ≈ finished_at + 400 days in epoch seconds (within ±5 s tolerance)."""
        rr = _import_run_records()
        finished_at = "2026-10-02T02:05:00.000Z"
        _, mock_ddb = _make_call(rr, finished_at=finished_at)
        item = self._item_from_call(mock_ddb)
        ttl = int(item.get("ttl", 0))

        # Expected: parse finished_at, add 400 days, to epoch seconds
        dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        expected = int(calendar.timegm((dt + timedelta(days=_TTL_DAYS)).utctimetuple()))
        assert abs(ttl - expected) <= 5, (
            f"TTL must be ~finished_at + {_TTL_DAYS} days in epoch seconds. "
            f"Expected ~{expected}, got {ttl} (diff {ttl - expected} s)"
        )

    def test_ttl_attribute_name_matches_stack_precedent(self):
        """TTL attribute name must be 'ttl' (matches other AVX tables in the stack)."""
        rr = _import_run_records()
        _, mock_ddb = _make_call(rr)
        item = self._item_from_call(mock_ddb)
        assert "ttl" in item, "TTL attribute must be named 'ttl' (stack precedent)"
        # No other TTL-sounding attribute should be present
        for key in item:
            if key != "ttl" and "ttl" in key.lower():
                pytest.fail(
                    f"Found an unexpected TTL-related attribute {key!r}; "
                    "use exactly 'ttl' to match the stack's timeToLiveAttribute"
                )


# ── millisecond precision ─────────────────────────────────────────────────────


class TestMillisecondStartedAt:
    """observability._utc_now must produce millisecond-precision timestamps."""

    def test_utc_now_has_millisecond_precision(self):
        """_utc_now() must produce a Z-suffixed string with sub-second digits (RED)."""
        from agents.tier2 import observability  # noqa: PLC0415
        ts = observability._utc_now()
        assert _MILLISECOND_RE.match(ts), (
            f"observability._utc_now() must have millisecond precision and Z suffix, "
            f"got {ts!r}. Expected pattern: YYYY-MM-DDTHH:MM:SS.<frac>Z"
        )
        assert _ISO_RE.match(ts), f"_utc_now() result does not match CMS _ISO_RE: {ts!r}"

    def test_utc_now_matches_cms_iso_re(self):
        """_utc_now() result must match the CMS _ISO_RE used in fi_action_queue.py."""
        from agents.tier2 import observability  # noqa: PLC0415
        ts = observability._utc_now()
        assert _ISO_RE.match(ts), (
            f"_utc_now() must match CMS _ISO_RE, got {ts!r}"
        )

    def test_run_observer_started_at_has_millisecond_precision(self):
        """RunObserver.record.started_at must have millisecond precision (RED)."""
        from agents.tier2.observability import RunObserver  # noqa: PLC0415
        obs = RunObserver(run_id="test-run", stage="staging", component="battery")
        ts = obs.record.started_at
        assert _MILLISECOND_RE.match(ts), (
            f"RunObserver.record.started_at must have millisecond precision, got {ts!r}"
        )

    def test_run_observer_finished_at_has_millisecond_precision(self):
        """RunObserver.record.finished_at must have millisecond precision (RED)."""
        from agents.tier2.observability import RunObserver  # noqa: PLC0415
        obs = RunObserver(run_id="test-run", stage="staging", component="battery")
        import io  # noqa: PLC0415
        record = obs.finish()
        ts = record.finished_at
        assert _MILLISECOND_RE.match(ts), (
            f"RunObserver.record.finished_at must have millisecond precision, got {ts!r}"
        )


class TestAutoApproverMillisecondPrecision:
    """lambdas/avx-auto-approver/handler._now_iso must produce millisecond precision."""

    @staticmethod
    def _load_handler():
        """Load the auto-approver handler from the project root relative to the test file."""
        import importlib.util  # noqa: PLC0415
        import os  # noqa: PLC0415
        # Resolve project root from this test file's location (tests/ → tier2/ → agents/ → root)
        here = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        handler_path = os.path.join(project_root, "lambdas", "avx-auto-approver", "handler.py")
        spec = importlib.util.spec_from_file_location("avx_auto_approver_handler_test", handler_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_now_iso_has_millisecond_precision(self):
        """_now_iso() must return a Z-suffixed ms-precision string (RED)."""
        mod = self._load_handler()
        ts = mod._now_iso()
        assert _MILLISECOND_RE.match(ts), (
            f"auto-approver _now_iso() must have millisecond precision, got {ts!r}"
        )
        assert _ISO_RE.match(ts), f"_now_iso() must match CMS _ISO_RE, got {ts!r}"

    def test_now_iso_matches_cms_iso_re(self):
        """_now_iso() must match the CMS _ISO_RE regex."""
        mod = self._load_handler()
        ts = mod._now_iso()
        assert _ISO_RE.match(ts), (
            f"_now_iso() must match CMS _ISO_RE, got {ts!r}"
        )


# ── conditional put ───────────────────────────────────────────────────────────


class TestConditionalPut:
    """write_run_record must use a conditional put to avoid overwrites."""

    def _capture_put_kwargs(self, mock_ddb) -> list[dict]:
        return [c.kwargs for c in mock_ddb.Table.return_value.put_item.call_args_list]

    def test_put_item_uses_attribute_not_exists_condition(self):
        """put_item must be called with ConditionExpression='attribute_not_exists(agent_component)'."""
        rr = _import_run_records()
        _, mock_ddb = _make_call(rr)
        calls = self._capture_put_kwargs(mock_ddb)
        assert calls, "put_item was not called"
        condition = calls[0].get("ConditionExpression", "")
        # Accept either a string or a boto3 condition object containing the attribute name
        condition_str = str(condition)
        assert "attribute_not_exists" in condition_str and "agent_component" in condition_str, (
            f"put_item must use ConditionExpression='attribute_not_exists(agent_component)', "
            f"got {condition!r}"
        )

    def test_conditional_check_failed_triggers_retry_with_new_timestamp(self):
        """On CCF, write_run_record must retry once with a new started_at."""
        from botocore.exceptions import ClientError  # noqa: PLC0415
        rr = _import_run_records()

        ccf_error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "test"}},
            "PutItem",
        )
        call_count = [0]
        put_items: list[dict] = []

        def _fake_put(**kwargs):
            call_count[0] += 1
            put_items.append(kwargs)
            if call_count[0] == 1:
                raise ccf_error
            # Second call succeeds

        mock_ddb = MagicMock()
        mock_ddb.Table.return_value.put_item.side_effect = _fake_put

        result = rr.write_run_record(
            agent_id=_AGENT_ID,
            component="battery",
            started_at="2026-10-02T02:00:00.000Z",
            finished_at="2026-10-02T02:05:00.000Z",
            status="completed",
            artifacts_written=1,
            duplicates_skipped=0,
            agent_version="0.1.0",
            ddb_client=mock_ddb,
            table_name=_TABLE,
        )

        assert call_count[0] == 2, (
            f"On CCF, write_run_record must retry exactly once. "
            f"put_item was called {call_count[0]} time(s)"
        )
        assert result is True, (
            "write_run_record must return True when the retry succeeds"
        )
        # The retry must use a different started_at
        first_started_at = put_items[0].get("Item", {}).get("started_at")
        second_started_at = put_items[1].get("Item", {}).get("started_at")
        assert first_started_at != second_started_at, (
            "Retry must use a new started_at timestamp, not the same one that collided"
        )

    def test_two_ccf_returns_false(self):
        """On a second CCF (retry also fails), write_run_record returns False."""
        from botocore.exceptions import ClientError  # noqa: PLC0415
        rr = _import_run_records()

        ccf_error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "test"}},
            "PutItem",
        )
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value.put_item.side_effect = ccf_error

        result = rr.write_run_record(
            agent_id=_AGENT_ID,
            component="battery",
            started_at="2026-10-02T02:00:00.000Z",
            finished_at="2026-10-02T02:05:00.000Z",
            status="completed",
            artifacts_written=1,
            duplicates_skipped=0,
            agent_version="0.1.0",
            ddb_client=mock_ddb,
            table_name=_TABLE,
        )

        assert result is False, (
            f"On two consecutive CCFs, write_run_record must return False (never overwrite). "
            f"Got: {result!r}"
        )
        assert mock_ddb.Table.return_value.put_item.call_count == 2, (
            "Must attempt exactly 2 puts (original + 1 retry), no more"
        )

    def test_non_ccf_client_error_returns_false(self):
        """Any other ClientError is caught and returns False (same as before)."""
        from botocore.exceptions import ClientError  # noqa: PLC0415
        rr = _import_run_records()

        other_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "test"}},
            "PutItem",
        )
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value.put_item.side_effect = other_error

        result = rr.write_run_record(
            agent_id=_AGENT_ID,
            component="battery",
            started_at="2026-10-02T02:00:00.000Z",
            finished_at="2026-10-02T02:05:00.000Z",
            status="completed",
            artifacts_written=1,
            duplicates_skipped=0,
            agent_version="0.1.0",
            ddb_client=mock_ddb,
            table_name=_TABLE,
        )

        assert result is False, (
            "Non-CCF ClientError must also return False (D10: never fail the run)"
        )
        # Must not retry on non-CCF errors
        assert mock_ddb.Table.return_value.put_item.call_count == 1, (
            "Non-CCF errors must NOT be retried"
        )


# ── moto integration tests ────────────────────────────────────────────────────


def _make_moto_table(resource):
    """Create the runs table inside an active moto context."""
    return resource.create_table(
        TableName=_TABLE,
        KeySchema=[
            {"AttributeName": "agent_component", "KeyType": "HASH"},
            {"AttributeName": "started_at",      "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "agent_component", "AttributeType": "S"},
            {"AttributeName": "started_at",      "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class TestMotoConditionalPut:
    """Integration tests using moto to verify conditional put against real DynamoDB semantics."""

    def test_two_writes_in_same_second_both_persist(self):
        """Two writes with different millisecond SKs in the same second must both persist."""
        boto3 = pytest.importorskip("boto3")
        moto = pytest.importorskip("moto")

        rr = _import_run_records()

        with moto.mock_aws():
            resource = boto3.resource("dynamodb", region_name="us-west-2")
            _make_moto_table(resource)

            # First write: 2026-10-02T02:00:00.000Z
            r1 = rr.write_run_record(
                agent_id=_AGENT_ID, component="battery",
                started_at="2026-10-02T02:00:00.000Z",
                finished_at="2026-10-02T02:05:00.000Z",
                status="completed", artifacts_written=1, duplicates_skipped=0,
                agent_version="0.1.0", ddb_client=resource, table_name=_TABLE,
            )
            # Second write: 1 ms later → different SK, must succeed
            r2 = rr.write_run_record(
                agent_id=_AGENT_ID, component="battery",
                started_at="2026-10-02T02:00:00.001Z",
                finished_at="2026-10-02T02:05:00.001Z",
                status="completed", artifacts_written=2, duplicates_skipped=0,
                agent_version="0.1.0", ddb_client=resource, table_name=_TABLE,
            )

            assert r1 is True, "First write must succeed"
            assert r2 is True, "Second write with different SK must also succeed"

            table = resource.Table(_TABLE)
            items = table.scan()["Items"]
            assert len(items) == 2, (
                f"Both writes must produce distinct items; found {len(items)}"
            )

    def test_forced_collision_never_replaces_first_item(self):
        """A second write with the SAME (agent_component, started_at) must not overwrite."""
        boto3 = pytest.importorskip("boto3")
        moto = pytest.importorskip("moto")

        rr = _import_run_records()
        started_at = "2026-10-02T02:00:00.000Z"

        with moto.mock_aws():
            resource = boto3.resource("dynamodb", region_name="us-west-2")
            _make_moto_table(resource)

            # First write — must succeed
            r1 = rr.write_run_record(
                agent_id=_AGENT_ID, component="battery",
                started_at=started_at, finished_at="2026-10-02T02:05:00.000Z",
                status="completed", artifacts_written=5, duplicates_skipped=0,
                agent_version="0.1.0", ddb_client=resource, table_name=_TABLE,
            )
            assert r1 is True

            # Second call with the SAME key: write_run_record's conditional must block it.
            # The retry gets a new timestamp (so it may succeed), but the original item
            # with artifacts_written=5 must be untouched.
            rr.write_run_record(
                agent_id=_AGENT_ID, component="battery",
                started_at=started_at,       # SAME SK — must not overwrite
                finished_at="2026-10-02T02:08:00.000Z",
                status="completed", artifacts_written=99,  # sentinel: would detect overwrite
                duplicates_skipped=0, agent_version="0.1.0",
                ddb_client=resource, table_name=_TABLE,
            )

            table = resource.Table(_TABLE)
            first_item = table.get_item(
                Key={"agent_component": f"{_AGENT_ID}#battery", "started_at": started_at}
            ).get("Item", {})
            assert int(first_item.get("artifacts_written", 0)) == 5, (
                f"First item must NOT be overwritten; "
                f"artifacts_written={first_item.get('artifacts_written')}"
            )


# ── F1.5 S1: retry timestamp ──────────────────────────────────────────────────


def test_retry_uses_started_at_plus_1ms_not_write_time():
    """S1 red: on CCF the retry must use started_at + 1 ms, not datetime.now().

    Current code (bug):
        new_ts = datetime.now(timezone.utc).isoformat(...)  # wall-clock time

    Correct behaviour:
        new_ts = (parse(started_at) + 1 ms).isoformat(...)  # keeps start ≤ finish

    With started_at="2026-10-02T02:00:00.000Z" the expected retry key is
    "2026-10-02T02:00:00.001Z".  Current code produces a 2026-10-27... timestamp,
    so this test FAILS on the current implementation and passes after the fix.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    rr = _import_run_records()
    original_started_at = "2026-10-02T02:00:00.000Z"
    expected_retry_started_at = "2026-10-02T02:00:00.001Z"

    ccf = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "test"}},
        "PutItem",
    )
    put_items: list[dict] = []
    call_count = [0]

    def _fake_put(**kwargs):
        call_count[0] += 1
        put_items.append(kwargs)
        if call_count[0] == 1:
            raise ccf

    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = _fake_put

    result = rr.write_run_record(
        agent_id=_AGENT_ID,
        component="battery",
        started_at=original_started_at,
        finished_at="2026-10-02T02:05:00.000Z",
        status="completed",
        artifacts_written=1,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name=_TABLE,
    )

    assert call_count[0] == 2, (
        f"Expected exactly 2 put_item calls (original + 1 retry), got {call_count[0]}"
    )
    assert result is True, "Retry must return True when it succeeds"

    retry_started_at = put_items[1].get("Item", {}).get("started_at")
    assert retry_started_at == expected_retry_started_at, (
        f"Retry started_at must be original + 1 ms = {expected_retry_started_at!r}. "
        f"Got {retry_started_at!r}. "
        "Current code uses datetime.now() (wall-clock time) instead of started_at + 1 ms; "
        "this breaks the invariant start ≤ finish when the retry happens after finished_at."
    )
