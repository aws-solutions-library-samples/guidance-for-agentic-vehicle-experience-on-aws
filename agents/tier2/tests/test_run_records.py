"""Red-test skeletons: run records written by the Tier 2 runner — spec D10.

Expected interface (Group 2 creates agents/tier2/run_records.py):

    # agents/tier2/run_records.py

    def write_run_record(
        *,
        agent_id: str,          # e.g. "agent-2-vehicle-health" (the Findings agent_id; CMS joins on it)
        component: str,         # e.g. "battery", "tires", "brakes"
        started_at: str,        # ISO-8601 UTC
        finished_at: str,       # ISO-8601 UTC
        status: str,            # "completed" | "aborted"
        artifacts_written: int,
        duplicates_skipped: int,
        agent_version: str,
        ddb_client,             # injected boto3 DynamoDB client
        table_name: str,        # from env var AVX_AGENT_RUNS_TABLE
    ) -> None:
        \"\"\"Write one run record to vsa-{stage}-avx-agent-runs.

        pk attribute "agent_component" = "{agent_id}#{component}"
        sk attribute "started_at"   (contract § 8)

        A failed write is logged and does not fail the run (D10).
        \"\"\"

    def get_runs_table_name() -> str:
        \"\"\"Read AVX_AGENT_RUNS_TABLE env var; raise RuntimeError if absent.\"\"\"

    The runner (agents/tier2/runner.py) calls write_run_record() once per component
    per invocation via a new optional injected parameter:
        run(stage, ..., run_record_writer=<callable>)
    When run_record_writer is None, write_run_record is used by default.

Tests are RED until agents/tier2/run_records.py exists (ImportError on import).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


AGENT_ID = "agent-2-vehicle-health"


def _import_run_records():
    """Import run_records; fails with pytest.fail on ImportError (red until G2)."""
    try:
        from agents.tier2 import run_records  # noqa: PLC0415
        return run_records
    except ImportError as exc:
        pytest.fail(
            f"agents.tier2.run_records not implemented yet (Group 2): {exc}"
        )


# ---------------------------------------------------------------------------
# get_runs_table_name: fail closed if env var unset
# ---------------------------------------------------------------------------

class TestTableNameFromEnv:
    """AVX_AGENT_RUNS_TABLE env var is required; fail closed if absent."""

    def test_get_runs_table_name_returns_env_var(self, monkeypatch):
        """Returns the table name when AVX_AGENT_RUNS_TABLE is set."""
        run_records = _import_run_records()  # RED: ImportError until module exists
        monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-staging-avx-agent-runs")
        assert run_records.get_runs_table_name() == "vsa-staging-avx-agent-runs"

    def test_get_runs_table_name_raises_when_unset(self, monkeypatch):
        """Raises RuntimeError when AVX_AGENT_RUNS_TABLE is absent (fail closed)."""
        run_records = _import_run_records()  # RED.
        monkeypatch.delenv("AVX_AGENT_RUNS_TABLE", raising=False)
        with pytest.raises((RuntimeError, KeyError, ValueError)):
            run_records.get_runs_table_name()


# ---------------------------------------------------------------------------
# write_run_record: PK / SK / fields
# ---------------------------------------------------------------------------

class TestWriteRunRecord:
    """write_run_record puts an item with the expected PK, SK and fields."""

    def _call(self, run_records, **kwargs):
        defaults = dict(
            agent_id=AGENT_ID,
            component="battery",
            started_at="2026-10-02T02:00:00Z",
            finished_at="2026-10-02T02:05:00Z",
            status="completed",
            artifacts_written=10,
            duplicates_skipped=2,
            agent_version="0.1.0",
            ddb_client=MagicMock(),
            table_name="vsa-staging-avx-agent-runs",
        )
        defaults.update(kwargs)
        return run_records.write_run_record(**defaults)

    def test_write_calls_ddb_put_item(self):
        """write_run_record calls ddb_client.put_item (or Table.put_item)."""
        run_records = _import_run_records()  # RED.
        mock_ddb = MagicMock()
        self._call(run_records, ddb_client=mock_ddb)
        # After implementation: either mock_ddb.put_item or mock_ddb.Table().put_item
        # was called.
        assert mock_ddb.put_item.called or mock_ddb.Table.return_value.put_item.called, (
            "write_run_record must call ddb.put_item (or ddb.Table().put_item)"
        )

    def test_pk_is_agent_id_hash_component(self):
        """The item's PK is '{agent_id}#{component}'."""
        run_records = _import_run_records()  # RED.
        put_calls: list = []
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = lambda **kw: put_calls.append(kw)
        # Also intercept Table().put_item:
        mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_calls.append(kw)

        self._call(run_records,
                   agent_id=AGENT_ID,
                   component="battery",
                   ddb_client=mock_ddb)

        assert put_calls, "ddb put_item was not called"
        item = put_calls[0].get("Item", {})
        assert item.get("agent_component") == f"{AGENT_ID}#battery", (
            f"pk attribute must be agent_component = 'agent_id#component', got {item!r}"
        )
        assert "pk" not in item, "the pk attribute is named agent_component, not pk"


    def test_sk_is_started_at(self):
        """The item's SK is started_at."""
        run_records = _import_run_records()  # RED.
        put_calls: list = []
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = lambda **kw: put_calls.append(kw)
        mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_calls.append(kw)

        self._call(run_records,
                   started_at="2026-10-02T02:00:00Z",
                   ddb_client=mock_ddb)

        assert put_calls
        item = put_calls[0].get("Item", {})
        assert item.get("started_at") == "2026-10-02T02:00:00Z"
        assert "sk" not in item, "the sk attribute is named started_at, not sk"

    def test_aborted_run_is_recorded_as_aborted(self):
        """An aborted run still writes a record, with status 'aborted' (contract § 8)."""
        run_records = _import_run_records()  # RED.
        put_calls: list = []
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = lambda **kw: put_calls.append(kw)
        mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_calls.append(kw)

        self._call(run_records, status="aborted", artifacts_written=0, ddb_client=mock_ddb)

        assert put_calls
        assert put_calls[0].get("Item", {}).get("status") == "aborted"

    @pytest.mark.parametrize("field", [
        "started_at", "finished_at", "status",
        "artifacts_written", "duplicates_skipped", "agent_version",
    ])
    def test_required_fields_present_in_item(self, field: str):
        """The item carries all required D10 fields."""
        run_records = _import_run_records()  # RED.
        put_calls: list = []
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = lambda **kw: put_calls.append(kw)
        mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_calls.append(kw)

        self._call(run_records, ddb_client=mock_ddb)

        assert put_calls
        item = put_calls[0].get("Item", {})
        assert field in item, (
            f"run record item must contain field {field!r} (D10); "
            f"got keys: {sorted(item)}"
        )


