"""Customer Profiles + Cases on /escalate (spec 2026-11-02-avx-connect-profiles-cases).

The human handoff is the safety property: nothing here may stop, fail or delay
the chat or the P0 callback. Mutants each test targets are named in its
docstring and recorded in the spec's decisions.md.

Run:
    .venv/bin/python3 -m pytest lambdas/api-escalate/tests/test_crm.py -q
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent
for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("TENANT_CONFIG_TABLE", "test-tenant-config")
os.environ.setdefault("CMS_STAGE", "test")
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("LIVE_STATE_FN_NAME", "vsa-test-live-state-fn")


def _load_handler():
    mod_name = "escalate_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _LAMBDA_ROOT / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_handler()
crm = h.crm

INSTANCE_ARN = "arn:aws:connect:us-east-1:111122223333:instance/inst-0001"
FIELD_IDS = {
    "severity": "f-sev",
    "vin": "f-vin",
    "vehicleId": "f-veh",
    "faultCodes": "f-faults",
    "escalationKey": "f-key",
    "triageSessionId": "f-tsid",
}
CRM_CONFIG = {
    "instanceArn": INSTANCE_ARN,
    "profilesDomain": "amazon-connect-test",
    "objectType": "CvxVehicle",
    "casesDomainId": "dom-0001",
    "casesTemplateId": "tpl-0001",
    "fieldIds": FIELD_IDS,
}
VIN = "1HGBH41JXMN000024"
PHONE = "+15555550142"
DRIVER = "DRV-TEST-0042"
VEHICLE = "VEH-TEST-0042"
TENANT = "meridian"


def _client_error(op: str, code: str = "InternalServerException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, op)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


_CP_MODEL = botocore.session.get_session().get_service_model("customer-profiles")


def _cp_request(op: str, params: dict) -> None:
    """Refuse a request Customer Profiles would refuse: botocore's own validation against
    the real service model (unknown or misspelt parameter, wrong type, None, too long)."""
    validate_parameters(params, _CP_MODEL.operation_model(op).input_shape)


class FakeProfiles:
    """In-memory Customer Profiles domain. `per_call_s` advances the fake clock per call.

    Requests are checked against the real service model, and UpdateProfile behaves like
    the service: an omitted field is kept, a field sent as "" is removed (API reference,
    UpdateProfile). `updates` records each UpdateProfile's fields as sent. Profiles in
    `account_index_drops` don't match `_account` (live quirk: the index drops a profile
    once its vehicle object is deleted)."""

    def __init__(self, clock: FakeClock, profiles: list[dict] | None = None, per_call_s: float = 0.0):
        self.clock = clock
        self.per_call_s = per_call_s
        self.profiles = {p["ProfileId"]: dict(p) for p in (profiles or [])}
        self.objects: dict[str, dict] = {}
        self.calls: list[str] = []
        self.updates: list[dict] = []
        self.fail: set[str] = set()
        self.account_index_drops: set[str] = set()

    def _op(self, name: str) -> None:
        self.calls.append(name)
        self.clock.t += self.per_call_s
        if name in self.fail or "*" in self.fail:
            raise _client_error(name)

    def search_profiles(self, DomainName, KeyName, Values, MaxResults=20):
        self._op("search_profiles")
        _cp_request("SearchProfiles", {"DomainName": DomainName, "KeyName": KeyName, "Values": Values,
                                       "MaxResults": MaxResults})
        attr = {"_account": "AccountNumber", "_phone": "PhoneNumber", "_profileId": "ProfileId"}[KeyName]
        return {"Items": [dict(p) for pid, p in self.profiles.items() if p.get(attr) in Values
                          and not (KeyName == "_account" and pid in self.account_index_drops)]}

    def update_profile(self, DomainName, ProfileId, **kw):
        self._op("update_profile")
        _cp_request("UpdateProfile", {"DomainName": DomainName, "ProfileId": ProfileId, **kw})
        self.updates.append(dict(kw))
        prof = self.profiles[ProfileId]
        for k, v in kw.items():
            if v == "":
                prof.pop(k, None)
            else:
                prof[k] = v
        return {"ProfileId": ProfileId}

    def create_profile(self, DomainName, **kw):
        self._op("create_profile")
        _cp_request("CreateProfile", {"DomainName": DomainName, **kw})
        pid = f"created-{len(self.profiles) + 1:03d}"
        self.profiles[pid] = {"ProfileId": pid, **kw}
        return {"ProfileId": pid}

    def put_profile_object(self, DomainName, ObjectTypeName, Object):
        self._op("put_profile_object")
        obj = json.loads(Object)
        self.objects[obj["assetKey"]] = obj
        return {"ProfileObjectUniqueKey": "u-" + obj["assetKey"]}


class FakeCases:
    """In-memory Cases domain; `search_lag` hides cases from search (index lag)."""

    def __init__(self, clock: FakeClock, per_call_s: float = 0.0, search_lag: bool = False):
        self.clock = clock
        self.per_call_s = per_call_s
        self.search_lag = search_lag
        self.cases: dict[str, dict[str, str]] = {}
        self.tokens: dict[str, str] = {}
        self.related: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def _op(self, name: str) -> None:
        self.calls.append(name)
        self.clock.t += self.per_call_s
        if name in self.fail or "*" in self.fail:
            raise _client_error(name)

    def search_cases(self, domainId, maxResults, filter, fields):
        self._op("search_cases")
        if self.search_lag:
            return {"cases": []}
        eq = filter["field"]["equalTo"]
        hits = [cid for cid, f in self.cases.items() if f.get(eq["id"]) == eq["value"]["stringValue"]]
        return {"cases": [{"caseId": c} for c in hits]}

    def create_case(self, domainId, templateId, fields, clientToken=None):
        self._op("create_case")
        if clientToken and clientToken in self.tokens:
            return {"caseId": self.tokens[clientToken]}
        cid = f"case-{len(self.cases) + 1:03d}"
        self.cases[cid] = {f["id"]: f["value"]["stringValue"] for f in fields}
        if clientToken:
            self.tokens[clientToken] = cid
        return {"caseId": cid}

    def create_related_item(self, domainId, caseId, type, content):
        self._op("create_related_item")
        assert type == "Contact"
        self.related.append((caseId, content["contact"]["contactArn"]))
        return {"relatedItemId": f"ri-{len(self.related)}"}


def _tenant_cfg(instance_arn: str = INSTANCE_ARN, callback: bool = False) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "instanceArn": instance_arn,
        "instanceId": instance_arn.split("/")[-1],
        "instanceRegion": "us-east-1",
        "contactFlowId": "chat-flow",
        "queueArn": f"{instance_arn}/queue/q1",
        "autoRequestRoadsideOnP0": False,
        "autoOpenHumanChatOnP0": False,
        "callbackFlowId": "cb-flow" if callback else None,
        "callbackQueueId": "q-cb" if callback else None,
        "callbackSourcePhoneNumber": None,
    }
    return cfg


def _event(body: dict, claims: dict | None = None) -> dict:
    c = {"custom:tenantId": TENANT, "custom:driverId": DRIVER}
    if claims is not None:
        c = claims
    return {"requestContext": {"authorizer": {"claims": c}}, "body": json.dumps(body)}


def _body(**extra) -> dict:
    b = {
        "severity": "P2",
        "vin": VIN,
        "vehicleId": VEHICLE,
        "reason": "check engine light",
        "summary": "Driver reports a steady check-engine lamp.",
        "triageSessionId": "sess-0042",
        "driverId": DRIVER,
        "displayName": "Test Driver",
    }
    b.update(extra)
    return b


DRIVER_ROW = {"driverId": DRIVER, "assignedVehicleId": VEHICLE, "phone": PHONE, "name": "Test Driver"}
VEHICLE_ROW = {"vehicleId": VEHICLE, "vin": VIN, "year": 2024, "make": "Meridian", "model": "Azimuth"}
TRIAGE_ITEM = {"sessionId": "sess-0042", "tenantId": TENANT, "vin": VIN,
               "inputsSnapshot": {"activeDtcs": ["P0420", "P0171"]}}


class World:
    """One test world: fake clock, fake domains, a Connect mock with fresh contact ids."""

    def __init__(self, *, profiles=None, per_call_s=0.0, search_lag=False, driver_row=DRIVER_ROW,
                 vehicle_row=VEHICLE_ROW, triage_item=TRIAGE_ITEM, tenant_cfg=None, env=None,
                 chat_advances_clock_to: float | None = None):
        self.clock = FakeClock()
        self.fp = FakeProfiles(self.clock, profiles, per_call_s)
        self.fc = FakeCases(self.clock, per_call_s, search_lag)
        self.connect = MagicMock()
        self._n = 0

        def _start_chat(**kw):
            self._n += 1
            if chat_advances_clock_to is not None:
                self.clock.t = chat_advances_clock_to
            return {"ContactId": f"chat-{self._n:03d}", "ParticipantId": "p", "ParticipantToken": "tok"}

        self.connect.start_chat_contact.side_effect = _start_chat
        self.connect.start_outbound_voice_contact.return_value = {"ContactId": "call-001"}
        self.driver_row = driver_row
        self.vehicle_row = vehicle_row
        self.triage_item = triage_item
        self.tenant_cfg = tenant_cfg or _tenant_cfg()
        self.env = {"CONNECT_CRM_CONFIG": json.dumps(CRM_CONFIG), "CONNECT_CRM_TENANTS": "meridian"} if env is None else env

    def run(self, body: dict | None = None, claims: dict | None = None, extra_patches=(), after=None,
            real_driver_read: bool = False):
        """`after()` runs once the handler has returned, before the patches stop (lets a
        test release and wait for a profile step the handler stopped waiting for).
        `real_driver_read`: leave h._crm_driver_row unpatched, so the driver row comes
        through the real botocore client (pair it with _dynamo_wire)."""
        body = _body() if body is None else body
        clients = {"customer-profiles": self.fp, "connectcases": self.fc}
        driver_patch = [] if real_driver_read else [
            patch.object(h, "_crm_driver_row", return_value=self.driver_row)]
        patches = [
            patch.dict(os.environ, self.env, clear=False),
            patch.object(crm, "clock", self.clock),
            patch.object(crm, "_client", side_effect=lambda svc, region: clients[svc]),
            patch.object(h, "_load_tenant_connect_config", return_value=self.tenant_cfg),
            patch.object(h, "_get_connect", return_value=self.connect),
            *driver_patch,
            patch.object(h, "_crm_vehicle_row", return_value=self.vehicle_row),
            patch.object(h, "_crm_triage_item", return_value=self.triage_item),
            patch.object(h, "_queue_rsa_action", return_value=None),
            *extra_patches,
        ]
        if not self.env:
            for k in ("CONNECT_CRM_CONFIG", "CONNECT_CRM_TENANTS"):
                patches.append(patch.dict(os.environ, {}, clear=False))
        for p in patches:
            p.start()
        saved = {k: os.environ.pop(k) for k in ("CONNECT_CRM_CONFIG", "CONNECT_CRM_TENANTS")
                 if not self.env and k in os.environ}
        try:
            resp = h.handler(_event(body, claims), None)
            if after is not None:
                after()
            return resp
        finally:
            for p in reversed(patches):
                p.stop()
            os.environ.update(saved)

    def chat_attrs(self, n: int = 0) -> dict:
        return self.connect.start_chat_contact.call_args_list[n].kwargs["Attributes"]


# ---------------------------------------------------------------------------
# Retry: one profile, one case
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retried_escalation_makes_one_profile_and_one_case(self):
        """Mutants: no account search; escalation key with a random part; no case search + no token."""
        w = World(driver_row={**DRIVER_ROW, "phone": None})  # account search is the only reuse path
        r1, r2 = w.run(), w.run()
        assert r1["statusCode"] == r2["statusCode"] == 200
        assert w.fp.calls.count("create_profile") == 1
        assert len(w.fp.profiles) == 1
        assert len(w.fc.cases) == 1
        case_id = next(iter(w.fc.cases))
        assert w.fc.related == [
            (case_id, f"{INSTANCE_ARN}/contact/chat-001"),
            (case_id, f"{INSTANCE_ARN}/contact/chat-002"),
        ]

    def test_retry_inside_search_lag_reuses_the_case_by_client_token(self):
        """Mutant: CreateCase without clientToken (or a token that differs per call)."""
        w = World(search_lag=True)
        w.run()
        w.run()
        assert len(w.fc.cases) == 1
        assert len(w.fc.related) == 2

    def test_retry_reuses_vehicle_object(self):
        w = World()
        w.run()
        w.run()
        assert len(w.fp.objects) == 1
        assert w.fp.calls.count("put_profile_object") == 2

    def test_different_triage_session_is_a_different_case(self):
        w = World()
        w.run()
        w.run(_body(triageSessionId="sess-0043"))
        assert len(w.fc.cases) == 2
        assert len(w.fp.profiles) == 1


# ---------------------------------------------------------------------------
# Reuse of an existing profile (the Connect-created one matching the phone)
# ---------------------------------------------------------------------------


class TestReuse:
    def test_unclaimed_phone_profile_is_adopted_not_duplicated(self):
        """Mutant: skip the phone search → a duplicate profile is created."""
        w = World(profiles=[{"ProfileId": "ctr-0001", "PhoneNumber": PHONE}])
        w.run()
        w.run()
        assert "create_profile" not in w.fp.calls
        assert w.fp.profiles["ctr-0001"]["AccountNumber"] == f"{TENANT}:{DRIVER}"
        assert w.chat_attrs(0)["customerProfileId"] == "ctr-0001"
        assert w.chat_attrs(1)["customerProfileId"] == "ctr-0001"
        assert w.fp.calls.count("update_profile") == 1

    def test_phone_profile_claimed_by_another_account_is_not_taken(self):
        """Mutant: adopt any phone match regardless of AccountNumber."""
        w = World(profiles=[{"ProfileId": "ctr-0001", "PhoneNumber": PHONE, "AccountNumber": "other:DRV-9"}])
        w.run()
        assert w.fp.profiles["ctr-0001"]["AccountNumber"] == "other:DRV-9"
        assert "update_profile" not in w.fp.calls
        assert w.fp.calls.count("create_profile") == 1
        assert w.chat_attrs()["customerProfileId"] != "ctr-0001"

    def test_several_unclaimed_matches_pick_the_lowest_id_every_time(self):
        w = World(profiles=[{"ProfileId": "ctr-b", "PhoneNumber": PHONE}, {"ProfileId": "ctr-a", "PhoneNumber": PHONE}])
        w.run()
        assert w.chat_attrs()["customerProfileId"] == "ctr-a"

    def test_adopt_update_failure_still_associates_the_profile(self):
        w = World(profiles=[{"ProfileId": "ctr-0001", "PhoneNumber": PHONE}])
        w.fp.fail = {"update_profile"}
        resp = w.run()
        assert resp["statusCode"] == 200
        assert w.chat_attrs()["customerProfileId"] == "ctr-0001"
        assert "create_profile" not in w.fp.calls


# ---------------------------------------------------------------------------
# Issue 2026-11-04-escalate-crm-nameless-profile: a driver's own profile with no
# name gets the driver row's, whoever created it. Pinned rules:
#   - only a profile with NEITHER a FirstName nor a LastName value is filled; a
#     profile with one of them (any non-empty value) is left exactly as it is,
#     the missing one included;
#   - the names come from the CMS driver row only (firstName/lastName, or its
#     `name` split once), never the request's displayName; a part the row lacks
#     is not sent (UpdateProfile removes a field sent as "");
#   - the update carries only FirstName/LastName (plus AccountNumber when adopting):
#     never the phone or any other field.
# ---------------------------------------------------------------------------

ACCT = f"{TENANT}:{DRIVER}"
OTHER_PHONE = "+15555550199"  # the profile's own phone, different from the row's
NAMED_ROW = {**DRIVER_ROW, "firstName": "Brandy", "lastName": "Okafor"}


def _acct_profile(**extra) -> dict:
    return {"ProfileId": "acct-0001", "AccountNumber": ACCT, "PhoneNumber": OTHER_PHONE, **extra}


def _unclaimed_profile(**extra) -> dict:
    return {"ProfileId": "ctr-0001", "PhoneNumber": PHONE, **extra}


PATHS = ["account", "phone", "adopt"]


def _path_world(path: str, driver_row=NAMED_ROW, **extra) -> tuple[World, str, list]:
    """A world whose escalation returns an existing profile by `path`: found by `_account`;
    found by `_phone` already carrying our account (dropped from the `_account` index);
    adopted by `_phone`. Returns the world, the profile id and the updates a profile that
    must keep its names expects (none, or the adoption's AccountNumber alone)."""
    if path == "account":
        prof = _acct_profile(**extra)
    elif path == "phone":
        prof = _acct_profile(PhoneNumber=PHONE, **extra)
    else:
        prof = _unclaimed_profile(**extra)
    w = World(profiles=[prof], driver_row=driver_row)
    if path == "phone":
        w.fp.account_index_drops = {prof["ProfileId"]}
    return w, prof["ProfileId"], ([{"AccountNumber": ACCT}] if path == "adopt" else [])


class TestNamelessProfileGetsTheDriverRowName:
    def test_a_nameless_profile_found_by_account_gets_the_row_names(self):
        """The take's case (the kit adopts first). Mutant: skip the fill on the account path."""
        w = World(profiles=[_acct_profile()], driver_row=NAMED_ROW)
        assert w.run()["statusCode"] == 200
        assert w.chat_attrs()["customerProfileId"] == "acct-0001"
        assert w.fp.updates == [{"FirstName": "Brandy", "LastName": "Okafor"}]
        p = w.fp.profiles["acct-0001"]
        assert (p["FirstName"], p["LastName"], p["PhoneNumber"], p["AccountNumber"]) == (
            "Brandy", "Okafor", OTHER_PHONE, ACCT)
        assert "create_profile" not in w.fp.calls

    def test_an_adopted_nameless_profile_gets_the_account_and_names_in_one_update(self):
        """Mutant: skip the fill on the adopt path."""
        w = World(profiles=[_unclaimed_profile()], driver_row=NAMED_ROW)
        w.run()
        assert w.fp.updates == [{"AccountNumber": ACCT, "FirstName": "Brandy", "LastName": "Okafor"}]
        assert w.fp.profiles["ctr-0001"]["PhoneNumber"] == PHONE
        assert w.chat_attrs()["customerProfileId"] == "ctr-0001"

    def test_a_profile_found_by_phone_with_our_account_gets_the_names(self):
        """The `_account` index can drop a profile (after its vehicle object is deleted);
        found by phone with our account it is still the driver's. Mutant: skip the fill
        on that path."""
        w = World(profiles=[_acct_profile(PhoneNumber=PHONE)], driver_row=NAMED_ROW)
        w.fp.account_index_drops = {"acct-0001"}
        w.run()
        assert w.fp.updates == [{"FirstName": "Brandy", "LastName": "Okafor"}]
        assert w.chat_attrs()["customerProfileId"] == "acct-0001"

    @pytest.mark.parametrize("names", [
        {"FirstName": "Kept", "LastName": "Name"},
        {"FirstName": "OnlyFirst"},
        {"LastName": "OnlyLast"},
        {"FirstName": " "},
    ], ids=["both", "first-only", "last-only", "whitespace-first"])
    @pytest.mark.parametrize("path", PATHS)
    def test_a_profile_with_any_name_is_never_renamed(self, names, path, caplog):
        """Pinned: one name present means no fill at all, the missing one included.
        Mutants: fill regardless of existing names (overwrite), on any path; fill when
        only one name is missing; fill only the missing part; whitespace counted as no name;
        the adopt log's named= hardcoded (review c2 S6)."""
        caplog.set_level(logging.INFO)
        w, pid, kept_updates = _path_world(path, **names)
        w.run()
        assert all("FirstName" not in u and "LastName" not in u for u in w.fp.updates)
        assert w.fp.updates == kept_updates
        p = w.fp.profiles[pid]
        assert {k: p.get(k) for k in ("FirstName", "LastName")} == {
            "FirstName": names.get("FirstName"), "LastName": names.get("LastName")}
        assert w.chat_attrs()["customerProfileId"] == pid
        if path == "adopt":
            assert f"crm-profile: adopted by=phone profile={pid} named=False" in caplog.text

    @pytest.mark.parametrize("path", PATHS)
    def test_the_names_come_from_the_driver_row_never_the_request(self, path):
        """The request's displayName ("Test Driver") never names a found profile.
        Mutant: pass display_name to the fill."""
        w, pid, kept_updates = _path_world(path, driver_row={k: v for k, v in DRIVER_ROW.items() if k != "name"})
        w.run()
        assert w.fp.updates == kept_updates
        p = w.fp.profiles[pid]
        assert "FirstName" not in p and "LastName" not in p

    @pytest.mark.parametrize("path", ["account", "phone"])
    def test_of_two_nameless_profiles_on_the_account_the_lowest_id_is_named(self, path):
        """Review c1 S1: the pick decides which profile gets named. Mutant: the highest id."""
        phone = PHONE if path == "phone" else OTHER_PHONE
        w = World(profiles=[_acct_profile(ProfileId="acct-0002", PhoneNumber=phone),
                            _acct_profile(ProfileId="acct-0001", PhoneNumber=phone)], driver_row=NAMED_ROW)
        if path == "phone":
            w.fp.account_index_drops = {"acct-0001", "acct-0002"}
        w.run()
        assert w.chat_attrs()["customerProfileId"] == "acct-0001"
        assert w.fp.profiles["acct-0001"].get("FirstName") == "Brandy"
        assert "FirstName" not in w.fp.profiles["acct-0002"]

    def test_a_row_full_name_is_split_once(self):
        w = World(profiles=[_acct_profile()], driver_row={**DRIVER_ROW, "name": "Ana de la Cruz"})
        w.run()
        assert w.fp.updates == [{"FirstName": "Ana", "LastName": "de la Cruz"}]

    def test_a_row_with_only_a_first_name_sends_only_the_first_name(self):
        """A LastName sent as "" would remove one (UpdateProfile); none is sent.
        Mutant: always send both fields."""
        row = {**{k: v for k, v in DRIVER_ROW.items() if k != "name"}, "firstName": "Brandy"}
        w = World(profiles=[_acct_profile()], driver_row=row)
        w.run()
        assert w.fp.updates == [{"FirstName": "Brandy"}]

    def test_names_are_capped_at_the_field_limit(self):
        row = {**DRIVER_ROW, "firstName": "F" * 300, "lastName": "L" * 300}
        w = World(profiles=[_acct_profile()], driver_row=row)
        w.run()
        assert w.fp.updates == [{"FirstName": "F" * 255, "LastName": "L" * 255}]

    def test_a_named_profile_is_not_updated_again(self):
        w = World(profiles=[_acct_profile()], driver_row=NAMED_ROW)
        w.run()
        w.run()
        assert len(w.fp.updates) == 1

    @pytest.mark.parametrize("path", PATHS)
    def test_the_fill_is_a_pre_chat_call_gated_by_the_budget(self, path):
        """The search that finds the profile ends past the cutoff: no update, the chat
        still names the profile. Mutant: no budget check before the fill (any path)."""
        w, pid, _ = _path_world(path)
        if path == "account":
            w.fp.per_call_s = 2.6  # the account search ends at 2.6 s
        else:
            w.fp.per_call_s = 1.3  # account 0→1.3, phone 1.3→2.6
        assert w.run()["statusCode"] == 200
        assert "update_profile" not in w.fp.calls
        assert w.chat_attrs()["customerProfileId"] == pid

    @pytest.mark.parametrize("path", PATHS)
    def test_a_failed_fill_still_names_the_profile_in_the_chat(self, path):
        """Mutant: let the update's error escape (the outer catch drops the profile)."""
        w, pid, _ = _path_world(path)
        w.fp.fail = {"update_profile"}
        assert w.run()["statusCode"] == 200
        assert w.chat_attrs()["customerProfileId"] == pid
        assert "create_profile" not in w.fp.calls

    @pytest.mark.parametrize("path", PATHS)
    def test_an_error_building_the_names_never_costs_the_profile_or_the_adoption(self, path):
        """Mutant: no guard in _name_fill (the outer catch drops the profile)."""
        w, pid, kept_updates = _path_world(path)
        w.run(extra_patches=[patch.object(crm, "_row_name_fields", side_effect=RuntimeError("bad row Okafor"))])
        assert w.chat_attrs()["customerProfileId"] == pid
        assert w.fp.updates == kept_updates

    @pytest.mark.parametrize("failing", [False, True], ids=["ok", "update-fails"])
    @pytest.mark.parametrize("path", PATHS)
    def test_the_fill_is_logged_by_id_never_by_name(self, caplog, path, failing):
        """Review c1 W2. Mutants: the adopt line or the failure warning logs the fields;
        the guard logs the exception text."""
        caplog.set_level(logging.INFO)
        w, pid, _ = _path_world(path)
        if failing:
            w.fp.fail = {"update_profile"}
        w.run()
        text = "\n".join(r.getMessage() for r in caplog.records if "crm" in r.getMessage())
        if not failing:
            assert (f"crm-profile: adopted by=phone profile={pid} named=True" if path == "adopt"
                    else f"crm-profile: named profile={pid}") in text
        else:
            # Security review c1 SS1: a ClientError's text can echo request values; class only.
            assert (f"crm-profile: adopt update failed (ClientError) profile={pid}" if path == "adopt"
                    else f"crm-profile: name update failed (ClientError) profile={pid}") in text
        for secret in ("Brandy", "Okafor", OTHER_PHONE, PHONE):
            assert secret not in text

    def test_an_error_building_the_names_is_logged_by_class_only(self, caplog):
        caplog.set_level(logging.INFO)
        w, _, _ = _path_world("account")
        w.run(extra_patches=[patch.object(crm, "_row_name_fields", side_effect=RuntimeError("bad row Okafor"))])
        text = "\n".join(r.getMessage() for r in caplog.records if "crm" in r.getMessage())
        assert "crm-profile: name fill skipped (RuntimeError)" in text
        assert "Okafor" not in text


# ---------------------------------------------------------------------------
# The safety property: the handoff is never blocked
# ---------------------------------------------------------------------------


class TestHandoffNeverBlocked:
    def _baseline_body(self) -> dict:
        w = World(env={})
        return json.loads(w.run()["body"])

    @pytest.mark.parametrize("failing", [
        {"*"},
        {"search_profiles"},
        {"create_profile"},
        {"put_profile_object"},
    ])
    def test_profiles_failure_never_blocks_the_chat(self, failing):
        """Mutant: drop the except in find_or_create_profile / put_vehicle."""
        w = World()
        w.fp.fail = set(failing)
        resp = w.run()
        assert resp["statusCode"] == 200
        assert w.connect.start_chat_contact.call_count == 1
        body = json.loads(resp["body"])
        assert set(body) == set(self._baseline_body())

    @pytest.mark.parametrize("failing", [{"*"}, {"search_cases"}, {"create_case"}, {"create_related_item"}])
    def test_cases_failure_never_blocks_the_chat(self, failing):
        """Mutant: drop the except in find_or_create_case / link_contact."""
        w = World()
        w.fc.fail = set(failing)
        resp = w.run()
        assert resp["statusCode"] == 200
        assert w.connect.start_chat_contact.call_count == 1

    def test_unexpected_exception_in_after_chat_orchestration_never_fails_the_response(self):
        """Mutant: drop the outer except in _record_crm_after_chat."""
        w = World()
        resp = w.run(extra_patches=[patch.object(crm, "escalation_key", side_effect=RuntimeError("bug"))])
        assert resp["statusCode"] == 200

    def test_unexpected_exception_in_driver_lookup_never_blocks_the_chat(self):
        w = World()
        resp = w.run(extra_patches=[patch.object(crm, "find_or_create_profile", side_effect=RuntimeError("bug"))])
        assert resp["statusCode"] == 200
        assert w.connect.start_chat_contact.call_count == 1

    def test_p0_callback_is_still_placed_before_the_chat_when_crm_fails(self):
        w = World(tenant_cfg=_tenant_cfg(callback=True))
        w.fp.fail = {"*"}
        w.fc.fail = {"*"}
        gates = [
            patch.object(h, "_verified_p0_decision", return_value=True),
            patch.object(h, "_vehicle_matches_vin", return_value=True),
            patch.object(h, "_caller_assigned_driver", return_value=DRIVER),
            patch.object(h, "_resolve_driver_phone", return_value=PHONE),
        ]
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=gates)
        assert resp["statusCode"] == 200
        names = [c[0] for c in w.connect.method_calls]
        assert names.index("start_outbound_voice_contact") < names.index("start_chat_contact")

    def test_off_when_unconfigured_attributes_unchanged(self):
        w_off = World(env={})
        w_off.run()
        assert "customerProfileId" not in w_off.chat_attrs()
        assert w_off.fp.calls == [] and w_off.fc.calls == []


