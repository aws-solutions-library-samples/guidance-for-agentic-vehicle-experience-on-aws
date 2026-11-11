"""Guards of scripts/live-verify-p0-crm-profile.py.

Drives the real main() with boto3 stubbed (pattern:
scripts/tests/test_live_verify_connect_crm.py, runs dir in tmp):
  - each refusal makes no write;
  - cleanup honours the profile-ownership rule both ways;
  - C3 FAIL when the profile attributes or p0CallbackPlaced are missing;
  - C4a FAIL when the case customer_id doesn't match the profile ARN;
  - C4b FAIL when the Contact related item is absent.

Run:
    .venv/bin/python3 -m pytest scripts/tests/test_live_verify_p0_crm_profile.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "live_verify_p0_crm_profile",
    _REPO / "scripts" / "live-verify-p0-crm-profile.py",
)
lv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lv)

INSTANCE_ARN = "arn:aws:connect:us-east-1:111122223333:instance/inst-0001"
CRM = {
    "instanceArn": INSTANCE_ARN,
    "profilesDomain": "amazon-connect-test",
    "objectType": "CvxVehicle",
    "casesDomainId": "dom-0001",
    "casesTemplateId": "tpl-0001",
    "region": "us-east-1",
    "account": "111122223333",
    "fieldIds": {n: f"f-{n}" for n in (
        "severity", "vin", "vehicleId", "faultCodes", "escalationKey", "triageSessionId")},
}

DRIVER = "DRV-TEST-0001"
VEHICLE = "VEH-TEST-0001"
VIN = "1HGBH41JXMN000099"
PHONE = "+15555550199"          # fictional: 1-NXX-555-01XX
PHONE_REAL = "+15551234567"     # not fictional: 555-12XX, not 555-01XX
ACCT = f"meridian:{DRIVER}"

# A profile this run created: absent before, AccountNumber set, source=avx-escalate.
FIRST, LAST = "Testa", "Driverson"   # the drivers row's names (C6)
OURS = [{"ProfileId": "p-1", "AccountNumber": ACCT, "FirstName": FIRST, "LastName": LAST,
          "PhoneNumber": PHONE, "Attributes": {"source": "avx-escalate"}}]
# A profile that existed before this run.
PRE_EXISTING = [{"ProfileId": "p-old", "AccountNumber": ACCT,
                  "Attributes": {"source": "avx-escalate"}}]

_RUNS_TMP: dict[str, Path] = {}


@pytest.fixture(autouse=True)
def _runs_dir_in_tmp(tmp_path):
    """Each test gets its own runs/ so the earlier-run refusal reads an isolated dir."""
    _RUNS_TMP["dir"] = tmp_path / "runs"
    yield
    _RUNS_TMP.clear()


# ---------------------------------------------------------------------------
# World — mock harness
# ---------------------------------------------------------------------------

class World:
    """Minimal mock world for live-verify-p0-crm-profile.py main()."""

    def __init__(
        self,
        *,
        crm_env: bool = True,
        callback_armed: bool = True,
        phone_profiles: list = (),      # for the unclaimed-phone check
        account_before: list = (),      # profiles before invoke
        account_after: list = (),       # profiles after invoke (_account search)
        profileid_after: list | None = None,  # profiles after invoke (_profileId search)
        case_customer_id: str | None = None,  # returned by get_case(fields=[customer_id])
        related_items: list = (),       # SearchRelatedItems return value
        invoke_logs: str = "",          # base64-decoded LogResult content
    ):
        # --- Lambda client ---
        self.lam = MagicMock()
        env: dict[str, str] = {}
        if crm_env:
            env["CONNECT_CRM_CONFIG"] = json.dumps(CRM)
        self.lam.get_function_configuration.return_value = {
            "Environment": {"Variables": env}}

        default_logs = (
            "START RequestId: req-0001 Version: $LATEST\n"
            "2026-11-03T19:54:37.011Z\treq-0001\tINFO\tcallback: initiated outbound call\n"
            "2026-11-03T19:54:38.161Z\treq-0001\tINFO\tescalate: contact=chat-1 severity=P0"
            " callback_initiated=True\n"
            "END RequestId: req-0001\n"
        )
        import base64
        log_b64 = base64.b64encode(
            (invoke_logs or default_logs).encode()).decode()
        self.lam.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({
                "statusCode": 200,
                "body": json.dumps({
                    "contactId": "chat-1",
                    "participantToken": "tok",
                    "rsaDispatched": False,
                }),
            }).encode()),
            "LogResult": log_b64,
        }

        # --- Customer Profiles client ---
        self.cp = MagicMock()
        self._invoked = False

        def _search(DomainName, KeyName, Values, MaxResults=10):
            if KeyName == "_phone":
                return {"Items": list(phone_profiles)}
            if KeyName == "_profileId":
                pool = (list(profileid_after) if profileid_after is not None
                        else list(account_after if self.lam.invoke.called else account_before))
                return {"Items": [p for p in pool if p.get("ProfileId") in Values]}
            # _account search
            pool = list(account_after if self.lam.invoke.called else account_before)
            return {"Items": pool}

        self.cp.search_profiles.side_effect = _search
        self.cp.list_profile_objects.return_value = {"Items": []}

        # --- Connect Cases client ---
        self.cc = MagicMock()
        self.cc.search_cases.return_value = {"cases": [{"caseId": "case-1"}]}
        _cust = case_customer_id
        self.cc.get_case.return_value = {"fields": (
            [{"id": "customer_id", "value": {"stringValue": _cust}}]
            if _cust is not None else []
        )}
        _ri = list(related_items)
        self.cc.search_related_items.return_value = {"relatedItems": _ri}

        # --- Connect client ---
        self.connect = MagicMock()
        cb_cfg: dict[str, str] = {}
        if callback_armed:
            cb_cfg = {
                "contactFlowId": "cb-flow-1",
                "queueArn": f"{INSTANCE_ARN}/queue/q-0001",
                "chatContactFlowId": "after-cb-flow-1",
            }
        _tenant_cfg = {
            "channels": {"connect": {
                "instanceArn": INSTANCE_ARN,
                "contactFlowId": "flow-1",
            }},
            "escalation": {"callback": cb_cfg},
        }
        self.connect.get_contact_attributes.return_value = {"Attributes": {
            "p0CallbackPlaced": "true",
            "p0CallbackEscalationId": "esc-001",
            "customerProfileId": "p-1",
            "profileSearchKey": "_profileId",
            "profileSearchValue": "p-1",
        }}
        self.connect.search_contacts.return_value = {"Contacts": []}

        # --- DynamoDB resource ---
        self.ddb = MagicMock()
        tables = {
            "vsa-staging-tenant-config": MagicMock(),
            "cms-staging-storage-live-plan": MagicMock(),
            "cms-staging-storage-drivers": MagicMock(),
            "cms-staging-storage-vehicles": MagicMock(),
            "vsa-staging-triage-decisions": MagicMock(),
        }
        tc = tables["vsa-staging-tenant-config"]
        tc.get_item.side_effect = lambda Key: (
            {"Item": {"activeVersion": "1.0"}} if Key["version"] == "current"
            else {"Item": _tenant_cfg}
        )
        tables["cms-staging-storage-live-plan"].scan.return_value = {"Items": []}
        tables["cms-staging-storage-drivers"].get_item.return_value = {
            "Item": {
                "driverId": DRIVER,
                "assignedVehicleId": VEHICLE,
                "phone": PHONE,
                "firstName": FIRST,
                "lastName": LAST,
            }
        }
        tables["cms-staging-storage-vehicles"].get_item.return_value = {
            "Item": {"vehicleId": VEHICLE, "vin": VIN}
        }
        tables["vsa-staging-triage-decisions"].get_item.return_value = {"Item": None}
        self.tables = tables
        self.ddb.Table.side_effect = lambda name: tables[name]

        # --- CloudWatch Logs client (for run record offsets) ---
        self.logs = MagicMock()
        self.logs.filter_log_events.return_value = {"events": []}

        self.runs_dir = _RUNS_TMP["dir"]
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def with_connect_attrs(self, **attrs):
        """Override the mock connect.get_contact_attributes return value."""
        self.connect.get_contact_attributes.return_value = {"Attributes": attrs}
        return self

    def run(self, monkeypatch, *extra_args):
        clients = {
            "lambda": self.lam,
            "customer-profiles": self.cp,
            "connectcases": self.cc,
            "connect": self.connect,
            "connectparticipant": MagicMock(),
            "logs": self.logs,
        }
        monkeypatch.setenv("LIVE_TESTS", "1")
        argv = [
            "live-verify-p0-crm-profile.py",
            "--driver-id", DRIVER,
            "--vehicle-id", VEHICLE,
            *extra_args,
        ]
        with patch("boto3.client", side_effect=lambda svc, **kw: clients[svc]), \
             patch("boto3.resource", return_value=self.ddb), \
             patch.object(lv, "_RUNS_DIR", self.runs_dir), \
             patch.object(lv.time, "sleep"), \
             patch.object(sys, "argv", argv):
            return lv.main()


def _verdicts(capsys) -> dict[str, str]:
    """Parse PASS/FAIL labels from stdout."""
    out = capsys.readouterr().out
    v: dict[str, str] = {}
    for line in out.splitlines():
        for word in ("PASS", "FAIL"):
            if line.startswith(f"{word}: C"):
                # e.g. "PASS: C3 Chat attributes ..."
                label = line.split(":", 2)[1].strip().split()[0]
                v[label] = word
    return v


# ---------------------------------------------------------------------------
# Refusal tests — none must invoke Lambda
# ---------------------------------------------------------------------------

def test_refused_driver_id_refuses_before_any_write(monkeypatch):
    """Mutant: drop the _REFUSED_DRIVERS check → DRV-MRDN-0015 could be invoked."""
    w = World()
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": "DRV-MRDN-0015", "assignedVehicleId": VEHICLE, "phone": PHONE}}
    rc = w.run(monkeypatch, "--driver-id", "DRV-MRDN-0015", "--vehicle-id", VEHICLE)
    assert rc == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_refused_vehicle_id_refuses_before_any_write(monkeypatch):
    w = World()
    w.tables["cms-staging-storage-vehicles"].get_item.return_value = {
        "Item": {"vehicleId": "VEH-MRDN-0015", "vin": VIN}}
    rc = w.run(monkeypatch, "--driver-id", DRIVER, "--vehicle-id", "VEH-MRDN-0015")
    assert rc == 1
    w.lam.invoke.assert_not_called()


def test_refused_vehicle_vin_refuses_before_any_write(monkeypatch, capsys):
    """The field-video car is refused under its VIN too (after the vehicleId = VIN migration),
    by the hard refusal itself: the driver row is assigned to it, so no later preflight would stop it."""
    w = World()
    w.tables["cms-staging-storage-vehicles"].get_item.return_value = {
        "Item": {"vehicleId": "MRDN0000000000015", "vin": "MRDN0000000000015"}}
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": DRIVER, "assignedVehicleId": "MRDN0000000000015", "phone": PHONE}}
    rc = w.run(monkeypatch, "--driver-id", DRIVER, "--vehicle-id", "MRDN0000000000015")
    assert rc == 1
    assert "PREFLIGHT FAILED: --driver-id / --vehicle-id must not be" in capsys.readouterr().out
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_absent_crm_config_refuses_before_any_write(monkeypatch):
    """Mutant: drop the CRM config absence check → a run without the feature would be
    recorded as live-verified."""
    w = World(crm_env=False)
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_callback_not_armed_refuses_before_any_write(monkeypatch):
    """Mutant: drop the callback_armed check → the P0 call path isn't being tested."""
    w = World(callback_armed=False)
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_non_fictional_phone_refuses_before_any_write(monkeypatch):
    """Mutant: drop the phone format check → callback would dial a real number."""
    w = World()
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": DRIVER, "assignedVehicleId": VEHICLE, "phone": PHONE_REAL}}
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_unclaimed_phone_match_refuses_before_any_write(monkeypatch):
    """Mutant: drop the unclaimed-phone check → handler adopts an existing profile
    that cleanup must not delete."""
    w = World(phone_profiles=[{"ProfileId": "ctr-1"}])  # no AccountNumber = unclaimed
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_driver_not_assigned_to_vehicle_refuses_before_any_write(monkeypatch):
    """Mutant: drop the assignment check → C4a would fail for a reason unrelated to
    the bug under test (vehicle object written under wrong profile key)."""
    w = World()
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": DRIVER, "assignedVehicleId": "VEH-OTHER-0001", "phone": PHONE}}
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_live_roster_car_refuses_before_any_write(monkeypatch):
    """Mutant: drop the roster check → a live-tracked vehicle could be disturbed."""
    w = World()
    w.tables["cms-staging-storage-live-plan"].scan.return_value = {
        "Items": [{"vehicleId": VEHICLE}]}
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_driver_used_by_earlier_run_refuses_before_any_write(monkeypatch):
    """Mutant: drop the earlier-run check → the vehicle object reuse silently breaks
    C4a for a reason unrelated to the CRM profile bug."""
    w = World()
    (w.runs_dir / "crm-20261103T120000Z-abc123.json").write_text(
        json.dumps({"runId": "crm-20261103T120000Z-abc123", "driverId": DRIVER}))
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_another_drivers_earlier_run_does_not_refuse(monkeypatch):
    """A run that used a DIFFERENT driver must not block this run."""
    w = World(account_after=OURS)
    (w.runs_dir / "crm-x.json").write_text(
        json.dumps({"runId": "crm-x", "driverId": "DRV-OTHER-0001"}))
    w.run(monkeypatch)
    w.lam.invoke.assert_called()


