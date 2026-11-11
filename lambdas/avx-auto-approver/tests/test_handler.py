"""Red-test skeletons: auto-approve evaluator — spec D9.

Expected interface (Group 2 creates lambdas/avx-auto-approver/handler.py):

    Lambda handler: handler(event: dict, context) -> dict
    Triggered by DynamoDB Streams (NEW_IMAGE events from vsa-{stage}-avx-findings).

    For each stream record that has a NEW_IMAGE with a recommended_action and a
    fleet_id, the evaluator:
      1. Loads enabled rules for the finding's (fleet_id, action_kind).
      2. Checks the limit.
      3. Computes a deterministic action_id from the finding_id alone
         (same as approve/decline — one decision per Finding, contract § 4).
         Format: AVX-ACT-{uuid4} where uuid4 is deterministic → ^AVX-ACT-[0-9a-f-]{36}$
      4. Conditional PutItem: if the action_id already exists → noop (replay-safe).
         First put succeeds → dispatch; second put (replay) raises
         ConditionalCheckFailedException → no second dispatch.
      5. Calls registry.dispatch() with decided_by={kind:"rule", id:"<fleet>/<rule>"}.
         Rule Action carries: fleet_id (storage-only), created_by: rule-<fleet>/<rule>,
         decided_by: {kind: "rule", id: "<fleet>/<rule>"}.
      6. Writes one run record per batch.
      7. Reports per-record failures (SFN-style batchItemFailures) using the
         record's dynamodb.SequenceNumber as itemIdentifier.

    Only token-free kinds (auto_approvable=True and no caller token needed) are
    dispatched. In Stage A that is only "acknowledge_only".

    Rule stubs carry "limit" (not "limit_value").

Tests are RED until lambdas/avx-auto-approver/handler.py exists
(ModuleNotFoundError on import inside each test function).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# xfail mark removed: T2.8 handler.py implemented (Stage A Group 2)

# ── sys.path setup ────────────────────────────────────────────────────────────
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# DynamoDB Streams event helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    """The three table names the evaluator requires (it fails closed without them)."""
    monkeypatch.setenv("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
    monkeypatch.setenv("AVX_RULES_TABLE", "vsa-test-avx-approval-rules")
    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-test-avx-agent-runs")


def _stream_record(
    finding_id: str = "FIND-auto-00000001",
    fleet_id: str = "fleet-mrdn-01",
    action_kind: str = "acknowledge_only",
    event_name: str = "INSERT",
    sequence_number: str = "000000000000000000001",
) -> dict:
    """Build a minimal DynamoDB Streams record with NEW_IMAGE and SequenceNumber."""
    return {
        "eventName": event_name,
        "dynamodb": {
            "SequenceNumber": sequence_number,
            "NewImage": {
                "finding_id": {"S": finding_id},
                "fleet_id": {"S": fleet_id},
                "finding_kind": {"S": "health.tires"},
                "scope": {"M": {"fleet_id": {"S": fleet_id}}},
                "recommended_action": {
                    "M": {
                        "action_kind": {"S": action_kind},
                        "target_system": {"S": "none"},
                        "parameters": {"M": {}},
                    }
                },
                "expires_at": {"S": "2099-12-31T00:00:00Z"},
                "computed_at": {"S": "2026-10-02T02:00:00Z"},
            },
        },
    }


def _stream_event(records: list) -> dict:
    return {"Records": records}


# ---------------------------------------------------------------------------
# Deterministic action_id
# ---------------------------------------------------------------------------

class TestDeterministicActionId:
    """action_id is the per-Finding decision id: derived from the finding_id alone
    (contract § 4). The helper-level tests below are supplemented by
    test_handler_writes_the_pinned_decision_id, which pins the id the HANDLER writes,
    so a handler that bypasses the helper cannot pass (review Cycle 3)."""

    def test_action_id_is_deterministic(self):
        """Same inputs always produce the same action_id."""
        import handler as h  # noqa: PLC0415  RED: ImportError until handler.py exists

        id1 = h.derive_action_id("FIND-001", "acknowledge_only", "rule-001")
        id2 = h.derive_action_id("FIND-001", "acknowledge_only", "rule-001")
        # RED: handler.py absent → ImportError.
        assert id1 == id2, "action_id must be deterministic for the same inputs"

    def test_action_id_same_for_all_rules_on_same_finding(self):
        """Contract § 4: action_id comes from finding_id alone — the rule does not affect it.

        All rules matching one Finding write the same action_id slot.  The first
        conditional put wins; subsequent puts (from other rules) raise
        ConditionalCheckFailed and are no-ops.  A per-rule derivation would produce
        a different action_id for each rule → each put succeeds → multiple Actions
        → test fails → catches the per-rule mutation.
        """
        import handler as h  # noqa: PLC0415  RED

        id_rule_1 = h.derive_action_id("FIND-001", "acknowledge_only", "rule-001")
        id_rule_2 = h.derive_action_id("FIND-001", "acknowledge_only", "rule-002")
        assert id_rule_1 == id_rule_2, (
            "action_id must be the same for different rules on the same Finding "
            "(contract § 4: one decision per Finding, derived from finding_id alone)"
        )

    def test_action_id_differs_for_different_finding(self):
        """Different findings produce different action_ids."""
        import handler as h  # noqa: PLC0415  RED

        id1 = h.derive_action_id("FIND-001", "acknowledge_only", "rule-001")
        id2 = h.derive_action_id("FIND-002", "acknowledge_only", "rule-001")
        assert id1 != id2


# ---------------------------------------------------------------------------
# W11: action_id format — ^AVX-ACT-[0-9a-f-]{36}$
# ---------------------------------------------------------------------------

class TestActionIdFormat:
    """W11: the action_id matches ^AVX-ACT-[0-9a-f-]{36}$ (contract § 4, 'AVX-ACT-{uuid}')."""

    def test_action_id_matches_avx_act_format(self):
        """derive_action_id returns a string matching ^AVX-ACT-[0-9a-f-]{36}$."""
        import re

        import handler as h  # noqa: PLC0415  RED

        aid = h.derive_action_id("FIND-001", "acknowledge_only", "rule-001")
        pattern = r"^AVX-ACT-[0-9a-f-]{36}$"
        assert re.match(pattern, aid), (
            f"action_id {aid!r} does not match {pattern!r} — "
            "expected AVX-ACT-<uuid4> where uuid4 uses only hex digits and dashes"
        )

    def test_action_id_per_finding_is_consistent_across_invocations(self):
        """W11: the same finding_id always produces the same action_id (per-Finding decision id).

        This ensures the conditional-put replay guard uses a stable key.
        """
        import handler as h  # noqa: PLC0415  RED

        finding_id = "FIND-mrdn-00000000-0000-0000-0000-000000000001"
        id1 = h.derive_action_id(finding_id, "acknowledge_only", "rule-001")
        id2 = h.derive_action_id(finding_id, "acknowledge_only", "rule-001")
        assert id1 == id2


# ---------------------------------------------------------------------------
# Replay safety: a replayed stream record dispatches exactly once
# ---------------------------------------------------------------------------

class TestReplaySafety:
    """W11: first put succeeds → dispatch; second put (replay) raises ConditionalCheckFailed → no dispatch."""

    def test_replayed_record_dispatches_exactly_once(self):
        """W11: processing the same record twice dispatches exactly once.

        The first Lambda invocation: put_item succeeds → dispatch() called.
        The second invocation (replay): same finding_id → same action_id derived
        → ConditionalCheckFailedException → dispatch() NOT called again.
        Total dispatches == 1.

        A handler that uses uuid4() for each invocation derives a NEW action_id on
        the second run → action_id not yet in the fake store → put succeeds again →
        dispatch fires a second time → dispatch_calls == 2 → test fails → random-id
        mutation caught.
        """
        from botocore.exceptions import ClientError
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        _CCF = type("ConditionalCheckFailedException", (ClientError,), {})
        store: dict = {}
        dispatch_calls: list = []

        def _fake_put(**kwargs):
            item = kwargs.get("Item", {})
            action_id = item.get("action_id")
            condition = kwargs.get("ConditionExpression", "")
            if (
                isinstance(condition, str)
                and "attribute_not_exists" in condition
                and action_id in store
            ):
                raise _CCF(
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": "The conditional request failed"}},
                    "PutItem",
                )
            store[action_id] = item
            return {}

        mock_table = MagicMock()
        mock_table.put_item.side_effect = _fake_put
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = _CCF
        rules = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
                   "action_kind": "acknowledge_only", "enabled": True, "limit": None}]
        record = _stream_record(sequence_number="seq-replay-001")

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=lambda _: mock_table)), \
             patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
             patch("agents.executors.registry.dispatch",
                   side_effect=lambda a, c, **kw: dispatch_calls.append(a)):
            # First invocation: action_id not in store → put succeeds → dispatch.
            h.handler(_stream_event([record]), None)
            # Second invocation (replay): same finding_id → same action_id derived
            # → already in store → CCF → no second dispatch.
            h.handler(_stream_event([record]), None)

        # RED: handler.py absent → ImportError.
        assert len(dispatch_calls) == 1, (
            "First invocation: put succeeds → dispatch once. "
            "Replay (second invocation): same action_id derived → CCF → no dispatch. "
            f"Total dispatches must be exactly 1, got {len(dispatch_calls)}"
        )


# ---------------------------------------------------------------------------
# Only token-free kinds are dispatched (D9)
# ---------------------------------------------------------------------------

class TestTokenFreeKindsOnly:
    """Only auto_approvable kinds (acknowledge_only in Stage A) are dispatched."""

    def test_non_auto_approvable_kind_is_not_dispatched(self):
        """book_recall_service requires a caller token; the evaluator must not dispatch it."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        dispatch_calls: list = []

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=lambda _: mock_table)), \
             patch.object(h, "_get_rules_for_fleet_kind",
                          return_value=[{"rule_id": "rule-001",
                                         "fleet_id": "fleet-mrdn-01",
                                         "action_kind": "book_recall_service",
                                         "enabled": True,
                                         "limit": None}]), \
             patch("agents.executors.registry.dispatch",
                   side_effect=lambda a, c, **kw: dispatch_calls.append(a)):
            h.handler(
                _stream_event([
                    _stream_record(action_kind="book_recall_service")
                ]),
                None,
            )

        # RED: handler.py absent → ImportError.
        assert len(dispatch_calls) == 0, (
            "book_recall_service requires a caller token; the auto-approver "
            "must not dispatch it (D9)"
        )

    def test_only_token_free_kinds_in_stage_a(self):
        """Assert the evaluator documents which kinds it can dispatch.

        The evaluator must expose a constant or function listing the token-free
        kinds it considers eligible, so the contract is testable.
        """
        import handler as h  # noqa: PLC0415  RED

        # After implementation: h.TOKEN_FREE_KINDS (or equivalent) is a frozenset
        # or list that contains "acknowledge_only" and excludes all others.
        token_free = getattr(h, "TOKEN_FREE_KINDS", None) or getattr(h, "_TOKEN_FREE_KINDS", None)
        assert token_free is not None, (
            "handler.py must expose TOKEN_FREE_KINDS (or _TOKEN_FREE_KINDS) listing "
            "the kinds the auto-approver may dispatch without a caller token"
        )
        assert "acknowledge_only" in token_free
        # The four guarded kinds must never be in the token-free set:
        for guarded in ("exempt_recall", "ground_vehicle", "submit_warranty_claim", "complete_recall"):
            assert guarded not in token_free, (
                f"Guarded kind {guarded!r} must not be auto-approvable (D9)"
            )


