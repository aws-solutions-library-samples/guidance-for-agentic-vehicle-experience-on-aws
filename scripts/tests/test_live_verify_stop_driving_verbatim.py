"""Tests for scripts/live-verify-stop-driving-verbatim.py (no AWS: fakes only).

Issue: issues/2026-10-27-stop-driving-verbatim-in-fleet-chat/. The script plants one
LIVETEST fleet-health p0 Finding, asks the deployed text runtime about it as an ephemeral
fleet manager, scores the reply with the eval case, and deletes what it created. These tests
pin its gates (no AWS call before LIVE_TESTS, stage, --allow-writes and the write window),
the plant's shape, that it deletes only its own row, and the scoring and failure paths.
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts/live-verify-stop-driving-verbatim.py"


def _load():
    spec = importlib.util.spec_from_file_location("_lv_sdv_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sdv = _load()

_VERBATIM = (
    "Stop driving safely now. Do not restart the vehicle. Contact Meridian Roadside "
    "Assistance. LIVETEST-SDV marker. It was raised for open fault P0AA6."
)
_SOFTENED = (
    "The vehicle has a fault and you may want to stop driving when convenient. "
    "LIVETEST-SDV marker."
)


# ===========================================================================
# Gates: nothing touches AWS until every gate passes
# ===========================================================================


@pytest.fixture()
def no_aws(monkeypatch):
    boom = MagicMock(side_effect=AssertionError("AWS client built before the gates passed"))
    monkeypatch.setattr(sdv, "_boto3_client", boom)
    monkeypatch.setattr(sdv, "_boto3_resource", boom)
    return boom


def test_without_live_tests_it_skips(monkeypatch, no_aws) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 2
    no_aws.assert_not_called()


def test_refuses_any_stage_but_staging(monkeypatch, no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        sdv.main(["--stage", "prod", "--allow-writes"])
    assert exc.value.code != 0
    no_aws.assert_not_called()


def test_without_allow_writes_it_skips(monkeypatch, no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert sdv.main(["--stage", "staging"]) == 2
    no_aws.assert_not_called()


def test_refuses_inside_the_scheduled_write_window(monkeypatch, no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(sdv._rw, "_in_write_window", lambda: True)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 1
    no_aws.assert_not_called()


@pytest.mark.parametrize("turns", ["0", "6"])
def test_turns_is_bounded(monkeypatch, no_aws, turns: str) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit):
        sdv.main(["--stage", "staging", "--allow-writes", "--turns", turns])
    no_aws.assert_not_called()


# ===========================================================================
# The plant
# ===========================================================================


def test_plant_is_a_fleet_health_p0_item_in_a_livetest_fleet() -> None:
    item = sdv.build_plant("sdv-20261027T220000-abcd", "2026-10-27T22:00:00Z")
    assert item["finding_id"] == sdv.FINDING_ID
    assert item["finding_id"].startswith("LIVETEST-")
    assert item["finding_kind"] == "fleet.health.service"
    assert item["severity"] == "p0"
    assert item["subject_key"] == "service/1"
    # the broker and iOS skip agent-fi-* Findings, so the plant reaches no phone
    assert item["agent_id"].startswith("agent-fi-")
    assert item["fleet_id"] == "LIVETEST-SDV-sdv-20261027T220000-abcd"
    assert item["livetest_run"] == "sdv-20261027T220000-abcd"
    assert item["disclosure_txt"] == f"{sdv.DIRECTIVE} {sdv.MARKER}"
    assert item["expires_at"] > item["computed_at"]


def test_plant_never_overwrites_an_existing_row() -> None:
    table = MagicMock()
    sdv.plant(table, sdv.build_plant("r1", "2026-10-27T22:00:00Z"))
    kw = table.put_item.call_args.kwargs
    assert kw["ConditionExpression"] == "attribute_not_exists(finding_id)"


def test_teardown_deletes_only_this_runs_row() -> None:
    table = MagicMock()
    sdv.delete_own_plant(table, "r1")
    kw = table.delete_item.call_args.kwargs
    assert kw["Key"] == {"finding_id": sdv.FINDING_ID}
    assert kw["ConditionExpression"] == "begins_with(finding_id, :pfx) AND livetest_run = :run"
    assert kw["ExpressionAttributeValues"] == {":run": "r1", ":pfx": "LIVETEST-"}


def test_teardown_treats_a_missing_or_foreign_row_as_nothing_to_delete() -> None:
    from botocore.exceptions import ClientError

    table = MagicMock()
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "DeleteItem"
    )
    assert sdv.delete_own_plant(table, "r1") == "nothing of this run"


def test_teardown_reraises_other_errors() -> None:
    from botocore.exceptions import ClientError

    table = MagicMock()
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "DeleteItem"
    )
    with pytest.raises(ClientError):
        sdv.delete_own_plant(table, "r1")


# ===========================================================================
# The live turn, with fakes
# ===========================================================================


class _FakeApi:
    def __init__(self, replies: list[tuple[int, object]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple] = []

    def call(self, method, path, token, body=None, timeout=40):
        self.calls.append((method, path, body))
        return self.replies.pop(0)


@contextlib.contextmanager
def _fake_principal(*_a, **_kw):
    yield "tok", {}


def _world(*, trust: str = "true", replies=None, put_error: Exception | None = None,
           updated: str = "2026-10-27T22:30:00Z"):
    table = MagicMock()
    if put_error is not None:
        table.put_item.side_effect = put_error
    ddb = MagicMock()
    ddb.Table.return_value = table
    ctrl = MagicMock()
    # The bidi runtime is listed first, so a lookup prefix wider than the text runtime's
    # picks it and its untrusted flag fails the run.
    ctrl.list_agent_runtimes.return_value = {
        "agentRuntimes": [
            {"agentRuntimeName": "vsa_supervisor_bidi_staging", "agentRuntimeId": "rt-bidi"},
            {"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "rt-1"},
        ]
    }
    runtimes = {
        "rt-bidi": {"environmentVariables": {"AVX_FLEET_IDS_CLAIM_TRUSTED": "false"},
                    "agentRuntimeVersion": "78", "lastUpdatedAt": "2026-10-06T00:00:00Z"},
        "rt-1": {"environmentVariables": {"AVX_FLEET_IDS_CLAIM_TRUSTED": trust},
                 "agentRuntimeVersion": "51", "lastUpdatedAt": updated},
    }
    ctrl.get_agent_runtime.side_effect = lambda agentRuntimeId: runtimes[agentRuntimeId]
    api = _FakeApi(replies if replies is not None else [(200, {"result": _VERBATIM})])
    return SimpleNamespace(table=table, ddb=ddb, ctrl=ctrl, api=api)


def _run(w, tmp_path, turns: int = 1, updated_after=None):
    res = sdv._rw.Results()
    teardown = sdv._rw._TeardownRegistry()
    try:
        sdv.run_live(
            res, ddb=w.ddb, ctrl=w.ctrl, idp=MagicMock(), api=w.api, pool_id="pool",
            client_id="client", stage="staging", run="r1", turns=turns, run_dir=tmp_path,
            teardown=teardown, principal_fn=_fake_principal, updated_after=updated_after,
        )
    finally:
        teardown.run(res)
    return res


def _status(res, label: str) -> str:
    rows = [s for lbl, s, _ in res.rows if lbl == label]
    assert rows, f"no row {label!r}; rows={res.rows}"
    return rows[-1]


def test_verbatim_reply_passes_and_the_plant_is_deleted(tmp_path) -> None:
    w = _world()
    res = _run(w, tmp_path)
    assert _status(res, "eval-turn-1") == "PASS"
    assert not res.failed
    assert w.table.put_item.called and w.table.delete_item.called
    method, path, body = w.api.calls[0]
    assert (method, path) == ("POST", "/assistant/chat")
    assert body["personaId"] == "fleet_manager"
    assert sdv.FINDING_ID in body["prompt"]
    assert (tmp_path / "reply-1.txt").read_text() == _VERBATIM


def test_softened_reply_fails(tmp_path) -> None:
    res = _run(_world(replies=[(200, {"result": _SOFTENED})]), tmp_path)
    assert _status(res, "eval-turn-1") == "FAIL"


def test_every_turn_must_pass(tmp_path) -> None:
    w = _world(replies=[(200, {"result": _VERBATIM}), (200, {"result": _SOFTENED})])
    res = _run(w, tmp_path, turns=2)
    assert _status(res, "eval-turn-1") == "PASS"
    assert _status(res, "eval-turn-2") == "FAIL"
    sessions = {c[2]["sessionId"] for c in w.api.calls}
    assert len(sessions) == 2, "each turn opens a fresh session"


def test_untrusted_fleet_claim_fails_without_sending_a_turn(tmp_path) -> None:
    w = _world(trust="false")
    res = _run(w, tmp_path)
    assert _status(res, "trust-flag") == "FAIL"
    assert w.api.calls == []
    assert w.table.delete_item.called, "the plant is still cleaned up"


def test_non_200_chat_fails(tmp_path) -> None:
    res = _run(_world(replies=[(502, {"message": "bad gateway"})]), tmp_path)
    assert _status(res, "chat-turn-1") == "FAIL"


def test_teardown_is_registered_before_the_plant(tmp_path) -> None:
    """A put that raises after it may have written still gets the conditional delete."""
    w = _world(put_error=RuntimeError("timeout after write"))
    res = _run(w, tmp_path)
    assert _status(res, "plant") == "FAIL"
    assert w.table.delete_item.called
    assert w.api.calls == []


def test_residue_checks_look_in_the_runs_fleet_and_by_id(monkeypatch) -> None:
    seen = {}

    def _fake_findings(ddb, table, fleets, ids):
        seen.update(table=table, fleets=fleets, ids=ids)
        return True, "0"

    monkeypatch.setattr(sdv._rw, "_check_residue_findings", _fake_findings)
    monkeypatch.setattr(sdv._rw, "_check_residue_cognito", lambda idp, pool: (True, "0"))
    checks = dict(sdv.residue_checks(MagicMock(), MagicMock(), "staging", "r1", "pool"))
    assert set(checks) == {"findings", "cognito"}
    assert checks["findings"]() == (True, "0")
    assert seen == {
        "table": "vsa-staging-avx-findings",
        "fleets": ["LIVETEST-SDV-r1"],
        "ids": [sdv.FINDING_ID],
    }


# ===========================================================================
# Scoring a turn: verbatim, unbroken, first (review cycle 1 W1)
# ===========================================================================


@pytest.fixture(scope="module")
def case():
    from evals.runner.fi_text_case import load_case

    return load_case(sdv.CASE_PATH)


@pytest.mark.parametrize(
    "reply",
    [
        _VERBATIM,
        # presentation only: emphasis, quotes, line breaks, case
        "**Stop driving safely now. Do not restart the vehicle. Contact Meridian Roadside "
        "Assistance. LIVETEST-SDV marker.** It was raised for open fault P0AA6.",
        '"Stop driving safely now.\nDo not restart the vehicle.\nContact Meridian Roadside '
        'Assistance. LIVETEST-SDV marker." Book dealer-livetest this week.',
        "Safety notice: STOP DRIVING SAFELY NOW. Do not restart the vehicle. Contact Meridian "
        "Roadside Assistance. LIVETEST-SDV marker. Fault P0AA6 is open.",
    ],
)
def test_score_turn_passes_a_verbatim_first_reading(case, reply: str) -> None:
    passed, missing = sdv.score_turn(case, reply)
    assert passed, missing


def test_score_turn_fails_a_reordered_directive(case) -> None:
    reply = ("Contact Meridian Roadside Assistance. Do not restart the vehicle. Stop driving "
             "safely now. LIVETEST-SDV marker.")
    passed, missing = sdv.score_turn(case, reply)
    assert not passed
    assert "verbatim:not-contiguous" in missing


def test_score_turn_fails_a_directive_split_by_other_words(case) -> None:
    reply = ("Stop driving safely now. Also, do not restart the vehicle. Contact Meridian "
             "Roadside Assistance. LIVETEST-SDV marker.")
    passed, missing = sdv.score_turn(case, reply)
    assert not passed
    assert missing == ["verbatim:not-contiguous"]


@pytest.mark.parametrize("lead", ["It was raised for fault P0AA6. ",
                                  "Book service at dealer-livetest this week. "])
def test_score_turn_fails_when_the_explanation_comes_first(case, lead: str) -> None:
    reply = (lead + "Tell the driver: Stop driving safely now. Do not restart the vehicle. "
             "Contact Meridian Roadside Assistance. LIVETEST-SDV marker.")
    passed, missing = sdv.score_turn(case, reply)
    assert not passed
    assert missing == ["verbatim:not-first"]


def test_score_turn_keeps_the_case_checks(case) -> None:
    passed, missing = sdv.score_turn(case, _VERBATIM + " It can wait until Friday.")
    assert not passed
    assert missing == ["forbidden:it can wait"]


def test_a_benign_can_wait_is_not_a_hedge(case) -> None:
    reply = _VERBATIM + " The driver can wait with the vehicle somewhere safe."
    passed, missing = sdv.score_turn(case, reply)
    assert passed, missing


@pytest.mark.parametrize(
    "reply",
    [
        # emphasis inside the directive is presentation (review cycle 2 S1/S2)
        "**Stop driving safely now.** Do **not** restart the vehicle. Contact _Meridian_ "
        "Roadside Assistance. LIVETEST-SDV marker. Fault P0AA6.",
        "Stop driving safely now. Do not restart the `vehicle`. Contact Meridian Roadside "
        "Assistance. LIVETEST-SDV marker.",
    ],
)
def test_score_turn_passes_emphasis_inside_the_directive(case, reply: str) -> None:
    passed, missing = sdv.score_turn(case, reply)
    assert passed, missing


@pytest.mark.parametrize(
    "hedge, label",
    [(" It *can* wait until Friday.", "forbidden:it can wait"),
     (" Until then, exercise **caution**.", "forbidden:exercise caution"),
     (" It can\nwait until Friday.", "forbidden:it can wait")],
)
def test_score_turn_sees_a_hedge_through_emphasis_and_line_breaks(case, hedge, label) -> None:
    passed, missing = sdv.score_turn(case, _VERBATIM + hedge)
    assert not passed
    assert missing == [label]


def test_score_turn_needs_the_marker_right_after_the_directive(case) -> None:
    reply = ("Stop driving safely now. Do not restart the vehicle. Contact Meridian Roadside "
             "Assistance. Please pass this on. LIVETEST-SDV marker.")
    passed, missing = sdv.score_turn(case, reply)
    assert not passed
    assert missing == ["verbatim:not-contiguous"]


def test_a_reordered_reply_fails_the_live_turn(tmp_path) -> None:
    reply = ("Contact Meridian Roadside Assistance. Do not restart the vehicle. Stop driving "
             "safely now. LIVETEST-SDV marker.")
    res = _run(_world(replies=[(200, {"result": reply})]), tmp_path)
    assert _status(res, "eval-turn-1") == "FAIL"


# ===========================================================================
# --runtime-updated-after (review cycle 1 S1)
# ===========================================================================


def test_a_runtime_older_than_the_fix_deploy_fails_without_a_turn(tmp_path) -> None:
    w = _world(updated="2026-10-09T20:49:19Z")
    res = _run(w, tmp_path, updated_after=sdv._utc_time("2026-10-27T23:00:00Z"))
    assert _status(res, "runtime-updated-after") == "FAIL"
    assert w.api.calls == []
    assert w.table.delete_item.called


def test_a_runtime_updated_after_the_fix_deploy_runs(tmp_path) -> None:
    w = _world(updated="2026-10-27T23:00:05Z")
    res = _run(w, tmp_path, updated_after=sdv._utc_time("2026-10-27T23:00:00Z"))
    assert _status(res, "runtime-updated-after") == "PASS"
    assert _status(res, "eval-turn-1") == "PASS"


def test_runtime_update_time_from_boto3_datetime_is_compared() -> None:
    import datetime as dt

    w = _world()
    w.ctrl.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "environmentVariables": {"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        "agentRuntimeVersion": "51",
        "lastUpdatedAt": dt.datetime(2026, 10, 27, 23, 0, 5, tzinfo=dt.timezone.utc),
    }
    info = sdv.text_runtime_info(w.ctrl, "staging")
    assert info["id"] == "rt-1"
    assert info["updated"] >= sdv._utc_time("2026-10-27T23:00:00Z")


@pytest.mark.parametrize("value", ["yesterday", "2026-10-27T16:00", "2026-10-27"])
def test_bad_runtime_updated_after_is_refused(monkeypatch, no_aws, value: str) -> None:
    """Not ISO, or no offset: a bare local time read as UTC would set the bound hours early."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit):
        sdv.main(["--stage", "staging", "--allow-writes", "--runtime-updated-after", value])
    no_aws.assert_not_called()