# ---------------------------------------------------------------------------
# The profile opens on its own (prompt 207): profileSearchKey / profileSearchValue
# ---------------------------------------------------------------------------

_SEARCH_KEYS = ("customerProfileId", "profileSearchKey", "profileSearchValue")


class TestProfileAutoOpen:
    """The agent workspace searches Customer Profiles with the contact attributes
    `profileSearchKey` / `profileSearchValue` (AWS: "Automatically populate customer
    profiles"). They must name exactly the profile the Lambda created or reused."""

    def _assert_names_profile(self, w: World, attrs: dict, profile_id: str) -> None:
        assert attrs["customerProfileId"] == profile_id
        assert attrs["profileSearchKey"] == "_profileId"
        assert attrs["profileSearchValue"] == profile_id
        hits = w.fp.search_profiles("amazon-connect-test", attrs["profileSearchKey"],
                                    [attrs["profileSearchValue"]])["Items"]
        assert [p["ProfileId"] for p in hits] == [profile_id]

    def test_created_profile_is_named_by_the_search_attributes(self):
        """Mutants: key `_account`; value = the account number; search keys not set."""
        w = World(driver_row={**DRIVER_ROW, "phone": None})
        w.run()
        (pid,) = w.fp.profiles
        self._assert_names_profile(w, w.chat_attrs(), pid)

    def test_reused_phone_profile_is_named_by_the_search_attributes(self):
        w = World(profiles=[{"ProfileId": "ctr-0001", "PhoneNumber": PHONE},
                            {"ProfileId": "ctr-0002", "PhoneNumber": "+15555550199"}])
        w.run()
        self._assert_names_profile(w, w.chat_attrs(), "ctr-0001")

    def test_retry_names_the_same_profile(self):
        w = World()
        w.run()
        w.run()
        assert w.chat_attrs(0)["profileSearchValue"] == w.chat_attrs(1)["profileSearchValue"]
        assert len(w.fp.profiles) == 1

    @pytest.mark.parametrize("case", ["off", "no_claim", "budget", "profiles_down", "body_mismatch"])
    def test_no_profile_means_no_search_attributes(self, case):
        """Mutant: set the search keys before (or without) a profile id, e.g. an empty value."""
        if case == "off":
            w = World(env={})
            w.run()
        elif case == "no_claim":
            w = World()
            w.run(claims={"custom:tenantId": TENANT})
        elif case == "budget":
            w = World()
            w.run(extra_patches=[patch.object(h, "_load_tenant_connect_config",
                                              side_effect=lambda t: (setattr(w.clock, "t", 2.6), w.tenant_cfg)[1])])
        elif case == "profiles_down":
            w = World()
            w.fp.fail = {"*"}
            w.run()
        else:
            w = World()
            w.run(_body(driverId="DRV-SOMEONE-ELSE"))
        attrs = w.chat_attrs()
        assert w.connect.start_chat_contact.call_count == 1
        assert not set(_SEARCH_KEYS) & set(attrs)

    def test_crm_attributes_never_replace_the_escalation_attributes(self):
        w_off = World(env={})
        w_off.run()
        w_on = World()
        w_on.run()
        off, on = w_off.chat_attrs(), w_on.chat_attrs()
        assert set(on) - set(off) == set(_SEARCH_KEYS)
        assert {k: on[k] for k in off} == off

    @pytest.mark.parametrize("bad", [None, "", "has space", "x" * 129, "a/b", "$.Attributes.x"])
    def test_an_id_that_is_not_a_profile_id_gives_no_attributes(self, bad):
        """Mutant: drop the id check, so a malformed id becomes a workspace search."""
        assert crm.chat_profile_attributes(bad) == {}

    def test_p0_after_callback_chat_carries_the_search_keys_and_the_callback_stamp(self):
        """Review G6 c1 suggestion: the placed-callback path with a created profile."""
        cfg = {**_tenant_cfg(callback=True), "chatContactFlowId": "after-cb-flow"}
        w = World(tenant_cfg=cfg, driver_row={**DRIVER_ROW, "phone": None})
        gates = [
            patch.object(h, "_verified_p0_decision", return_value=True),
            patch.object(h, "_vehicle_matches_vin", return_value=True),
            patch.object(h, "_caller_assigned_driver", return_value=DRIVER),
            patch.object(h, "_resolve_driver_phone", return_value=PHONE),
        ]
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=gates)
        assert resp["statusCode"] == 200
        kw = w.connect.start_chat_contact.call_args.kwargs
        assert kw["ContactFlowId"] == "after-cb-flow"
        (pid,) = w.fp.profiles
        self._assert_names_profile(w, kw["Attributes"], pid)
        assert kw["Attributes"]["p0CallbackPlaced"] == "true"
        call_kw = w.connect.start_outbound_voice_contact.call_args.kwargs
        assert kw["Attributes"]["p0CallbackEscalationId"] == call_kw["Attributes"]["escalationId"]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudget:
    def test_no_pre_chat_call_after_the_pre_chat_cutoff(self):
        """Mutant: no budget check before the first profile search."""
        w = World()
        w.clock.t = 0.0
        # The callback or tenant lookup ate the pre-chat budget.
        resp = w.run(extra_patches=[patch.object(h, "_load_tenant_connect_config",
                                                 side_effect=lambda t: (setattr(w.clock, "t", 2.6), w.tenant_cfg)[1])])
        assert resp["statusCode"] == 200
        assert "search_profiles" not in w.fp.calls
        assert "customerProfileId" not in w.chat_attrs()
        # Post-chat steps still run (2.6 s <= 12.5 s): the case opens without a customer.
        assert len(w.fc.cases) == 1
        assert "customer_id" not in next(iter(w.fc.cases.values()))

    def test_each_pre_chat_call_is_gated_not_only_the_first(self):
        """Mutant: check the budget once at the start of find_or_create_profile."""
        w = World(per_call_s=1.4)  # search account 0→1.4, search phone 1.4→2.8, create would start at 2.8
        resp = w.run()
        assert resp["statusCode"] == 200
        assert w.fp.calls[:2] == ["search_profiles", "search_profiles"]
        assert "create_profile" not in w.fp.calls
        assert w.connect.start_chat_contact.call_count == 1

    def test_no_post_chat_call_after_the_post_chat_cutoff(self):
        """Mutant: no budget check in put_vehicle / find_or_create_case / link_contact."""
        w = World(chat_advances_clock_to=12.6)
        resp = w.run()
        assert resp["statusCode"] == 200
        assert w.fc.calls == []
        assert "put_profile_object" not in w.fp.calls

    def test_budget_cutoffs_fit_the_handoff_timeouts(self):
        # Chat worst case ~10 s (2 attempts x (1 s + 4 s)); started by cutoff + one call.
        chat_worst = 2 * (h._CONNECT_CONFIG.connect_timeout + h._CONNECT_CONFIG.read_timeout)
        crm_call = crm._CRM_CLIENT_CONFIG.connect_timeout + crm._CRM_CLIENT_CONFIG.read_timeout
        assert crm.PRE_CHAT_CUTOFF_S + crm_call + chat_worst <= 14.0
        assert crm.POST_CHAT_CUTOFF_S + crm_call <= 14.0
        assert crm._CRM_CLIENT_CONFIG.retries["total_max_attempts"] == 1