# ---------------------------------------------------------------------------
# Routes through registry.dispatch() — guardrails apply
# ---------------------------------------------------------------------------

class TestDispatchThroughRegistry:
    """The evaluator calls registry.dispatch(), so guardrails fire (D9 defence-in-depth)."""

    def test_dispatch_goes_through_registry_not_executor_directly(self):
        """Calls registry.dispatch(), not an executor directly."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        registry_calls: list = []
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=lambda _: mock_table)), \
             patch.object(h, "_get_rules_for_fleet_kind",
                          return_value=[{"rule_id": "rule-001",
                                         "fleet_id": "fleet-mrdn-01",
                                         "action_kind": "acknowledge_only",
                                         "enabled": True,
                                         "limit": None}]), \
             patch("agents.executors.registry.dispatch",
                   side_effect=lambda a, c, **kw: registry_calls.append(a)):
            h.handler(
                _stream_event([_stream_record()]),
                None,
            )

        # RED: handler.py absent → ImportError before here.
        assert len(registry_calls) >= 1, (
            "evaluator must call agents.executors.registry.dispatch() for each "
            "matched rule, not reach the executor directly"
        )

    def test_decided_by_kind_is_rule(self):
        """W11: the action passed to dispatch has decided_by.kind='rule', fleet_id,
        and created_by: rule-<fleet>/<rule> (contract § 6).
        """
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        dispatched: list = []
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=lambda _: mock_table)), \
             patch.object(h, "_get_rules_for_fleet_kind",
                          return_value=[{"rule_id": "rule-001",
                                         "fleet_id": "fleet-mrdn-01",
                                         "action_kind": "acknowledge_only",
                                         "enabled": True,
                                         "limit": None}]), \
             patch("agents.executors.registry.dispatch",
                   side_effect=lambda a, c, **kw: dispatched.append(a)):
            h.handler(
                _stream_event([_stream_record()]),
                None,
            )

        # RED.
        assert len(dispatched) >= 1
        action = dispatched[0]
        decided_by = action.get("decided_by", {})

        # W11: decided_by.kind must be "rule"
        assert decided_by.get("kind") == "rule", (
            f"decided_by.kind must be 'rule' for auto-approved actions, "
            f"got {decided_by.get('kind')!r}"
        )
        # W11: decided_by.id must be <fleet_id>/<rule_id>
        assert decided_by.get("id") == "fleet-mrdn-01/rule-001", (
            f"decided_by.id for a rule Action must be '<fleet_id>/<rule_id>', "
            f"got {decided_by.get('id')!r}"
        )
        # W11: rule Action carries storage-only fleet_id (contract § 6, § 7)
        assert action.get("fleet_id") == "fleet-mrdn-01", (
            f"Rule Action must carry storage-only fleet_id='fleet-mrdn-01', "
            f"got fleet_id={action.get('fleet_id')!r}"
        )
        # W11: created_by is rule-<fleet>/<rule>
        created_by = action.get("created_by", "")
        assert created_by == "rule-fleet-mrdn-01/rule-001", (
            f"created_by for a rule Action must be 'rule-<fleet>/<rule>', "
            f"got {created_by!r}"
        )


# ---------------------------------------------------------------------------
# Partial-batch failure reporting
# ---------------------------------------------------------------------------

class TestPartialBatchFailures:
    """W11: one bad record must not block the whole shard; its itemIdentifier appears in batchItemFailures."""

    def test_bad_record_reported_as_batch_item_failure(self):
        """W11: a record that raises is reported in batchItemFailures with its SequenceNumber as itemIdentifier."""
        from unittest.mock import MagicMock, patch

        import handler as h  # noqa: PLC0415  RED

        good_record = _stream_record(finding_id="FIND-good-001",
                                     sequence_number="seq-good-001")
        # A malformed record (no NewImage) must be reported, not kill the batch:
        bad_record = {
            "eventName": "INSERT",
            "dynamodb": {"SequenceNumber": "seq-bad-002"},  # no NewImage
        }

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=lambda _: mock_table)), \
             patch.object(h, "_get_rules_for_fleet_kind", return_value=[]), \
             patch("agents.executors.registry.dispatch", return_value=None):
            result = h.handler(
                _stream_event([good_record, bad_record]),
                None,
            )

        # RED: handler.py absent → ImportError.
        # After implementation: bad record's SequenceNumber appears in batchItemFailures.
        assert isinstance(result, dict), "handler must return a dict, not raise"
        assert "batchItemFailures" in result, (
            f"handler must return batchItemFailures for bad records, got keys: {list(result)}"
        )
        failures = result.get("batchItemFailures", [])
        assert len(failures) >= 1, "batchItemFailures must contain at least one entry"
        bad_ids = [f.get("itemIdentifier") for f in failures]
        assert "seq-bad-002" in bad_ids, (
            f"Bad record's SequenceNumber 'seq-bad-002' must appear as itemIdentifier "
            f"in batchItemFailures, got {bad_ids}"
        )


# ---------------------------------------------------------------------------
# Run record per batch
# ---------------------------------------------------------------------------

class TestRunRecord:
    """The evaluator writes one run record per batch (D10)."""

    def test_run_record_written_per_batch(self):
        """A run record is written to the agent-runs table after the batch."""
        from unittest.mock import MagicMock, call, patch

        import handler as h  # noqa: PLC0415  RED

        run_record_writes: list = []
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        # Intercept run-record writes by watching for puts to the runs table:
        def _table_factory(table_name):
            t = MagicMock()
            t.put_item.side_effect = (
                lambda **kw: run_record_writes.append({"table": table_name, **kw})
            )
            return t

        with patch.object(h, "_get_ddb", return_value=MagicMock(
                Table=_table_factory)), \
             patch.object(h, "_get_rules_for_fleet_kind", return_value=[]), \
             patch("agents.executors.registry.dispatch", return_value=None):
            h.handler(
                _stream_event([_stream_record()]),
                None,
            )

        # RED: handler.py absent → ImportError.
        # After implementation: at least one put goes to the runs table.
        runs_table_writes = [w for w in run_record_writes if "runs" in w.get("table", "")]
        assert len(runs_table_writes) >= 1, (
            "evaluator must write a run record per batch to the agent-runs table (D10)"
        )


# ---------------------------------------------------------------------------
# Review Cycle 3: pin the id the handler actually writes, by literal. The same
# formula is pinned in lambdas/api-avx-findings/tests/test_decline_override.py,
# so the two Lambdas cannot derive different ids for one Finding:
#   "AVX-ACT-" + uuid-shaped(sha256("<finding_id>|fi-decision").hexdigest()[:32])
# ---------------------------------------------------------------------------

_PINNED_DECISION_ID = "AVX-ACT-d1895b2c-b5af-158d-7657-38c95fe9f472"  # for finding_id "FIND-auto-00000001"


@pytest.mark.parametrize("rule_id", ["rule-001", "rule-002"])
def test_handler_writes_the_pinned_decision_id(rule_id):
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    import handler as h  # noqa: PLC0415  RED

    written: list = []
    mock_table = MagicMock()
    mock_table.put_item.side_effect = lambda **kw: written.append(kw.get("Item", {})) or {}
    rules = [{"rule_id": rule_id, "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True, "limit": None}]

    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: mock_table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch", return_value=MagicMock()):
        h.handler(_stream_event([_stream_record()]), None)

    actions = [i for i in written if i.get("action_id", "").startswith("AVX-ACT-")]
    assert [a["action_id"] for a in actions] == [_PINNED_DECISION_ID]



# ---------------------------------------------------------------------------
# Stage A Fix Group 5 (spec D14; review FG4-contract Warning 5): a rule decision
# that dispatch does not complete is stored with its failure_reason. In Stage A the
# only reachable case is an acknowledge_only executor error (target `none`, contract
# § 10a row 10); a guarded kind never reaches dispatch from a rule.
#
# Mutations this must catch (Group 2 records them in decisions.md): the evaluator
# stores the failed transition without `failure_reason` (E1), with the reason in `note`
# instead (E2), with `error_status: null` (E3), or with the reason on the Action's
# headline and none on the audit entry (E4). Review Cycle 6b W2.
# ---------------------------------------------------------------------------

def test_an_incomplete_rule_decision_is_stored_with_its_reason():
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from agents.executors import registry  # noqa: PLC0415
    from agents.executors.base import ExecuteResult  # noqa: PLC0415

    import handler as h  # noqa: PLC0415  RED

    mock_table = MagicMock()
    mock_table.put_item.return_value = {}
    mock_table.update_item.return_value = {"Attributes": {}}
    rules = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True, "limit": None}]

    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: mock_table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch.object(registry.EXECUTORS["none"], "dispatch",
                      return_value=ExecuteResult.terminal_error(last_error_class="RuntimeError")), \
         patch("agents.executors.escalation.fire", return_value={}):
        h.handler(_stream_event([_stream_record()]), None)

    written_calls = [c.kwargs for c in mock_table.mock_calls]

    def _entries(obj):
        """Every dict in the write calls that looks like an audit entry."""
        if isinstance(obj, dict):
            if "to_status" in obj and "from_status" in obj:
                yield obj
            for value in obj.values():
                yield from _entries(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                yield from _entries(value)

    failed = [e for e in _entries(written_calls) if e.get("to_status") == "failed"]
    assert failed, f"no failed transition was stored; writes: {written_calls!r}"
    entry = failed[-1]
    # Review Cycle 6b W2: on the audit entry itself, as `failure_reason`, so CMS finds it
    # (contract § 3). Not in `note`, not on the Action's headline, not null-padded.
    assert entry["failure_kind"] == "executor_terminal_error"
    assert entry["failure_reason"] == "AVX could not complete this action."
    assert "error_status" not in entry, "omitted, never null (contract § 10a)"
    assert "AVX could not complete this action." not in (entry.get("note") or "")

    import jsonschema  # noqa: PLC0415

    with open(os.path.join(_REPO_ROOT, "agents", "tier2", "schemas", "action.schema.json")) as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/audit_entry"})
    errors = sorted(validator.iter_errors(json.loads(json.dumps(entry, default=str))), key=str)
    assert not errors, [e.message for e in errors]



# ---------------------------------------------------------------------------
# Group 2 architect review of T2.8: stream events that must not be decided, and
# configuration that must fail closed.
# ---------------------------------------------------------------------------

_ACK_RULE = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True, "limit": None}]


def _run(records, rules=_ACK_RULE):
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    import handler as h  # noqa: PLC0415

    table = MagicMock()
    table.put_item.return_value = {}
    calls: list = []
    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch",
               side_effect=lambda a, c, **kw: calls.append(a)):
        result = h.handler(_stream_event(records), None)
    actions = [c.kwargs["Item"] for c in table.put_item.call_args_list
               if str(c.kwargs.get("Item", {}).get("action_id", "")).startswith("AVX-ACT-")]
    return result, actions, calls


def test_remove_events_are_skipped_not_retried():
    """A REMOVE (TTL expiry) has no NewImage. Reporting it as a failure would retry
    the shard forever."""
    record = {"eventName": "REMOVE", "dynamodb": {"SequenceNumber": "seq-remove"}}
    result, actions, calls = _run([record])
    assert result["batchItemFailures"] == []
    assert actions == [] and calls == []


def test_an_expired_recommendation_is_never_auto_approved():
    record = _stream_record()
    record["dynamodb"]["NewImage"]["expires_at"] = {"S": "2020-01-01T00:00:00Z"}
    result, actions, calls = _run([record])
    assert actions == [] and calls == []
    assert result["batchItemFailures"] == []


def test_a_record_without_a_finding_id_is_a_batch_failure():
    record = _stream_record(sequence_number="seq-no-id")
    del record["dynamodb"]["NewImage"]["finding_id"]
    result, actions, _ = _run([record])
    assert {"itemIdentifier": "seq-no-id"} in result["batchItemFailures"]
    assert actions == []


@pytest.mark.parametrize("unset", ["AVX_ACTIONS_TABLE", "AVX_RULES_TABLE", "AVX_AGENT_RUNS_TABLE"])
def test_unset_table_name_fails_closed(monkeypatch, unset):
    """No default stage: a misconfigured evaluator errors (and alarms) rather than
    writing to another stage's tables."""
    monkeypatch.delenv(unset)
    with pytest.raises(RuntimeError, match=unset):
        _run([_stream_record()])



