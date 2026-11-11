"""Warranty finding writer for the FI recall and warranty agent (spec D10).

Spec source: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/spec.md D10
Contract: docs/fi-recall-warranty-contract.md § 2

Public API
----------
build_recommended_action(...) -> dict
    Build the recommended_action dict; estimated_value_usd always from DMS.

build_finding(...) -> dict | None
    Build a full Finding dict; None for denied lines.
"""
from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

from agents.fi_recall_warranty.writer import AGENT_ID, AGENT_VERSION, _iso_add_days
from agents.tier2.writer import compute_finding_key

#: Finding kind for all warranty Findings.
FINDING_KIND: str = "fleet.warranty.claim_candidate"

#: TTL in days (contract § 2.8).
_EXPIRY_DAYS: int = 3

#: Time reserve for model calls (seconds); default for model_call_reserve_s.
MODEL_CALL_RESERVE_S: float = 150.0

#: Severity threshold — recoverable_usd >= this -> "soon" (overridable).
SOON_RECOVERABLE_USD: float = 1_000.00

#: Severity threshold — filing deadline within this many days -> "soon" (overridable).
SOON_DEADLINE_DAYS: int = 7

#: Fixed sentence appended to disclosure_txt per contract § 2.5.
_DISCLOSURE_SUFFIX = "DMS decides coverage when the claim is filed."

class _NoTermsError(Exception):
    """A candidate row whose coverage group has no current DMS terms row (refused, § 2.5)."""


#: Miles ← km conversion factor (1 mile = 1.609344 km exactly).
_KM_TO_MILES: float = 1.609344

#: Human-readable labels for DMS coverage_group keys (for term_statement, § 2.5).
_GROUP_LABEL: dict[str, str] = {
    "basic": "Basic",
    "hv_battery": "High-voltage battery",
    "drive_unit": "Drive unit",
}


# ---------------------------------------------------------------------------
# 0. _parse_deadline  (private)
# ---------------------------------------------------------------------------

def _parse_deadline(s: str) -> datetime.datetime:
    """Parse a DMS ``filing_deadline`` to a UTC-aware datetime.

    Date-only ``YYYY-MM-DD`` (DMS § 2.2 shape) is end of day UTC (23:59:59Z).
    Full ISO 8601 with ``Z`` or ``+00:00`` is parsed as-is.

    Raises ``ValueError`` for any unrecognised shape so callers can count the
    row as refused rather than falling through to ``routine``.
    """
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        d = datetime.date.fromisoformat(s)  # raises ValueError on bad date
        return datetime.datetime(d.year, d.month, d.day, 23, 59, 59,
                                 tzinfo=datetime.timezone.utc)
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"filing_deadline has no timezone info: {s!r}")
    return dt



# ---------------------------------------------------------------------------
# 1. build_recommended_action
# ---------------------------------------------------------------------------

def build_recommended_action(
    *,
    dms_recoverable_usd: float,
    model_value_usd: float,  # accepted but NEVER used for estimated_value_usd
    ro_id: str,
    line_no: str,
    vin: str,
    dealer_id: str,
    labor_op: str,
    fault_code: Optional[str] = None,
    notes: str = "",
    estimated_value_basis: str = "",
) -> dict[str, Any]:
    """Build recommended_action for a warranty claim candidate (contract § 2.7).

    estimated_value_usd is always DMS's recoverable_usd; model_value_usd is
    accepted in the signature so callers can pass both, but it is never read.

    Mutation guard: wiring to model_value_usd makes
    test_estimated_value_mutation_wired_to_model_output_fails fail (it asserts
    action["estimated_value_usd"] != 9_999.99 when model_value_usd=9_999.99).
    """
    params: dict[str, Any] = {
        "vehicle_vin": vin,
        "ro_id": ro_id,
        "line_no": line_no,
        "dealer_id": dealer_id,
        "labor_op": labor_op,
    }
    if fault_code:
        params["fault_code"] = fault_code
    if notes:
        params["notes"] = notes[:1_000]

    action: dict[str, Any] = {
        "action_kind": "submit_warranty_claim",
        "target_system": "dms",
        "estimated_value_usd": dms_recoverable_usd,  # ← always DMS, never model
        "estimated_value_basis": estimated_value_basis,
        "parameters": params,
    }
    return action