# ---------------------------------------------------------------------------
# Identity and gates
# ---------------------------------------------------------------------------


class TestIdentityAndGates:
    def test_no_driver_claim_no_profile_but_case_opens(self):
        w = World()
        w.run(claims={"custom:tenantId": TENANT})
        assert w.fp.calls == []
        assert len(w.fc.cases) == 1
        assert "customer_id" not in next(iter(w.fc.cases.values()))

    def test_body_driver_that_differs_from_the_claim_gets_no_profile(self):
        """Mutant: prefer the body driverId over the claim."""
        w = World()
        w.run(_body(driverId="DRV-SOMEONE-ELSE"))
        assert w.fp.calls == []

    def test_tenant_not_allowlisted_makes_no_calls(self):
        w = World()
        w.run(claims={"custom:tenantId": "michelin", "custom:driverId": DRIVER})
        assert w.fp.calls == [] and w.fc.calls == []

    def test_tenant_on_another_instance_makes_no_calls(self):
        """Mutant: drop the instance gate in config_for."""
        w = World(tenant_cfg=_tenant_cfg(instance_arn="arn:aws:connect:us-east-1:111122223333:instance/other"))
        w.run()
        assert w.fp.calls == [] and w.fc.calls == []

    @pytest.mark.parametrize("bad", [
        "not json",
        json.dumps({**CRM_CONFIG, "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/*"}),
        json.dumps({**CRM_CONFIG, "casesDomainId": "dom/../x"}),
        json.dumps({**CRM_CONFIG, "fieldIds": {**FIELD_IDS, "vin": ""}}),
        json.dumps([CRM_CONFIG]),
    ])
    def test_malformed_config_is_off(self, bad):
        assert crm.load_config({"CONNECT_CRM_CONFIG": bad, "CONNECT_CRM_TENANTS": "meridian"}) is None

    def test_vehicle_object_requires_the_caller_assigned_to_that_vehicle(self):
        w = World(driver_row={**DRIVER_ROW, "assignedVehicleId": "VEH-OTHER"})
        w.run()
        assert "put_profile_object" not in w.fp.calls

    def test_vehicle_object_requires_vin_binding(self):
        w = World(vehicle_row=None)  # _crm_vehicle_row returns None on a vehicleId/VIN mismatch
        w.run()
        assert "put_profile_object" not in w.fp.calls

    def test_vehicle_object_content(self):
        w = World()
        w.run()
        obj = next(iter(w.fp.objects.values()))
        assert obj["vin"] == VIN
        assert obj["model"] == "2024 Meridian Azimuth"
        assert obj["openFaults"] == "P0420, P0171"
        assert obj["accountNumber"] == f"{TENANT}:{DRIVER}"

    def test_faults_ignored_from_a_triage_row_for_another_vin(self):
        w = World(triage_item={**TRIAGE_ITEM, "vin": "OTHERVIN000000000"})
        w.run()
        obj = next(iter(w.fp.objects.values()))
        assert obj["openFaults"] == "none reported"
        case = next(iter(w.fc.cases.values()))
        assert case[FIELD_IDS["faultCodes"]] == "none reported"