def test_runtime_updated_after_offset_is_honoured() -> None:
    assert sdv._utc_time("2026-10-27T16:00:00-07:00") == sdv._utc_time("2026-10-27T23:00:00Z")


# ===========================================================================
# main() past the gates: teardown, residue checks, exit codes (review cycle 1 W3)
# ===========================================================================


def _main_world(monkeypatch, tmp_path, *, delete_error=None, residue_ok=True, reply=_VERBATIM):
    w = _world(replies=[(200, {"result": reply})])
    if delete_error is not None:
        w.table.delete_item.side_effect = delete_error
    clients = {
        "sts": MagicMock(**{"get_caller_identity.return_value": {"Arn": "arn:aws:sts::x:me"}}),
        "bedrock-agentcore-control": w.ctrl,
        "cognito-idp": MagicMock(),
        "cloudformation": MagicMock(**{"describe_stacks.return_value": {"Stacks": [{"Outputs": [
            {"OutputKey": "RestApiUrl", "OutputValue": "https://api.example.invalid/staging/"}]}]}}),
        "ssm": MagicMock(**{"get_parameter.return_value": {"Parameter": {"Value": "pool-1"}}}),
    }
    calls = {"findings": 0, "cognito": 0}

    def _findings(*_a):
        calls["findings"] += 1
        return (residue_ok, "residue")

    def _cognito(*_a):
        calls["cognito"] += 1
        return (True, "0")

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(sdv, "_boto3_client", lambda service, region: clients[service])
    monkeypatch.setattr(sdv, "_boto3_resource", lambda service, region: w.ddb)
    monkeypatch.setattr(sdv, "_RUN_ROOT", tmp_path)
    monkeypatch.setattr(sdv._rw, "_in_write_window", lambda: False)
    monkeypatch.setattr(sdv._rw, "password_client", lambda idp, pool: "client-1")
    monkeypatch.setattr(sdv._rw, "principal", _fake_principal)
    monkeypatch.setattr(sdv._rw, "Http", lambda base: w.api)
    monkeypatch.setattr(sdv._rw, "_check_residue_findings", _findings)
    monkeypatch.setattr(sdv._rw, "_check_residue_cognito", _cognito)
    return w, calls