# ---------------------------------------------------------------------------
# Failed write does not fail the run
# ---------------------------------------------------------------------------

class TestRealServiceModel:
    """A stub cannot fail the way the service fails (spec-workflow live-verification
    gate). botocore's Stubber validates parameters against the real DynamoDB model, so
    an Item the service would reject fails here instead of inside the swallowed
    `except` in production. Caught in review of Group 2: the first implementation passed
    plain values to the low-level client, which every MagicMock test accepted."""

    def test_write_validates_against_the_real_service_model(self, caplog):
        import logging

        import boto3
        from botocore.stub import Stubber

        from agents.tier2.run_records import _compute_run_ttl  # noqa: PLC0415

        run_records = _import_run_records()
        resource = boto3.resource("dynamodb", region_name="us-west-2")
        # The Stubber checks expected params before the resource's serializer runs, so
        # they are plain values here. Model validation runs after the serializer, on the
        # typed form; a write the service would reject raises there, and the swallowed
        # warning below fails the test.
        finished_at = "2026-10-02T02:05:00Z"
        expected_item = {
            "agent_component": f"{AGENT_ID}#battery",
            "started_at": "2026-10-02T02:00:00Z",
            "finished_at": finished_at,
            "status": "completed",
            "artifacts_written": 10,
            "duplicates_skipped": 2,
            "agent_version": "0.1.0",
            # T1.5: TTL field added.
            "ttl": _compute_run_ttl(finished_at),
        }
        with Stubber(resource.meta.client) as stub, caplog.at_level(logging.WARNING):
            stub.add_response("put_item", {},
                              {"TableName": "vsa-staging-avx-agent-runs",
                               "Item": expected_item,
                               "ConditionExpression": "attribute_not_exists(agent_component)"})
            run_records.write_run_record(
                agent_id=AGENT_ID, component="battery",
                started_at="2026-10-02T02:00:00Z", finished_at=finished_at,
                status="completed", artifacts_written=10, duplicates_skipped=2,
                agent_version="0.1.0", ddb_client=resource,
                table_name="vsa-staging-avx-agent-runs")
            stub.assert_no_pending_responses()
        assert not [r for r in caplog.records if "failed to write" in r.message], (
            "the write must reach the service model, not be swallowed")

    def test_runner_builds_a_resource_for_the_default_writer(self, monkeypatch):
        """The default path hands write_run_record a resource, not a low-level client."""
        import boto3

        from agents.tier2 import runner

        seen = {}
        monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-test-avx-agent-runs")
        monkeypatch.setattr(boto3, "client", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the run-record writer must not use the low-level client")))
        monkeypatch.setattr(boto3, "resource", lambda name, **k: seen.setdefault("res", MagicMock()))
        runner._emit_run_record(None, component="battery",
                                summary={"started_at": "2026-10-02T02:00:00Z",
                                         "finished_at": "2026-10-02T02:05:00Z",
                                         "status": "completed", "artifacts_written": 1,
                                         "duplicates_skipped": 0},
                                agent_version="0.1.0")
        assert seen["res"].Table.return_value.put_item.called


# ---------------------------------------------------------------------------

class TestFailedWriteIsSafe:
    """A DynamoDB write failure is logged and does not propagate (D10)."""

    def test_ddb_error_is_caught_not_raised(self, caplog):
        """write_run_record catches a DDB error, logs it, and returns without raising."""
        import logging

        run_records = _import_run_records()  # RED.

        failing_ddb = MagicMock()
        failing_ddb.put_item.side_effect = Exception("simulated DDB failure")
        failing_ddb.Table.return_value.put_item.side_effect = Exception("simulated DDB failure")

        # Must NOT raise — D10: "a failed write is logged and does not fail the run".
        try:
            with caplog.at_level(logging.WARNING):
                run_records.write_run_record(
                    agent_id=AGENT_ID,
                    component="tires",
                    started_at="2026-10-02T02:00:00Z",
                    finished_at="2026-10-02T02:05:00Z",
                    status="completed",
                    artifacts_written=5,
                    duplicates_skipped=0,
                    agent_version="0.1.0",
                    ddb_client=failing_ddb,
                    table_name="vsa-staging-avx-agent-runs",
                )
        except Exception as exc:
            pytest.fail(
                f"write_run_record must not raise on a DDB failure (D10), "
                f"but raised {type(exc).__name__}: {exc}"
            )

        # A warning should have been logged about the failure:
        assert any("run" in rec.message.lower() or "write" in rec.message.lower()
                   for rec in caplog.records), (
            "A failed write must be logged (D10)"
        )


# ---------------------------------------------------------------------------
# Integration: runner.py calls write_run_record once per component
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Integration: runner.py calls write_run_record once per component
# ---------------------------------------------------------------------------

class TestRunnerIntegration:
    """runner.run() / run_diagnostics() write a run record once per invocation."""

    # ------------------------------------------------------------------
    # Shared fakes (no AWS access)
    # ------------------------------------------------------------------

    class _FakeSource:
        def fetch_findings(self, vins):
            return []
        def select_vins(self, *, cursor, limit):
            return [], None

    class _FakeSourceOneVin:
        """Returns one row so the judgment loop runs at least once."""
        def fetch_findings(self, vins):
            return [{"vin": "MRDVEH00000000015", "component": "battery",
                     "state_of_health_pct": "92.0", "battery_age_days": "1095"}]
        def select_vins(self, *, cursor, limit):
            return [], None

    class _FakeWriter:
        def write_finding(self, finding, **kw):
            return {"action": "put", "finding_id": "FIND-fake"}
        def put(self, artifact):
            return True
        def put_raw(self, item):
            pass

    @staticmethod
    def _simple_agent_fn(vin, rows):
        """Minimal agent double that returns valid agent fields."""
        return {
            "finding": f"Battery for {vin} is healthy.",
            "severity_hint": "routine",
            "confidence": "high",
            "evidence": [{"source": "energy_usage", "value": {"soh": 92.0}, "provenance": "live"}],
        }

    @staticmethod
    def _collect_writes():
        """Return (write_calls list, writer callable).

        review FG3b S8: capture all keyword arguments (including artifacts_written
        and duplicates_skipped) so mutants that zero those fields are caught.
        """
        write_calls: list = []
        def _fake_writer(*, agent_id, component, status, **kwargs):
            write_calls.append({"agent_id": agent_id, "component": component, "status": status, **kwargs})
        return write_calls, _fake_writer

    # ------------------------------------------------------------------
    # 1. Normal completion path: exactly one record, correct agent_component
    # ------------------------------------------------------------------

    def test_normal_completion_writes_exactly_one_record(self):
        """runner.run() writes exactly one run record on normal completion (full loop path)."""
        from agents.tier2 import runner

        _import_run_records()  # confirm module exists

        write_calls, fake_writer = self._collect_writes()

        # Use a source that returns one row so the judgment loop runs, exercising
        # the final _emit_run_record at the end of run() (not the early-exit path).
        runner.run(
            "staging",
            source=self._FakeSourceOneVin(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="battery",
            agent_fn=self._simple_agent_fn,
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1, (
            f"Expected exactly one run record for a normal completion, got {write_calls}"
        )
        rec = write_calls[0]
        assert rec["agent_id"] == AGENT_ID, (
            f"agent_id must be the literal '{AGENT_ID}', got {rec['agent_id']!r}"
        )
        assert rec["component"] == "battery", (
            f"component must be 'battery', got {rec['component']!r}"
        )
        assert rec["status"] == "completed", (
            f"status must be 'completed' on normal exit, got {rec['status']!r}"
        )

    # ------------------------------------------------------------------
    # 2. Budget-abort path: exactly one record with status="aborted"
    # ------------------------------------------------------------------

    def test_budget_abort_writes_aborted_record(self):
        """runner.run() writes one aborted record when the token budget is exceeded."""
        from agents.tier2 import runner

        write_calls, fake_writer = self._collect_writes()

        # agent_fn returns 2 tokens; max_tokens=1 → BudgetExceeded after first VIN
        def _spendthrift_agent(vin, rows):
            return {"finding": "partial text", "_tokens_used": 2}

        # No exception propagates out of run() on budget abort — it returns normally.
        result = runner.run(
            "staging",
            source=self._FakeSourceOneVin(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="battery",
            max_tokens=1,
            agent_fn=_spendthrift_agent,
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1, (
            f"Expected exactly one run record after budget abort, got {write_calls}"
        )
        rec = write_calls[0]
        assert rec["status"] == "aborted", (
            f"Budget-aborted run must record status='aborted', got {rec['status']!r}"
        )
        assert rec["agent_id"] == AGENT_ID, (
            f"agent_id must be '{AGENT_ID}', got {rec['agent_id']!r}"
        )
        # The summary itself also reports aborted:
        assert result.get("status") == "aborted", (
            f"run() return value must have status='aborted', got {result.get('status')!r}"
        )

    # ------------------------------------------------------------------
    # 3. Exactly one record per run (not two)
    # ------------------------------------------------------------------

    def test_at_most_one_record_per_run(self):
        """runner.run() must NOT write two run records for a single invocation."""
        from agents.tier2 import runner

        write_calls, fake_writer = self._collect_writes()

        runner.run(
            "staging",
            source=self._FakeSource(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="tires",
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1, (
            f"runner.run() must write exactly one run record per invocation; "
            f"got {len(write_calls)}: {write_calls}"
        )

    # ------------------------------------------------------------------
    # 4. Diagnostics component writes a run record
    # ------------------------------------------------------------------

    def test_diagnostics_component_writes_run_record(self):
        """run_diagnostics() writes exactly one run record (W6 regression guard)."""
        from agents.tier2 import runner

        write_calls, fake_writer = self._collect_writes()

        class _FakeDtcSource:
            def fetch_groups(self):
                return []

        runner.run_diagnostics(
            "staging",
            source=_FakeDtcSource(),
            avx_writer=self._FakeWriter(),
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1, (
            f"run_diagnostics() must write exactly one run record; got {write_calls}"
        )
        rec = write_calls[0]
        assert rec["component"] == "diagnostics", (
            f"component must be 'diagnostics', got {rec['component']!r}"
        )
        assert rec["agent_id"] == AGENT_ID, (
            f"agent_id must be '{AGENT_ID}', got {rec['agent_id']!r}"
        )
        assert rec["status"] == "completed", (
            f"status must be 'completed' on clean diagnostics run, got {rec['status']!r}"
        )

    # ------------------------------------------------------------------
    # Legacy (keep): runner.run() accepts run_record_writer kwarg
    # ------------------------------------------------------------------

    def test_runner_calls_write_run_record_per_component(self):
        """runner.run() calls the injected run_record_writer once per component."""
        from agents.tier2 import runner  # noqa: PLC0415  (always exists)

        _import_run_records()  # RED.

        write_calls, fake_writer = self._collect_writes()

        # runner.run() must accept a run_record_writer kwarg (Group 2 adds this):
        runner.run(
            "staging",
            source=self._FakeSource(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="battery",
            run_record_writer=fake_writer,
        )

        assert any(c["component"] == "battery" for c in write_calls), (
            f"Expected a run record for component='battery', got {write_calls}"
        )


    # ------------------------------------------------------------------
    # 5. run_diagnostics() failure path writes an 'aborted' record (W3/W6)
    # ------------------------------------------------------------------

    def test_run_diagnostics_failure_records_aborted(self):
        """run_diagnostics() must write exactly one 'aborted' record when a write fails.

        review FG3b W3 / W6 residual: the failure path (avx_writer.write_finding
        raises → failures > 0 → DiagnosticsRunError) was untested.  Two mutants
        survived: no record emitted before the raise, and the record forced to
        status='completed'.

        Mutation guards:
        - Drop the _emit_run_record before the raise → len(write_calls) == 0 fails.
        - Force obs.finish(abort_reason=None) → rec["status"] == "completed" fails.
        """
        from agents.tier2 import runner
        from agents.health.cms_dtc_source import DiagnosticsGroup

        write_calls, fake_writer = self._collect_writes()

        class _OneGroupSource:
            def fetch_groups(self):
                # component="diagnostics" bypasses _live_row_is_adp_written check,
                # so no avx_writer attributes are required for the redirect step.
                return [DiagnosticsGroup(
                    vin="MRDVEH00000000015",
                    component="diagnostics",
                    dtcs=[{
                        "code": "P0420",
                        "level": "P3",
                        "first_seen_at": "",
                        "system": "engine",
                        "description": "Catalyst below threshold",
                    }],
                )]

        class _FailingWriter:
            """Raises on every write_finding call to trigger the failure path."""
            def write_finding(self, finding, **kwargs):
                raise RuntimeError("injected avx write failure for test")
            def put(self, artifact):
                return True
            def put_raw(self, item):
                pass

        import pytest as _pytest
        with _pytest.raises(runner.DiagnosticsRunError):
            runner.run_diagnostics(
                "staging",
                source=_OneGroupSource(),
                avx_writer=_FailingWriter(),
                run_record_writer=fake_writer,
            )

        assert len(write_calls) == 1, (
            f"run_diagnostics() must write exactly one record on failure path; "
            f"got {len(write_calls)}: {write_calls}"
        )
        rec = write_calls[0]
        assert rec["status"] == "aborted", (
            f"run_diagnostics() failure record must have status='aborted'; "
            f"got {rec['status']!r}"
        )
        assert rec["component"] == "diagnostics", (
            f"component must be 'diagnostics'; got {rec['component']!r}"
        )
        assert rec["agent_id"] == AGENT_ID, (
            f"agent_id must be '{AGENT_ID}'; got {rec['agent_id']!r}"
        )

    # ------------------------------------------------------------------
    # 6. run() early return for rows with no VIN writes a 'completed' record (W3/W7)
    # ------------------------------------------------------------------

    def test_run_no_vin_rows_records_completed(self):
        """runner.run() must write exactly one 'completed' record for rows with no VIN.

        review FG3b W3 / W7 residual: the early-return path for rows that have no
        'vin' key was untested.  Dropping the _emit_run_record before the return
        survived every suite.

        Mutation guard: remove the _emit_run_record in the ``if not vin_rows:``
        branch → len(write_calls) == 0 fails.
        """
        from agents.tier2 import runner

        write_calls, fake_writer = self._collect_writes()

        class _NoVinSource:
            """Returns rows that carry no 'vin' key."""
            def fetch_findings(self, vins):
                return [{"component": "battery", "state_of_health_pct": "90"}]
            def select_vins(self, *, cursor, limit):
                return [], None

        runner.run(
            "staging",
            source=_NoVinSource(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="battery",
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1, (
            f"run() must write exactly one record for no-VIN rows; "
            f"got {len(write_calls)}: {write_calls}"
        )
        rec = write_calls[0]
        assert rec["status"] == "completed", (
            f"no-VIN-rows path must write status='completed'; got {rec['status']!r}"
        )

    # ------------------------------------------------------------------
    # 7. run_record_writer receives artifacts_written and duplicates_skipped (S8)
    # ------------------------------------------------------------------

    def test_run_record_captures_artifacts_and_duplicates_counters(self):
        """_emit_run_record must forward artifacts_written and duplicates_skipped.

        review FG3b S8: _collect_writes previously captured only agent_id,
        component and status, so a mutant zeroing artifacts_written in
        _emit_run_record survived every integration test.

        Mutation guard: set artifacts_written=0 unconditionally in _emit_run_record
        → this test fails when a real write happens in a different suite; here with
        FakeSource(returns []) we assert the counters are forwarded as 0, which
        confirms the kwargs are captured at all (preventing the field from being
        silently absent).
        """
        from agents.tier2 import runner

        write_calls, fake_writer = self._collect_writes()

        runner.run(
            "staging",
            source=self._FakeSource(),
            writer=self._FakeWriter(),
            vins=["MRDVEH00000000015"],
            component="battery",
            run_record_writer=fake_writer,
        )

        assert len(write_calls) == 1
        rec = write_calls[0]
        assert "artifacts_written" in rec, (
            "artifacts_written must be forwarded to run_record_writer; "
            f"got keys: {list(rec.keys())}"
        )
        assert "duplicates_skipped" in rec, (
            "duplicates_skipped must be forwarded to run_record_writer; "
            f"got keys: {list(rec.keys())}"
        )
        # FakeSource returns no rows → no writes, no skips.
        assert rec["artifacts_written"] == 0
        assert rec["duplicates_skipped"] == 0


def test_missing_runs_table_is_logged_as_its_own_error(monkeypatch, caplog):
    """Group 2 security review S3: an unset AVX_AGENT_RUNS_TABLE is a deploy defect.
    No record and no exception (D10), but an ERROR with its own event, not the
    write-failure warning."""
    import logging

    from agents.tier2 import runner

    monkeypatch.delenv("AVX_AGENT_RUNS_TABLE", raising=False)
    with caplog.at_level(logging.WARNING):
        runner._emit_run_record(None, component="battery",
                                summary={"started_at": "2026-10-02T02:00:00Z",
                                         "finished_at": "2026-10-02T02:01:00Z",
                                         "status": "completed"},
                                agent_version="0.1.0")
    events = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert any(lvl == "ERROR" and "event=run_record_table_unset" in msg for lvl, msg in events), events
    assert not any("event=run_record_write_failed" in msg for _, msg in events), events



# ---------------------------------------------------------------------------
# Group 3: the RunRecordsWritten heartbeat the run-staleness alarms watch (D10)
# ---------------------------------------------------------------------------

_SUMMARY = {"started_at": "2026-10-02T02:00:00Z", "finished_at": "2026-10-02T02:01:00Z",
            "status": "completed"}


class _FakeCloudWatch:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    def put_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("simulated PutMetricData failure")

    def heartbeats(self) -> list[dict]:
        return [d for c in self.calls for d in c["MetricData"]
                if d["MetricName"] == "RunRecordsWritten"]


def _write(run_records, ddb, started_at="2026-10-02T02:00:00Z"):
    return run_records.write_run_record(
        agent_id=AGENT_ID, component="battery", started_at=started_at,
        finished_at="2026-10-02T02:01:00Z", status="completed", artifacts_written=0,
        duplicates_skipped=0, agent_version="0.1.0", ddb_client=ddb,
        table_name="vsa-staging-avx-agent-runs")


def test_write_reports_true_only_when_the_item_was_stored():
    run_records = _import_run_records()
    assert _write(run_records, MagicMock()) is True
    failing = MagicMock()
    failing.Table.return_value.put_item.side_effect = Exception("boom")
    assert _write(run_records, failing) is False
    assert _write(run_records, MagicMock(), started_at="") is False


def test_a_stored_record_emits_one_heartbeat_with_stage_and_component():
    from agents.tier2 import runner

    cw = _FakeCloudWatch()
    runner._emit_run_record(lambda **_: None, component="tires", summary=_SUMMARY,
                            agent_version="0.1.0", stage="staging", cloudwatch_client=cw)
    beats = cw.heartbeats()
    assert len(beats) == 1, cw.calls
    assert cw.calls[0]["Namespace"] == "CVX/Tier2"
    assert beats[0]["Value"] == 1.0
    assert {d["Name"]: d["Value"] for d in beats[0]["Dimensions"]} == {
        "stage": "staging", "component": "tires"}


@pytest.mark.parametrize("writer", [
    pytest.param(lambda **_: False, id="writer-reports-not-stored"),
    pytest.param(MagicMock(side_effect=RuntimeError("write failed")), id="writer-raises"),
])
def test_no_heartbeat_when_the_record_was_not_stored(writer):
    from agents.tier2 import runner

    cw = _FakeCloudWatch()
    runner._emit_run_record(writer, component="battery", summary=_SUMMARY,
                            agent_version="0.1.0", stage="staging", cloudwatch_client=cw)
    assert cw.heartbeats() == []


def test_no_heartbeat_when_the_runs_table_is_unset(monkeypatch):
    from agents.tier2 import runner

    monkeypatch.delenv("AVX_AGENT_RUNS_TABLE", raising=False)
    cw = _FakeCloudWatch()
    runner._emit_run_record(None, component="battery", summary=_SUMMARY,
                            agent_version="0.1.0", stage="staging", cloudwatch_client=cw)
    assert cw.heartbeats() == []


def test_the_default_writer_heartbeats_only_on_a_stored_item(monkeypatch):
    """The production path: write_run_record's return value gates the heartbeat."""
    import boto3

    from agents.tier2 import runner, run_records

    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-staging-avx-agent-runs")
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: MagicMock())
    for stored, expected in ((True, 1), (False, 0)):
        monkeypatch.setattr(run_records, "write_run_record", lambda **_: stored)
        cw = _FakeCloudWatch()
        runner._emit_run_record(None, component="brakes", summary=_SUMMARY,
                                agent_version="0.1.0", stage="staging", cloudwatch_client=cw)
        assert len(cw.heartbeats()) == expected, (stored, cw.calls)


def test_a_failed_heartbeat_never_fails_the_run():
    from agents.tier2 import runner

    runner._emit_run_record(lambda **_: None, component="battery", summary=_SUMMARY,
                            agent_version="0.1.0", stage="staging",
                            cloudwatch_client=_FakeCloudWatch(fail=True))


def test_every_run_record_call_site_threads_stage_and_the_cloudwatch_client():
    """A call site that drops either keyword silently stops the heartbeat for that
    path, and the alarm then fires on a healthy run. Structural, because the six call
    sites sit on six different exit paths."""
    import ast
    import pathlib

    from agents.tier2 import runner

    tree = ast.parse(pathlib.Path(runner.__file__).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_emit_run_record"]
    assert len(calls) >= 6, f"expected the six run-record call sites, found {len(calls)}"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("stage"), ast.Name) and kw["stage"].id == "stage", call.lineno
        assert (isinstance(kw.get("cloudwatch_client"), ast.Name)
                and kw["cloudwatch_client"].id == "cloudwatch_client"), call.lineno


def test_a_real_run_heartbeats_for_its_component():
    """End to end through run(): the client the run already has reaches the heartbeat."""
    from agents.tier2 import runner

    cw = _FakeCloudWatch()
    runner.run("staging", source=TestRunnerIntegration._FakeSource(),
               writer=TestRunnerIntegration._FakeWriter(), vins=["MRDVEH00000000015"],
               component="battery", run_record_writer=lambda **_: None, cloudwatch_client=cw)
    beats = cw.heartbeats()
    assert len(beats) == 1
    assert {d["Name"]: d["Value"] for d in beats[0]["Dimensions"]}["component"] == "battery"



# ---------------------------------------------------------------------------
# G3-3 Fix: W4-b / W4-c — stored item must carry p0_directive_fallbacks and
# agent2_read_errors (mutants remove their tuples from the for loop in
# write_run_record, leaving the signature unchanged but the DDB item missing
# the field).
# ---------------------------------------------------------------------------


def test_p0_directive_fallbacks_stored_in_item():
    """W4-b: p0_directive_fallbacks is written into the DDB item.

    Mutation W4-b removes the ("p0_directive_fallbacks", p0_directive_fallbacks)
    tuple from the for loop, so the field never appears in the item.
    This test passes a non-None value and asserts the item contains it.
    """
    run_records = _import_run_records()
    put_items: list = []
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_items.append(kw)

    run_records.write_run_record(
        agent_id="agent-fi-fleet-health",
        component="service",
        started_at="2026-10-10T02:00:00Z",
        finished_at="2026-10-10T02:01:00Z",
        status="completed",
        artifacts_written=5,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name="vsa-staging-avx-agent-runs",
        p0_directive_fallbacks=3,
    )

    assert put_items, "write_run_record must call ddb.Table().put_item"
    item = put_items[0]["Item"]
    assert item.get("p0_directive_fallbacks") == 3, (
        f"p0_directive_fallbacks=3 must be stored in the run record item. "
        f"Got item keys: {sorted(item.keys())}. "
        "W4-b mutation removes the tuple from the for loop → field absent → CAUGHT."
    )


def test_agent2_read_errors_stored_in_item():
    """W4-c: agent2_read_errors is written into the DDB item.

    Mutation W4-c removes the ("agent2_read_errors", agent2_read_errors) tuple,
    so the field never appears in the item.
    """
    run_records = _import_run_records()
    put_items: list = []
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_items.append(kw)

    run_records.write_run_record(
        agent_id="agent-fi-fleet-health",
        component="service",
        started_at="2026-10-10T02:00:00Z",
        finished_at="2026-10-10T02:01:00Z",
        status="completed",
        artifacts_written=5,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name="vsa-staging-avx-agent-runs",
        agent2_read_errors=7,
    )

    assert put_items, "write_run_record must call ddb.Table().put_item"
    item = put_items[0]["Item"]
    assert item.get("agent2_read_errors") == 7, (
        f"agent2_read_errors=7 must be stored in the run record item. "
        f"Got item keys: {sorted(item.keys())}. "
        "W4-c mutation removes the tuple from the for loop → field absent → CAUGHT."
    )



@pytest.mark.parametrize("field", [
    "open_items_read_errors", "locations_incomplete",
    "gap_items_without_vehicle", "forecast_days_missing",
    "candidates_forecast_days_missing",
])
def test_rebalance_counters_stored_in_item(field):
    """FI rebalance counters (Fix Group G4-2 F8) are written into the DDB item when passed.

    The runner tests check what reaches write_run_record; this checks that write_run_record
    puts each one in the item (architect mutant G42-rrpass removed a tuple from the loop and
    every runner test still passed). Distinct values per field so a swapped tuple fails too.
    """
    run_records = _import_run_records()
    put_items: list = []
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_items.append(kw)
    values = {"open_items_read_errors": 2, "locations_incomplete": 3,
              "gap_items_without_vehicle": 4, "forecast_days_missing": 5,
              "candidates_forecast_days_missing": 6}

    run_records.write_run_record(
        agent_id="agent-fi-rebalancing",
        component="rebalance",
        started_at="2026-10-12T06:00:00Z",
        finished_at="2026-10-12T06:01:00Z",
        status="completed",
        artifacts_written=1,
        duplicates_skipped=0,
        agent_version="1.0.0",
        ddb_client=mock_ddb,
        table_name="vsa-staging-avx-agent-runs",
        **values,
    )

    assert put_items, "write_run_record must call ddb.Table().put_item"
    item = put_items[0]["Item"]
    assert item.get(field) == values[field], (
        f"{field}={values[field]} must be stored in the run record item; "
        f"got item keys {sorted(item.keys())}"
    )


def test_rebalance_counters_absent_when_not_passed():
    """Other components' records are unchanged: the four rebalance fields appear only when passed."""
    run_records = _import_run_records()
    put_items: list = []
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_items.append(kw)

    run_records.write_run_record(
        agent_id="agent-fi-fleet-health",
        component="service",
        started_at="2026-10-12T02:00:00Z",
        finished_at="2026-10-12T02:01:00Z",
        status="completed",
        artifacts_written=1,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name="vsa-staging-avx-agent-runs",
    )

    item = put_items[0]["Item"]
    for field in ("open_items_read_errors", "locations_incomplete",
                  "gap_items_without_vehicle", "forecast_days_missing",
                  "candidates_forecast_days_missing"):
        assert field not in item, f"{field} must not be written for a component that didn't pass it"


def test_guardrail_errors_stored_in_item():
    """W4/M5: guardrail_errors is written into the DDB item.

    Mutation M5 removes the ("guardrail_errors", guardrail_errors) tuple from
    the write loop, so the field never appears in the item.
    Passing a non-None value and asserting the item contains it catches M5.
    """
    run_records = _import_run_records()
    put_items: list = []
    mock_ddb = MagicMock()
    mock_ddb.Table.return_value.put_item.side_effect = lambda **kw: put_items.append(kw)

    run_records.write_run_record(
        agent_id="agent-fi-fleet-health",
        component="service",
        started_at="2026-10-10T02:00:00Z",
        finished_at="2026-10-10T02:01:00Z",
        status="completed",
        artifacts_written=5,
        duplicates_skipped=0,
        agent_version="0.1.0",
        ddb_client=mock_ddb,
        table_name="vsa-staging-avx-agent-runs",
        guardrail_errors=4,
    )

    assert put_items, "write_run_record must call ddb.Table().put_item"
    item = put_items[0]["Item"]
    assert item.get("guardrail_errors") == 4, (
        f"guardrail_errors=4 must be stored in the run record item. "
        f"Got item keys: {sorted(item.keys())}. "
        "M5 mutation removes the tuple from the write loop → field absent → CAUGHT."
    )