def test_without_live_tests_exits_2(monkeypatch):
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    argv = ["live-verify-p0-crm-profile.py",
            "--driver-id", DRIVER, "--vehicle-id", VEHICLE]
    with patch.object(sys, "argv", argv):
        assert lv.main() == 2


def test_dry_run_exits_0_without_live_tests(monkeypatch, tmp_path):
    """--dry-run must not require LIVE_TESTS=1."""
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    w = World()
    argv = ["live-verify-p0-crm-profile.py",
            "--driver-id", DRIVER, "--vehicle-id", VEHICLE, "--dry-run"]
    with patch("boto3.client", side_effect=lambda svc, **kw: {
                "lambda": w.lam, "customer-profiles": w.cp,
                "connectcases": w.cc, "connect": w.connect,
                "connectparticipant": MagicMock(), "logs": w.logs}[svc]), \
         patch("boto3.resource", return_value=w.ddb), \
         patch.object(lv, "_RUNS_DIR", tmp_path / "runs"), \
         patch.object(sys, "argv", argv):
        assert lv.main() == 0
    w.lam.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Fixture / triage row lifecycle
# ---------------------------------------------------------------------------

def test_fixture_row_is_deleted_on_the_way_out(monkeypatch):
    w = World(account_after=OURS)
    w.run(monkeypatch)
    triage = w.tables["vsa-staging-triage-decisions"]
    assert triage.put_item.call_count == 1
    sid = triage.put_item.call_args.kwargs["Item"]["sessionId"]
    triage.delete_item.assert_called_once_with(Key={"sessionId": sid})