# ---------------------------------------------------------------------------
# Case content and logs
# ---------------------------------------------------------------------------


class TestCaseContent:
    def test_case_fields(self):
        w = World()
        w.run()
        case = next(iter(w.fc.cases.values()))
        pid = w.chat_attrs()["customerProfileId"]
        assert case["title"] == "[P2] check engine light"
        assert case[FIELD_IDS["severity"]] == "P2"
        assert case[FIELD_IDS["vin"]] == VIN
        assert case[FIELD_IDS["faultCodes"]] == "P0420, P0171"
        assert case[FIELD_IDS["triageSessionId"]] == "sess-0042"
        assert case["customer_id"] == (
            f"arn:aws:profile:us-east-1:111122223333:domains/amazon-connect-test/profiles/{pid}"
        )
        assert "Driver reports a steady check-engine lamp." in case["summary"]

    def test_profile_attribute_is_set_before_the_chat_starts(self):
        w = World()
        w.run()
        assert w.chat_attrs()["customerProfileId"] in w.fp.profiles

    def test_logs_never_carry_phone_name_or_vin(self, caplog):
        caplog.set_level(logging.INFO)
        w = World()
        w.run()
        text = "\n".join(r.getMessage() for r in caplog.records if "crm" in r.getMessage())
        assert "crm-case: created" in text
        for secret in (PHONE, VIN, "Test Driver"):
            assert secret not in text

    def test_escalation_key_is_stable_and_scoped(self):
        k = dict(tenant_id=TENANT, vin=VIN, escalation_type="safety", severity="P2",
                 triage_session_id="s1", voice_session_id=None, reason="r")
        assert crm.escalation_key(**k) == crm.escalation_key(**k)
        assert crm.escalation_key(**k) != crm.escalation_key(**{**k, "tenant_id": "other"})
        assert crm.escalation_key(**k) != crm.escalation_key(**{**k, "triage_session_id": "s2"})
        assert len(crm.escalation_key(**k)) == 64