# ===========================================================================
# Fix Group 2f fixes — review FG2-lambdas findings
# ===========================================================================

# ---------------------------------------------------------------------------
# Critical 1: stored Action validates against action.schema.json (whole row)
# ---------------------------------------------------------------------------

# A FIND-UUID that matches the finding_id pattern in action.schema.json:
#   ^FIND-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$
_SCHEMA_FINDING_ID = "FIND-deadbeef-cafe-abcd-ef01-234567890abc"

_SCHEMA_STREAM_RECORD = {
    "eventName": "INSERT",
    "dynamodb": {
        "SequenceNumber": "seq-schema-test-001",
        "NewImage": {
            "finding_id": {"S": _SCHEMA_FINDING_ID},
            "fleet_id": {"S": "fleet-mrdn-01"},
            "finding_kind": {"S": "fleet.cost.anomaly"},
            "scope": {"M": {"fleet_id": {"S": "fleet-mrdn-01"}}},
            "recommended_action": {
                "M": {
                    "action_kind": {"S": "acknowledge_only"},
                    "target_system": {"S": "none"},
                    "parameters": {"M": {}},
                }
            },
            "expires_at": {"S": "2099-12-31T00:00:00Z"},
            "computed_at": {"S": "2026-10-02T02:00:00Z"},
            # Evidence list: empty array, valid per schema (evidence is required; [] is valid).
            "evidence": {"L": []},
        },
    },
}