# ---------------------------------------------------------------------------
# Cleanup ownership — profile deletion rule
# ---------------------------------------------------------------------------

def test_cleanup_deletes_a_profile_this_run_created(monkeypatch):
    """Mutant: drop the ownership check → deletes profiles even when pre-existing."""
    w = World(account_after=OURS)
    w.run(monkeypatch)
    w.cp.delete_profile.assert_called_once_with(
        DomainName="amazon-connect-test", ProfileId="p-1")


def test_cleanup_keeps_a_profile_that_existed_before_the_run(monkeypatch):
    """Mutant: flip profile_was_new unconditionally to True → deletes pre-existing
    profiles that belong to real drivers."""
    w = World(account_before=PRE_EXISTING, account_after=PRE_EXISTING)
    w.run(monkeypatch)
    w.cp.delete_profile.assert_not_called()


def test_cleanup_keeps_a_profile_without_the_source_marker(monkeypatch):
    """Mutant: drop the source=avx-escalate check → any absent-then-created profile
    gets deleted even if the handler didn't set the marker."""
    no_source = [{"ProfileId": "p-1", "AccountNumber": ACCT, "Attributes": {}}]
    w = World(account_after=no_source)
    w.run(monkeypatch)
    w.cp.delete_profile.assert_not_called()


# ---------------------------------------------------------------------------
# Check assertions — C1
# ---------------------------------------------------------------------------

def test_c1_fails_when_status_is_not_200(monkeypatch, capsys):
    w = World(account_after=OURS)
    import base64
    w.lam.invoke.return_value = {
        "Payload": MagicMock(read=lambda: json.dumps({
            "statusCode": 500,
            "body": json.dumps({"error": "internal error"}),
        }).encode()),
        "LogResult": base64.b64encode(b"").decode(),
    }
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C1") == "FAIL"


def test_c1_fails_when_rsa_dispatched_is_true(monkeypatch, capsys):
    w = World(account_after=OURS)
    import base64
    w.lam.invoke.return_value = {
        "Payload": MagicMock(read=lambda: json.dumps({
            "statusCode": 200,
            "body": json.dumps({
                "contactId": "chat-1", "rsaDispatched": True}),
        }).encode()),
        "LogResult": base64.b64encode(b"START RequestId: req-0001\n"
                                      b"escalate: contact=chat-1 callback_initiated=True\n").decode(),
    }
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C1") == "FAIL"


# ---------------------------------------------------------------------------
# Check assertions — C2
# ---------------------------------------------------------------------------