# ---------------------------------------------------------------------------
# 2. _compute_severity
# ---------------------------------------------------------------------------

def _compute_severity(
    dms_recoverable_usd: float,
    filing_deadline: Optional[str],
    computed_at: str,
    *,
    soon_recoverable: float = SOON_RECOVERABLE_USD,
    soon_deadline_days: int = SOON_DEADLINE_DAYS,
) -> str:
    """Return "soon" when recoverable >= threshold or deadline is near; else "routine".

    Mutation guard: using > instead of >= makes
    test_severity_soon_at_recoverable_threshold fail (passes 1000.00 and expects "soon").
    """
    if dms_recoverable_usd >= soon_recoverable:
        return "soon"
    if filing_deadline:
        dt_computed = datetime.datetime.fromisoformat(
            computed_at.replace("Z", "+00:00")
        )
        # _parse_deadline raises ValueError for unparseable input — propagates to loop.
        dt_deadline = _parse_deadline(filing_deadline)
        days_left = (dt_deadline - dt_computed).days
        if days_left <= soon_deadline_days:
            return "soon"
    return "routine"



# ---------------------------------------------------------------------------
# 3. _compute_expires_at
# ---------------------------------------------------------------------------

def _compute_expires_at(computed_at: str, filing_deadline: Optional[str]) -> str:
    """Return the earlier of computed_at + 3 days and the filing deadline."""
    plus3 = _iso_add_days(computed_at, _EXPIRY_DAYS)
    if not filing_deadline:
        return plus3
    dt_plus3 = datetime.datetime.fromisoformat(plus3.replace("Z", "+00:00"))
    # _parse_deadline raises ValueError for unparseable input — propagates to loop.
    dt_deadline = _parse_deadline(filing_deadline)
    if dt_plus3 <= dt_deadline:
        return plus3
    return dt_deadline.isoformat(timespec="seconds").replace("+00:00", "Z")



# ---------------------------------------------------------------------------
# 4. build_finding
# ---------------------------------------------------------------------------

def build_finding(
    *,
    verdict: str,
    model_result: Optional[dict[str, Any]],
    ro_id: str,
    line_no: str,
    vin: str,
    dms_recoverable_usd: float,
    model_value_usd: float = 0.0,
    labor_op: str = "",
    dealer_id: Optional[str] = None,
    fault_code: Optional[str] = None,
    notes: str = "",
    term_statement: str = "",
    estimated_value_basis: str = "",
    computed_at: str = "2024-01-01T00:00:00Z",
    filing_deadline: Optional[str] = None,
    evidence: Optional[list[dict[str, Any]]] = None,
    confidence: str = "medium",
    inputs_hash: Optional[str] = None,
    detail: str = "",
) -> Optional[dict[str, Any]]:
    """Build a Finding for a warranty candidate line, or None for denied.

    Rules (contract § 2.2):
    - verdict == "denied" -> return None (no Finding).
    - model_result["proceed"] == "insufficient_evidence" -> informational Finding,
      no recommended_action, regardless of verdict.
    - Otherwise ("approved" or "requires_review") -> Finding with recommended_action.

    line_no must be a string (architect call G3-6: stringify DMS's int before passing).

    Mutation guard: returning a Finding for a denied line makes
    test_denied_line_produces_no_finding fail.

    Mutation guard: leaving recommended_action on an insufficient_evidence result
    makes test_insufficient_evidence_produces_item_with_no_recommended_action fail.
    """
    if verdict == "denied":
        return None  # ← denied → no Finding

    proceed = (model_result or {}).get("proceed", "")
    insufficient = proceed == "insufficient_evidence"

    subject_key = f"{ro_id}/{line_no}"  # line_no is already a string

    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )

    severity = _compute_severity(
        dms_recoverable_usd,
        filing_deadline,
        computed_at,
    )

    # Headline (contract § 2.4).
    if insufficient:
        headline = f"Warranty: insufficient evidence — {labor_op}"
    else:
        headline = f"Warranty claim candidate: {verdict} — {labor_op}"

    # disclosure_txt (contract § 2.5).
    if term_statement:
        disclosure = f"{term_statement} {_DISCLOSURE_SUFFIX}"
    else:
        disclosure = _DISCLOSURE_SUFFIX

    expires_at = _compute_expires_at(computed_at, filing_deadline)

    _inputs_hash = inputs_hash or ("0" * 64)

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": confidence,
        "finding_kind": FINDING_KIND,
        "headline": headline,
        "detail": detail,
        "evidence": evidence if evidence is not None else [],
        "inputs_hash": _inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        "disclosure_txt": disclosure,
    }

    # recommended_action only when not insufficient_evidence (contract § 2.7).
    if not insufficient:
        finding["recommended_action"] = build_recommended_action(
            dms_recoverable_usd=dms_recoverable_usd,
            model_value_usd=model_value_usd,
            ro_id=ro_id,
            line_no=line_no,
            vin=vin,
            dealer_id=dealer_id or "",
            labor_op=labor_op,
            fault_code=fault_code,
            notes=notes,
            estimated_value_basis=estimated_value_basis,
        )

    return finding



