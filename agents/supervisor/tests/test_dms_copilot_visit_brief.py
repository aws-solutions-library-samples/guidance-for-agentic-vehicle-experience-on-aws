"""``visit_brief`` AVX pre-brief superset read — DMS Spec 2, Task 3.4.

Covers two properties that the task makes load-bearing:

1. **Backward compatibility.** With no AVX pre-brief artifact, the response is
   byte-identical to the pre-existing base shape — same key set, same values.
   Pre-existing ``dm_copilot`` consumers must not observe any change.
2. **Superset.** With an AVX pre-brief present, the response gains
   ``avx_evidence`` plus the three keys identifying which visit it describes
   (``avx_appointment_date``, ``avx_ro_id``, ``avx_computed_at``), and the base
   keys keep their base-table values.
3. **A base brief is required.** An AVX pre-brief alone does not make a response
   ``available`` — the four narrative keys live only on the base artifact, and
   rendering them empty would assert a determination never made (review Cycle 1
   Critical).

Also pins the corrected access pattern. Both brief tables have COMPOSITE
primary keys, so the pre-existing point ``GetItem`` on ``dealer_id`` alone
raised ``ValidationException`` on every call — see CVX
``issues/2026-09-19-dms-copilot-getitem-key-schema-mismatch/``. The reads are
now Query, descending, ``Limit=1``.

The two tables' sort keys mean different things, and
``test_avx_read_selects_latest_appointment_not_newest_computed`` pins that: the
base table sorts on ``computed_at`` (so descending IS newest), while the AVX
table's SK is ``{appointment_date}#{ro_id}#{computed_at}`` (so descending is by
appointment date). A fixed-list mock cannot tell those apart, which is why the
sort-aware ``_sorting_table`` helper exists.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from agents.supervisor.tests.test_dms_personas import _make_config, _make_ctx

# The exact key set the pre-existing consumer sees. Adding to this set is a
# breaking change for the base shape; the AVX keys below are the ONLY additions
# the superset is permitted to make.
_BASE_KEYS = frozenset({
    "available",
    "dealer_id",
    "computed_at",
    "agent_version",
    "confidence",
    "inputs_hash",
    "evidence",
    "priority_items",
    "service_forecast",
    "open_concerns",
    "recommendations",
})

# The superset. ``avx_evidence`` is the payload; the other three identify WHICH
# visit it describes and how stale it is, which the AVX table's sort-key
# semantics make mandatory rather than decorative — see
# test_avx_read_selects_latest_appointment_not_newest_computed.
_AVX_KEYS = frozenset({
    "avx_evidence",
    "avx_appointment_date",
    "avx_ro_id",
    "avx_computed_at",
})

_BASE_ITEM = {
    "dealer_id": "DLR-0042",
    "computed_at": "2026-09-18T02:00:00Z",
    "agent_version": "visit-brief-v1.2",
    "confidence": "high",
    "inputs_hash": "sha256:basebase",
    "evidence": [{"source": "athena://adp_dealer_domain", "rows_scanned": 1200}],
    "priority_items": ["CSI recovery plan for Q3"],
    "service_forecast": {"next_30d_ros": 42},
    "open_concerns": ["Customer complaint: long wait times"],
    "recommendations": ["Conduct technician capacity review"],
}

# Shaped per the DMS pre-brief contract: Tier 2 doctrine fields + the identity
# triple (dealer_id, appointment_date, ro_id), evidence entries carrying
# ``provenance``. See DMS source/tier2/pre_brief_agent.py.
_AVX_ITEM = {
    "dealer_id": "DLR-0042",
    "appointment_date_ro_id": "2026-09-19#RO-77#2026-09-19T03:00:00Z",
    "appointment_date": "2026-09-19",
    "ro_id": "RO-77",
    "computed_at": "2026-09-19T03:00:00Z",
    "agent_version": "pre-brief-v0.3",
    "confidence": "medium",
    "inputs_hash": "sha256:avxavx",
    "evidence": [
        {"detail": "2 open recalls", "provenance": "live"},
        {"detail": "customer_360 tenure unavailable", "provenance": "absent"},
    ],
}


def _client_error(op: str = "Query") -> ClientError:
    """A ClientError whose message embeds an ARN and an account number.

    Used to prove the surfaced ``reason`` never carries either.
    """
    return ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "User is not authorized to perform dynamodb:Query on "
                           "arn:aws:dynamodb:us-west-2:999999999999:table/dms-secret-name",
            }
        },
        operation_name=op,
    )


@pytest.fixture
def _gold_tier(monkeypatch):
    """Patch the cert gate to Gold so the tool reaches DynamoDB."""
    import importlib

    from agents.supervisor._lib import dms_cert_cache

    monkeypatch.setattr(
        dms_cert_cache,
        "check_dealer_certification",
        lambda dealer_id: ("Gold", "2026-08-29T00:00:00Z"),
    )
    mod = importlib.import_module("agents.supervisor.tools.dms_copilot")
    monkeypatch.setattr(
        mod, "check_dealer_certification",
        lambda dealer_id: ("Gold", "2026-08-29T00:00:00Z"),
    )


@contextmanager
def _patched_tables(
    base_item: dict | None = None,
    avx_item: dict | None = None,
    base_error: Exception | None = None,
    avx_error: Exception | None = None,
):
    """Patch ``boto3.resource`` so the two brief tables answer independently.

    Dispatch is on table name, because ``visit_brief`` reads two tables in one
    call and a single shared mock cannot distinguish them.

    Yields:
        (base_tbl, avx_tbl) mocks, for asserting on call kwargs.
    """
    base_tbl = MagicMock()
    avx_tbl = MagicMock()

    if base_error is not None:
        base_tbl.query.side_effect = base_error
    else:
        base_tbl.query.return_value = {"Items": [base_item] if base_item else []}

    if avx_error is not None:
        avx_tbl.query.side_effect = avx_error
    else:
        avx_tbl.query.return_value = {"Items": [avx_item] if avx_item else []}

    def _dispatch(table_name: str):
        return avx_tbl if "avx-pre-briefs" in table_name else base_tbl

    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.side_effect = _dispatch
        yield base_tbl, avx_tbl


def _call(**kwargs) -> dict:
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    with _patched_tables(**kwargs):
        tool_fn = make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
        return tool_fn("DLR-0042")


# ---------------------------------------------------------------------------
# 1. Backward compatibility — the mutation target
# ---------------------------------------------------------------------------


def test_visit_brief_backward_compat(_gold_tier):
    """No AVX artifact -> byte-identical to the pre-existing base shape.

    MUTATION TARGET (Task 3.4): rewriting visit_brief to always source the
    response from the AVX artifact, with no fallback to the base brief, MUST
    fail this test.
    """
    result = _call(base_item=_BASE_ITEM, avx_item=None)

    assert set(result) == set(_BASE_KEYS), (
        "with no AVX pre-brief the key set must be exactly the base shape; "
        f"unexpected={set(result) - set(_BASE_KEYS)} "
        f"missing={set(_BASE_KEYS) - set(result)}"
    )
    assert "avx_evidence" not in result
    assert result["available"] is True
    assert result["dealer_id"] == "DLR-0042"
    for field in ("computed_at", "agent_version", "confidence", "inputs_hash",
                  "evidence", "priority_items", "service_forecast",
                  "open_concerns", "recommendations"):
        assert result[field] == _BASE_ITEM[field], (
            f"{field!r} must come through from the base brief unchanged"
        )


# ---------------------------------------------------------------------------
# 2. Superset
# ---------------------------------------------------------------------------


def test_visit_brief_superset_when_avx_present(_gold_tier):
    """AVX artifact present -> base shape PLUS the AVX keys, and only those."""
    result = _call(base_item=_BASE_ITEM, avx_item=_AVX_ITEM)

    assert set(result) == set(_BASE_KEYS) | set(_AVX_KEYS), (
        "the AVX superset may add only the AVX keys; "
        f"got extra={set(result) - set(_BASE_KEYS) - set(_AVX_KEYS)}"
    )
    assert result["avx_evidence"] == _AVX_ITEM["evidence"]
    # Base keys keep BASE values — the superset augments, it does not overwrite.
    assert result["computed_at"] == _BASE_ITEM["computed_at"]
    assert result["inputs_hash"] == _BASE_ITEM["inputs_hash"]
    assert result["evidence"] == _BASE_ITEM["evidence"]
    # ...and the two evidence lists stay distinct objects, so a consumer
    # mutating one cannot corrupt the other.
    assert result["evidence"] is not result["avx_evidence"]


def test_avx_evidence_entries_carry_provenance(_gold_tier):
    """Surfaced AVX evidence keeps ``provenance``, so Tier 1 can narrate it."""
    result = _call(base_item=_BASE_ITEM, avx_item=_AVX_ITEM)

    assert result["avx_evidence"], "avx_evidence must not be empty when present"
    for entry in result["avx_evidence"]:
        assert "provenance" in entry, (
            "every AVX evidence entry must retain provenance — narrating "
            "evidence without it would assert a conclusion Tier 1 cannot support"
        )


# ---------------------------------------------------------------------------
# 3. Degradation — the optional table must not break the existing consumer
# ---------------------------------------------------------------------------


def test_avx_read_failure_degrades_to_base_brief(_gold_tier):
    """An unreadable AVX table degrades to 'absent', not to unavailable.

    The AVX table is an optional enhancement. If it is undeployed, or the
    cross-account grant has not landed, the pre-existing consumer must be
    unaffected.
    """
    result = _call(base_item=_BASE_ITEM, avx_error=_client_error())

    assert result["available"] is True, (
        "an AVX-table failure must NOT take down the base brief"
    )
    assert set(result) == set(_BASE_KEYS)
    assert "avx_evidence" not in result


def test_base_read_failure_returns_generic_unavailable(_gold_tier):
    """A base-table failure is still surfaced as unavailable, with no leakage."""
    result = _call(base_error=_client_error())

    assert result["available"] is False
    assert result["reason"] == "DMS table temporarily unreachable"
    assert "arn:aws:" not in result["reason"]
    assert "999999999999" not in result["reason"]


def test_both_absent_returns_exact_phrase(_gold_tier):
    """Neither artifact -> the exact deferral phrase, never a fabrication."""
    result = _call(base_item=None, avx_item=None)

    assert result["available"] is False
    assert result["reason"] == "no brief computed yet"


def test_avx_only_without_base_is_not_available(_gold_tier):
    """An AVX pre-brief alone does NOT stand in for a missing base brief.

    Review Cycle 1 Critical: the four narrative keys (priority_items,
    service_forecast, open_concerns, recommendations) exist only on the base
    artifact. Reporting ``available: True`` with those rendered as []/None
    states "no open concerns" as though it had been determined, and routes
    around the persona's anti-fabrication instruction, which keys on the
    literal phrase "no brief computed yet".
    """
    result = _call(base_item=None, avx_item=_AVX_ITEM)

    assert result["available"] is False
    assert result["reason"] == "no brief computed yet"
    # The narrative keys must not appear at all — not even empty.
    for absent in ("priority_items", "open_concerns", "recommendations",
                   "service_forecast", "avx_evidence"):
        assert absent not in result, (
            f"{absent!r} must not be surfaced when no base brief exists"
        )


# ---------------------------------------------------------------------------
# 4. Access pattern — pins the composite-key fix AND the sort-key semantics
# ---------------------------------------------------------------------------


def _sorting_table(items: list[dict], sk_attr: str) -> MagicMock:
    """A table mock that honours ScanIndexForward and Limit against a partition.

    The default mocks return a fixed list regardless of query parameters, so
    they cannot distinguish "queried descending" from "selected the right item".
    Review Cycle 1 found a real defect that a fixed-list mock hid: the AVX
    table's SK is a composite whose FIRST component is appointment_date, so
    descending order is by appointment date, not by computed_at.
    """
    tbl = MagicMock()

    def _query(**kwargs):
        ordered = sorted(items, key=lambda i: i[sk_attr],
                         reverse=not kwargs.get("ScanIndexForward", True))
        limit = kwargs.get("Limit")
        return {"Items": ordered[:limit] if limit else ordered}

    tbl.query.side_effect = _query
    return tbl


def test_base_read_selects_newest_computed_at(_gold_tier):
    """dm-visit-briefs sorts on computed_at, so descending Limit=1 IS newest."""
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    older = {**_BASE_ITEM, "computed_at": "2026-09-10T02:00:00Z",
             "inputs_hash": "sha256:older"}
    newer = {**_BASE_ITEM, "computed_at": "2026-09-18T02:00:00Z",
             "inputs_hash": "sha256:newer"}
    base_tbl = _sorting_table([older, newer], "computed_at")
    avx_tbl = _sorting_table([], "appointment_date_ro_id")

    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.side_effect = (
            lambda n: avx_tbl if "avx-pre-briefs" in n else base_tbl
        )
        tool_fn = make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
        result = tool_fn("DLR-0042")

    assert result["inputs_hash"] == "sha256:newer", (
        "the base read must select the most recently computed brief"
    )


def test_avx_read_selects_latest_appointment_not_newest_computed(_gold_tier):
    """Documents the AVX table's ACTUAL selection semantics.

    The SK is ``{appointment_date}#{ro_id}#{computed_at}``, so descending order
    is by appointment_date first. The pre-brief for the furthest-future
    appointment wins even when another was computed more recently.

    This test pins the behaviour the code documents, so that a future change to
    "newest computed" is a deliberate, visible change rather than a silent one.
    Review Cycle 1 Warning 3 — the original code claimed newest-computed for
    both tables, which was false for this one.
    """
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    # Computed LATER, but for an EARLIER appointment.
    earlier_appt = {
        **_AVX_ITEM,
        "appointment_date": "2026-09-19",
        "ro_id": "RO-11",
        "computed_at": "2026-09-19T09:00:00Z",
        "appointment_date_ro_id": "2026-09-19#RO-11#2026-09-19T09:00:00Z",
        "evidence": [{"detail": "earlier appointment", "provenance": "live"}],
    }
    # Computed EARLIER, but for a LATER appointment.
    later_appt = {
        **_AVX_ITEM,
        "appointment_date": "2026-09-25",
        "ro_id": "RO-22",
        "computed_at": "2026-09-19T03:00:00Z",
        "appointment_date_ro_id": "2026-09-25#RO-22#2026-09-19T03:00:00Z",
        "evidence": [{"detail": "later appointment", "provenance": "live"}],
    }
    base_tbl = _sorting_table([_BASE_ITEM], "computed_at")
    avx_tbl = _sorting_table([earlier_appt, later_appt], "appointment_date_ro_id")

    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.side_effect = (
            lambda n: avx_tbl if "avx-pre-briefs" in n else base_tbl
        )
        tool_fn = make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
        result = tool_fn("DLR-0042")

    assert result["avx_appointment_date"] == "2026-09-25"
    assert result["avx_ro_id"] == "RO-22"
    assert result["avx_evidence"] == later_appt["evidence"]
    # The selected artifact is NOT the most recently computed one — which is
    # exactly why the response must carry its identifying context.
    assert result["avx_computed_at"] == "2026-09-19T03:00:00Z"
    assert result["avx_computed_at"] < earlier_appt["computed_at"]


def test_avx_context_keys_identify_the_visit(_gold_tier):
    """avx_evidence is never surfaced without the context identifying it.

    Asserts the values come FROM the selected artifact. An `is not None` check
    would pass against a hardcoded constant — review Cycle 2 mutation M-F.
    """
    result = _call(base_item=_BASE_ITEM, avx_item=_AVX_ITEM)

    assert "avx_evidence" in result
    assert result["avx_appointment_date"] == _AVX_ITEM["appointment_date"]
    assert result["avx_ro_id"] == _AVX_ITEM["ro_id"]
    assert result["avx_computed_at"] == _AVX_ITEM["computed_at"]
    # And they must differ from the BASE artifact's computed_at, so a copy of
    # the wrong field cannot pass.
    assert result["avx_computed_at"] != result["computed_at"]


def test_avx_read_failure_emits_a_metric(_gold_tier, capsys):
    """The degradation is metered, AND the emitted event is parseable as EMF.

    Asserts the FORMATTED log event, captured off stdout — not caplog's
    pre-formatter message. CloudWatch extracts EMF only when the entire log
    event is the JSON blob, so a root formatter prefixing it with a timestamp
    and level would silently defeat extraction while a caplog-based test still
    passed. That is exactly what review Cycle 2 Warning 1 found: proving the
    JSON was CONSTRUCTED is not proving the metric WORKS.
    """
    result = _call(base_item=_BASE_ITEM, avx_error=_client_error())
    assert result["available"] is True

    captured = capsys.readouterr().out
    emf_lines = [
        ln for ln in captured.splitlines()
        if "avx_read_failure" in ln
    ]
    assert emf_lines, "an AVX read failure must emit a metric line to stdout"

    # The WHOLE line must be JSON — no timestamp/level prefix.
    payload = json.loads(emf_lines[0])
    assert payload["avx_read_failure"] == 1
    assert payload["exception_type"] == "ClientError"
    group = payload["_aws"]["CloudWatchMetrics"][0]
    assert group["Namespace"] == "dms/visit_brief"
    assert [m["Name"] for m in group["Metrics"]] == ["avx_read_failure"]
    assert group["Dimensions"] == [["exception_type"]]
    assert isinstance(payload["_aws"]["Timestamp"], int)


def test_metric_line_carries_no_log_formatting(_gold_tier, capsys):
    """Guards the specific regression: a formatter prefix defeating EMF.

    Separate from the test above so the failure message names the cause. If the
    metrics emission ever routes back through a formatted logger, this fails.

    The existence assertion is load-bearing. An earlier version only inspected
    lines it happened to find, so routing the emission back through `log.info`
    (where it never reaches stdout) made the loop body unreachable and the test
    passed vacuously — caught by review Cycle 2 mutation M11.
    """
    _call(base_item=_BASE_ITEM, avx_error=_client_error())

    lines = [
        ln for ln in capsys.readouterr().out.splitlines()
        if "avx_read_failure" in ln
    ]
    assert lines, (
        "no metric line reached stdout — the emission must not go through a "
        "logger, whose formatter would prefix the JSON and defeat EMF extraction"
    )
    for line in lines:
        assert line.lstrip().startswith("{"), (
            "the EMF event must begin with '{' — a formatter prefix "
            f"(timestamp/logger/level) defeats CloudWatch extraction: {line[:80]!r}"
        )
        json.loads(line)  # raises if the line is not pure JSON


def test_metric_line_is_flushed(_gold_tier, monkeypatch):
    """The metric line is flushed — a buffered metric is no metric.

    In a container stdout is block-buffered when not a TTY, so an unflushed
    write can be lost if the process is recycled. capsys reads the buffer
    directly and so cannot observe this, which is why the property is asserted
    against the call itself (review Cycle 2 mutation M12 went uncaught without
    this).
    """
    flushed: list[bool] = []
    real_flush = sys.stdout.flush
    monkeypatch.setattr(
        sys.stdout, "flush",
        lambda: (flushed.append(True), real_flush())[1],
        raising=False,
    )

    _call(base_item=_BASE_ITEM, avx_error=_client_error())

    assert flushed, "the metric write must be followed by an explicit flush"


def test_reads_newest_first_via_query(_gold_tier):
    """Both tables are read by Query, descending, Limit=1 — never GetItem.

    A point GetItem supplying only ``dealer_id`` raises ValidationException on
    both tables, since both have composite primary keys.
    """
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    with _patched_tables(base_item=_BASE_ITEM, avx_item=_AVX_ITEM) as (base_tbl, avx_tbl):
        tool_fn = make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
        tool_fn("DLR-0042")

    for label, tbl in (("base", base_tbl), ("avx", avx_tbl)):
        tbl.get_item.assert_not_called()
        assert tbl.query.call_count == 1, f"{label} table must be queried exactly once"
        kwargs = tbl.query.call_args.kwargs
        assert kwargs["ScanIndexForward"] is False, (
            f"{label}: must sort descending"
        )
        assert kwargs["Limit"] == 1, f"{label}: only the top item is needed"
        values = kwargs["KeyConditionExpression"].get_expression()["values"]
        assert values[0].name == "dealer_id", (
            f"{label}: partition key must be dealer_id"
        )
        assert values[1] == "DLR-0042"


# ---------------------------------------------------------------------------
# 5. The two gates still fire ahead of any read
# ---------------------------------------------------------------------------


def test_below_gold_caller_never_reads_ddb(monkeypatch):
    """Below-Gold callers get the deterministic response; no table is touched."""
    import importlib

    from agents.supervisor._lib import dms_cert_cache

    monkeypatch.setattr(
        dms_cert_cache, "check_dealer_certification",
        lambda dealer_id: ("Bronze", "2026-08-29T00:00:00Z"),
    )
    mod = importlib.import_module("agents.supervisor.tools.dms_copilot")
    monkeypatch.setattr(
        mod, "check_dealer_certification",
        lambda dealer_id: ("Bronze", "2026-08-29T00:00:00Z"),
    )

    with _patched_tables(base_item=_BASE_ITEM, avx_item=_AVX_ITEM) as (base_tbl, avx_tbl):
        tool_fn = mod.make_visit_brief_tool(_make_ctx("dm_copilot"), _make_config())
        result = tool_fn("DLR-0042")

    base_tbl.query.assert_not_called()
    avx_tbl.query.assert_not_called()
    assert result.get("available") is not True


def test_out_of_scope_dealer_never_reads_ddb(_gold_tier):
    """Out-of-scope target refuses BEFORE any read — including the AVX read.

    The AVX read is added ahead of the base read, so this guards against the
    new read bypassing the scope gate.
    """
    from agents.supervisor.tools.dms_copilot import make_visit_brief_tool

    ctx = _make_ctx("dm_copilot", dealer_id="DLR-MINE", dealer_ids=["DLR-MINE"])

    with _patched_tables(base_item=_BASE_ITEM, avx_item=_AVX_ITEM) as (base_tbl, avx_tbl):
        tool_fn = make_visit_brief_tool(ctx, _make_config())
        result = tool_fn("DLR-SOMEONE-ELSE")

    base_tbl.query.assert_not_called()
    avx_tbl.query.assert_not_called()
    assert result.get("available") is not True