def test_c2_fails_when_crm_skipped_is_in_logs(monkeypatch, capsys):
    """Mutant: C2 passes when crm:skipped is present → bug not caught."""
    import base64
    bad_logs = (
        "START RequestId: req-0001 Version: $LATEST\n"
        "2026-11-03T19:54:37.011Z\treq-0001\tINFO\tcallback: initiated\n"
        "2026-11-03T19:54:37.198Z\treq-0001\tINFO\tcrm: skipped step=profile-search-account"
        " reason=budget\n"
        "2026-11-03T19:54:38.161Z\treq-0001\tINFO\tescalate: contact=chat-1 severity=P0"
        " callback_initiated=True\n"
        "END RequestId: req-0001\n"
    )
    w = World(
        account_after=OURS,
        invoke_logs=bad_logs,
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C2") == "FAIL"


def test_c2_fails_when_callback_initiated_is_absent(monkeypatch, capsys):
    """Mutant: C2 passes on any non-empty log → misses the callback flag."""
    import base64
    bad_logs = (
        "START RequestId: req-0001 Version: $LATEST\n"
        "escalate: contact=chat-1 severity=P0\n"   # no callback_initiated=True
        "END RequestId: req-0001\n"
    )
    w = World(account_after=OURS, invoke_logs=bad_logs)
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C2") == "FAIL"


# ---------------------------------------------------------------------------
# Check assertions — C3
# ---------------------------------------------------------------------------

def test_c3_fails_when_profile_search_key_is_wrong(monkeypatch, capsys):
    """Mutant: C3 checks presence of customerProfileId only, ignoring key."""
    w = World(account_after=OURS).with_connect_attrs(
        customerProfileId="p-1",
        profileSearchKey="_account",   # wrong key
        profileSearchValue="p-1",
        p0CallbackPlaced="true",
        p0CallbackEscalationId="esc-001",
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C3") == "FAIL"


def test_c3_fails_when_profile_search_value_is_account_not_profile_id(monkeypatch, capsys):
    """Mutant: C3 accepts any non-empty profileSearchValue."""
    w = World(account_after=OURS).with_connect_attrs(
        customerProfileId="p-1",
        profileSearchKey="_profileId",
        profileSearchValue=ACCT,  # value is the account, not the profile id
        p0CallbackPlaced="true",
        p0CallbackEscalationId="esc-001",
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C3") == "FAIL"


def test_c3_fails_when_p0_callback_placed_is_absent(monkeypatch, capsys):
    """Mutant: C3 doesn't check p0CallbackPlaced → the callback path isn't verified."""
    w = World(account_after=OURS).with_connect_attrs(
        customerProfileId="p-1",
        profileSearchKey="_profileId",
        profileSearchValue="p-1",
        # p0CallbackPlaced omitted
        p0CallbackEscalationId="esc-001",
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C3") == "FAIL"


def test_c3_fails_when_customer_profile_id_disagrees(monkeypatch, capsys):
    """Mutant: C3 only checks profileSearchValue, not customerProfileId."""
    w = World(account_after=OURS).with_connect_attrs(
        customerProfileId="p-other",  # disagrees with profileSearchValue
        profileSearchKey="_profileId",
        profileSearchValue="p-1",
        p0CallbackPlaced="true",
        p0CallbackEscalationId="esc-001",
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C3") == "FAIL"


def test_c3_fails_when_no_profile_found(monkeypatch, capsys):
    """No profile by _account → C3 fails (the handler didn't create/find the profile)."""
    w = World(account_after=[])  # empty profile search
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C3") == "FAIL"


# ---------------------------------------------------------------------------
# Check assertions — C4a
# ---------------------------------------------------------------------------

def test_c4a_fails_when_customer_id_is_wrong(monkeypatch, capsys):
    """Mutant: C4a passes when any customer_id is present, ignoring the value."""
    w = World(
        account_after=OURS,
        case_customer_id="arn:aws:profile:wrong-arn",
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C4a") == "FAIL"


def test_c4a_fails_when_customer_id_is_absent(monkeypatch, capsys):
    """Mutant: C4a passes when get_case returns no customer_id field."""
    w = World(account_after=OURS, case_customer_id=None)
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C4a") == "FAIL"


def test_c4a_fails_when_case_not_found(monkeypatch, capsys):
    w = World(account_after=OURS)
    w.cc.search_cases.return_value = {"cases": []}
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C4a") == "FAIL"


# ---------------------------------------------------------------------------
# Check assertions — C4b
# ---------------------------------------------------------------------------

def test_c4b_fails_when_no_related_item_for_the_chat(monkeypatch, capsys):
    """Mutant: C4b passes when search_related_items returns any items."""
    w = World(account_after=OURS, related_items=[])
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C4b") == "FAIL"


def test_c4b_fails_when_related_item_has_wrong_contact_arn(monkeypatch, capsys):
    """Mutant: C4b checks related item existence only, not that the ARN matches."""
    wrong_arn = f"{INSTANCE_ARN}/contact/other-contact"
    w = World(
        account_after=OURS,
        related_items=[{
            "relatedItemId": "ri-1",
            "content": {"contact": {"contactArn": wrong_arn}},
        }],
    )
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C4b") == "FAIL"


def test_c4b_passes_when_the_chat_contact_arn_matches(monkeypatch, capsys):
    """Happy-path C4b: the related item's contactArn points at chat-1."""
    from crm import contact_arn  # noqa: PLC0415
    w = World(account_after=OURS)
    arn = contact_arn(CRM, "chat-1")
    w.cc.search_related_items.return_value = {"relatedItems": [{
        "relatedItemId": "ri-1",
        "content": {"contact": {"contactArn": arn}},
    }]}
    # C4a also needs to pass: set a plausible customer_id
    from crm import profile_arn  # noqa: PLC0415
    w.cc.get_case.return_value = {"fields": [{
        "id": "customer_id",
        "value": {"stringValue": profile_arn(CRM, "p-1")},
    }]}
    w.run(monkeypatch)
    v = _verdicts(capsys)
    assert v.get("C4b") == "PASS"


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

def test_run_record_is_written_under_runs_dir(monkeypatch):
    w = World(account_after=OURS)
    w.run(monkeypatch)
    records = list(w.runs_dir.glob("crm-p0-*.json"))
    assert len(records) == 1
    rec = json.loads(records[0].read_text())
    assert rec["driverId"] == DRIVER
    assert rec["vehicleId"] == VEHICLE
    assert "vin" not in rec, "VIN must not appear in the run record"
    assert isinstance(rec.get("vinLength"), int)
    assert "contactId" in rec


def test_run_record_name_starts_with_crm_p0(monkeypatch):
    """Mutant: run record name misses the p0- prefix → _drivers_used_by_earlier_runs
    in the original script would still find it (glob crm-*.json), but a different
    script looking specifically for P0 records would not."""
    w = World(account_after=OURS)
    w.run(monkeypatch)
    names = [p.name for p in w.runs_dir.glob("crm-p0-*.json")]
    assert len(names) == 1


def test_this_runs_driver_blocks_a_subsequent_call(monkeypatch):
    """A crm-p0-*.json written by this run must also be found by
    _drivers_used_by_earlier_runs (glob crm-*.json), so a second run with the
    same driver fails the earlier-run check."""
    w = World(account_after=OURS)
    w.run(monkeypatch)
    used = lv._drivers_used_by_earlier_runs(w.runs_dir)
    assert DRIVER in used


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def test_signal_during_cleanup_is_held_until_cleanup_finishes():
    import os
    import signal
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    done = []
    with pytest.raises(SystemExit) as ei:
        with lv._signals_held():
            os.kill(os.getpid(), signal.SIGTERM)
            done.append("ran after signal")
    assert done == ["ran after signal"]
    assert ei.value.code == 143


# ---------------------------------------------------------------------------
# Read-back of the chat stop
# ---------------------------------------------------------------------------

def _all_pass_world() -> World:
    from crm import contact_arn, profile_arn  # noqa: PLC0415
    w = World(account_after=OURS, case_customer_id=profile_arn(CRM, "p-1"))
    w.cc.search_related_items.return_value = {"relatedItems": [{
        "relatedItemId": "ri-1", "content": {"contact": {"contactArn": contact_arn(CRM, "chat-1")}}}]}
    # After cleanup the case, profile and fixture are gone. SearchCases still lists the
    # deleted case (it lags a DeleteCase, as live on 2026-11-04); GetCase says it's gone.
    w.cc.search_cases.return_value = {"cases": [{"caseId": "case-1"}]}
    _case_get_world(w)
    w.cp.list_profile_objects.return_value = {"Items": [
        {"ProfileObjectUniqueKey": "u-1", "Object": json.dumps({"vin": VIN, "assetKey": f"{ACCT}#{VIN}"})}]}
    w.connect.describe_contact.return_value = {"Contact": {"DisconnectTimestamp": "2026-11-03T23:00:00Z"}}
    search = w.cp.search_profiles.side_effect
    w.cp.search_profiles.side_effect = lambda **kw: (
        {"Items": []} if kw.get("KeyName") == "_profileId" and w.cp.delete_profile.called else search(**kw))
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}])
    _log_events(w, callback_ms=600)
    return w


def _client_error(code: str, op: str = "GetCase"):
    from botocore.exceptions import ClientError  # noqa: PLC0415
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _case_get_world(w: World, after_delete: str = "gone") -> None:
    """GetCase answers the customer field until the case is deleted; after the delete,
    `after_delete` is "gone" (ResourceNotFoundException), "present" or an error code."""
    present = w.cc.get_case.return_value

    def _get(**kw):
        if not w.cc.delete_case.called or after_delete == "present":
            return present
        raise _client_error("ResourceNotFoundException" if after_delete == "gone" else after_delete)

    w.cc.get_case.side_effect = _get


def _call_world(w: World, calls: list[dict], pages: int = 1, live: bool = False) -> None:
    """VOICE contacts search_contacts returns (split over `pages` pages), each with its
    escalationId attribute; describe_contact says whether the call is still live."""
    contact_attrs = w.connect.get_contact_attributes.return_value["Attributes"]
    per = max(1, -(-len(calls) // pages))
    chunks = [calls[i:i + per] for i in range(0, len(calls), per)] or [[]]

    def _search(**kw):
        i = int(kw.get("NextToken") or 0)
        out = {"Contacts": [{"Id": c["Id"]} for c in chunks[i]]}
        if i + 1 < len(chunks):
            out["NextToken"] = str(i + 1)
        return out

    by_id = {c["Id"]: c for c in calls}
    w.connect.search_contacts.side_effect = _search
    w.connect.get_contact_attributes.side_effect = lambda **kw: {"Attributes": (
        {"escalationId": by_id[kw["InitialContactId"]]["escalationId"]}
        if kw["InitialContactId"] in by_id else contact_attrs)}
    w.connect.describe_contact.side_effect = lambda **kw: {"Contact": (
        {"Id": kw["ContactId"]} if kw["ContactId"] in by_id and live
        and kw["ContactId"] not in _stopped(w)
        else {"Id": kw["ContactId"], "DisconnectTimestamp": "2026-11-03T23:00:00Z"})}


def _log_events(w: World, callback_ms: int | None, init_ms: int | None = None) -> None:
    events = [{"timestamp": 1_000_000, "message": "START RequestId: req-0001 Version: $LATEST"}]
    if callback_ms is not None:
        events.append({"timestamp": 1_000_000 + callback_ms,
                       "message": "req-0001\tcallback: initiated outbound call"})
    events.append({"timestamp": 1_000_000 + (callback_ms or 0) + 400,
                   "message": "req-0001\tescalate: contact=chat-1 callback_initiated=True"})
    w.logs.filter_log_events.return_value = {"events": events}
    if init_ms is not None:
        tail = ("START RequestId: req-0001 Version: $LATEST\n"
                "escalate: contact=chat-1 severity=P0 callback_initiated=True\n"
                f"REPORT RequestId: req-0001\tDuration: 3000.00 ms\tInit Duration: {init_ms}.50 ms\n")
        import base64
        payload = w.lam.invoke.return_value["Payload"]
        w.lam.invoke.return_value = {"Payload": payload,
                                     "LogResult": base64.b64encode(tail.encode()).decode()}


def test_a_clean_run_exits_0(monkeypatch):
    """Positive control for the read-backs below."""
    w = _all_pass_world()
    assert w.run(monkeypatch) == 0


def test_a_chat_still_open_after_stop_fails_the_run(monkeypatch, capsys):
    """Mutant: no read-back of the chat stop."""
    w = _all_pass_world()
    w.connect.describe_contact.side_effect = lambda **kw: {"Contact": (
        {} if kw["ContactId"] == "chat-1"
        else {"Id": kw["ContactId"], "DisconnectTimestamp": "2026-11-03T23:00:00Z"})}
    assert w.run(monkeypatch) == 1
    assert "CLEANUP FAIL: chat chat-1 (not ended)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Review cycle 1: the outbound call's cleanup, the timing check, two refusals
# ---------------------------------------------------------------------------

def _stopped(w: World) -> list[str]:
    return [c.kwargs["ContactId"] for c in w.connect.stop_contact.call_args_list]


def test_only_our_live_call_is_stopped(monkeypatch):
    """Mutant: stop any VOICE contact in the window (another session's or an agent's)."""
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-theirs", "escalationId": "esc-other"},
                    {"Id": "call-ours", "escalationId": "esc-001"}], live=True)
    assert w.run(monkeypatch) == 0
    assert "call-ours" in _stopped(w)
    assert "call-theirs" not in _stopped(w)


def test_an_ended_call_is_not_stopped(monkeypatch):
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}], live=False)
    assert w.run(monkeypatch) == 0
    assert "call-ours" not in _stopped(w)