# ---------------------------------------------------------------------------
# 5. _to_schema_ev  (private — converts domain entries to schema-entry format)
# ---------------------------------------------------------------------------

def _to_schema_ev(raw_entries: list[dict], computed_at: str) -> list[dict]:
    """Convert domain-key evidence entries to finding.schema.json evidence_entry format."""
    _meta_keys = frozenset({"vin", "provenance"})
    result: list[dict] = []
    for entry in raw_entries:
        domain_keys = [k for k in entry if k not in _meta_keys]
        if not domain_keys:
            continue
        kind = domain_keys[0]
        value = entry[kind]
        schema_entry: dict[str, Any] = {
            "kind": kind,
            "source": (
                "agent-fi-recall-warranty" if "diagnosis_source" in kind else "dms"
            ),
            "source_ref": "fi-recall-warranty-run",
            "value": value,
            "computed_at": computed_at,
            "provenance": entry.get("provenance", "live"),
        }
        if "vin" in entry:
            schema_entry["vin"] = entry["vin"]
        result.append(schema_entry)
    return result


# ---------------------------------------------------------------------------
# 6. _skip_unchanged_warranty  (private)
# ---------------------------------------------------------------------------

def _skip_unchanged_warranty(live: Optional[dict], inputs_hash: str) -> bool:
    """Skip rule (spec D8 step 4): the ONE shared predicate, runner.skip_unchanged."""
    from agents.fi_recall_warranty.runner import skip_unchanged
    return skip_unchanged(live, inputs_hash, source_kind="warranty.diagnosis_source")


# ---------------------------------------------------------------------------
# 7. _rules_narrative  (private)
# ---------------------------------------------------------------------------

def _rules_narrative(row: dict) -> str:
    """Fixed narrative built only from the row's own fields (at most 1000 chars).

    Mutation guard (d): removing [:1_000] lets a long narrative through,
    making test_narrative_truncated_to_1000 fail.
    """
    labor_op = row.get("labor_op") or ""
    repair_date = row.get("repair_date") or ""
    coverage_group = row.get("coverage_group") or ""
    verdict_status = row.get("verdict_status") or ""
    verdict_reason = row.get("verdict_reason") or ""
    recoverable_usd = row.get("recoverable_usd") or ""
    text = (
        f"Warranty claim: {labor_op} on {repair_date}. "
        f"Coverage: {coverage_group}. "
        f"Verdict: {verdict_status} \u2014 {verdict_reason}. "
        f"Recoverable: ${recoverable_usd}."
    )
    return text[:1_000]