def test_evaluator_stored_action_validates_against_action_schema():
    """Critical 1: the evaluator's stored Action must satisfy action.schema.json.

    Before the fix, `evidence` was absent from the stored dict; the schema marks it
    required → validation fails.  After the fix it is copied from the Finding (`[]`
    here) and the row validates.

    Mutation to catch: remove ``"evidence": finding.get("evidence") or []`` from
    the action dict → `evidence` absent → schema validation fails → test fails.
    """
    import json as _json

    import jsonschema
    from unittest.mock import MagicMock, patch

    import handler as h  # noqa: PLC0415

    written: list = []
    mock_table = MagicMock()
    mock_table.put_item.side_effect = lambda **kw: written.append(kw.get("Item", {})) or {}
    rules = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True}]

    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: mock_table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch", return_value=MagicMock()):
        h.handler(_stream_event([_SCHEMA_STREAM_RECORD]), None)

    actions = [i for i in written if str(i.get("action_id", "")).startswith("AVX-ACT-")]
    assert actions, "evaluator must store an action for a matching rule"

    # Strip storage-only fleet_id before schema check (additionalProperties: false)
    action = dict(actions[0])
    action.pop("fleet_id", None)

    schema_path = os.path.join(_REPO_ROOT, "agents", "tier2", "schemas", "action.schema.json")
    with open(schema_path) as fh:
        schema = _json.load(fh)

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(_json.loads(_json.dumps(action, default=str))),
        key=str,
    )
    assert not errors, (
        "Evaluator's stored Action fails action.schema.json:\n"
        + "\n".join(f"  {e.json_path}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Critical 2 (EV1 + EV2): test _get_rules_for_fleet_kind against a fake table
# ---------------------------------------------------------------------------

class TestGetRulesForFleetKind:
    """Critical 2 (EV1/EV2): _get_rules_for_fleet_kind must filter by enabled=True
    AND action_kind.  Without these filters, a disabled or wrong-kind rule would
    auto-approve findings it should not touch, and the evaluator tests that patch
    this function out would never notice.

    Mutations caught:
      EV1 — remove `r.get("enabled") is True` from the list comprehension
      EV2 — remove `r.get("action_kind") == action_kind` from the list comprehension
    """

    def _fake_ddb(self, rules):
        from unittest.mock import MagicMock
        table = MagicMock()
        table.query.return_value = {"Items": rules}
        return MagicMock(Table=lambda _: table)

    def test_disabled_rule_is_not_returned(self):
        """EV1: enabled=False rules are filtered out."""
        from unittest.mock import patch
        import handler as h

        disabled = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-off",
            "action_kind": "acknowledge_only", "enabled": False,
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([disabled])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert result == [], (
            "A disabled rule (enabled=False) must be filtered out by "
            "_get_rules_for_fleet_kind; got {result!r}"
        )

    def test_wrong_kind_rule_is_not_returned(self):
        """EV2: a rule for a different action_kind is filtered out."""
        from unittest.mock import patch
        import handler as h

        wrong_kind = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-wrong-kind",
            "action_kind": "book_recall_service", "enabled": True,
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([wrong_kind])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert result == [], (
            "A rule for a different kind must be filtered out; got {result!r}"
        )

    def test_enabled_matching_rule_is_returned(self):
        """An enabled rule for the correct kind passes through."""
        from unittest.mock import patch
        import handler as h

        matching = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-on",
            "action_kind": "acknowledge_only", "enabled": True,
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([matching])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert len(result) == 1
        assert result[0]["rule_id"] == "rule-on"

    def test_mixed_rules_only_matching_enabled_returned(self):
        """Disabled and wrong-kind siblings are dropped; the one match comes through."""
        from unittest.mock import patch
        import handler as h

        rules = [
            {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-disabled",
             "action_kind": "acknowledge_only", "enabled": False},
            {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-wrong-kind",
             "action_kind": "book_recall_service", "enabled": True},
            {"fleet_id": "fleet-mrdn-01", "rule_id": "rule-match",
             "action_kind": "acknowledge_only", "enabled": True},
        ]
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb(rules)):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert [r["rule_id"] for r in result] == ["rule-match"]


# ---------------------------------------------------------------------------
# Warning 8 (evaluator): EV3, EV4, EV5/EV6, EV14, EV16
# ---------------------------------------------------------------------------

def test_finding_without_fleet_id_is_silently_skipped():
    """EV3: a Finding with no fleet_id must be skipped without dispatch or batch failure.

    Mutation to catch: remove ``if not fleet_id: return`` from _process_record.
    Without the guard the handler proceeds with fleet_id=None, finds patched rules,
    and stores an action — actions becomes non-empty and the assertion fails.
    """
    record = _stream_record()
    del record["dynamodb"]["NewImage"]["fleet_id"]
    result, actions, calls = _run([record])
    assert actions == [], "No action must be stored for a fleet-less Finding"
    assert calls == [], "No dispatch must occur for a fleet-less Finding"
    assert result["batchItemFailures"] == [], "A fleet-less Finding must be skipped, not failed"


def test_finding_without_recommended_action_is_silently_skipped():
    """EV4: a Finding without recommended_action must be skipped.

    Mutation to catch: remove ``if not recommended: return`` from _process_record.
    Without the guard the handler crashes on ``None.get(...)`` → batch failure →
    batchItemFailures non-empty → assertion fails.
    """
    record = _stream_record()
    del record["dynamodb"]["NewImage"]["recommended_action"]
    result, actions, calls = _run([record])
    assert actions == [], "No action for a non-FI Finding"
    assert calls == [], "No dispatch for a non-FI Finding"
    assert result["batchItemFailures"] == [], (
        "A Finding without recommended_action must be silently skipped, not a batch failure"
    )


def test_auto_approved_action_carries_decision_auto_approved():
    """EV5/EV6: stored rule action must carry decision='auto_approved'.

    Without this field the actions Lambda's 409 fi_decision_immutable and the iOS
    exclusion (contract § 12) cannot identify the Action as an FI decision.
    Mutations to catch:
      EV5 — replace 'auto_approved' with another value
      EV6 — drop the 'decision' key entirely
    """
    _, actions, _ = _run([_stream_record()])
    assert actions, "no action stored"
    assert actions[0].get("decision") == "auto_approved", (
        f"Rule action must carry decision='auto_approved'; "
        f"got {actions[0].get('decision')!r}"
    )


def test_auto_approved_action_initial_status_is_pending_execution():
    """EV16: the stored rule action must start with status='pending_execution'.

    A different initial status breaks the queue status derivation (contract § 3)
    and the update_item condition that advances the status after dispatch.
    Mutation to catch: change 'pending_execution' to another status value.
    """
    _, actions, _ = _run([_stream_record()])
    assert actions, "no action stored"
    assert actions[0].get("status") == "pending_execution", (
        f"Rule action must start with status='pending_execution'; "
        f"got {actions[0].get('status')!r}"
    )


def test_non_ccf_put_error_is_reported_as_batch_failure():
    """EV14: a non-CCF put_item error must propagate as a batch failure, not be
    silently swallowed as 'duplicate'.

    The handler re-raises any ClientError whose code is not
    ConditionalCheckFailedException.  A mutation that changes 'raise' to
    'return "duplicate"' causes the record to be silently skipped instead of
    reported — the evaluator would lose an action write with no trace.
    Mutation to catch: replace ``raise`` with ``return "duplicate"`` in the
    non-CCF ClientError branch.
    """
    from botocore.exceptions import ClientError
    from unittest.mock import MagicMock, patch
    import handler as h

    _CCF = type("ConditionalCheckFailedException", (ClientError,), {})

    def fake_put(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "..."}},
            "PutItem",
        )

    mock_table = MagicMock()
    mock_table.put_item.side_effect = fake_put
    mock_table.meta.client.exceptions.ConditionalCheckFailedException = _CCF
    rules = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True, "limit": None}]

    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: mock_table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch", return_value=None):
        result = h.handler(
            _stream_event([_stream_record(sequence_number="seq-throughput-err")]),
            None,
        )

    failures = result.get("batchItemFailures", [])
    assert failures, (
        "A non-CCF put_item error must be reported as a batch failure; "
        "silently swallowing it loses the write with no trace"
    )
    assert any(f.get("itemIdentifier") == "seq-throughput-err" for f in failures), (
        f"The failing record's SequenceNumber must appear in batchItemFailures; "
        f"got {failures}"
    )