def test_our_call_on_a_later_page_is_found(monkeypatch):
    """Mutant: read only the first search_contacts page."""
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-a", "escalationId": "esc-a"}, {"Id": "call-b", "escalationId": "esc-b"},
                    {"Id": "call-ours", "escalationId": "esc-001"}], pages=3, live=True)
    assert w.run(monkeypatch) == 0
    assert _stopped(w).count("call-ours") == 1


def test_an_unfound_call_fails_the_run(monkeypatch, capsys):
    """Mutant: an unfound call is only printed (review c1 W3)."""
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-theirs", "escalationId": "esc-other"}], live=True)
    assert w.run(monkeypatch) == 1
    assert "CLEANUP FAIL: outbound call not found" in capsys.readouterr().out
    assert "call-theirs" not in _stopped(w)


def test_a_chat_that_failed_still_finds_the_call_by_the_response_body(monkeypatch):
    """The 502 body carries p0CallbackEscalationId when the call was placed but the chat
    didn't start. Mutant: read the escalation id only from the chat's attributes."""
    w = _all_pass_world()
    w.lam.invoke.return_value = {
        "Payload": MagicMock(read=lambda: json.dumps({"statusCode": 502, "body": json.dumps({
            "callbackInitiated": True, "p0CallbackEscalationId": "esc-001",
            "error": "Amazon Connect start_chat_contact failed"})}).encode()),
        "LogResult": "",
    }
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}], live=True)
    assert w.run(monkeypatch) == 1  # C1 fails: no chat
    assert "call-ours" in _stopped(w)


@pytest.mark.parametrize("init_ms,callback_ms,verdict", [
    (None, 900, "PASS"),    # warm, under 1020 ms
    (None, 1500, "FAIL"),   # warm, later than the warm baseline
    (700, 2000, "PASS"),    # cold, under 2310 ms
    (700, 2400, "FAIL"),    # cold, later than the cold baseline
    (None, None, "FAIL"),   # no callback line read: not shown, so not passed
])
def test_c5_holds_the_call_to_the_pre_fix_baseline(monkeypatch, capsys, init_ms, callback_ms, verdict):
    """Mutants: no C5; one bar for cold and warm; C5 passing without an offset."""
    w = _all_pass_world()
    _log_events(w, callback_ms=callback_ms, init_ms=init_ms)
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C5") == verdict


def test_a_driver_whose_profile_already_exists_is_refused(monkeypatch):
    """Review c1 suggestion: the run would overwrite and then delete that profile's
    vehicle object. Mutant: reuse the existing profile."""
    w = World(account_before=PRE_EXISTING, account_after=PRE_EXISTING)
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_a_lambda_reading_another_drivers_table_is_refused(monkeypatch):
    """The phone guard must read the table the callback dials from."""
    w = World()
    env = w.lam.get_function_configuration.return_value["Environment"]["Variables"]
    env["CMS_DRIVERS_TABLE"] = "cms-other-storage-drivers"
    assert w.run(monkeypatch) == 1
    w.lam.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Review cycle 2
