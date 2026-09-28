"""CMS active-DTC health source (spec 2026-09-25-avx-own-vehicle-findings, T4.2).

Turns the active diagnostic trouble codes CMS holds for a vehicle into health Findings, so a
fault on a demo vehicle (or one injected from the CMS web DTCs tab) becomes a card without a
script. Runs as the ``diagnostics`` Tier 2 component on a 5-minute schedule (T4.4).

Two rules are reused, never copied:

* **What counts as active** is ``RealCmsClient.get_active_dtcs`` (``lambdas/shared``), the
  same status rule CVX triage uses since ``a137e13``. The table scan here only discovers
  which vehicles to ask about; it never decides activeness.
* **What is P0** is the triage classifier's own ``_classify_dtcs``. A code it rates P0
  produces no Finding: P0 belongs to the classifier and the escalation card, narrated
  verbatim there, and a Finding restating it could soften "stop driving" (spec, deterministic
  seam; ``decisions.md`` P0 rule per code, user-confirmed 2026-09-26).

The text is deterministic. Recognising a fault is a fact from the vehicle; the urgency comes
from the classifier's level, not from a model. No LLM runs on this 5-minute path.

``inputs_hash`` covers the sorted set of active codes only, so a fault that stays active
hashes the same on every run and ``lastSeenAt``/``firstSeenAt`` churn cannot re-open a card.
Findings expire ``DIAGNOSTICS_TTL_SECONDS`` after their last write; the runner refreshes the
expiry of an unchanged Finding on every run, so a cleared fault's card expires within about
35 minutes.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import logging
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: A diagnostics Finding expires this long after its last write (spec D4, T4.2).
DIAGNOSTICS_TTL_SECONDS = 30 * 60

#: Classifier ``system`` → the health component a Finding is filed under. Anything not
#: listed is ``diagnostics`` (codes outside tires, brakes and battery; D3 sub-decision).
_COMPONENT_BY_SYSTEM = {
    "electrical": "battery",
    "hv_battery": "battery",
    "brakes": "brakes",
    "abs": "brakes",
    "tpms": "tires",
    "tires": "tires",
}

#: Classifier level → Finding ``severity_hint``. P0 never reaches this map.
_SEVERITY_BY_LEVEL_NAME = {"P1": "attention", "P2": "soon", "P3": "routine"}

_ADVICE_BY_SEVERITY = {
    "attention": "Book a service check soon.",
    "soon": "Have it looked at during your next service.",
    "routine": "No action is needed now; mention it at your next service.",
}

_LABEL_BY_COMPONENT = {
    "battery": "Electrical system",
    "brakes": "Brake system",
    "tires": "Tire system",
    "diagnostics": "Vehicle diagnostics",
}

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DiagnosticsReadError(RuntimeError):
    """Raised after ``fetch_groups`` when at least one vehicle's read failed.

    W4 fix (review Cycle 1): partial reads must surface as an error so the
    caller (``run_diagnostics``) can propagate it and alert ops.  A sustained
    per-VIN failure would otherwise silently expire cards without any Lambda
    error, leaving ``Tier2RunFailureAlarm`` dark.

    The error is raised at the END of the scan so every other vehicle's codes
    are still processed.  The partial ``groups`` list (for VINs that DID succeed)
    is carried on the exception so ``run_diagnostics`` can still write those cards
    before raising ``DiagnosticsRunError``.
    ``failed_vins`` holds the VIN of each vehicle whose DTC query failed, and the
    ``vehicleId`` of each vehicle whose registry read failed (its VIN is unknown then).
    """

    def __init__(self, failed_vins: set[str], groups: "list[Any]") -> None:
        self.failed_vins = failed_vins
        self.groups = groups
        count = len(failed_vins)
        super().__init__(f"{count} vehicle read(s) failed")


# ---------------------------------------------------------------------------
# Reuse of the classifier and the CMS client (no copies)
# ---------------------------------------------------------------------------

def _classifier():
    """``lambdas/triage-classifier`` as the ``triage_classifier`` package (hyphenated dir).

    The Tier 2 bundle copies ``lambdas/`` verbatim (``vsa-tier2-stack.ts`` bundling), so the
    directory keeps its hyphen; this registers it under the importable name once.
    """
    if "triage_classifier" not in sys.modules:
        root = _REPO_ROOT / "lambdas" / "triage-classifier"
        spec = importlib.util.spec_from_file_location(
            "triage_classifier", root / "__init__.py", submodule_search_locations=[str(root)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["triage_classifier"] = pkg
        spec.loader.exec_module(pkg)
    from triage_classifier import classifier  # noqa: E402
    from triage_classifier.types import ActiveDtc, Level  # noqa: E402

    return classifier, ActiveDtc, Level


def _cms_client_class():
    """``RealCmsClient`` from ``lambdas/shared/cms_client_real.py``."""
    _classifier()  # cms_client_real imports triage_classifier.types
    lambdas = str(_REPO_ROOT / "lambdas")
    if lambdas not in sys.path:
        sys.path.append(lambdas)
    from shared.cms_client_real import RealCmsClient  # noqa: E402

    return RealCmsClient


def _level_and_system(code: str) -> tuple[str, str]:
    """``(level_name, system)`` for *code* from the classifier's own rule table."""
    classifier, ActiveDtc, _ = _classifier()
    contributions = classifier._classify_dtcs((ActiveDtc(code=code, first_seen_at=""),))
    level, fragment = contributions[0]
    return level.name, str(fragment.extra.get("system", "unknown"))