# ===========================================================================
# Fix Group 3d fixes — review Cycle 8b Warnings and Suggestions (W2, W3/EC1a,
# EV1b, S6)
# ===========================================================================

# ---------------------------------------------------------------------------
# W2: fleet partition isolation in _get_rules_for_fleet_kind
# ---------------------------------------------------------------------------

class TestGetRulesFleetIsolation:
    """W2: _get_rules_for_fleet_kind must query the Finding's fleet_id partition only.

    Mutant EV2b: Key("fleet_id").eq("fleet-x") — queries a fixed partition regardless
    of the fleet_id argument.  A fake table that returns the same rules regardless of
    the query value cannot catch this; a partition-aware fake (or a ConditionExpression
    capture) can.

    Mutation tests (fail against EV2b mutant):
      - Queried value is captured via ConditionExpressionBuilder; must equal fleet_id arg.
      - A two-fleet fake returns fleet-A rules only for fleet-A queries; EV2b queries
        fleet-x → gets nothing → test fails.
    """

    def test_queried_partition_key_matches_fleet_id_argument(self):
        """W2: the KeyConditionExpression passed to query must use the fleet_id argument.

        Captures the value supplied to Key(...).eq(...) via ConditionExpressionBuilder
        and asserts it equals the fleet_id passed to _get_rules_for_fleet_kind.

        Fails against EV2b mutant: queried value is 'fleet-x', not 'fleet-UNIQUE-W2'.
        """
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        from unittest.mock import MagicMock, patch
        import handler as h

        queried_values: list = []

        def _capturing_query(**kwargs):
            key_cond = kwargs.get("KeyConditionExpression")
            builder = ConditionExpressionBuilder()
            expr = builder.build_expression(key_cond)
            queried_values.extend(expr.attribute_value_placeholders.values())
            return {"Items": []}

        table = MagicMock()
        table.query.side_effect = _capturing_query

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            h._get_rules_for_fleet_kind("fleet-UNIQUE-W2", "acknowledge_only")

        assert "fleet-UNIQUE-W2" in queried_values, (
            "_get_rules_for_fleet_kind must query the fleet_id argument "
            "'fleet-UNIQUE-W2' as the partition key; "
            f"got queried values: {queried_values}. "
            "EV2b mutant queries 'fleet-x' unconditionally."
        )

    def test_fleet_b_rules_never_returned_for_fleet_a_query(self):
        """W2: a partition-aware fake stores two fleets; only fleet-A rules come back.

        The fake honours the partition key by inspecting the KeyConditionExpression
        and returning only the fleet's own rules.  EV2b queries 'fleet-x' →
        the fake returns nothing (no fleet-x rules) → result is empty → assert fails.
        """
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        from unittest.mock import MagicMock, patch
        import handler as h

        fleet_a_rule = {
            "fleet_id": "fleet-A", "rule_id": "rule-A",
            "action_kind": "acknowledge_only", "enabled": True,
        }
        fleet_b_rule = {
            "fleet_id": "fleet-B", "rule_id": "rule-B",
            "action_kind": "acknowledge_only", "enabled": True,
        }
        db: dict = {"fleet-A": [fleet_a_rule], "fleet-B": [fleet_b_rule]}

        def _partition_honoring_query(**kwargs):
            key_cond = kwargs.get("KeyConditionExpression")
            builder = ConditionExpressionBuilder()
            expr = builder.build_expression(key_cond)
            fleet_id = list(expr.attribute_value_placeholders.values())[0]
            return {"Items": db.get(str(fleet_id), [])}

        table = MagicMock()
        table.query.side_effect = _partition_honoring_query

        with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)):
            result = h._get_rules_for_fleet_kind("fleet-A", "acknowledge_only")

        assert len(result) == 1, (
            "Querying fleet-A must return exactly one rule (fleet-A's); "
            f"got {len(result)}: {result}. "
            "EV2b mutant queries 'fleet-x' → gets [] → fails this assertion."
        )
        assert result[0]["fleet_id"] == "fleet-A", (
            f"The returned rule must belong to fleet-A; got fleet_id={result[0].get('fleet_id')!r}"
        )
        assert not any(r["fleet_id"] == "fleet-B" for r in result), (
            "Fleet-B rules must NEVER be returned for a fleet-A query (W2)"
        )