# ---------------------------------------------------------------------------
# Review cycle 1 W1: the CRM DynamoDB reads are bounded and inside the budget
# ---------------------------------------------------------------------------


class TestDynamoReadsBounded:
    def test_crm_reads_use_the_bounded_client_not_the_handler_default(self):
        """Mutant: _crm_vehicle_row/_crm_triage_item read via h._get_ddb().

        These two run on the handler's thread after the chat and read through the
        bounded resource's Table (plain keys). The driver read runs on the profile
        step's thread and goes through crm.ddb_client(): TestDriverRowReadAtTheWire."""
        table = MagicMock()
        table.get_item.return_value = {"Item": {"vehicleId": VEHICLE, "vin": VIN}}
        bounded = MagicMock()
        bounded.Table.return_value = table
        default = MagicMock()
        with patch.object(crm, "ddb", return_value=bounded), \
             patch.object(h, "_get_ddb", return_value=default), \
             patch.object(h, "_CMS_VEHICLES_TABLE", "vehicles"), \
             patch.object(h, "_TRIAGE_DECISIONS_TABLE", "triage"):
            h._crm_vehicle_row(VEHICLE, VIN)
            h._crm_triage_item("sess-1")
        assert bounded.Table.call_count == 2
        default.Table.assert_not_called()
        default.meta.client.get_item.assert_not_called()

    def test_bounded_client_config(self):
        cfg = crm._CRM_CLIENT_CONFIG
        assert (cfg.connect_timeout, cfg.read_timeout, cfg.retries["total_max_attempts"]) == (0.5, 1.0, 1)

    def test_slow_driver_read_skips_every_profile_call(self):
        """The driver read eats the pre-chat budget: no Profiles call, chat still starts."""
        w = World()
        slow = lambda d: (setattr(w.clock, "t", 2.7), DRIVER_ROW)[1]
        resp = w.run(extra_patches=[patch.object(h, "_crm_driver_row", side_effect=slow)])
        assert resp["statusCode"] == 200
        assert w.fp.calls == []
        assert w.connect.start_chat_contact.call_count == 1

    def test_post_chat_reads_are_gated(self):
        w = World(chat_advances_clock_to=12.6)
        reads = MagicMock(return_value=VEHICLE_ROW)
        triage = MagicMock(return_value=TRIAGE_ITEM)
        w.run(extra_patches=[patch.object(h, "_crm_vehicle_row", reads),
                             patch.object(h, "_crm_triage_item", triage)])
        reads.assert_not_called()
        triage.assert_not_called()


    def test_ddb_and_clients_are_built_with_the_bounded_config(self):
        """Mutant: boto3.resource("dynamodb") / boto3.client(...) without config=."""
        with patch.object(crm, "_ddb_resource", None), patch.object(crm, "_clients", {}), \
             patch.object(crm, "_ddb_client", None, create=True), \
             patch.object(crm.boto3, "resource") as res, patch.object(crm.boto3, "client") as cli:
            crm.ddb()
            crm._client("connectcases", "us-east-1")
            crm.ddb_client()
        assert res.call_args.kwargs["config"] is crm._CRM_CLIENT_CONFIG
        assert [c.args[0] for c in cli.call_args_list] == ["connectcases", "dynamodb"]
        assert all(c.kwargs["config"] is crm._CRM_CLIENT_CONFIG for c in cli.call_args_list)