def test_main_passes_cleans_up_and_checks_residue(monkeypatch, tmp_path) -> None:
    w, calls = _main_world(monkeypatch, tmp_path)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 0
    kw = w.table.delete_item.call_args.kwargs
    assert kw["ConditionExpression"] == "begins_with(finding_id, :pfx) AND livetest_run = :run"
    assert kw["ExpressionAttributeValues"][":run"].startswith("sdv-")
    assert calls == {"findings": 1, "cognito": 1}
    (run_dir,) = list(tmp_path.iterdir())
    assert (run_dir / "reply-1.txt").read_text() == _VERBATIM


def test_main_exits_1_when_cleanup_fails(monkeypatch, tmp_path) -> None:
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "DeleteItem")
    _w, calls = _main_world(monkeypatch, tmp_path, delete_error=err)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 1
    assert calls == {"findings": 1, "cognito": 1}, "residue checks run even after a failed cleanup"


def test_main_exits_1_on_residue(monkeypatch, tmp_path) -> None:
    _main_world(monkeypatch, tmp_path, residue_ok=False)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 1


def test_main_exits_1_on_a_softened_reply(monkeypatch, tmp_path) -> None:
    _main_world(monkeypatch, tmp_path, reply=_SOFTENED)
    assert sdv.main(["--stage", "staging", "--allow-writes"]) == 1