# ---------------------------------------------------------------------------
# 7b. _build_refresh_plan_w  (private)
# ---------------------------------------------------------------------------

def _build_refresh_plan_w(live: dict) -> dict:
    """Extract warranty model content from a live Finding for the refresh_from_live path.

    Returns notes (from recommended_action.parameters.notes), detail, confidence,
    and proceed (file when a recommended_action is present, else insufficient_evidence).
    """
    params = ((live.get("recommended_action") or {}).get("parameters") or {})
    return {
        "notes": params.get("notes", ""),
        "detail": live.get("detail", ""),
        "confidence": live.get("confidence", "low"),
        "proceed": "file" if live.get("recommended_action") else "insufficient_evidence",
    }


# ---------------------------------------------------------------------------
# 7c. _write_w_item  (private)
# ---------------------------------------------------------------------------

def _write_w_item(
    item: dict,
    *,
    notes: str,
    model_result: dict,
    detail: str = "",
    confidence: str = "low",
    write_finding: Callable,
    computed_at: str,
    stats: dict,
    refresh_on_noop: bool = True,
    update_on_same_hash: bool = False,
    count_as_written: bool = True,
    diagnosis_source: Optional[str] = None,
) -> bool:
    """Build and write one warranty Finding from a decided item dict.

    Returns True on success (finding written), False on error (counted in rows_refused).
    Mutation guard (b): model_value_usd=0.0 ensures DMS recoverable_usd is always used.
    Mutation guard (c): standing comes from item["standing"] (set by standing_fn in decide).
    When diagnosis_source is provided the evidence is rebuilt with that value so the
    Finding records the correct source (as recall's _write_decided_item does).
    """
    # C2: rebuild evidence with the plan's diagnosis_source at write time so a
    # model-written Finding carries warranty.diagnosis_source=model, enabling the
    # day-2 skip rule (D8 step 4).  Hash is unchanged because warranty.diagnosis_source
    # is in _HASH_EXCLUDED.
    if diagnosis_source is not None:
        raw_ev = build_warranty_evidence(item["row"], diagnosis_source,
                                         warranty_terms_row=item.get("terms_row"))
        schema_ev = _to_schema_ev(raw_ev, computed_at)
    else:
        schema_ev = item["schema_ev"]
    try:
        finding = build_finding(
            verdict=item["row"].get("verdict_status", ""),
            model_result=model_result,
            ro_id=item["row"].get("ro_id", ""),
            line_no=str(item["row"].get("line_no", "")),
            vin=item["vin"],
            dms_recoverable_usd=item["recoverable_usd"],
            model_value_usd=0.0,
            labor_op=item["row"].get("labor_op") or "",
            dealer_id=item["row"].get("dealer_id"),
            fault_code=item["row"].get("fault_code"),
            notes=notes,
            term_statement=item["term_statement"],
            estimated_value_basis=item["basis"],
            computed_at=computed_at,
            filing_deadline=item["row"].get("filing_deadline"),
            evidence=schema_ev,
            confidence=confidence,
            inputs_hash=item["inputs_hash"],
            detail=detail,
        )
    except (ValueError, KeyError, TypeError) as exc:
        log.warning(
            "warranty_loop: build_finding failed ro_id=%s error=%s",
            item["row"].get("ro_id"), type(exc).__name__,
        )
        stats["rows_refused"] += 1
        return False
    if finding is None:
        stats["rows_refused"] += 1
        return False
    if update_on_same_hash:
        write_finding(finding, item["standing"], update_on_same_hash=True)
    else:
        write_finding(finding, item["standing"], refresh_on_noop=refresh_on_noop)
    if count_as_written:
        stats["items_written"] += 1
    return True


# ---------------------------------------------------------------------------
# 8. build_warranty_evidence
# ---------------------------------------------------------------------------

