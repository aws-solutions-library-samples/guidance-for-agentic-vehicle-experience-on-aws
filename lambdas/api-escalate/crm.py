"""Amazon Connect Customer Profiles and Cases for POST /escalate.

Spec: .kiro/specs/2026-11-02-avx-connect-profiles-cases/spec.md

On an escalation the handler (a) finds or creates the caller's Customer Profile
BEFORE the chat starts, so the chat can carry the profile id as the agent
workspace's search attributes (chat_profile_attributes), and (b) AFTER the chat
starts, puts the vehicle on the profile, finds or creates the escalation's case
and links the chat contact to it. Deterministic; no model.

The human handoff is the safety property. So:
- every public function here catches every exception and returns None/False;
- every AWS call is gated by a wall-clock Budget measured from handler entry:
  pre-chat calls only while elapsed <= PRE_CHAT_CUTOFF_S, post-chat calls only
  while elapsed <= POST_CHAT_CUTOFF_S, each call bounded by _CRM_CLIENT_CONFIG.
  The chat client can take ~10 s (handler._CONNECT_CONFIG), so a chat started by
  4 s returns by ~14 s, inside the 15 s Lambda and bidi client timeouts;
- the pre-chat profile step runs on its own thread, started just before the P0
  callback, so the call's time doesn't use up the step's budget and nothing new
  runs before the call on the handler's thread; the handler waits for the step
  until PRE_CHAT_DEADLINE_S at most (issue 2026-11-03-p0-callback-starves-crm-profile);
- logs carry ids and exception class names, never a phone, a name or a VIN.

Configuration is the Lambda env (set by CDK only when `connectCrmConfig` is
supplied): CONNECT_CRM_CONFIG (JSON written by scripts/connect-crm-setup.py) and
CONNECT_CRM_TENANTS (comma-separated allowlist). Missing or malformed → off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()

PRE_CHAT_CUTOFF_S = 2.5
POST_CHAT_CUTOFF_S = 12.5

# One attempt, short timeouts: a stalled Profiles/Cases call costs at most ~1.5 s.
_CRM_CLIENT_CONFIG = Config(connect_timeout=0.5, read_timeout=1.0, retries={"total_max_attempts": 1})

# The pre-chat profile step runs on its own thread, alongside the P0 callback (issue
# 2026-11-03-p0-callback-starves-crm-profile). The handler waits for it only until
# the latest end of a call started at the cutoff: 2.5 + 1.5 = 4.0 s from entry, the
# chat-start bound the budget already gave (module docstring). A step still running
# then is abandoned; the chat starts without a profile.
PRE_CHAT_DEADLINE_S = PRE_CHAT_CUTOFF_S + _CRM_CLIENT_CONFIG.connect_timeout + _CRM_CLIENT_CONFIG.read_timeout

FIELD_NAMES = ("severity", "vin", "vehicleId", "faultCodes", "escalationKey", "triageSessionId")

_INSTANCE_ARN = re.compile(r"^arn:aws:connect:([a-z0-9-]+):(\d{12}):instance/[A-Za-z0-9-]+$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Replaced by tests. monotonic: immune to wall-clock steps.
clock: Callable[[], float] = time.monotonic


class Budget:
    """Wall-clock budget from handler entry (see module docstring)."""

    def __init__(self, started_at: float):
        self.started_at = started_at

    def elapsed(self) -> float:
        return clock() - self.started_at

    def pre_chat_ok(self) -> bool:
        return self.elapsed() <= PRE_CHAT_CUTOFF_S

    def post_chat_ok(self) -> bool:
        return self.elapsed() <= POST_CHAT_CUTOFF_S


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(env: Any = None) -> dict[str, Any] | None:
    """Parse CONNECT_CRM_CONFIG; None (feature off) on absence or any shape error."""
    env = os.environ if env is None else env
    raw = env.get("CONNECT_CRM_CONFIG")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        log.warning("crm: CONNECT_CRM_CONFIG is not JSON; off")
        return None
    if not isinstance(cfg, dict):
        log.warning("crm: CONNECT_CRM_CONFIG is not an object; off")
        return None
    arn = cfg.get("instanceArn")
    m = _INSTANCE_ARN.match(arn) if isinstance(arn, str) else None
    if not m:
        log.warning("crm: instanceArn malformed; off")
        return None
    for key in ("profilesDomain", "objectType", "casesDomainId", "casesTemplateId"):
        if not (isinstance(cfg.get(key), str) and _ID.match(cfg[key])):
            log.warning("crm: %s malformed; off", key)
            return None
    field_ids = cfg.get("fieldIds")
    if not isinstance(field_ids, dict) or not all(
        isinstance(field_ids.get(n), str) and _ID.match(field_ids[n]) for n in FIELD_NAMES
    ):
        log.warning("crm: fieldIds malformed; off")
        return None
    tenants = frozenset(
        t.strip() for t in (env.get("CONNECT_CRM_TENANTS") or "").split(",") if t.strip()
    )
    return {
        "instanceArn": arn,
        "region": m.group(1),
        "account": m.group(2),
        "profilesDomain": cfg["profilesDomain"],
        "objectType": cfg["objectType"],
        "casesDomainId": cfg["casesDomainId"],
        "casesTemplateId": cfg["casesTemplateId"],
        "fieldIds": {n: field_ids[n] for n in FIELD_NAMES},
        "tenants": tenants,
    }


def config_for(tenant_id: str, tenant_instance_arn: Any, env: Any = None) -> dict[str, Any] | None:
    """The CRM config if it applies to this tenant AND this tenant's Connect instance, else None.

    Both gates are exact matches. A tenant missing from the allowlist, or whose
    config names another Connect instance, never writes into these domains.
    """
    try:
        cfg = load_config(env)
        if not cfg:
            return None
        if tenant_id not in cfg["tenants"]:
            return None
        if tenant_instance_arn != cfg["instanceArn"]:
            log.info("crm: tenant=%s instance differs from the CRM instance; off", tenant_id)
            return None
        return cfg
    except Exception as e:  # noqa: BLE001
        log.warning("crm: config failed (%s); off", type(e).__name__)
        return None


def claim_driver_id(claims: dict[str, Any], body_driver_id: Any) -> str | None:
    """The verified `custom:driverId` claim, or None.

    A body driverId may repeat the claim but never replace it: one that differs
    means no profile at all (the case still opens, with no customer).
    """
    claim = claims.get("custom:driverId") if isinstance(claims, dict) else None
    if not isinstance(claim, str) or not claim.strip():
        return None
    if body_driver_id not in (None, "") and body_driver_id != claim:
        return None
    return claim


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_clients: dict[tuple[str, str], Any] = {}

# DynamoDB reads the CRM steps make (CMS drivers/vehicles rows, the triage row)
# share the same bound; the handler's own DynamoDB resource uses botocore defaults
# (60 s timeouts, legacy retries) and must not be used inside the budget.
_ddb_resource = None
# The profile step's driver read (on its own thread) uses a plain low-level client:
# boto3 clients are thread-safe, resources aren't. Not ddb().meta.client: a resource's
# client keeps the resource's serializer, so a typed key ({"S": ...}) is typed a second
# time and DynamoDB refuses it (issue 2026-11-04-escalate-crm-driver-row-unread).
_ddb_client = None


def ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", config=_CRM_CLIENT_CONFIG)
    return _ddb_resource


def ddb_client():
    """Plain DynamoDB client with the CRM bound: requests and replies in AttributeValue form."""
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb", config=_CRM_CLIENT_CONFIG)
    return _ddb_client


def _client(service: str, region: str):
    key = (service, region)
    if key not in _clients:
        _clients[key] = boto3.client(service, region_name=region, config=_CRM_CLIENT_CONFIG)
    return _clients[key]


def warm_clients(env: Any = None) -> None:
    """Build the CRM clients at cold start (Lambda init), outside the handler's budget."""
    try:
        cfg = load_config(env)
        if cfg:
            _client("customer-profiles", cfg["region"])
            _client("connectcases", cfg["region"])
            ddb()
            ddb_client()
    except Exception as e:  # noqa: BLE001
        log.warning("crm: client warm-up failed (%s)", type(e).__name__)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def account_number(tenant_id: str, driver_id: str) -> str:
    return f"{tenant_id}:{driver_id}"[:255]