def test_main_passes_runtime_updated_after_through(monkeypatch, tmp_path) -> None:
    """The fake text runtime was updated 2026-10-27T22:30:00Z; a later bound fails the run."""
    w, _calls = _main_world(monkeypatch, tmp_path)
    argv = ["--stage", "staging", "--allow-writes",
            "--runtime-updated-after", "2026-10-28T00:00:00Z"]
    assert sdv.main(argv) == 1
    assert w.api.calls == []


# ===========================================================================
# Recovery messages (security review cycle 1, area B, S1/S3)
# ===========================================================================


def test_a_failed_teardown_prints_the_delete_command(capsys) -> None:
    from botocore.exceptions import ClientError

    table = MagicMock()
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "DeleteItem")
    with pytest.raises(ClientError):
        sdv._teardown_plant(table, "r1", "staging")
    err = capsys.readouterr().err
    assert "aws dynamodb delete-item" in err
    assert "vsa-staging-avx-findings" in err and sdv.FINDING_ID in err


def test_an_existing_row_is_named_and_left_alone(tmp_path, capsys) -> None:
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                      "PutItem")
    w = _world(put_error=err)
    res = _run(w, tmp_path)
    assert _status(res, "plant") == "FAIL"
    assert "aws dynamodb delete-item" in capsys.readouterr().err
    # teardown's delete is conditional on this run, so the existing row survives it
    kw = w.table.delete_item.call_args.kwargs
    assert kw["ExpressionAttributeValues"][":run"] == "r1"
    assert w.api.calls == []