# ---------------------------------------------------------------------------
# Issue 2026-11-04-escalate-crm-driver-row-unread: the profile step's driver read.
# Checked at the wire, with real botocore clients, because a MagicMock accepted the
# doubly-typed key that DynamoDB refused (ValidationException on every live read since
# the 2026-11-04 01:21Z deploy: profiles without name or phone, no phone adoption, no
# vehicle object).
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
from decimal import Decimal  # noqa: E402

import boto3  # noqa: E402
from botocore.awsrequest import AWSResponse  # noqa: E402

# How DynamoDB returns a CMS drivers row (AttributeValue JSON). The names differ from
# the request's displayName ("Test Driver"), so a profile named from them shows the row
# was read.
WIRE_DRIVER_ITEM = {
    "driverId": {"S": DRIVER},
    "assignedVehicleId": {"S": VEHICLE},
    "phone": {"S": PHONE},
    "firstName": {"S": "Brandy"},
    "lastName": {"S": "Okafor"},
    "incidentCount": {"N": "0"},
}
WIRE_DRIVER_ROW = {
    "driverId": DRIVER, "assignedVehicleId": VEHICLE, "phone": PHONE,
    "firstName": "Brandy", "lastName": "Okafor", "incidentCount": Decimal(0),
}


class _Raw:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, **_kw):
        yield self._data


class DynamoWire:
    """Answers DynamoDB GetItem on one table at botocore's `before-send`, i.e. after
    every serializer and handler has run, for each client built from the session it is
    registered on. Records each request body as it would go on the wire. Answers like
    DynamoDB: a key that isn't `{"driverId": {"S": <str>}}` (the table's key schema) is
    a ValidationException; a found item comes back in AttributeValue JSON."""

    def __init__(self, table: str, items: dict[str, dict]) -> None:
        self.table = table
        self.items = items
        self.requests: list[dict] = []

    def __call__(self, request, **_kw):
        body = json.loads(request.body)
        self.requests.append(body)
        key = body.get("Key")
        well_typed = (
            isinstance(key, dict) and set(key) == {"driverId"}
            and isinstance(key["driverId"], dict) and set(key["driverId"]) == {"S"}
            and isinstance(key["driverId"]["S"], str)
        )
        if body.get("TableName") != self.table or not well_typed:
            return self._response(400, {
                "__type": "com.amazon.coral.validate#ValidationException",
                "message": "The provided key element does not match the schema",
            })
        item = self.items.get(key["driverId"]["S"])
        return self._response(200, {"Item": item} if item else {})

    @staticmethod
    def _response(status: int, payload: dict) -> AWSResponse:
        return AWSResponse(
            "https://dynamodb.us-west-2.amazonaws.com/", status,
            {"x-amzn-RequestId": "wire-req-1", "Content-Type": "application/x-amz-json-1.0"},
            _Raw(json.dumps(payload).encode("utf-8")),
        )


@contextlib.contextmanager
def _dynamo_wire(items: dict[str, dict] | None = None):
    """A fresh boto3 default session carrying DynamoWire, with crm's cached DynamoDB
    resource and client cleared, so whatever crm builds goes through the wire."""
    session = boto3.session.Session(region_name="us-west-2")
    wire = DynamoWire(h._CMS_DRIVERS_TABLE, {DRIVER: WIRE_DRIVER_ITEM} if items is None else items)
    session.events.register("before-send.dynamodb.GetItem", wire)
    with patch.object(boto3, "DEFAULT_SESSION", session), \
         patch.object(crm, "_ddb_resource", None), \
         patch.object(crm, "_ddb_client", None, create=True):
        yield wire