# ---------------------------------------------------------------------------
# W3 / EC1a: evidence is copied from the Finding, not hardcoded as []
# ---------------------------------------------------------------------------

def test_evidence_is_copied_from_finding_when_present():
    """W3/EC1a: the evaluator must copy the Finding's evidence onto the stored action.

    Before the fix (C1), 'evidence' was absent from the stored action dict.
    After the C1 fix, it is set via `finding.get("evidence") or []`.
    This test verifies that the OR branch can never hardcode []:

    Mutation EC1a: replace ``finding.get("evidence") or []`` with ``[]``.
    A Finding with evidence=[{source: tire-sensor}] stores evidence=[] → test fails.
    """
    from unittest.mock import MagicMock, patch
    import handler as h

    # Stream record carrying non-empty evidence (one entry from a tire sensor)
    record = _stream_record(finding_id="FIND-auto-00000001")
    record["dynamodb"]["NewImage"]["evidence"] = {
        "L": [
            {"M": {
                "source": {"S": "tire-sensor"},
                "value": {"S": "15.2 psi"},
            }}
        ]
    }

    written: list = []
    table = MagicMock()
    table.put_item.side_effect = lambda **kw: written.append(kw.get("Item", {})) or {}
    rules = [{"rule_id": "rule-001", "fleet_id": "fleet-mrdn-01",
              "action_kind": "acknowledge_only", "enabled": True}]

    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch", return_value=MagicMock()):
        h.handler(_stream_event([record]), None)

    actions = [i for i in written if str(i.get("action_id", "")).startswith("AVX-ACT-")]
    assert actions, "evaluator must store an action for a matching rule"

    stored_evidence = actions[0].get("evidence")
    assert stored_evidence is not None, (
        "stored action must have an 'evidence' key (EC1a fix)"
    )
    assert len(stored_evidence) > 0, (
        f"evidence must be copied from the Finding (non-empty); "
        f"got evidence={stored_evidence!r}. "
        "EC1a mutant hardcodes [] → this assertion fails."
    )
    assert stored_evidence[0].get("source") == "tire-sensor", (
        f"evidence[0].source must match the Finding's evidence entry; "
        f"got {stored_evidence[0]!r}"
    )


# ---------------------------------------------------------------------------
# EV1b: enabled is True strictness — truthy non-bool must not match
# ---------------------------------------------------------------------------

