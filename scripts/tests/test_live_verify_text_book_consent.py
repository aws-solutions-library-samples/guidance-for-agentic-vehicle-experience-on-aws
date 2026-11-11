"""Tests for scripts/live-verify-text-book-consent.py (no AWS).

Covers the gates (refuse before any AWS client), the pure verdict function per
scenario, the message scripts' own words (each scenario's driver turns must be
what the runtime treats as a yes / not a yes / a refusal), and running the
script as a file.

Run from the repo root:
    .venv/bin/python -m pytest scripts/tests/test_live_verify_text_book_consent.py -q
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-text-book-consent.py"
_REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("_lv_tbc_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


lv = _load()


# ──────────────────────────────────────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _no_run(monkeypatch):
    monkeypatch.setattr(lv, "run", MagicMock(side_effect=AssertionError("run() reached before the gates passed")))


def test_refused_without_live_tests(monkeypatch, _no_run):
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    monkeypatch.setenv(lv._LIVE_GO_ENV, "staging")
    assert lv.main(["--allow-writes", "--scenario", "typed-yes"]) == 1


def test_refused_without_allow_writes(monkeypatch, _no_run):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv(lv._LIVE_GO_ENV, "staging")
    assert lv.main(["--scenario", "typed-yes"]) == 1


def test_refused_without_the_go_env(monkeypatch, _no_run):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.delenv(lv._LIVE_GO_ENV, raising=False)
    monkeypatch.setattr(lv._lwg, "aws_block_active", lambda: False)
    assert lv.main(["--allow-writes", "--scenario", "typed-yes"]) == 1


def test_all_gates_passed_reaches_run(monkeypatch):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv(lv._LIVE_GO_ENV, "staging")
    ran = MagicMock(return_value=0)
    monkeypatch.setattr(lv, "run", ran)
    assert lv.main(["--allow-writes", "--scenario", "never-mind"]) == 0
    assert ran.call_args.args[0].scenario == "never-mind"


@pytest.mark.parametrize("argv", [["--stage", "prod"], ["--region", "us-east-1"], ["--scenario", "nope"]])
def test_only_staging_us_west_2_and_known_scenarios(monkeypatch, _no_run, argv):
    monkeypatch.setenv("LIVE_TESTS", "1")
    base = ["--allow-writes", "--scenario", "typed-yes"]
    with pytest.raises(SystemExit):
        lv.main(base + argv)


def test_the_field_video_car_is_excluded():
    assert {"VEH-MRDN-0015", "MRDN0000000000015"} <= lv.EXCLUDED_VEHICLES


def test_session_ids_reach_the_runtime_unpadded():
    """/assistant/chat pads ids under 33 chars; the log filters look for the id as sent."""
    sid = f"{lv.PREFIX}-{'0' * 24}"
    assert len(sid) >= 33


# ──────────────────────────────────────────────────────────────────────────────
# The driver's words mean what each scenario needs
# ──────────────────────────────────────────────────────────────────────────────

from agents.supervisor import booking_consent as bc  # noqa: E402


def test_no_agree_second_message_is_neither_a_yes_nor_a_refusal():
    text = lv.MESSAGES["no-agree"][1]
    assert not bc.is_typed_booking_yes(text)
    assert not bc.is_booking_refusal(text)


def test_typed_yes_second_message_is_a_yes():
    assert bc.is_typed_booking_yes(lv.MESSAGES["typed-yes"][1])
    assert not bc.is_booking_refusal(lv.MESSAGES["typed-yes"][1])


def test_never_mind_messages():
    _, refusal, yes = lv.MESSAGES["never-mind"]
    assert bc.is_booking_refusal(refusal)
    assert bc.is_typed_booking_yes(yes)


def test_first_messages_are_not_a_yes():
    for msgs in lv.MESSAGES.values():
        assert not bc.is_typed_booking_yes(msgs[0])


# ──────────────────────────────────────────────────────────────────────────────
# evaluate()
# ──────────────────────────────────────────────────────────────────────────────

OFFER = "booking(text): offer set session=S center=SVC-1"
CONSENT = "booking(text): consent set from offer session=S center=SVC-1"
CONSENT_OTHER = "booking(text): consent set from offer session=S center=SVC-2"
CLEARED = "booking(text): offer and consent cleared by refusal session=S text='Never mind.'"
REFUSED = "book: consent refused — no driver confirmation session=S"
ROW = {"requestNumber": "VSA-2026-11-03-ABCD"}


def _ev(scenario, **kw):
    base = dict(rows=[], escalated=False, offer_lines_after_first=[], unranked_lines_after_first=[],
                fsc_ran_after_first=False, consent_lines=[],
                refusal_clear_lines=[], book_refused_lines=[], book_tool_lines=[])
    base.update(kw)
    return lv.evaluate(scenario, **base)


@pytest.mark.parametrize("scenario", sorted(lv.MESSAGES))
def test_any_escalation_fails(scenario):
    assert _ev(scenario, escalated=True, rows=[ROW], offer_lines_after_first=[OFFER],
               consent_lines=[CONSENT])[0] == 1


@pytest.mark.parametrize("scenario", ["no-agree", "asks-first"])
def test_no_agree_pass_needs_a_refusal_and_no_row(scenario):
    assert _ev(scenario, book_refused_lines=[REFUSED])[0] == 0


@pytest.mark.parametrize("scenario", ["no-agree", "asks-first"])
def test_no_agree_any_row_fails(scenario):
    assert _ev(scenario, rows=[ROW], book_refused_lines=[REFUSED])[0] == 1


@pytest.mark.parametrize("scenario", ["no-agree", "asks-first"])
def test_no_agree_without_a_book_call_is_inconclusive(scenario):
    assert _ev(scenario)[0] == 2


def test_typed_yes_pass():
    code, msg = _ev("typed-yes", rows=[ROW], offer_lines_after_first=[OFFER], consent_lines=[CONSENT])
    assert code == 0 and "VSA-2026-11-03-ABCD" in msg


def test_typed_yes_without_an_offer_is_inconclusive():
    assert _ev("typed-yes", rows=[ROW], consent_lines=[CONSENT])[0] == 2


def test_typed_yes_without_consent_fails():
    assert _ev("typed-yes", rows=[ROW], offer_lines_after_first=[OFFER])[0] == 1


def test_typed_yes_consent_for_another_center_fails():
    assert _ev("typed-yes", rows=[ROW], offer_lines_after_first=[OFFER], consent_lines=[CONSENT_OTHER])[0] == 1


def test_typed_yes_two_rows_fail():
    assert _ev("typed-yes", rows=[ROW, ROW], offer_lines_after_first=[OFFER], consent_lines=[CONSENT])[0] == 1


def test_typed_yes_refused_after_consent_fails():
    assert _ev("typed-yes", offer_lines_after_first=[OFFER], consent_lines=[CONSENT],
               book_refused_lines=[REFUSED])[0] == 1


def test_typed_yes_no_book_call_is_inconclusive():
    assert _ev("typed-yes", offer_lines_after_first=[OFFER], consent_lines=[CONSENT])[0] == 2


def test_typed_yes_book_called_but_no_row_fails():
    assert _ev("typed-yes", offer_lines_after_first=[OFFER], consent_lines=[CONSENT],
               book_tool_lines=["Tool #3: book"])[0] == 1


def test_never_mind_pass():
    assert _ev("never-mind", offer_lines_after_first=[OFFER], refusal_clear_lines=[CLEARED])[0] == 0


def test_never_mind_row_fails():
    assert _ev("never-mind", rows=[ROW], offer_lines_after_first=[OFFER], refusal_clear_lines=[CLEARED])[0] == 1


def test_never_mind_not_cleared_fails():
    assert _ev("never-mind", offer_lines_after_first=[OFFER])[0] == 1


def test_never_mind_without_an_offer_is_inconclusive():
    assert _ev("never-mind", refusal_clear_lines=[CLEARED])[0] == 2


@pytest.mark.parametrize("scenario", ["typed-yes", "never-mind"])
def test_no_offer_with_the_location_unknown_is_inconclusive(scenario):
    code, msg = _ev(scenario, unranked_lines_after_first=["booking(text): no offer recorded"], fsc_ran_after_first=True)
    assert code == 2 and "location" in msg


@pytest.mark.parametrize("scenario", ["typed-yes", "never-mind"])
def test_find_service_center_ran_but_no_offer_fails(scenario):
    """The recorder couldn't read a live result: a defect, not the model's choice."""
    assert _ev(scenario, fsc_ran_after_first=True)[0] == 1


def test_a_p0_booking_is_inconclusive_not_a_pass():
    p0 = {"requestNumber": "VSA-2026-11-03-P0P0", "triagePriority": "P0"}
    assert _ev("typed-yes", rows=[p0], offer_lines_after_first=[OFFER], consent_lines=[CONSENT])[0] == 2


def test_the_center_regex_reads_the_runtime_log_line():
    assert lv._center(OFFER) == "SVC-1"


# ──────────────────────────────────────────────────────────────────────────────
# Run as a file (sys.path[0] is scripts/, not the repo root)
# ──────────────────────────────────────────────────────────────────────────────


def test_runs_as_a_file_from_outside_the_repo(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.pop("LIVE_TESTS", None)
    code = (
        "import runpy, sys; "
        f"sys.argv=['x','--allow-writes','--scenario','typed-yes']; "
        f"m=runpy.run_path({str(_SCRIPT)!r}, run_name='probe'); "
        "import agents.supervisor.booking_consent, live_check_helpers, live_write_guard; "
        "print('rc', m['main'](sys.argv[1:]))"
    )
    p = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    assert "rc 1" in p.stdout  # refused without LIVE_TESTS, after importing everything


# ──────────────────────────────────────────────────────────────────────────────
# run() wiring with every AWS call stubbed (found live 2026-11-03: a one-message
# scenario passed endTime=None to filter_log_events and never printed a verdict)
# ──────────────────────────────────────────────────────────────────────────────


class _FakeIdp:
    def __init__(self):
        self.deleted = []

    def get_paginator(self, name):
        return MagicMock(paginate=lambda **kw: [{"UserPoolClients": [{"ClientId": "c1"}]}])

    def describe_user_pool_client(self, **kw):
        return {"UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}

    def admin_get_user(self, **kw):
        return {"UserAttributes": [{"Name": "custom:tenantId", "Value": "meridian-fleet"}]}

    def admin_list_groups_for_user(self, **kw):
        return {"Groups": [{"GroupName": "vehicle-owner"}]}

    def admin_create_user(self, **kw):
        pass

    def admin_set_user_password(self, **kw):
        pass

    def admin_add_user_to_group(self, **kw):
        pass

    def admin_update_user_attributes(self, **kw):
        pass

    def initiate_auth(self, **kw):
        return {"AuthenticationResult": {"IdToken": "tok"}}


def _stub_aws(monkeypatch, idp):
    import types

    clients = {
        "cognito-idp": idp,
        "ssm": MagicMock(get_parameter=lambda **kw: {"Parameter": {"Value": "pool"}}),
        "cloudformation": MagicMock(describe_stacks=lambda **kw: {
            "Stacks": [{"Outputs": [{"OutputKey": "RestApiUrl", "OutputValue": "https://api.invalid/"}]}]}),
        "bedrock-agentcore-control": MagicMock(list_agent_runtimes=lambda **kw: {"agentRuntimes": [
            {"agentRuntimeName": "vsa_supervisor_text_staging", "agentRuntimeId": "rt-1",
             "agentRuntimeVersion": "52"}]}),
        "logs": MagicMock(),
    }
    fake_boto3 = types.SimpleNamespace(
        client=lambda name, region_name=None: clients[name],
        resource=lambda name, region_name=None: MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(lv.time, "sleep", lambda s: None)
    monkeypatch.setattr(lv, "pick_vehicle", lambda *a, **k: ("VIN00000000000001", "VEH-TEST-001", "P2", None))


@pytest.mark.parametrize("scenario", sorted(lv.MESSAGES))
def test_run_reaches_a_verdict_and_cleans_up(monkeypatch, scenario):
    idp = _FakeIdp()
    _stub_aws(monkeypatch, idp)
    sent = []
    monkeypatch.setattr(lv, "post_chat", lambda base, token, body: (sent.append(body) or (200, {"result": "ok"})))
    windows = []

    def fake_log_lines(logs, group, start_ms, end_ms, pattern):
        assert isinstance(start_ms, int) and isinstance(end_ms, int) and start_ms <= end_ms
        windows.append(pattern)
        return []

    monkeypatch.setattr(lv, "log_lines", fake_log_lines)
    monkeypatch.setattr(lv, "_query_session_bookings", lambda *a: [])
    cleaned = {}
    monkeypatch.setattr(lv, "_run_cleanup", lambda **kw: cleaned.update(kw))
    args = lv.argparse.Namespace(stage="staging", region="us-west-2", scenario=scenario, max_tries=3)

    code = lv.run(args)

    assert code in (0, 1, 2)
    assert [b["prompt"] for b in sent] == lv.MESSAGES[scenario]
    assert len({b["sessionId"] for b in sent}) == 1
    assert windows, "no runtime log was read"
    assert cleaned["created"] is True and cleaned["vid"] == "VEH-TEST-001"
    assert cleaned["session_id"] == sent[0]["sessionId"]


def test_run_cleans_up_when_a_chat_call_raises(monkeypatch):
    idp = _FakeIdp()
    _stub_aws(monkeypatch, idp)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(lv, "post_chat", boom)
    cleaned = {}
    monkeypatch.setattr(lv, "_run_cleanup", lambda **kw: cleaned.update(kw))
    args = lv.argparse.Namespace(stage="staging", region="us-west-2", scenario="typed-yes", max_tries=3)
    with pytest.raises(RuntimeError):
        lv.run(args)
    assert cleaned["created"] is True
    # A chat call that raises (a timeout) may still have booked: row cleanup needs these.
    assert cleaned["vid"] == "VEH-TEST-001"
    assert cleaned["session_id"] and cleaned["session_id"].startswith(lv.PREFIX)


@pytest.mark.parametrize("with_row, expected", [(True, 1), (False, 0)])
def test_run_passes_the_evidence_to_the_verdict(monkeypatch, with_row, expected):
    """asks-first with a refusal line: a booking row makes it FAIL, none makes it PASS."""
    idp = _FakeIdp()
    _stub_aws(monkeypatch, idp)
    sent = []
    monkeypatch.setattr(lv, "post_chat", lambda base, token, body: (sent.append(body) or (200, {"result": "ok"})))

    def fake_log_lines(logs, group, start_ms, end_ms, pattern):
        sid = sent[0]["sessionId"]
        if pattern == '"book: consent refused"':
            return [f"book: consent refused — no driver confirmation session={sid}"]
        return []

    monkeypatch.setattr(lv, "log_lines", fake_log_lines)
    row = {"requestNumber": "VSA-2026-11-03-TEST", "triagePriority": "P2"}
    monkeypatch.setattr(lv, "_query_session_bookings", lambda *a: [row] if with_row else [])
    monkeypatch.setattr(lv, "_run_cleanup", lambda **kw: None)
    args = lv.argparse.Namespace(stage="staging", region="us-west-2", scenario="asks-first", max_tries=3)
    assert lv.run(args) == expected