class TestDriverRowReadAtTheWire:
    def test_the_key_goes_out_typed_once_and_the_row_comes_back_as_python_values(self):
        """Mutants: today's call (crm.ddb().meta.client with the typed key: the
        resource's client types it again); a plain client with a plain key
        (ParamValidationError); no TypeDeserializer (the row keeps {"S": ...});
        crm.ddb_client() returning crm.ddb().meta.client."""
        with _dynamo_wire() as wire:
            row = h._crm_driver_row(DRIVER)
        assert wire.requests == [{"TableName": h._CMS_DRIVERS_TABLE, "Key": {"driverId": {"S": DRIVER}}}]
        assert row == WIRE_DRIVER_ROW

    def test_a_driver_with_no_row_reads_as_none(self, caplog):
        """Review c1 S2: no row is not a lookup failure (C2 of the live check treats that
        line as the regression's signature). Mutant: drop the `if not item` guard."""
        caplog.set_level(logging.INFO)
        with _dynamo_wire(items={}) as wire:
            assert h._crm_driver_row(DRIVER) is None
        assert len(wire.requests) == 1
        assert "driver lookup failed" not in caplog.text

    def test_the_step_thread_never_touches_a_boto3_resource(self):
        """boto3 resources aren't thread-safe and the handler's thread uses crm.ddb()
        after the chat while a late step may still run. Mutants: the pre-11-04 read
        crm.ddb().Table(...).get_item(Key={"driverId": ...}); today's
        crm.ddb().meta.client read; h._get_ddb() (unbounded); a client the handler
        builds itself instead of the bounded crm.ddb_client()."""
        with _dynamo_wire() as wire, \
             patch.object(crm, "ddb", side_effect=AssertionError("resource on the step thread")), \
             patch.object(h, "_get_ddb", side_effect=AssertionError("unbounded handler resource")), \
             patch.object(crm, "ddb_client", wraps=crm.ddb_client) as bounded:
            row = h._crm_driver_row(DRIVER)
        assert row == WIRE_DRIVER_ROW
        assert len(wire.requests) == 1
        assert bounded.call_count == 1

    def test_the_read_client_is_bounded_reused_and_built_at_init(self):
        """The client is built in warm_clients (Lambda init), not on the step's thread,
        keeps the CRM bound, and is in the Lambda's region (the CMS tables'), not the
        Connect region the Profiles/Cases clients use. Mutants: not warmed; a new client
        per call; built without config=; built in the CRM region (review c1 S1)."""
        with _dynamo_wire(), patch.object(crm, "_clients", {}):
            crm.warm_clients({"CONNECT_CRM_CONFIG": json.dumps(CRM_CONFIG)})
            built = crm._ddb_client
            assert built is not None
            assert crm.ddb_client() is built
            assert built.meta.service_model.service_name == "dynamodb"
            assert built.meta.region_name == "us-west-2" != crm.load_config(
                {"CONNECT_CRM_CONFIG": json.dumps(CRM_CONFIG)})["region"]
            c = built.meta.config
            assert (c.connect_timeout, c.read_timeout, c.retries["total_max_attempts"]) == (0.5, 1.0, 1)

    def test_the_profile_carries_the_drivers_name_and_phone_and_the_vehicle_object(self):
        """The live symptom (P0 run crm-p0-20261104T134715Z-b6c4cf): a profile with no
        name or phone and vehicle_object=False. Through the handler, the profile step's
        thread and the real client."""
        w = World()
        with _dynamo_wire() as wire:
            resp = w.run(real_driver_read=True)
        assert resp["statusCode"] == 200
        assert len(wire.requests) == 1
        [created] = list(w.fp.profiles.values())
        assert (created.get("FirstName"), created.get("LastName"), created.get("PhoneNumber")) == (
            "Brandy", "Okafor", PHONE)
        assert list(w.fp.objects) == [crm.asset_key(crm.account_number(TENANT, DRIVER), VIN)]
        assert w.chat_attrs()["customerProfileId"] == created["ProfileId"]

    def test_the_drivers_unclaimed_phone_profile_is_adopted(self):
        """The live symptom: a second profile created beside the driver's own (the
        `_phone` search needs the row's phone)."""
        w = World(profiles=[{"ProfileId": "ctr-0001", "PhoneNumber": PHONE}])
        with _dynamo_wire():
            w.run(real_driver_read=True)
        assert "create_profile" not in w.fp.calls
        assert w.fp.profiles["ctr-0001"]["AccountNumber"] == f"{TENANT}:{DRIVER}"
        assert w.chat_attrs()["customerProfileId"] == "ctr-0001"

    @pytest.mark.parametrize("path", ["account", "adopt"])
    def test_a_nameless_profile_of_the_driver_gets_the_row_names_read_at_the_wire(self, path):
        """Issue 2026-11-04-escalate-crm-nameless-profile through the handler, the profile
        step's thread and the real driver read: the names are the row's (Brandy Okafor,
        not the request's "Test Driver") and the phone is untouched."""
        prof = ({"ProfileId": "acct-0001", "AccountNumber": f"{TENANT}:{DRIVER}", "PhoneNumber": "+15555550199"}
                if path == "account" else {"ProfileId": "ctr-0001", "PhoneNumber": PHONE})
        w = World(profiles=[prof])
        with _dynamo_wire() as wire:
            assert w.run(real_driver_read=True)["statusCode"] == 200
        assert len(wire.requests) == 1
        p = w.fp.profiles[prof["ProfileId"]]
        assert (p.get("FirstName"), p.get("LastName"), p["PhoneNumber"]) == (
            "Brandy", "Okafor", prof["PhoneNumber"])
        assert all(set(u) <= {"AccountNumber", "FirstName", "LastName"} for u in w.fp.updates)
        assert w.chat_attrs()["customerProfileId"] == prof["ProfileId"]


# ---------------------------------------------------------------------------
# Issue 2026-11-03-p0-callback-starves-crm-profile: the profile step runs alongside
# the P0 callback, the call starts no later than before, and the chat never waits
# past the pre-chat deadline for it.
# ---------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

_P0_GATES = (
    ("_verified_p0_decision", True),
    ("_vehicle_matches_vin", True),
    ("_caller_assigned_driver", DRIVER),
    ("_resolve_driver_phone", PHONE),
)


def _p0_world(**kw) -> World:
    cfg = {**_tenant_cfg(callback=True), "chatContactFlowId": "after-cb-flow"}
    return World(tenant_cfg=cfg, **kw)


def _p0_patches(*extra):
    return [*(patch.object(h, name, return_value=value) for name, value in _P0_GATES), *extra]