# ---------------------------------------------------------------------------

def _record(w: World) -> dict:
    (rec,) = w.runs_dir.glob("crm-p0-*.json")
    return json.loads(rec.read_text())


@pytest.mark.parametrize("init_ms", [None, 700])
def test_the_run_record_says_cold_or_warm_and_each_check(monkeypatch, init_ms):
    """Review c2 Warning: the record is the durable evidence; it must show whether the
    run was the cold start and what each check said. Mutants: no coldStart /
    initDurationMs / checks / cleanupFail in the record."""
    w = _all_pass_world()
    _log_events(w, callback_ms=600, init_ms=init_ms)
    rc = w.run(monkeypatch)
    rec = _record(w)
    assert rec["coldStart"] is (init_ms is not None)
    assert rec["initDurationMs"] == init_ms
    assert rec["checks"] == {"C1": "PASS", "C2": "PASS", "C5": "PASS", "C3": "PASS", "C6": "PASS",
                             "C7": "PASS", "C4a": "PASS", "C4b": "PASS"}
    assert rec["cleanupFail"] is False and rc == 0


def test_the_record_carries_a_cleanup_failure(monkeypatch):
    w = _all_pass_world()
    _call_world(w, [], live=True)
    assert w.run(monkeypatch) == 1
    assert _record(w)["cleanupFail"] is True


def test_placed_false_still_stops_a_live_call_with_our_escalation_id(monkeypatch):
    """Review c3 W1: the handler stamps p0CallbackPlaced=false after a read timeout too,
    when Connect may have placed the call. Mutant: skip the search on placed=false."""
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}], live=True)
    attrs = {"p0CallbackPlaced": "false", "p0CallbackEscalationId": "esc-001", "customerProfileId": "p-1",
             "profileSearchKey": "_profileId", "profileSearchValue": "p-1"}
    calls = w.connect.get_contact_attributes.side_effect
    w.connect.get_contact_attributes.side_effect = lambda **kw: (
        {"Attributes": attrs} if kw["InitialContactId"] == "chat-1" else calls(**kw))
    w.run(monkeypatch)
    assert "call-ours" in _stopped(w)
    assert _record(w)["cleanupFail"] is False


def test_placed_false_and_no_call_found_is_a_note_not_a_failure(monkeypatch, capsys):
    w = _all_pass_world()
    _call_world(w, [], live=True)
    attrs = {"p0CallbackPlaced": "false", "p0CallbackEscalationId": "esc-001", "customerProfileId": "p-1",
             "profileSearchKey": "_profileId", "profileSearchValue": "p-1"}
    w.connect.get_contact_attributes.side_effect = lambda **kw: {"Attributes": attrs}
    w.run(monkeypatch)
    assert "reported none placed" in capsys.readouterr().out
    assert _record(w)["cleanupFail"] is False


def test_no_call_attempted_means_no_search_and_no_cleanup_failure(monkeypatch, capsys):
    """The chat carries no p0CallbackPlaced at all: the handler never tried a call."""
    w = _all_pass_world()
    w.connect.get_contact_attributes.side_effect = lambda **kw: {"Attributes": {
        "customerProfileId": "p-1", "profileSearchKey": "_profileId", "profileSearchValue": "p-1"}}
    w.run(monkeypatch)
    w.connect.search_contacts.assert_not_called()
    assert "attempted no call" in capsys.readouterr().out
    assert _record(w)["cleanupFail"] is False


def test_the_record_shows_a_failed_check(monkeypatch):
    """Review c3 W2. Mutant: record PASS for every check."""
    w = _all_pass_world()
    _log_events(w, callback_ms=1500)  # warm, over the 1020 ms bar
    assert w.run(monkeypatch) == 1
    checks = _record(w)["checks"]
    assert checks["C5"] == "FAIL"
    assert all(v == "PASS" for k, v in checks.items() if k != "C5")


def test_an_aborted_run_records_cold_start_as_unknown(monkeypatch):
    w = _all_pass_world()
    w.lam.invoke.side_effect = RuntimeError("invoke failed")
    with pytest.raises(RuntimeError):
        w.run(monkeypatch)
    assert _record(w)["coldStart"] is None


def test_a_call_still_live_after_a_failed_stop_fails_the_run(monkeypatch, capsys):
    """Review c2 suggestion: a call left live sits in the callback queue."""
    w = _all_pass_world()
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}], live=True)
    w.connect.stop_contact.side_effect = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    w.connect.describe_contact.side_effect = lambda **kw: {"Contact": (
        {"Id": "call-ours"} if kw["ContactId"] == "call-ours"
        else {"Id": kw["ContactId"], "DisconnectTimestamp": "2026-11-03T23:00:00Z"})}
    assert w.run(monkeypatch) == 1
    assert "CLEANUP FAIL: call call-ours (not ended)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Issue 2026-11-04-escalate-crm-driver-row-unread: C6/C7 and the case read-back
# ---------------------------------------------------------------------------

def test_a_deleted_case_the_search_still_lists_is_not_a_cleanup_failure(monkeypatch, capsys):
    """Run crm-p0-20261104T134715Z-b6c4cf: SearchCases still listed the deleted case and
    the run reported CLEANUP FAIL (rc 1). Mutant: read the case back by SearchCases."""
    w = _all_pass_world()
    assert w.run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "CLEANUP FAIL" not in out
    assert _record(w)["cleanupFail"] is False
    after = [c for c in w.cc.method_calls if c[0] in ("delete_case", "get_case")]
    i = [n for n, *_ in after].index("delete_case")
    assert any(n == "get_case" and kw.get("caseId") == "case-1" for n, _, kw in after[i + 1:])


@pytest.mark.parametrize("after_delete,wording", [
    ("present", "CLEANUP FAIL: case case-1 still present after delete"),
    ("AccessDeniedException", "CLEANUP FAIL: case case-1 read-back raised ClientError"),
])
def test_a_case_not_shown_gone_fails_the_run(monkeypatch, capsys, after_delete, wording):
    """Mutants: _case_gone treats any error as gone; returns True without a lookup;
    stops polling before the case is gone and calls it gone."""
    w = _all_pass_world()
    _case_get_world(w, after_delete=after_delete)
    assert w.run(monkeypatch) == 1
    assert wording in capsys.readouterr().out
    assert _record(w)["cleanupFail"] is True