# ---------------------------------------------------------------------------
# Public helpers (pinned by agents/health/tests/test_cms_dtc_source.py)
# ---------------------------------------------------------------------------

def component_for_code(code: str) -> str | None:
    """The component a Finding for *code* is filed under, or ``None`` when the classifier rates it P0."""
    level_name, system = _level_and_system(code)
    if level_name == "P0":
        return None
    return _COMPONENT_BY_SYSTEM.get(system, "diagnostics")


def inputs_hash_for(active_dtcs: Iterable[Any]) -> str:
    """SHA-256 over the sorted set of codes only; timestamps and order do not count."""
    codes = sorted({str(d.code) for d in active_dtcs})
    return hashlib.sha256(json.dumps(codes, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------

@dataclass
class DiagnosticsGroup:
    """One Finding's worth of input: a VIN, the component it is filed under, its codes."""

    vin: str
    component: str
    dtcs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inputs_hash(self) -> str:
        return inputs_hash_for(_Code(d["code"]) for d in self.dtcs)

    def agent_fields(self) -> dict[str, Any]:
        """Deterministic Finding fields (``finding``, ``headline``, severity, evidence)."""
        return build_fields(self.component, self.dtcs)


@dataclass(frozen=True)
class _Code:
    code: str


class _QueryErrorRecorder:
    """A boto3 DynamoDB resource wrapper that records ``query`` exceptions.

    ``RealCmsClient.get_active_dtcs`` catches a failed partition query and returns the
    rows it already had (right for triage, where a partial list beats none). Here a short
    list would let a real fault's card expire with no alarm, so the source reads the
    recorded errors after each call and treats that vehicle as failed.
    """

    def __init__(self, resource: Any) -> None:
        self._resource = resource
        self.errors: list[Exception] = []

    def Table(self, name: str) -> Any:  # noqa: N802 - boto3 resource API
        return _RecordingTable(self._resource.Table(name), self.errors)


class _RecordingTable:
    def __init__(self, table: Any, errors: list[Exception]) -> None:
        self._table = table
        self._errors = errors

    def query(self, **kwargs: Any) -> Any:
        try:
            return self._table.query(**kwargs)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            self._errors.append(exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)


class CmsDtcSource:
    """Every vehicle with an active non-P0 DTC, grouped into Findings.

    Parameters
    ----------
    dtc_table:
        ``cms-{stage}-storage-dtc-history`` (fully resolved by the stack).
    vehicles_table:
        ``cms-{stage}-storage-vehicles``, the CMS registry. A Finding's VIN is the
        registry's ``vin`` for the vehicle, never a DTC row's own ``vin`` attribute: on
        staging that attribute is stale, empty or absent on most rows (issue
        ``2026-09-28-diagnostics-vin-taken-from-dtc-rows``), and the owner resolver and
        ``/findings/me`` key standing on the registry VIN.
    ddb_client:
        boto3 low-level DynamoDB client, for the discovery scan and the registry reads.
    cms_client:
        Anything with ``get_active_dtcs(vin) -> tuple[ActiveDtc, ...]``; defaults to
        ``RealCmsClient`` over *ddb_resource*.
    """

    def __init__(self, *, dtc_table: str, vehicles_table: str, ddb_client: Any,
                 cms_client: Any = None, ddb_resource: Any = None) -> None:
        if not vehicles_table:
            raise ValueError("vehicles_table is required: the registry is the VIN of record")
        self._table = dtc_table
        self._vehicles_table = vehicles_table
        self._ddb = ddb_client
        self._recorder: _QueryErrorRecorder | None = None
        if cms_client is None:
            if ddb_resource is None:
                import boto3

                ddb_resource = boto3.resource("dynamodb")
            # get_active_dtcs catches its own query errors and returns what it read;
            # the recorder sees them, so a failed read is reported, not silently short.
            self._recorder = _QueryErrorRecorder(ddb_resource)
            cms_client = _cms_client_class()(ddb_resource=self._recorder)
        self._cms = cms_client

    def _scan_vehicles(self) -> tuple[set[str], dict[tuple[str, str], str]]:
        """``{vehicleId}`` and ``{(vehicleId, code): description}`` from a full table scan.

        Discovery only: which vehicles have DTC rows. The rows' ``vin`` attribute is not
        read; ``_registry_vins`` supplies the VIN. Raises on any page failure: a partial
        vehicle list would read as "those vehicles have no faults" and let their cards expire.
        """
        vehicle_ids: set[str] = set()
        newest: dict[tuple[str, str], tuple[int, str]] = {}
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "ProjectionExpression": "vehicleId, code, description, #ts",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
        }
        while True:
            resp = self._ddb.scan(**kwargs)
            for item in resp.get("Items", []):
                vid = item.get("vehicleId", {}).get("S")
                if not vid:
                    continue
                vehicle_ids.add(vid)
                code = item.get("code", {}).get("S")
                desc = item.get("description", {}).get("S", "")
                try:
                    ts = int(item.get("timestamp", {}).get("N", "0"))
                except ValueError:
                    ts = 0
                if code and desc and ts >= newest.get((vid, code), (-1, ""))[0]:
                    newest[(vid, code)] = (ts, desc)
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return vehicle_ids, {k: v[1] for k, v in newest.items()}

    def _registry_vins(self, vehicle_ids: Iterable[str]) -> tuple[dict[str, str], set[str]]:
        """``{vehicleId: registry vin}``, and the vehicleIds whose registry read failed.

        One ``GetItem`` per vehicle on the registry (key ``vehicleId``), then one ``vin-index``
        Query for that VIN, every page. A vehicle is kept only when the index holds exactly
        one vehicle for its VIN and that vehicle is this one: the owner resolver later takes
        the index's first item for the VIN, so a VIN held by two registry vehicles could
        project the other vehicle's owner onto this vehicle's faults. The check covers
        registered vehicles with no DTC rows, which a comparison among the scanned vehicles
        would miss (review G7 cycle 1, C1).

        A vehicle with no registry item, or no ``vin`` on it, is skipped: no party has
        standing on it, so a Finding for it is unreadable. A failed ``GetItem`` or index
        Query fails the vehicle, and the run reports it after the other vehicles.
        Logs carry counts and error types only, never a VIN or vehicleId.
        """
        by_vehicle: dict[str, str] = {}
        failed: set[str] = set()
        unregistered = 0
        not_unique = 0
        for vid in sorted(vehicle_ids):
            try:
                resp = self._ddb.get_item(
                    TableName=self._vehicles_table,
                    Key={"vehicleId": {"S": vid}},
                    ProjectionExpression="#vin",
                    ExpressionAttributeNames={"#vin": "vin"},
                )
            except Exception as exc:  # noqa: BLE001 - reported with the run, not swallowed
                log.warning("cms_dtc_source: event=registry_read_failed error=%s", type(exc).__name__)
                failed.add(vid)
                continue
            vin = str((resp.get("Item") or {}).get("vin", {}).get("S", "")).strip()
            if not vin:
                unregistered += 1
                continue
            try:
                holders = self._vin_holders(vin)
            except Exception as exc:  # noqa: BLE001 - reported with the run, not swallowed
                log.warning("cms_dtc_source: event=registry_vin_index_failed error=%s", type(exc).__name__)
                failed.add(vid)
                continue
            if holders != [vid]:
                not_unique += 1
                continue
            by_vehicle[vid] = vin
        if not_unique:
            log.warning("cms_dtc_source: event=registry_vin_not_unique vehicles_skipped=%d", not_unique)
        if unregistered:
            log.info("cms_dtc_source: event=unregistered_vehicles_skipped count=%d", unregistered)
        return by_vehicle, failed

    def _vin_holders(self, vin: str) -> list[str]:
        """Every vehicleId the registry's ``vin-index`` holds for *vin*, across all pages."""
        holders: list[str] = []
        kwargs: dict[str, Any] = {
            "TableName": self._vehicles_table,
            "IndexName": "vin-index",
            "KeyConditionExpression": "#vin = :v",
            "ExpressionAttributeNames": {"#vin": "vin"},
            "ExpressionAttributeValues": {":v": {"S": vin}},
        }
        while True:
            resp = self._ddb.query(**kwargs)
            holders.extend(item.get("vehicleId", {}).get("S", "") for item in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return holders
            kwargs["ExclusiveStartKey"] = last

    def _seed_vin_cache(self, vehicle_id_to_vin: dict[str, str]) -> None:
        """Pre-populate the CMS client's VIN→vehicleId cache from the registry mapping.

        ``_registry_vins`` already resolved each vehicleId to its registry VIN; seeding the
        cache avoids a redundant vin-index GSI query (or fallback Scan on storage-vehicles)
        for every VIN on the active-DTC read path.

        W4 fix (review Cycle 1): both the per-VIN vin-index query and the ungranted
        fallback Scan on ``storage-vehicles`` are eliminated for every VIN seen in the
        scan; the role only holds ``CmsVehiclesVinIndexQuery`` so the fallback Scan
        would fail with AccessDeniedException.
        """
        cache = getattr(self._cms, "_vin_to_vehicle_id", None)
        if cache is None:
            # Initialise if the client has the attribute but it's still None.
            try:
                self._cms._vin_to_vehicle_id = {}
                cache = self._cms._vin_to_vehicle_id
            except AttributeError:
                return  # stub client; skip seeding
        for vehicle_id, vin in vehicle_id_to_vin.items():
            cache.setdefault(vin, vehicle_id)

    def fetch_groups(self) -> list[DiagnosticsGroup]:
        """One group per (VIN, component) with at least one active non-P0 code.

        Raises
        ------
        DiagnosticsReadError
            If at least one VIN's DTC query, or one vehicle's registry read, raised an
            exception.  The error is raised at the END of the scan so every other vehicle
            is still processed.
        """
        vehicle_ids, descriptions_by_vehicle = self._scan_vehicles()
        vins, failed_vehicles = self._registry_vins(vehicle_ids)
        descriptions = {
            (vins[vid], code): desc
            for (vid, code), desc in descriptions_by_vehicle.items()
            if vid in vins
        }

        # W4: seed the VIN→vehicleId cache from the registry mapping so no redundant
        # vin-index query or ungranted fallback Scan runs per VIN.
        self._seed_vin_cache(vins)

        groups: dict[tuple[str, str], DiagnosticsGroup] = {}
        failed_vins: set[str] = set(failed_vehicles)

        for vin in sorted(set(vins.values())):
            if self._recorder is not None:
                self._recorder.errors.clear()
            try:
                dtcs = self._cms.get_active_dtcs(vin)
                if self._recorder is not None and self._recorder.errors:
                    raise self._recorder.errors[0]
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "cms_dtc_source: event=get_active_dtcs_failed vin_hash=%s error=%s",
                    vin[:4] + "…",
                    type(exc).__name__,
                )
                failed_vins.add(vin)
                continue

            for dtc in dtcs:
                component = component_for_code(dtc.code)
                if component is None:
                    continue  # P0: the escalation card's, never a Finding
                level_name, system = _level_and_system(dtc.code)
                key = (vin, component)
                groups.setdefault(key, DiagnosticsGroup(vin=vin, component=component)).dtcs.append({
                    "code": dtc.code,
                    "first_seen_at": _iso_date(dtc.first_seen_at),
                    "description": descriptions.get((vin, dtc.code), ""),
                    "system": system,
                    "level": level_name,
                })

        for g in groups.values():
            g.dtcs.sort(key=lambda d: d["code"])
        result = [groups[k] for k in sorted(groups)]

        if failed_vins:
            raise DiagnosticsReadError(failed_vins, result)
        return result


# ---------------------------------------------------------------------------
# Deterministic text
# ---------------------------------------------------------------------------

def _iso_date(value: Any) -> str:
    """Epoch ms/s or ISO string → ``YYYY-MM-DD`` (UTC); empty when unparseable."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.isdigit():
        n = int(s)
        seconds = n / 1000 if n > 10**11 else n
        try:
            return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10] if len(s) >= 10 and s[4] == "-" else ""


def _describe(d: dict[str, Any]) -> str:
    return d["description"].strip() or f"{d['system'].replace('_', ' ')} system fault"


def build_fields(component: str, dtcs: list[dict[str, Any]]) -> dict[str, Any]:
    """Finding text, severity and evidence for one (VIN, component) group."""
    worst = min(dtcs, key=lambda d: int(d["level"][1:]))["level"]
    severity = _SEVERITY_BY_LEVEL_NAME[worst]
    advice = _ADVICE_BY_SEVERITY[severity]
    label = _LABEL_BY_COMPONENT.get(component, "Vehicle diagnostics")

    def _since(d: dict[str, Any]) -> str:
        return f", first reported {d['first_seen_at']}" if d["first_seen_at"] else ""

    if len(dtcs) == 1:
        d = dtcs[0]
        headline = f"{label}: fault code {d['code']} is active"
        finding = f"Fault code {d['code']} ({_describe(d)}) is active on this vehicle{_since(d)}. {advice}"
    else:
        codes = ", ".join(d["code"] for d in dtcs)
        headline = f"{label}: {len(dtcs)} fault codes are active"
        lines = "\n".join(f"- {d['code']}: {_describe(d)}{_since(d)}" for d in dtcs)
        finding = f"{len(dtcs)} fault codes are active on this vehicle ({codes}).\n{lines}\n{advice}"

    evidence = [
        {
            "source": "cms.dtc_history",
            "kind": "cms.dtc.active",
            "source_ref": f"dtc:{d['code']}",
            "value": {"code": d["code"], "status": "ACTIVE", "first_seen_at": d["first_seen_at"],
                      "classifier_level": d["level"], "system": d["system"]},
            "provenance": "live",
        }
        for d in dtcs
    ]
    return {
        "finding": finding,
        "headline": headline[:120],
        "severity_hint": severity,
        "confidence": "high",
        "evidence": evidence,
    }