class TestP0CallbackDoesNotStarveTheProfile:
    def test_cold_start_call_past_the_cutoff_still_gets_the_profile(self):
        """Live 2026-11-03 19:54Z (cold start): the call returned 2.3 s after entry,
        the profile step ran after it, found the 2.5 s pre-chat budget gone and was
        skipped, so the chat carried no profile and the case no customer.

        The fake call returns 2.6 s after entry (past the cutoff) and stays in flight
        until the profile step has finished, as a real call does while a concurrent
        step runs; a profile step that only starts after the call can't finish first.
        Mutants: run the profile step after the callback (today's order); count the
        call's time against the step's budget.
        """
        w = _p0_world()
        profile_done = threading.Event()
        real = crm.find_or_create_profile

        def _profile(*a, **kw):
            try:
                return real(*a, **kw)
            finally:
                profile_done.set()

        def _call(**kw):
            profile_done.wait(timeout=1.0)
            w.clock.t = 2.6
            return {"ContactId": "call-001"}

        w.connect.start_outbound_voice_contact.side_effect = _call
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=_p0_patches(
            patch.object(crm, "find_or_create_profile", side_effect=_profile)))

        assert resp["statusCode"] == 200
        names = [c[0] for c in w.connect.method_calls]
        assert names.index("start_outbound_voice_contact") < names.index("start_chat_contact")
        (pid,) = w.fp.profiles
        attrs = w.chat_attrs()
        assert attrs["customerProfileId"] == pid
        assert attrs["profileSearchKey"] == "_profileId"
        assert attrs["profileSearchValue"] == pid
        assert attrs["p0CallbackPlaced"] == "true"
        (case,) = w.fc.cases.values()
        assert case["customer_id"].endswith(f"/profiles/{pid}")

    def test_a_slow_profile_step_never_delays_the_call(self):
        """PO constraint (prompt 226): the P0 call must not start later than before.
        The profile step's first read blocks until the call has been placed; the call
        must be placed at once in real time, while the step is still blocked, with no
        CRM time spent before it.
        Mutants: run the profile step before the callback (issue option 1); start it
        on a thread but wait for it before placing the call, for 2 s or for 0.5 s
        (review cycle 1 Critical: a fake-clock reading can't see a short real wait).
        """
        w = _p0_world()
        placed = threading.Event()
        step_finished = threading.Event()
        seen: dict[str, Any] = {}
        real = crm.find_or_create_profile

        def _driver_row(driver_id):
            seen["call_first"] = placed.wait(timeout=2.0)
            w.clock.t += 1.0
            return DRIVER_ROW

        def _profile(*a, **kw):
            try:
                return real(*a, **kw)
            finally:
                step_finished.set()

        def _call(**kw):
            seen["real_s_to_call"] = time.monotonic() - t0
            seen["step_finished_at_call"] = step_finished.is_set()
            seen["clock_at_call"] = w.clock.t
            placed.set()
            return {"ContactId": "call-001"}

        w.connect.start_outbound_voice_contact.side_effect = _call
        t0 = time.monotonic()
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=_p0_patches(
            patch.object(h, "_crm_driver_row", side_effect=_driver_row),
            patch.object(crm, "find_or_create_profile", side_effect=_profile)))

        assert resp["statusCode"] == 200
        assert seen["real_s_to_call"] < 0.2
        assert seen["step_finished_at_call"] is False
        assert seen["call_first"] is True
        assert seen["clock_at_call"] == 0.0
        assert "customerProfileId" in w.chat_attrs()

    def test_the_step_starts_after_the_p0_gates_not_before(self):
        """Nothing pins the start point otherwise (review cycle 1 suggestion): started
        above the gate reads it would compete with them for CPU before the call.
        Mutant: start the step before the P0 gates."""
        w = _p0_world()
        alive: list[bool] = []
        gates_done = threading.Event()

        def _phone(*a):
            alive.append(any(t.name == "escalate-crm-profile" and t.is_alive()
                             for t in threading.enumerate()))
            gates_done.set()
            return PHONE

        def _driver_row(driver_id):
            gates_done.wait(timeout=1.0)  # a step started early is still alive at the gate
            return DRIVER_ROW

        gates = [patch.object(h, n, return_value=v) for n, v in _P0_GATES if n != "_resolve_driver_phone"]
        resp = w.run(_body(severity="P0", dispatchRoadside=False),
                     extra_patches=[*gates, patch.object(h, "_resolve_driver_phone", side_effect=_phone),
                                    patch.object(h, "_crm_driver_row", side_effect=_driver_row)])
        assert resp["statusCode"] == 200
        assert alive == [False]

    def test_a_call_ending_past_the_deadline_still_uses_a_step_that_finished(self):
        """The issue's case at its worst observed time (a 4.64 s cold call, 10-05): the
        step finished during the call, the call returned after the 4.0 s deadline, and
        the chat still carries the profile (no wait needed, the result is ready).
        Mutant: drop a finished step's result once the deadline has passed (review
        cycle 1 Warning)."""
        w = _p0_world()
        step_done = threading.Event()
        real_run = h._ProfileStep._run

        def _run(self):
            try:
                real_run(self)
            finally:
                step_done.set()  # after the step stored its result (review c2: no race)

        def _call(**kw):
            assert step_done.wait(timeout=2.0)
            w.clock.t = 4.6
            return {"ContactId": "call-001"}

        w.connect.start_outbound_voice_contact.side_effect = _call
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=_p0_patches(
            patch.object(h._ProfileStep, "_run", _run)))
        assert resp["statusCode"] == 200
        # The past-the-deadline path really ran (an assert inside _call would be swallowed
        # by _initiate_callback's except; review c3 suggestion).
        assert w.clock.t == 4.6
        assert w.chat_attrs()["p0CallbackPlaced"] == "true"
        (pid,) = w.fp.profiles
        assert w.chat_attrs()["customerProfileId"] == pid
        (case,) = w.fc.cases.values()
        assert case["customer_id"].endswith(f"/profiles/{pid}")

    def test_a_thread_that_cannot_start_never_stops_the_call_or_holds_the_chat(self):
        """`Thread.start()` can raise ("can't start new thread"). The call is still
        placed, the chat starts at once without a profile, the response is 200.
        Mutants: no try/except around start (500 before the call); the except leaves
        the step not done (every chat waits until the deadline). Review cycle 1."""
        w = _p0_world()

        class _NoStart:
            def __init__(self, *a, **kw):
                self.name = kw.get("name")

            def start(self):
                raise RuntimeError("can't start new thread")

        t0 = time.monotonic()
        resp = w.run(_body(severity="P0", dispatchRoadside=False), extra_patches=_p0_patches(
            patch.object(h.threading, "Thread", _NoStart)))
        assert time.monotonic() - t0 < 1.0
        assert resp["statusCode"] == 200
        assert w.connect.start_outbound_voice_contact.call_count == 1
        assert w.connect.start_chat_contact.call_count == 1
        assert not set(_SEARCH_KEYS) & set(w.chat_attrs())

    def test_the_chat_waits_for_a_profile_step_still_running_inside_the_deadline(self):
        """The step is still running when the handler reaches the chat (here: no call,
        a P2), but inside the deadline: the chat waits and carries the profile.
        Mutant: don't wait for the step (a 0 s wait)."""
        w = World()

        def _driver_row(driver_id):
            time.sleep(0.3)
            return DRIVER_ROW

        resp = w.run(extra_patches=[patch.object(h, "_crm_driver_row", side_effect=_driver_row)])
        assert resp["statusCode"] == 200
        (pid,) = w.fp.profiles
        assert w.chat_attrs()["customerProfileId"] == pid

    def test_a_hung_profile_step_never_holds_the_chat_past_the_deadline(self):
        """A profile call that hangs (e.g. a DNS stall the client timeouts don't cover)
        must not hold the chat: the handler stops waiting at crm.PRE_CHAT_DEADLINE_S
        and starts the chat without a profile. A result arriving later is not used.
        Mutant: wait for the step with no timeout."""
        w = World()
        release = threading.Event()
        step_done = threading.Event()
        real = crm.find_or_create_profile

        def _driver_row(driver_id):
            release.wait(timeout=5.0)
            return DRIVER_ROW

        def _profile(*a, **kw):
            try:
                return real(*a, **kw)
            finally:
                step_done.set()

        def _after():
            release.set()
            assert step_done.wait(timeout=5.0)

        t0 = time.monotonic()
        with patch.object(crm, "PRE_CHAT_DEADLINE_S", 0.2, create=True):
            resp = w.run(extra_patches=[
                patch.object(h, "_crm_driver_row", side_effect=_driver_row),
                patch.object(crm, "find_or_create_profile", side_effect=_profile),
            ], after=lambda: (setattr(w, "handler_s", time.monotonic() - t0), _after()))
        assert resp["statusCode"] == 200
        assert w.handler_s < 1.5
        assert w.connect.start_chat_contact.call_count == 1
        assert not set(_SEARCH_KEYS) & set(w.chat_attrs())
        (case,) = w.fc.cases.values()
        assert "customer_id" not in case

    def test_no_driver_read_once_the_budget_is_gone_before_the_step_starts(self):
        """The step checks the budget before its first read too (as the inline step did).
        Mutant: read the driver row without the budget check."""
        w = World()
        reads = MagicMock(return_value=DRIVER_ROW)
        w.run(extra_patches=[
            patch.object(h, "_load_tenant_connect_config",
                         side_effect=lambda t: (setattr(w.clock, "t", 2.6), w.tenant_cfg)[1]),
            patch.object(h, "_crm_driver_row", reads),
        ])
        reads.assert_not_called()
        assert w.fp.calls == []
        assert w.connect.start_chat_contact.call_count == 1

    def test_a_step_exception_is_logged_not_left_to_the_thread_hook(self, caplog):
        """An exception in the step is caught and logged by name; an uncaught one would
        print a Traceback from the thread hook (the post-deploy log scan's signal) even
        though the chat still starts. Mutant: drop the step's except."""
        caplog.set_level(logging.WARNING)
        hooked: list[Any] = []
        w = World()
        with patch.object(threading, "excepthook", side_effect=lambda a: hooked.append(a.exc_type)):
            resp = w.run(extra_patches=[
                patch.object(crm, "find_or_create_profile", side_effect=RuntimeError("bug"))])
        assert resp["statusCode"] == 200
        assert hooked == []
        assert any("profile step failed (RuntimeError)" in r.getMessage() for r in caplog.records)

    def test_the_pre_chat_deadline_is_the_last_calls_latest_end(self):
        """The handler stops waiting when the step's last allowed call (started at
        the cutoff) must have ended, so the chat starts no later than before the fix
        (spec § Budget: by 4 s) and returns inside the timeouts."""
        crm_call = crm._CRM_CLIENT_CONFIG.connect_timeout + crm._CRM_CLIENT_CONFIG.read_timeout
        chat_worst = 2 * (h._CONNECT_CONFIG.connect_timeout + h._CONNECT_CONFIG.read_timeout)
        assert crm.PRE_CHAT_DEADLINE_S == pytest.approx(crm.PRE_CHAT_CUTOFF_S + crm_call)
        assert crm.PRE_CHAT_DEADLINE_S + chat_worst <= 14.0


def test_escalate_lambda_has_the_cpu_for_both_threads():
    """The escalate Lambda ran at the 128 MB default (~0.07 vCPU): the cold call took
    2.3-4.6 s, mostly CPU. With the profile step on a second thread, CPU is what the
    call would share, so the function gets 512 MB (~4x CPU, same note as
    ApiServiceHistory). Read from the CDK source because no CI job runs the jest suites.
    Mutant: drop memorySize (back to 128 MB)."""
    import re
    src = (Path(__file__).resolve().parents[3] / "infrastructure" / "lib" / "vsa-api-stack.ts").read_text()
    start = src.index("new lambda.Function(this, 'ApiEscalate', {")
    block = src[start:src.index("environment:", start)]
    m = re.search(r"^\s*memorySize:\s*(\d+)\s*,", block, re.M)
    assert m and int(m.group(1)) >= 512