def build_warranty_evidence(
    row: dict[str, Any],
    diagnosis_source: str,
    *,
    warranty_terms_row: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Build §2.6 evidence entries for a warranty candidate row (rules path).

    warranty.fault_history and warranty.telemetry are model-path context
    (task 4.1) and are omitted here.

    Each entry carries vin and provenance: live.
    Hash with evidence.compute_inputs_hash; volatile fields excluded per G3-11.
    """
    vin: str = row.get("vehicle_vin", "")
    _base: dict[str, Any] = {"vin": vin, "provenance": "live"}
    entries: list[dict[str, Any]] = []

    entries.append({
        **_base,
        "warranty.verdict": {
            "verdict": row.get("verdict_status"),
            "reason": row.get("verdict_reason"),
            "matched_rule_id": row.get("matched_rule_id"),
            "terms_version": row.get("terms_version"),
        },
    })
    entries.append({
        **_base,
        "warranty.ro_lines": {
            "line_no": row.get("line_no"),
            "labor_op": row.get("labor_op"),
            "fault_code": row.get("fault_code"),
        },
    })

    # odometer_at_repair in miles per contract § 2.6
    odometer_raw = row.get("odometer_km")
    odometer_miles: Optional[int] = (
        round(float(odometer_raw) / _KM_TO_MILES) if odometer_raw is not None else None
    )
    cov: dict[str, Any] = {
        "coverage_group": row.get("coverage_group"),
        "in_service_date": row.get("in_service_date"),
        "odometer_at_repair": odometer_miles,  # miles per contract § 2.6
    }
    if warranty_terms_row:
        term_years = warranty_terms_row.get("term_years")
        term_miles = warranty_terms_row.get("term_miles")
        isd = row.get("in_service_date")
        if isd and term_years is not None:
            try:
                d = datetime.date.fromisoformat(isd)
                try:
                    ends = datetime.date(d.year + int(term_years), d.month, d.day)
                except ValueError:
                    # 29 Feb + N years into a non-leap year: 28 Feb (a leap target year
                    # keeps 29 Feb, as DMS computes it; cycle 3 agent Suggestion).
                    ends = datetime.date(d.year + int(term_years), d.month, 28)
                cov["term_ends_on"] = ends.isoformat()
            except (ValueError, OverflowError):
                pass
        if term_miles is not None and odometer_miles is not None:
            cov["term_miles_left"] = max(0, int(term_miles) - odometer_miles)
    entries.append({**_base, "warranty.coverage": cov})

    rec: dict[str, Any] = {
        "recoverable_usd": row.get("recoverable_usd"),
        "terms_version": row.get("terms_version"),
    }
    if warranty_terms_row:
        rec["labor_rate"] = warranty_terms_row.get("labor_rate_usd")
    entries.append({**_base, "warranty.recovery": rec})

    # volatile — excluded from hash via _HASH_EXCLUDED in evidence.py
    entries.append({
        **_base,
        "warranty.diagnosis_source": diagnosis_source,
        "diagnosis_source": diagnosis_source,
    })
    return entries


# ---------------------------------------------------------------------------
# 9. run_warranty_loop
# ---------------------------------------------------------------------------

def run_warranty_loop(
    *,
    candidate_rows: list[dict[str, Any]],
    computed_at: str,
    standing_fn: Callable[[str], Any],
    lookup_live: Callable[[str], Optional[dict]],
    write_finding: Callable[[dict, Any], Any],
    plan_fn: Optional[Callable] = None,
    warranty_terms: Optional[dict[str, dict]] = None,
    model_call_cap: Optional[int] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
    model_call_reserve_s: float = MODEL_CALL_RESERVE_S,
) -> dict[str, Any]:
    """Decide/select/write run loop for warranty candidates (spec D10, task 4.1 S3a).

    For each non-denied row: build evidence, compute hash, apply skip/refresh rule,
    build finding with diagnosis_source=rules/confidence=low, write_finding.
    Denied rows get no write.  Bad rows are skipped and counted; reader/writer
    errors propagate unchanged.

    ``plan_fn(row=, evidence=)`` → ``{narrative, proceed, detail, confidence,
    diagnosis_source}``; None → rules path (unchanged).  ``narrative`` (≤ 1 000 chars)
    becomes the finding's notes field.  ``proceed`` chooses the item kind
    (insufficient_evidence → informational).  ``estimated_value_usd``, verdict word,
    severity and ``disclosure_txt`` are never read from the plan.

    ``warranty_terms``: optional dict of ``{coverage_group: terms_row}`` from
    ``selection.select_warranty_terms``; used to build the term statement (§ 2.5),
    estimated_value_basis (§ 2.7), and evidence fields (§ 2.6).

    Mutation guard (a): skipping denied check → test_denied_produces_no_write.
    Mutation guard (b): using plan value for usd → test_estimated_value_equals_recoverable_usd.
    Mutation guard (c): omitting standing → test_standing_passed_verbatim.
    Mutation guard (d): removing narrative truncation → test_narrative_truncated_to_1000.
    """
    from agents.tier2.writer import compute_finding_key
    from agents.fi_recall_warranty import evidence as _evidence

    stats: dict[str, Any] = {
        "rows": 0,
        "items_written": 0,
        "items_skipped_unchanged": 0,
        "rows_refused": 0,
        "rows_without_terms": 0,
        "model_items": 0,
        "deferred": 0,
        "items_refreshed_from_live": 0,
    }

    # Phase 1: Decide — classify all rows without writing
    all_items: list[dict] = []
    for row in candidate_rows:
        stats["rows"] += 1
        vin: str = row.get("vehicle_vin", "")
        verdict: str = row.get("verdict_status", "")

        # denied → no finding (mutation guard a)
        if verdict == "denied":
            stats["rows_refused"] += 1
            continue

        try:
            recoverable_usd = float(row.get("recoverable_usd") or 0.0)
            terms_row = (warranty_terms or {}).get(row.get("coverage_group") or "")
            if terms_row is None and row.get("coverage_group"):
                raise _NoTermsError("no current DMS warranty_terms row for coverage_group")
            raw_ev = build_warranty_evidence(row, "rules", warranty_terms_row=terms_row)
        except _NoTermsError as exc:
            log.warning("warranty_loop: refusing row ro_id=%s: %s", row.get("ro_id"), exc)
            stats["rows_refused"] += 1
            stats["rows_without_terms"] += 1
            continue
        except (ValueError, KeyError) as exc:
            log.warning(
                "warranty_loop: skipping bad row ro_id=%s error=%s",
                row.get("ro_id"), type(exc).__name__,
            )
            stats["rows_refused"] += 1
            continue

        inputs_hash = _evidence.compute_inputs_hash(raw_ev)
        schema_ev = _to_schema_ev(raw_ev, computed_at)
        subject_key = f"{row.get('ro_id', '')}/{row.get('line_no', '')}"
        finding_key = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value=vin,
            finding_kind=FINDING_KIND, subject_key=subject_key,
        )
        live = lookup_live(finding_key)

        path = "refresh_from_live" if _skip_unchanged_warranty(live, inputs_hash) else "eligible"
        same_hash_rules = (
            path == "eligible"
            and live is not None
            and live.get("inputs_hash") == inputs_hash
        )
        try:
            severity = _compute_severity(recoverable_usd, row.get("filing_deadline"), computed_at)
        except ValueError as exc:
            log.warning("warranty_loop: bad deadline ro_id=%s error=%s",
                        row.get("ro_id"), exc)
            stats["rows_refused"] += 1
            continue

        term_statement = ""
        if terms_row:
            ty = terms_row.get("term_years")
            tm = terms_row.get("term_miles")
            grp = terms_row.get("coverage_group", "")
            label = _GROUP_LABEL.get(grp, grp.replace("_", " ").title())
            if ty is not None and tm is not None:
                term_statement = f"{label} warranty: {ty} years / {tm:,} miles"
            rate = terms_row.get("labor_rate_usd", 0)
            tv = terms_row.get("terms_version") or row.get("terms_version") or ""
            basis = f"recoverable_usd={recoverable_usd}; labor_rate_usd={rate}; terms {tv}"
        else:
            terms_version = row.get("terms_version") or ""
            basis = f"recoverable_usd={recoverable_usd} (terms {terms_version})"

        all_items.append({
            "vin": vin, "row": row, "subject_key": subject_key,
            "finding_key": finding_key, "path": path, "same_hash_rules": same_hash_rules,
            "live": live, "inputs_hash": inputs_hash, "schema_ev": schema_ev,
            "standing": standing_fn(vin), "recoverable_usd": recoverable_usd,
            "terms_row": terms_row, "term_statement": term_statement, "basis": basis,
            "finding_kind": FINDING_KIND, "severity": severity,
        })

    # Phase 2: Select — cap eligible items for model calls
    selected: list[dict] = []
    if plan_fn is not None:
        from agents.fi_recall_warranty.runner import WARRANTY_CAP, apply_cap
        cap = model_call_cap if model_call_cap is not None else WARRANTY_CAP
        eligible = [x for x in all_items if x["path"] == "eligible"]
        eligible_sorted = sorted(eligible, key=lambda x: (x["vin"], x["subject_key"]))
        selected = apply_cap(eligible_sorted, recall_cap=0, warranty_cap=cap)
    selected_keys = {x["finding_key"] for x in selected}

    # Phase 3a: Write non-selected items (refresh_from_live = skip for now)
    for item in all_items:
        path = item["path"]
        if path == "eligible" and item["finding_key"] in selected_keys:
            continue  # reserved for phase 3b

        if path == "refresh_from_live":
            rp = _build_refresh_plan_w(item["live"])
            ok = _write_w_item(
                item, notes=rp["notes"], model_result={"proceed": rp["proceed"]},
                detail=rp["detail"], confidence=rp["confidence"],
                write_finding=write_finding, computed_at=computed_at, stats=stats,
                refresh_on_noop=True, count_as_written=False, diagnosis_source="model",
            )
            if ok:
                stats["items_skipped_unchanged"] += 1
                stats["items_refreshed_from_live"] += 1
            continue

        # unselected eligible: rules path (mutation guard c via _write_w_item)
        _write_w_item(
            item, notes=_rules_narrative(item["row"]),
            model_result={"proceed": "file"},
            write_finding=write_finding, computed_at=computed_at,
            stats=stats, refresh_on_noop=True,
        )

    # Phase 3b: Write selected items (model path)
    for item in selected:
        if remaining_seconds is not None and remaining_seconds() < model_call_reserve_s:
            _write_w_item(
                item, notes=_rules_narrative(item["row"]),
                model_result={"proceed": "file"},
                write_finding=write_finding, computed_at=computed_at,
                stats=stats, refresh_on_noop=True,
            )
            stats["deferred"] += 1
            continue

        plan = plan_fn(row=item["row"], evidence=item["schema_ev"])
        stats["model_items"] += 1
        notes = (plan.get("narrative") or "")[:1_000]
        proceed = plan.get("proceed", "file")
        detail = plan.get("detail", "")
        confidence = plan.get("confidence", "low")
        diagnosis_source = plan.get("diagnosis_source", "rules")

        # Call 59: only a model plan replaces same-hash rules content; a rules
        # fallback (fell back from model → rules) leaves update_on_same_hash False.
        use_update = item["same_hash_rules"] and diagnosis_source == "model"
        _write_w_item(
            item, notes=notes, model_result={"proceed": proceed},
            detail=detail, confidence=confidence,
            write_finding=write_finding, computed_at=computed_at,
            stats=stats, refresh_on_noop=True, update_on_same_hash=use_update,
            diagnosis_source=diagnosis_source,
        )

    return stats