class TestEnabledIsTrueStrictness:
    """EV1b: _get_rules_for_fleet_kind uses ``enabled is True``, not a truthy check.

    The rules route only stores booleans, but another writer might store a truthy
    non-boolean (e.g. 'false', 1, []).  Defence in depth: only Python True matches.

    Mutant EV1b: ``bool(r.get('enabled'))`` (truthy) instead of ``r.get('enabled') is True``.
    A rule with enabled='false' (truthy string) would pass the truthy check but must not
    pass the ``is True`` check.  The test asserts result == [] for a truthy-non-bool rule.
    """

    def _fake_ddb(self, rules):
        from unittest.mock import MagicMock
        table = MagicMock()
        table.query.return_value = {"Items": rules}
        return MagicMock(Table=lambda _: table)

    def test_truthy_string_false_is_not_enabled(self):
        """enabled='false' (truthy string) must NOT match enabled is True.

        Fails against EV1b mutant: bool('false') is True → rule returned.
        """
        from unittest.mock import patch
        import handler as h

        truthy_string_rule = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-truthy",
            "action_kind": "acknowledge_only",
            "enabled": "false",  # truthy string, not True
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([truthy_string_rule])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert result == [], (
            f"enabled='false' (truthy string) must NOT be treated as enabled=True; "
            f"got result={result!r}. "
            "EV1b mutant: bool('false') is True → rule returned → test fails."
        )

    def test_truthy_int_one_is_not_enabled(self):
        """enabled=1 (truthy int) must NOT match enabled is True.

        Fails against EV1b mutant: bool(1) is True → rule returned.
        """
        from unittest.mock import patch
        import handler as h

        truthy_int_rule = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-int",
            "action_kind": "acknowledge_only",
            "enabled": 1,  # truthy int, not True
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([truthy_int_rule])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert result == [], (
            f"enabled=1 (truthy int) must NOT be treated as enabled=True; "
            f"got result={result!r}. "
            "EV1b mutant: bool(1) is True → rule returned → test fails."
        )

    def test_python_true_is_enabled(self):
        """Sanity: enabled=True (Python bool) is still treated as enabled."""
        from unittest.mock import patch
        import handler as h

        enabled_rule = {
            "fleet_id": "fleet-mrdn-01", "rule_id": "rule-on",
            "action_kind": "acknowledge_only", "enabled": True,
        }
        with patch.object(h, "_get_ddb", return_value=self._fake_ddb([enabled_rule])):
            result = h._get_rules_for_fleet_kind("fleet-mrdn-01", "acknowledge_only")

        assert len(result) == 1, f"enabled=True must still pass; got {result!r}"


# ---------------------------------------------------------------------------
# S6: evaluator takes target_system from policy row and skips mismatched Finding
# ---------------------------------------------------------------------------

def test_mismatched_target_system_is_skipped():
    """S6: a recommendation whose target_system disagrees with the policy row is skipped.

    For acknowledge_only, policy.target_system='none'.  A Finding that recommends
    target_system='cms' disagrees (data-integrity issue); the evaluator must skip it
    rather than dispatch to the wrong system.

    Mutation (unfixed code): target_system taken from the Finding → no skip guard →
    action stored with target_system='cms'; assert actions == [] fails.
    """
    record = _stream_record()
    # Inject a target_system that disagrees with the policy row for acknowledge_only
    record["dynamodb"]["NewImage"]["recommended_action"]["M"]["target_system"] = {"S": "cms"}

    _, actions, calls = _run([record])

    assert actions == [], (
        "A recommendation with target_system='cms' for acknowledge_only "
        "(policy says 'none') must be skipped; no action must be stored. "
        "S6 unfixed code takes target_system from the Finding → no skip → action stored."
    )
    assert calls == [], (
        "dispatch must not be called when the Finding's target_system disagrees "
        "with the policy row (S6)"
    )


def test_correct_target_system_is_taken_from_policy_not_finding():
    """S6: the stored action's target_system must equal the policy row's value, not the Finding's.

    Both the Finding ('none') and the policy row ('none') agree for acknowledge_only.
    This verifies that even when they agree, the stored value comes from the policy row.

    Mutation (S6-naive fix): target_system from Finding survives when they agree but
    could still carry a stale/modified value; the code change ensures the authoritative
    source is always policy.  The two-case structure (agree → store, disagree → skip)
    is verified by this test and test_mismatched_target_system_is_skipped together.
    """
    # Record with the correct target_system matching policy ('none' for acknowledge_only)
    record = _stream_record()  # target_system='none' by default in _stream_record
    _, actions, _ = _run([record])

    assert actions, "evaluator must store an action when target_system matches policy"
    stored_ts = actions[0].get("target_system")
    assert stored_ts == "none", (
        f"stored action.target_system must equal the policy row value 'none'; "
        f"got {stored_ts!r}"
    )


def test_error_class_is_folded_into_note_like_the_findings_lambda():
    """Group 2 security review S4: one convention for both writers. The class name
    (never a message) goes into `note`; it is never stored as its own key."""
    from agents.executors.base import ExecuteResult  # noqa: PLC0415

    import handler as h  # noqa: PLC0415

    result = ExecuteResult.terminal_error(last_error_class="RuntimeError")
    entry = h._build_audit_entry(result, target_system="none", at="2026-10-02T00:00:00Z")
    assert "last_error_class=RuntimeError" in entry["note"]
    assert "last_error_class" not in entry


# ---------------------------------------------------------------------------
# Contract § 10b: the target's own refusal sentence survives the projection
# (issue 2026-10-23-executor-drops-cms-refusal-reason)
# ---------------------------------------------------------------------------


def test_audit_entry_keys_equal_the_schema_audit_entry_properties():
    """`_AUDIT_ENTRY_KEYS` is a hand copy of the schema; pin it to the file, as the
    findings Lambda does. A key missing here is silently dropped from the stored row."""
    import handler as h  # noqa: PLC0415

    path = os.path.join(_REPO_ROOT, "agents", "tier2", "schemas", "action.schema.json")
    with open(path) as fh:
        props = json.load(fh)["$defs"]["audit_entry"]["properties"]
    assert set(h._AUDIT_ENTRY_KEYS) == set(props)


def test_target_reason_is_kept_in_the_stored_entry():
    """A rule-approved move CMS refused keeps CMS's sentence beside the § 10a one."""
    import dataclasses  # noqa: PLC0415

    from agents.executors.base import ExecuteResult  # noqa: PLC0415

    import handler as h  # noqa: PLC0415

    sentence = "Vehicle MRDVEH00000000021 is grounded and can't be moved."
    result = dataclasses.replace(
        ExecuteResult.refused(last_error_status=409, last_error_class="HTTPError"),
        failure_reason="CMS refused the request (HTTP 409).",
        target_reason=sentence,
    )
    entry = h._build_audit_entry(result, target_system="cms", at="2026-10-23T00:00:00Z")
    assert entry["target_reason"] == sentence
    assert entry["failure_reason"] == "CMS refused the request (HTTP 409)."
    assert entry["error_status"] == 409


# ===========================================================================
# F1.5 S2: expiry check must use parsed datetimes, not string comparison
# ===========================================================================