def test_the_case_read_back_polls_until_get_case_says_gone(monkeypatch, capsys):
    """GetCase may lag too: present twice after the delete, then gone, is a clean run."""
    w = _all_pass_world()
    present = w.cc.get_case.side_effect
    seen = {"n": 0}

    def _get(**kw):
        if w.cc.delete_case.called:
            seen["n"] += 1
            if seen["n"] <= 2:
                return {"fields": []}
        return present(**kw)

    w.cc.get_case.side_effect = _get
    assert w.run(monkeypatch) == 0
    assert seen["n"] == 3


@pytest.mark.parametrize("field,value", [
    ("FirstName", None), ("FirstName", "Test"), ("LastName", None), ("LastName", "Driver"),
    ("PhoneNumber", None), ("PhoneNumber", "+15555550100"),
])
def test_c6_fails_when_the_profile_lacks_the_drivers_name_or_phone(monkeypatch, capsys, field, value):
    """The live symptom: a profile created without the row's name and phone. Mutants:
    C6 skips one field; C6 passes on any profile read."""
    w = _all_pass_world()
    prof = dict(OURS[0])
    if value is None:
        prof.pop(field)
    else:
        prof[field] = value
    search = w.cp.search_profiles.side_effect
    w.cp.search_profiles.side_effect = lambda **kw: (
        {"Items": [prof]} if kw.get("KeyName") == "_profileId" and not w.cp.delete_profile.called
        else search(**kw))
    assert w.run(monkeypatch) == 1
    out = capsys.readouterr().out
    assert _verdicts_from(out).get("C6") == "FAIL"
    assert all(v == "PASS" for k, v in _verdicts_from(out).items() if k != "C6")


def test_c6_never_prints_the_names_or_the_phone(monkeypatch, capsys):
    w = _all_pass_world()
    assert w.run(monkeypatch) == 0
    out = capsys.readouterr().out
    assert _verdicts_from(out).get("C6") == "PASS"
    assert FIRST not in out and LAST not in out and PHONE not in out


def test_c6_fails_when_no_profile_can_be_read(monkeypatch, capsys):
    w = _all_pass_world()
    search = w.cp.search_profiles.side_effect
    w.cp.search_profiles.side_effect = lambda **kw: (
        {"Items": []} if kw.get("KeyName") == "_profileId" else search(**kw))
    w.run(monkeypatch)
    assert _verdicts(capsys).get("C6") == "FAIL"


@pytest.mark.parametrize("items", [
    [],
    [{"ProfileObjectUniqueKey": "u-2", "Object": json.dumps({"vin": "1HGBH41JXMN000000"})}],
])
def test_c7_fails_without_a_vehicle_object_for_the_vin(monkeypatch, capsys, items):
    """The live symptom: vehicle_object=False. Mutant: C7 always passes."""
    w = _all_pass_world()
    w.cp.list_profile_objects.return_value = {"Items": items}
    assert w.run(monkeypatch) == 1
    assert _verdicts(capsys).get("C7") == "FAIL"


def test_c2_fails_on_a_driver_lookup_failure_line(monkeypatch, capsys):
    """The live log line of the regression. Mutant: C2 ignores it."""
    import base64
    w = _all_pass_world()
    tail = ("START RequestId: req-0001 Version: $LATEST\n"
            "req-0001\tWARNING\tescalate-crm: driver lookup failed (ClientError)\n"
            "req-0001\tINFO\tescalate: contact=chat-1 severity=P0 callback_initiated=True\n")
    payload = w.lam.invoke.return_value["Payload"]
    w.lam.invoke.return_value = {"Payload": payload,
                                 "LogResult": base64.b64encode(tail.encode()).decode()}
    assert w.run(monkeypatch) == 1
    assert _verdicts(capsys).get("C2") == "FAIL"


def test_a_driver_row_without_names_refuses_before_any_write(monkeypatch, capsys):
    """Without names on the row C6 can't tell a read row from an unread one. Mutant:
    drop the refusal."""
    w = World()
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": DRIVER, "assignedVehicleId": VEHICLE, "phone": PHONE}}
    assert w.run(monkeypatch) == 1
    assert "no firstName or name" in capsys.readouterr().out
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_a_row_with_only_a_full_name_expects_it_split(monkeypatch, capsys):
    w = _all_pass_world()
    w.tables["cms-staging-storage-drivers"].get_item.return_value = {
        "Item": {"driverId": DRIVER, "assignedVehicleId": VEHICLE, "phone": PHONE,
                 "name": f"{FIRST} {LAST}"}}
    assert w.run(monkeypatch) == 0
    assert _verdicts(capsys).get("C6") == "PASS"


def _verdicts_from(out: str) -> dict[str, str]:
    v: dict[str, str] = {}
    for line in out.splitlines():
        for word in ("PASS", "FAIL"):
            if line.startswith(f"{word}: C"):
                v[line.split(":", 2)[1].strip().split()[0]] = word
    return v


# ---------------------------------------------------------------------------
# Issue 2026-11-04-escalate-crm-nameless-profile: --nameless-profile drives the
# found-by-account naming path (the field-video take's shape: a nameless profile the
# kit adopted before the escalation runs).
# ---------------------------------------------------------------------------

NAMELESS = {"ProfileId": "p-nl", "AccountNumber": ACCT, "PhoneNumber": PHONE}
_VEH_ITEMS = {"Items": [{"ProfileObjectUniqueKey": "u-1",
                         "Object": json.dumps({"vin": VIN, "assetKey": f"{ACCT}#{VIN}"})}]}


def _nameless_world(before: list | None = None, after: list | None = None,
                    chat_profile: str = "p-nl", vehicle_before: bool = False) -> World:
    from crm import contact_arn, profile_arn  # noqa: PLC0415
    before = [dict(NAMELESS)] if before is None else before
    after = [{**NAMELESS, "FirstName": FIRST, "LastName": LAST}] if after is None else after
    w = World(account_before=before, account_after=after,
              case_customer_id=profile_arn(CRM, chat_profile))
    w.with_connect_attrs(p0CallbackPlaced="true", p0CallbackEscalationId="esc-001",
                         customerProfileId=chat_profile, profileSearchKey="_profileId",
                         profileSearchValue=chat_profile)
    w.cc.search_related_items.return_value = {"relatedItems": [{
        "relatedItemId": "ri-1", "content": {"contact": {"contactArn": contact_arn(CRM, "chat-1")}}}]}
    _case_get_world(w)
    w.cp.list_profile_objects.side_effect = lambda **kw: (
        _VEH_ITEMS if (w.lam.invoke.called or vehicle_before) else {"Items": []})
    w.connect.describe_contact.return_value = {"Contact": {"DisconnectTimestamp": "2026-11-03T23:00:00Z"}}
    _call_world(w, [{"Id": "call-ours", "escalationId": "esc-001"}])
    _log_events(w, callback_ms=600)
    return w