def asset_key(account: str, vin: str) -> str:
    return f"{account}#{vin}"[:255]


def profile_arn(cfg: dict[str, Any], profile_id: str) -> str:
    return (
        f"arn:aws:profile:{cfg['region']}:{cfg['account']}:domains/"
        f"{cfg['profilesDomain']}/profiles/{profile_id}"
    )


def contact_arn(cfg: dict[str, Any], contact_id: str) -> str:
    return f"{cfg['instanceArn']}/contact/{contact_id}"


# The agent workspace searches Customer Profiles with these two contact attributes
# when the contact reaches an agent (AWS admin guide, "Automatically populate customer
# profiles"; the default for chat is the email, which escalations don't carry).
# `_profileId` names exactly the profile this Lambda created or reused, so the agent
# sees that one profile, not every profile sharing a phone or an account index entry.
PROFILE_SEARCH_KEY = "_profileId"


def chat_profile_attributes(profile_id: Any) -> dict[str, str]:
    """Contact attributes that point the chat at `profile_id`; {} without a valid id."""
    pid = str(profile_id or "")
    if not _ID.match(pid):
        return {}
    return {
        "customerProfileId": pid,
        "profileSearchKey": PROFILE_SEARCH_KEY,
        "profileSearchValue": pid,
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def escalation_key(
    *,
    tenant_id: str,
    vin: str,
    escalation_type: str,
    severity: str,
    triage_session_id: Any,
    voice_session_id: Any,
    reason: str,
    now: datetime | None = None,
) -> str:
    """Stable id of one escalation, so a retry finds the same case.

    Keyed on the triage (or voice) session when the caller sent one; otherwise on
    the UTC hour and the reason text. 64 hex chars = CreateCase's clientToken limit.
    """
    session = next(
        (s.strip() for s in (triage_session_id, voice_session_id) if isinstance(s, str) and s.strip()),
        None,
    )
    if session:
        basis = f"session|{session}"
    else:
        hour = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H")
        basis = f"nosession|{hour}|{_sha(reason or '')}"
    return _sha(f"{tenant_id}|{vin}|{escalation_type}|{severity}|{basis}")[:64]


# ---------------------------------------------------------------------------
# Profile (before the chat)
# ---------------------------------------------------------------------------


def _names(driver_row: dict[str, Any] | None, display_name: Any) -> tuple[str | None, str | None]:
    row = driver_row or {}
    first, last = row.get("firstName"), row.get("lastName")
    if isinstance(first, str) and first.strip():
        return first.strip(), (last.strip() if isinstance(last, str) and last.strip() else None)
    full = row.get("name") if isinstance(row.get("name"), str) else display_name
    if not isinstance(full, str) or not full.strip():
        return None, None
    parts = full.strip().split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


def _phone(driver_row: dict[str, Any] | None) -> str | None:
    phone = (driver_row or {}).get("phone")
    return phone.strip() if isinstance(phone, str) and phone.strip() else None


def _has_a_name(profile: dict[str, Any]) -> bool:
    """Any FirstName or LastName value at all, even whitespace: such a profile is left as it is."""
    return any(profile.get(k) not in (None, "") for k in ("FirstName", "LastName"))


def _row_name_fields(driver_row: dict[str, Any] | None) -> dict[str, str]:
    """FirstName/LastName from the CMS driver row only, never the request's displayName.

    A part the row lacks is left out, never sent as "": UpdateProfile removes a field
    sent as an empty string.
    """
    first, last = _names(driver_row, None)
    fields: dict[str, str] = {}
    if first:
        fields["FirstName"] = first[:255]
    if last:
        fields["LastName"] = last[:255]
    return fields


def _name_fill(profile: dict[str, Any], driver_row: dict[str, Any] | None) -> dict[str, str]:
    """The names to set on a profile found for the driver: the row's, only when the
    profile has neither a first nor a last name (issue 2026-11-04-escalate-crm-nameless-profile).
    {} on any error, so a bad row never costs the profile."""
    try:
        return {} if _has_a_name(profile) else _row_name_fields(driver_row)
    except Exception as e:  # noqa: BLE001
        log.warning("crm-profile: name fill skipped (%s)", type(e).__name__)
        return {}


def _fill_names(cp, domain: str, budget: Budget, profile: dict[str, Any], driver_row) -> None:
    """Name a found profile from the driver row if it has no name. Never raises; never the phone."""
    fields = _name_fill(profile, driver_row)
    if not fields:
        return
    pid = profile["ProfileId"]
    if not budget.pre_chat_ok():
        _skip("profile-name")
        return
    try:
        cp.update_profile(DomainName=domain, ProfileId=pid, **fields)
        log.info("crm-profile: named profile=%s", pid)
    except Exception as e:  # noqa: BLE001
        # Still the driver's profile; the next escalation tries again.
        log.warning("crm-profile: name update failed (%s) profile=%s", type(e).__name__, pid)


def find_or_create_profile(
    cfg: dict[str, Any],
    budget: Budget,
    *,
    tenant_id: str,
    driver_id: str,
    driver_row: dict[str, Any] | None,
    display_name: Any,
) -> str | None:
    """ProfileId of the caller's profile, reusing before creating. Never raises.

    Order: `_account` = "<tenant>:<driver>"; else `_phone` from the CMS drivers
    row, adopting a profile only when it has no AccountNumber (UpdateProfile sets
    ours) or already has ours; else CreateProfile. Several candidates → the lowest
    ProfileId, so repeated calls pick the same one. A profile found or adopted this
    way that has neither a FirstName nor a LastName gets the driver row's names
    (never displayName, never the phone or any other field; adoption sends them in
    the same UpdateProfile as the AccountNumber).
    """
    try:
        return _find_or_create_profile(cfg, budget, tenant_id, driver_id, driver_row, display_name)
    except Exception as e:  # noqa: BLE001
        log.warning("crm-profile: failed (%s)", type(e).__name__)
        return None


def _skip(step: str) -> None:
    log.info("crm: skipped step=%s reason=budget", step)


def _lowest(items: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return min(items, key=lambda p: p["ProfileId"], default=None)


def _find_or_create_profile(cfg, budget, tenant_id, driver_id, driver_row, display_name):
    cp = _client("customer-profiles", cfg["region"])
    domain = cfg["profilesDomain"]
    acct = account_number(tenant_id, driver_id)

    if not budget.pre_chat_ok():
        _skip("profile-search-account")
        return None
    items = cp.search_profiles(DomainName=domain, KeyName="_account", Values=[acct], MaxResults=20).get("Items") or []
    mine = _lowest(p for p in items if p.get("ProfileId") and p.get("AccountNumber") == acct)
    if mine:
        log.info("crm-profile: found by=account profile=%s", mine["ProfileId"])
        _fill_names(cp, domain, budget, mine, driver_row)
        return mine["ProfileId"]

    phone = _phone(driver_row)
    if phone:
        if not budget.pre_chat_ok():
            _skip("profile-search-phone")
            return None
        items = cp.search_profiles(DomainName=domain, KeyName="_phone", Values=[phone], MaxResults=20).get("Items") or []
        same = _lowest(p for p in items if p.get("ProfileId") and p.get("AccountNumber") == acct)
        if same:
            log.info("crm-profile: found by=phone profile=%s", same["ProfileId"])
            _fill_names(cp, domain, budget, same, driver_row)
            return same["ProfileId"]
        unclaimed = _lowest(p for p in items if p.get("ProfileId") and not p.get("AccountNumber"))
        if unclaimed:
            pid = unclaimed["ProfileId"]
            if budget.pre_chat_ok():
                fields = {"AccountNumber": acct, **_name_fill(unclaimed, driver_row)}
                try:
                    cp.update_profile(DomainName=domain, ProfileId=pid, **fields)
                    log.info("crm-profile: adopted by=phone profile=%s named=%s", pid, "FirstName" in fields)
                except Exception as e:  # noqa: BLE001
                    # Still the driver's profile; the next escalation finds it by phone again.
                    log.warning("crm-profile: adopt update failed (%s) profile=%s", type(e).__name__, pid)
            else:
                _skip("profile-adopt")
            return pid
        # Every phone match belongs to another account: never take it over.

    if not budget.pre_chat_ok():
        _skip("profile-create")
        return None
    kwargs: dict[str, Any] = {
        "DomainName": domain,
        "AccountNumber": acct,
        "Attributes": {"tenantId": tenant_id[:255], "driverId": driver_id[:255], "source": "avx-escalate"},
    }
    first, last = _names(driver_row, display_name)
    if first:
        kwargs["FirstName"] = first[:255]
    if last:
        kwargs["LastName"] = last[:255]
    if phone:
        kwargs["PhoneNumber"] = phone[:255]
    pid = cp.create_profile(**kwargs)["ProfileId"]
    log.info("crm-profile: created profile=%s", pid)
    return pid


# ---------------------------------------------------------------------------
# After the chat: vehicle object, case, contact link
# ---------------------------------------------------------------------------


def open_faults_from_decision(item: dict[str, Any] | None, tenant_id: str, vin: str) -> list[str]:
    """`inputsSnapshot.activeDtcs` of a triage decision row bound to this tenant + VIN, else []."""
    if not isinstance(item, dict) or item.get("tenantId") != tenant_id or item.get("vin") != vin:
        return []
    snap = item.get("inputsSnapshot")
    codes = snap.get("activeDtcs") if isinstance(snap, dict) else None
    if not isinstance(codes, list):
        return []
    return [c for c in codes if isinstance(c, str) and c.strip()][:20]


def model_label(vehicle_row: dict[str, Any] | None) -> str:
    row = vehicle_row or {}
    parts = [str(row[k]) for k in ("year", "make", "model") if row.get(k) not in (None, "")]
    return " ".join(parts)[:255] or "Vehicle"


def put_vehicle(
    cfg: dict[str, Any],
    budget: Budget,
    *,
    tenant_id: str,
    driver_id: str,
    vin: str,
    vehicle_id: str,
    vehicle_row: dict[str, Any] | None,
    open_faults: Iterable[str],
) -> str | None:
    """Put the vehicle on the profile (matched by AccountNumber). Returns its unique key. Never raises."""
    try:
        if not budget.post_chat_ok():
            _skip("vehicle-object")
            return None
        acct = account_number(tenant_id, driver_id)
        faults = list(open_faults)
        obj = {
            "accountNumber": acct,
            "assetKey": asset_key(acct, vin),
            "vin": vin[:32],
            "vehicleId": vehicle_id[:64],
            "model": model_label(vehicle_row),
            "openFaults": (", ".join(faults) or "none reported")[:1000],
            "status": "Open faults" if faults else "No open faults",
        }
        resp = _client("customer-profiles", cfg["region"]).put_profile_object(
            DomainName=cfg["profilesDomain"], ObjectTypeName=cfg["objectType"], Object=json.dumps(obj)
        )
        key = resp.get("ProfileObjectUniqueKey")
        log.info("crm-vehicle: put ok=%s", bool(key))
        return key
    except Exception as e:  # noqa: BLE001
        log.warning("crm-vehicle: failed (%s)", type(e).__name__)
        return None


def _search_case(cc, cfg: dict[str, Any], key: str) -> str | None:
    fid = cfg["fieldIds"]["escalationKey"]
    resp = cc.search_cases(
        domainId=cfg["casesDomainId"],
        maxResults=5,
        filter={"field": {"equalTo": {"id": fid, "value": {"stringValue": key}}}},
        fields=[{"id": fid}],
    )
    ids = sorted(c["caseId"] for c in (resp.get("cases") or []) if c.get("caseId"))
    return ids[0] if ids else None


def case_fields(
    cfg: dict[str, Any],
    *,
    key: str,
    severity: str,
    reason: str,
    summary: Any,
    vin: str,
    vehicle_id: str,
    model: str,
    fault_codes: list[str],
    triage_session_id: Any,
    escalation_type: str,
    profile_id: str | None,
) -> list[dict[str, Any]]:
    f = cfg["fieldIds"]
    faults = ", ".join(fault_codes) or "none reported"
    lines = []
    if isinstance(summary, str) and summary.strip():
        lines.append(f"Triage summary: {summary.strip()[:1000]}")
    lines.append(f"Reason: {reason[:300]}")
    lines.append(f"Open faults: {faults}")
    lines.append(f"Vehicle: {model} ({vehicle_id[:64]}), VIN {vin[:32]}")
    lines.append(f"Escalation: {escalation_type}, severity {severity}")

    def s(v: str) -> dict[str, Any]:
        return {"stringValue": v}

    fields = [
        {"id": "title", "value": s(f"[{severity}] {reason}"[:200])},
        {"id": "summary", "value": s("\n".join(lines)[:1500])},
        {"id": f["severity"], "value": s(severity)},
        {"id": f["vin"], "value": s(vin[:32])},
        {"id": f["vehicleId"], "value": s(vehicle_id[:64])},
        {"id": f["faultCodes"], "value": s(faults[:500])},
        {"id": f["escalationKey"], "value": s(key)},
    ]
    if isinstance(triage_session_id, str) and triage_session_id.strip():
        fields.append({"id": f["triageSessionId"], "value": s(triage_session_id.strip()[:128])})
    if profile_id:
        fields.append({"id": "customer_id", "value": s(profile_arn(cfg, profile_id))})
    return fields


def find_or_create_case(cfg: dict[str, Any], budget: Budget, *, key: str, fields: list[dict[str, Any]]) -> str | None:
    """caseId for this escalation key, reusing before creating. Never raises.

    SearchCases on the escalationKey field first; else CreateCase with
    clientToken = key, which also covers a retry inside the search index's lag.
    A conflict on the token means another request created it: search again.
    """
    try:
        if not budget.post_chat_ok():
            _skip("case-search")
            return None
        cc = _client("connectcases", cfg["region"])
        found = _search_case(cc, cfg, key)
        if found:
            log.info("crm-case: found case=%s", found)
            return found
        if not budget.post_chat_ok():
            _skip("case-create")
            return None
        try:
            case_id = cc.create_case(
                domainId=cfg["casesDomainId"],
                templateId=cfg["casesTemplateId"],
                fields=fields,
                clientToken=key,
            )["caseId"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConflictException" and budget.post_chat_ok():
                found = _search_case(cc, cfg, key)
                log.info("crm-case: create conflicted; search found=%s", bool(found))
                return found
            raise
        log.info("crm-case: created case=%s", case_id)
        return case_id
    except Exception as e:  # noqa: BLE001
        log.warning("crm-case: failed (%s)", type(e).__name__)
        return None


def link_contact(cfg: dict[str, Any], budget: Budget, *, case_id: str, contact_id: str) -> bool:
    """Add the contact as a `Contact` related item of the case. Never raises."""
    try:
        if not budget.post_chat_ok():
            _skip("case-link")
            return False
        _client("connectcases", cfg["region"]).create_related_item(
            domainId=cfg["casesDomainId"],
            caseId=case_id,
            type="Contact",
            content={"contact": {"contactArn": contact_arn(cfg, contact_id)}},
        )
        log.info("crm-case: linked case=%s contact=%s", case_id, contact_id)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("crm-case: link failed (%s)", type(e).__name__)
        return False