def _run_with_now(records, now_ms_str, rules=_ACK_RULE):
    """Like _run but patches _now_iso to a fixed string for testing expiry edge cases."""
    from datetime import datetime as _real_dt, timezone as _tz  # noqa: PLC0415
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    import handler as h  # noqa: PLC0415

    # Parse the fixed "now" from the ms string so we can also patch datetime.now
    _fixed_now = _real_dt.fromisoformat(now_ms_str.replace("Z", "+00:00"))

    class _MockDatetime(_real_dt):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _fixed_now

        @classmethod
        def fromisoformat(cls, s):  # type: ignore[override]
            return _real_dt.fromisoformat(s)

    table = MagicMock()
    table.put_item.return_value = {}
    calls: list = []
    with patch.object(h, "_now_iso", return_value=now_ms_str), \
         patch("handler.datetime", _MockDatetime), \
         patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _: table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=rules), \
         patch("agents.executors.registry.dispatch",
               side_effect=lambda a, c, **kw: calls.append(a)):
        result = h.handler(_stream_event(records), None)
    actions = [c.kwargs["Item"] for c in table.put_item.call_args_list
               if str(c.kwargs.get("Item", {}).get("action_id", "")).startswith("AVX-ACT-")]
    return result, actions, calls


def test_seconds_precision_expires_at_400ms_past_is_expired():
    """S2 red: expires_at='...T12:00:00Z' must be expired when now='...T12:00:00.400Z'.

    String comparison bug: '2026-10-27T12:00:00Z' <= '2026-10-27T12:00:00.400Z'
    evaluates to False because ord('Z')=90 > ord('.')=46, so the current code
    incorrectly treats an expired recommendation as still live.

    Datetime comparison (the fix): parsed 12:00:00 UTC < parsed 12:00:00.400 UTC → expired.

    This test FAILS on the current string-compare implementation.
    """
    fixed_now_ms = "2026-10-27T12:00:00.400Z"
    # expires_at is seconds-precision, 400 ms before fixed_now_ms.
    # String compare: "...00Z" > "...00.400Z" → NOT expired (bug).
    # Datetime parse: 12:00:00 < 12:00:00.400 → IS expired (correct).
    expires_at = "2026-10-27T12:00:00Z"

    record = _stream_record()
    record["dynamodb"]["NewImage"]["expires_at"] = {"S": expires_at}

    _, actions, _ = _run_with_now([record], fixed_now_ms)

    assert actions == [], (
        f"expires_at='{expires_at}' is 400 ms before now='{fixed_now_ms}': "
        "string comparison '...00Z' > '...00.400Z' says NOT expired (bug). "
        "Datetime comparison must be used so this is treated as expired. "
        f"Got {len(actions)} action(s) instead of 0."
    )


def test_malformed_expires_at_is_treated_as_expired():
    """S2 red: a malformed expires_at string must be treated as expired (fail-closed).

    String comparison: 'malformed-date' > '2026-...' (m > 2 in ASCII), so current
    code incorrectly says NOT expired and tries to auto-approve the finding.

    Datetime parse (the fix): fromisoformat raises ValueError → caught → expired.

    This test FAILS on the current string-compare implementation.
    """
    fixed_now_ms = "2026-10-27T12:00:00.400Z"
    expires_at = "malformed-date"  # not a valid ISO timestamp

    record = _stream_record()
    record["dynamodb"]["NewImage"]["expires_at"] = {"S": expires_at}

    _, actions, _ = _run_with_now([record], fixed_now_ms)

    assert actions == [], (
        f"expires_at='{expires_at}' is malformed: must be treated as expired (fail-closed). "
        "Current string comparison allows it through because 'malformed' > '2026-...'. "
        f"Got {len(actions)} action(s) instead of 0."
    )


def test_naive_expires_at_is_treated_as_expired():
    """G2.2 S3/security-S1: a naive expires_at (no offset) must be treated as expired.

    Uses a FUTURE naive value so the test distinguishes "naive treated as expired" from
    "naive silently read as UTC and already past".  With the mutant
    ``_expires_dt = _expires_dt.replace(tzinfo=timezone.utc)`` a future naive becomes a
    future UTC datetime → NOT expired → action taken → this test fails.

    ``datetime.fromisoformat("2099-01-01T00:00:00")`` returns a naive datetime (no tzinfo).
    Comparing ``naive <= datetime.now(timezone.utc)`` raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.

    The correct fix catches TypeError and treats the record as expired (fail-closed).
    """
    fixed_now_ms = "2026-10-27T12:00:00.400Z"
    # Future naive datetime: if read as UTC it would be far in the future → NOT expired.
    # Only the correct fail-closed path (TypeError → expired) makes this pass.
    expires_at = "2099-01-01T00:00:00"  # valid ISO, no 'Z' / '+00:00', far future

    record = _stream_record()
    record["dynamodb"]["NewImage"]["expires_at"] = {"S": expires_at}

    result, actions, _ = _run_with_now([record], fixed_now_ms)

    # No action must be written — the naive record is treated as expired (fail-closed).
    assert actions == [], (
        f"expires_at='{expires_at}' is naive (no offset): must be treated as expired. "
        f"Got {len(actions)} action(s) instead of 0."
    )
    # Must not appear as a batchItemFailure: TypeError is caught, not propagated.
    assert result.get("batchItemFailures", []) == [], (
        f"expires_at='{expires_at}' (naive datetime) raised TypeError instead of being "
        f"treated as expired. batchItemFailures={result.get('batchItemFailures')!r}"
    )


def test_date_only_expires_at_is_treated_as_expired():
    """c3 W2: a date-only expires_at (no time component) must be treated as expired.

    ``datetime.fromisoformat("2099-01-01")`` returns a naive datetime at midnight.
    Like the datetime-naive case, comparing with ``datetime.now(timezone.utc)`` raises
    ``TypeError``, which must be caught and treated as expired (fail-closed).

    Uses a FUTURE date so the test fails under the naive→UTC mutant (future date read as
    UTC would not be expired → action taken).
    """
    fixed_now_ms = "2026-10-27T12:00:00.400Z"
    # Future date-only string: fromisoformat returns a naive datetime at midnight.
    expires_at = "2099-01-01"  # date only, no time, no offset, far future

    record = _stream_record()
    record["dynamodb"]["NewImage"]["expires_at"] = {"S": expires_at}

    result, actions, _ = _run_with_now([record], fixed_now_ms)

    assert actions == [], (
        f"expires_at='{expires_at}' is a date-only string: must be treated as expired "
        f"(fail-closed). Got {len(actions)} action(s) instead of 0."
    )
    assert result.get("batchItemFailures", []) == [], (
        f"expires_at='{expires_at}' (date-only → naive datetime) raised TypeError instead "
        f"of being treated as expired. batchItemFailures={result.get('batchItemFailures')!r}"
    )