def test_a_nameless_profile_run_passes_c6_c8_and_keeps_the_profile(monkeypatch, capsys):
    """Mutant: the flag doesn't lift the pre-existing-profile refusal. Review c1 S2: the
    run deletes the vehicle object it wrote from the profile it keeps."""
    w = _nameless_world()
    rc = w.run(monkeypatch, "--nameless-profile", "p-nl")
    v = _verdicts(capsys)
    assert rc == 0, v
    assert v["C6"] == v["C8"] == "PASS"
    w.cp.delete_profile.assert_not_called()
    w.cp.delete_profile_object.assert_called_once()
    assert w.cp.delete_profile_object.call_args.kwargs["ProfileId"] == "p-nl"
    rec = _record(w)
    assert rec["namelessProfile"] == "p-nl" and rec["profileWasNew"] is False
    assert rec["checks"]["C8"] == "PASS"


def test_the_vehicle_object_is_cleaned_off_the_nameless_profile_when_the_account_read_fails(monkeypatch):
    """Review c1 S3: the post-run `_account` search raising left the run's vehicle object
    on the kept profile. Mutant: drop the nameless fallback for the cleanup's profile id."""
    w = _nameless_world()
    search = w.cp.search_profiles.side_effect
    w.cp.search_profiles.side_effect = lambda **kw: (
        (_ for _ in ()).throw(RuntimeError("boom")) if kw.get("KeyName") == "_account" and w.lam.invoke.called
        else search(**kw))
    assert w.run(monkeypatch, "--nameless-profile", "p-nl") == 1
    w.cp.delete_profile.assert_not_called()
    assert w.cp.delete_profile_object.call_args.kwargs["ProfileId"] == "p-nl"


def test_c8_fails_when_the_chat_names_another_profile(monkeypatch, capsys):
    """Review c1 S5: C8 stands on its own. Mutant: C8 without the chat's customerProfileId."""
    w = _nameless_world(chat_profile="p-other")
    assert w.run(monkeypatch, "--nameless-profile", "p-nl") == 1
    assert _verdicts(capsys)["C8"] == "FAIL"


def test_c6_fails_when_the_nameless_profile_was_not_named(monkeypatch, capsys):
    """The property under test: the handler named the found profile."""
    w = _nameless_world(after=[dict(NAMELESS)])
    assert w.run(monkeypatch, "--nameless-profile", "p-nl") == 1
    v = _verdicts(capsys)
    assert v["C6"] == "FAIL" and v["C8"] == "PASS"


@pytest.mark.parametrize("after,chat_profile", [
    ([{**NAMELESS, "FirstName": FIRST, "LastName": LAST},
      {"ProfileId": "p-a", "AccountNumber": ACCT, "FirstName": FIRST, "LastName": LAST, "PhoneNumber": PHONE,
       "Attributes": {"source": "avx-escalate"}}], "p-a"),
    ([{**NAMELESS, "FirstName": FIRST, "LastName": LAST},
      {"ProfileId": "p-z", "AccountNumber": ACCT, "PhoneNumber": PHONE}], "p-nl"),
], ids=["handler-made-another", "second-profile-on-account"])
def test_c8_fails_unless_the_chat_named_the_only_profile_on_the_account(monkeypatch, capsys, after, chat_profile):
    """Mutants: C8 always passes; C8 ignores other profiles on the account. Review c2 S7:
    the run's vehicle object is cleaned off the profile the post-run read found (the
    nameless fallback applies only when it found none)."""
    w = _nameless_world(after=after, chat_profile=chat_profile)
    assert w.run(monkeypatch, "--nameless-profile", "p-nl") == 1
    assert _verdicts(capsys)["C8"] == "FAIL"
    found = min(p["ProfileId"] for p in after)
    assert w.cp.delete_profile_object.call_args.kwargs["ProfileId"] == found


@pytest.mark.parametrize("before,flag,phone_profiles,vehicle_before", [
    ([{**NAMELESS, "FirstName": "Pre"}], "p-nl", (), False),
    ([{**NAMELESS, "LastName": "Pre"}], "p-nl", (), False),
    ([{**NAMELESS, "Attributes": {"source": "avx-escalate"}}], "p-nl", (), False),
    ([{**NAMELESS, "PhoneNumber": "+15555550188"}], "p-nl", (), False),
    ([{**NAMELESS, "AccountNumber": "meridian:DRV-OTHER"}], "p-nl", (), False),
    ([dict(NAMELESS), {"ProfileId": "p-z", "AccountNumber": ACCT}], "p-nl", (), False),
    ([dict(NAMELESS)], "p-other", (), False),
    ([], "p-nl", (), False),
    ([dict(NAMELESS)], "p-nl", ({"ProfileId": "ctr-1", "PhoneNumber": PHONE},), False),
    ([dict(NAMELESS)], "p-nl", (), True),
], ids=["first-name", "last-name", "attributes", "other-phone", "other-account", "two-profiles",
        "other-id", "no-profile", "unclaimed-phone-match", "vehicle-object"])
def test_a_nameless_profile_that_does_not_fit_refuses_before_any_write(
        monkeypatch, capsys, before, flag, phone_profiles, vehicle_before):
    """Mutants: each condition of _nameless_profile_problem dropped; the flag bypassing
    the whole check."""
    w = _nameless_world(before=before, vehicle_before=vehicle_before)
    w.cp.search_profiles.side_effect = (lambda orig: lambda **kw: (
        {"Items": list(phone_profiles)} if kw.get("KeyName") == "_phone" else orig(**kw)))(
        w.cp.search_profiles.side_effect)
    assert w.run(monkeypatch, "--nameless-profile", flag) == 1
    assert "PREFLIGHT FAILED: --nameless-profile" in capsys.readouterr().out
    w.lam.invoke.assert_not_called()
    w.tables["vsa-staging-triage-decisions"].put_item.assert_not_called()


def test_the_refusal_never_prints_the_phone(monkeypatch, capsys):
    w = _nameless_world(before=[{**NAMELESS, "PhoneNumber": "+15555550188"}])
    assert w.run(monkeypatch, "--nameless-profile", "p-nl") == 1
    out = capsys.readouterr().out
    assert PHONE not in out and "+15555550188" not in out


def test_the_dry_run_plan_names_c8_with_the_flag(monkeypatch, capsys):
    w = _nameless_world()
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    clients = {"lambda": w.lam, "customer-profiles": w.cp, "connectcases": w.cc,
               "connect": w.connect, "connectparticipant": MagicMock(), "logs": w.logs}
    argv = ["x", "--driver-id", DRIVER, "--vehicle-id", VEHICLE, "--dry-run", "--nameless-profile", "p-nl"]
    with patch("boto3.client", side_effect=lambda svc, **kw: clients[svc]), \
         patch("boto3.resource", return_value=w.ddb), \
         patch.object(lv, "_RUNS_DIR", w.runs_dir), patch.object(sys, "argv", argv):
        assert lv.main() == 0
    out = capsys.readouterr().out
    assert "[C8]" in out and "[OK] Nameless profile 'p-nl'" in out
    w.lam.invoke.assert_not_called()
