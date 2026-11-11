"""Deterministic daily run loop for the FI fleet-health agent (spec D8 steps 1–8, D9).

Rules path only — no Bedrock, no model calls (task 4.1 adds the model).

Modelled on Stage C's ``agents.fi_recall_warranty.recall.run_recall_loop``:
  - injected readers (athena_fn, grounded_fn, history_fn, standing_fn, lookup_live)
  - write_finding(finding, standing, refresh_on_noop=True)
  - stats dict (keys listed below)
  - a bad row is counted and skipped, not fatal

Public API
----------
load_needs_config(path=None) -> dict
    Load (and cache) the YAML needs-config.  Returns the parsed dict.

map_need_to_labor_op(need, config) -> dict
    Map one need dict → {labor_op, parts}.

read_attempt_history_health(vin, ddb_client, findings_table, actions_table) -> tuple[dict, int]
    Read the newest fleet-health Finding per (vin, subject_segment) and resolve
    each to an Action queue status. Mirrors Stage C's ``read_attempt_history``.

run_health_loop(...) -> dict
    Main entry point.  Returns stats dict with keys:
      vins_selected, vins_processed, items_written, items_skipped_unchanged,
      vins_refused, grounding_read_errors, exhausted_items_written, by_item_type.

Stats contract (mutation guards):
    - ``grounding_read_errors`` increments when make_groundings_reader raises
      OR returns error_count==1; the loop still writes the grounding item (fail-closed).
    - ``items_skipped_unchanged`` increments when skip_unchanged returns True.
    - ``exhausted_items_written`` increments on both "service" and "ground" slots
      when attempt == MAX_ATTEMPTS; exhausted items carry NO recommended_action.
    - ``book_items_without_rooftops`` increments when no candidate rooftop is
      available; only the booking slot is skipped — the grounding slot is still
      written when ``bands`` says so (fail-safer than refusing the whole vehicle).
    - ``p0_directive_fallbacks`` increments (once per vehicle) when a p0 fault
      code has no catalog directive; both slots then carry the ``pullover_stop_engine``
      first sentence (first letter upper-cased) as ``disclosure_txt``.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Imports — reused from Stage C without copying
# ---------------------------------------------------------------------------
from agents.fi_recall_warranty.runner import same_hash_model_written  # skip predicate
from agents.fi_recall_warranty.rooftops import rank_rooftops  # top-3 rooftops
from agents.fi_recall_warranty.selection import check_grounded  # grounded check
from agents.fi_recall_warranty.attempts import (
    _map_action_to_queue_status,         # Action → (queue_status, decided_at)
    fi_decision_id,                       # finding_id → action_id
    MAX_ATTEMPTS,
)
from agents.fi_recall_warranty.evidence import compute_inputs_hash  # C4: stable hash
from boto3.dynamodb.types import TypeDeserializer as _TypeDeserializer

from agents.fi_fleet_health.attempts import should_retry_health, build_max_attempts_item
from agents.fi_fleet_health.bands import classify_needs
from agents.fi_fleet_health.writer import (
    build_all_items,
    build_headline,
    build_disclosure_txt,
)
from agents.fi_fleet_health.write import (
    AGENT_ID,
    AGENT_VERSION,
    FINDING_KIND,
    build_finding,
)
from agents.tier2.writer import compute_finding_key

_DESERIALIZER = _TypeDeserializer()

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).parent / "needs_config.yaml"
_EXPIRY_DAYS = 3

# Statuses that indicate a live, in-progress action (spec D9 / Stage C rule):
# in_progress, and unknown never trigger a retry attempt.
_NO_RETRY_STATUSES = frozenset({"in_progress", "unknown",
                                 "stalled", "blocked",
                                 "exhausted"})  # C3: terminal once cap is reached

# C2 (design answer 1): closing statuses end an episode.  After a closing
# status a new episode starts when current needs differ from the closing
# item's needs, or when the decline window has passed (14 days for p0/attention,
# 30 days otherwise).
_CLOSING_STATUSES = frozenset({"completed", "executed", "dismissed",
                               "exhausted"})   # G3F3.1 (a1 C3): exhausted is closing

#: Diagnosis source for the rules path
_RULES_SOURCE = "rules"

#: Fixed rules-path detail for exhausted items (both slots)
_EXHAUSTED_DETAIL: dict[str, str] = {
    "service": (
        "Five booking attempts for this vehicle did not complete. "
        "Follow up with the dealer manually."
    ),
    "ground": (
        "Five grounding attempts for this vehicle did not complete. "
        "Follow up manually; the vehicle should not be driven."
    ),
}


# ---------------------------------------------------------------------------
# YAML config loader (lazy-loaded and cached)
# ---------------------------------------------------------------------------
_config_cache: Optional[dict] = None


def load_needs_config(path: Optional[Path] = None) -> dict:
    """Load the YAML needs-config from *path* (default: module-adjacent file).

    Caches the result so the file is read at most once per process.
    """
    global _config_cache
    if _config_cache is not None and path is None:
        return _config_cache
    import yaml  # pyyaml from requirements.txt
    target = path or _CONFIG_PATH
    with open(target, encoding="utf-8") as fh:
        result = yaml.safe_load(fh)
    if path is None:
        _config_cache = result
    return result


# ---------------------------------------------------------------------------
# map_need_to_labor_op
# ---------------------------------------------------------------------------

#: Ops used only when needs_config.yaml has no entry for a need (G6-D2: DMS's names).
#: DMS's generic electrical op, which ADP's code_labor_op_map lists for no code and no
#: task (option (a), user 2026-10-18), so a booking made on a fallback clears no fault and
#: completes no PM task. tests/test_labor_op_vocabulary.py pins both: that ADP's map lists
#: neither, and that no known code or task reaches them.
_FALLBACK_LABOR_OP = "LAB-ELE-07"
_FALLBACK_PM_LABOR_OP = "LAB-ELE-07"


def map_need_to_labor_op(need: dict, config: dict) -> dict:
    """Return {labor_op, parts} for one need.

    Resolution order:
    1. fault need: fault_codes[code] if present, else fault_bands[band]
    2. pm_task need: pm_tasks[task_code]
    3. agent2_finding need: agent2_findings[kind], else agent2_findings["default"]

    Falls back to {labor_op: _FALLBACK_LABOR_OP, parts: []} when nothing matches.
    Every op it can return is a DMS labor op (LAB-*; G6-D2, user 2026-10-17).
    """
    kind = need.get("kind")
    if kind == "fault":
        code = need.get("code", "")
        code_map = config.get("fault_codes", {})
        if code in code_map:
            entry = code_map[code]
        else:
            # W3a: contract uses 'severity_band', not 'band', on fault needs.
            band = need.get("severity_band") or need.get("band", "soon")
            band_map = config.get("fault_bands", {})
            entry = band_map.get(band) or band_map.get("soon") or {}
        return {
            "labor_op": entry.get("labor_op", _FALLBACK_LABOR_OP),
            "parts": list(entry.get("parts") or []),
        }

    if kind == "pm_task":
        task_code = need.get("task_code", "")
        entry = (config.get("pm_tasks") or {}).get(task_code) or {}
        return {
            "labor_op": entry.get("labor_op", _FALLBACK_PM_LABOR_OP),
            "parts": list(entry.get("parts") or []),
        }

    if kind == "agent2_finding":
        # W1: use finding_kind (e.g. "health.battery") to key the config, not the
        #     severity string. Strip the "health." prefix to get the component name
        #     ("battery", "brakes", "tires", "diagnostics").
        raw_kind = need.get("finding_kind") or need.get("severity_band") or need.get("kind", "")
        component = raw_kind.split(".")[-1] if "." in raw_kind else raw_kind
        a2 = config.get("agent2_findings") or {}
        entry = a2.get(component) or a2.get("default") or {}
        return {
            "labor_op": entry.get("labor_op", _FALLBACK_LABOR_OP),
            "parts": list(entry.get("parts") or []),
        }

    return {"labor_op": _FALLBACK_LABOR_OP, "parts": []}


# ---------------------------------------------------------------------------
# read_attempt_history_health
# ---------------------------------------------------------------------------
_HEALTH_AGENT_ID = AGENT_ID           # "agent-fi-fleet-health"
_HEALTH_FINDING_KIND = FINDING_KIND   # "fleet.health.service"


def read_attempt_history_health(
    vin: str,
    *,
    ddb_client: Any,
    findings_table: str,
    actions_table: str,
) -> tuple[dict, int]:
    """Return per-(vin, subject_segment) highest-attempt history.

    Subject segments are "service" and "ground".  The subject_key form is
    ``<segment>/<attempt>`` (contract § 1.2).

    Returns
    -------
    (history, skip_count)
        history    : dict[subject_segment] → {
                       "attempt_number", "finding_id", "inputs_hash",
                       "computed_at", "expires_at", "diagnosis_source",
                       "episode_start",
                       "queue_status", "decided_at" }
        skip_count : number of findings with unparseable subject_key
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    best: dict[str, dict] = {}   # subject_segment → best row
    # G3F3.1 (probe fix): track the highest exhausted attempt_n per base_segment so
    # episode_start can be derived from history rows instead of stored evidence.
    # "attempts numbered after the last closing attempt" (decisions.md G3 cycle 3 answer 3).
    exhausted_n: dict[str, int] = {}
    skip_count = 0
    kwargs: dict[str, Any] = {
        "TableName": findings_table,
        "IndexName": "by_vin",
        "KeyConditionExpression": "vin = :v",
        "ExpressionAttributeValues": {":v": {"S": vin}},
    }
    while True:
        response = ddb_client.query(**kwargs)
        for raw in response.get("Items", []):
            item = {k: _DESERIALIZER.deserialize(v) for k, v in raw.items()}
            if item.get("agent_id") != _HEALTH_AGENT_ID:
                continue
            if item.get("finding_kind") != _HEALTH_FINDING_KIND:
                continue
            parts = (item.get("subject_key") or "").split("/")
            if len(parts) != 2:
                skip_count += 1
                continue
            segment, n_str = parts
            # C3: exhausted items use their own segment (e.g. service_exhausted/1).
            # Map them back to the base segment and treat as terminal so
            # _plan_health_attempt returns "skip" on the next run.
            base_segment = segment
            is_exhausted = False
            if segment.endswith("_exhausted"):
                base_segment = segment[: -len("_exhausted")]
                is_exhausted = True
            if base_segment not in ("service", "ground"):
                skip_count += 1
                continue
            try:
                attempt_n = int(n_str)
            except ValueError:
                skip_count += 1
                continue
            if is_exhausted:
                # G3F3.1 (a1 C3): exhausted items are the closing record for their base
                # segment. They override the base item when attempt_n >= existing
                # (service/10 and service_exhausted/10 → exhausted wins).
                # A new base item (service/11 in a new episode) overrides strictly.
                # Mutation: revert to continue → exhausted never wins → episode never
                # closes → test_exhausted_is_closing_record fails.
                _finding_need_ids_ex: frozenset = frozenset(
                    (ev.get("value") or {}).get("need_id")
                    for ev in (item.get("evidence") or [])
                    if ev.get("kind") == "health.need"
                    and (ev.get("value") or {}).get("need_id")
                )
                _episode_start_ex: Optional[int] = None
                for _ev in (item.get("evidence") or []):
                    if _ev.get("kind") == "health.episode":
                        _ep_val = _ev.get("value") or {}
                        _episode_start_ex = _ep_val.get("started_at_attempt")
                        break
                existing = best.get(base_segment)
                if existing is None or attempt_n >= existing["attempt_number"]:
                    best[base_segment] = {
                        "attempt_number": attempt_n,
                        "finding_id": item.get("finding_id", ""),
                        "inputs_hash": item.get("inputs_hash", ""),
                        "computed_at": item.get("computed_at", ""),
                        "expires_at": item.get("expires_at", ""),
                        "diagnosis_source": _extract_health_diagnosis_source(item),
                        "episode_start": _episode_start_ex,
                        "_need_ids": _finding_need_ids_ex,
                        "_exhausted": True,
                    }
                # G3F3.1 (probe fix): always track highest exhausted attempt_n for
                # episode derivation even when this item is overridden in best by a
                # higher non-exhausted attempt (new episode).
                if attempt_n > exhausted_n.get(base_segment, 0):
                    exhausted_n[base_segment] = attempt_n
                continue
            existing = best.get(base_segment)
            # Keep newest attempt; tie-break on computed_at.
            # Base overrides exhausted only when strictly higher attempt (new episode).
            if existing is None:
                newer = True
            elif existing.get("_exhausted"):
                # Strict inequality: base at same attempt loses to exhausted (C3).
                newer = attempt_n > existing["attempt_number"]
            else:
                newer = (
                    attempt_n > existing["attempt_number"]
                    or (
                        attempt_n == existing["attempt_number"]
                        and item.get("computed_at", "") > existing.get("computed_at", "")
                    )
                )
            if newer:
                # C2: store need_ids from health.need evidence entries so the
                # episode-boundary check can compare them to the current run's needs.
                _finding_need_ids: frozenset = frozenset(
                    (ev.get("value") or {}).get("need_id")
                    for ev in (item.get("evidence") or [])
                    if ev.get("kind") == "health.need"
                    and (ev.get("value") or {}).get("need_id")
                )
                # G3F3.1 (a1 C3): read episode_start from health.episode evidence.
                _episode_start: Optional[int] = None
                for _ev in (item.get("evidence") or []):
                    if _ev.get("kind") == "health.episode":
                        _ep_val = _ev.get("value") or {}
                        _episode_start = _ep_val.get("started_at_attempt")
                        break
                best[base_segment] = {
                    "attempt_number": attempt_n,
                    "finding_id": item.get("finding_id", ""),
                    "inputs_hash": item.get("inputs_hash", ""),
                    "computed_at": item.get("computed_at", ""),
                    "expires_at": item.get("expires_at", ""),
                    "diagnosis_source": _extract_health_diagnosis_source(item),
                    "episode_start": _episode_start,
                    "_need_ids": _finding_need_ids,
                }
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    # Resolve queue status for each subject segment
    history: dict[str, dict] = {}
    for segment, entry in best.items():
        if entry.get("_exhausted"):
            # G3F3.1 (a1 C3): exhausted items are closing records.
            # decided_at = computed_at so the decline window runs from the exhaustion.
            closing_needs = entry.get("_need_ids") or frozenset()
            history[segment] = {
                **entry,
                "queue_status": "exhausted",
                "decided_at": entry.get("computed_at"),
                "closing_needs": closing_needs,
            }
            continue
        action_id = fi_decision_id(entry["finding_id"])
        resp = ddb_client.get_item(
            TableName=actions_table,
            Key={"action_id": {"S": action_id}},
        )
        raw_action = resp.get("Item")
        action_obj: Optional[dict] = None
        if raw_action:
            action_obj = {k: _DESERIALIZER.deserialize(v) for k, v in raw_action.items()}
        queue_status, decided_at = _map_action_to_queue_status(action_obj, now)
        # G3F3.1 (a1 C2): DMS consumer and dismiss write no audit entry, so
        # _map_action_to_queue_status returns decided_at=None for 'completed',
        # 'executed', and 'dismissed'. Fall back to the Action's updated_at / created_at.
        # Design answer 2: "From the Action's updated_at, else created_at, never an
        # audit entry (the DMS consumer and dismiss write none)."
        # Mutation guard: keep decided_at=None for completed → window branch dead →
        # test_completed_booking_with_updated_at_sets_decided_at fails.
        if queue_status in _CLOSING_STATUSES and decided_at is None and action_obj is not None:
            decided_at = action_obj.get("updated_at") or action_obj.get("created_at")
        history_row: dict = {**entry, "queue_status": queue_status, "decided_at": decided_at}
        # C2: for closing statuses, store the closing item's need_ids so
        # _plan_health_attempt can detect whether the current needs differ
        # (i.e. a new episode should start).
        if queue_status in _CLOSING_STATUSES:
            closing_needs = entry.get("_need_ids") or frozenset()
            history_row["closing_needs"] = closing_needs
        # G3F3.1 (probe fix): derive episode_start from history rows (exhausted items)
        # rather than solely from health.episode evidence. This means the reader
        # always provides a correct episode_start without needing the evidence entry,
        # matching "attempts numbered after the last closing attempt" (decisions.md
        # G3 cycle 3 answer 3). Fallback to evidence-derived value when no exhausted
        # item precedes the current attempt.
        exh_n = exhausted_n.get(segment)
        ev_start = entry.get("episode_start")
        if exh_n is not None and exh_n < entry["attempt_number"]:
            # A previous episode was exhausted; current episode started after it.
            derived_ep_start: Optional[int] = exh_n + 1
            # Cycle 4 a C1: a later closing (completed/executed/dismissed) after that
            # exhaustion starts a later episode, which only the item's own
            # health.episode evidence records. Take the later of the two, never past
            # the current attempt.
            ev_attempt = _as_attempt_number(ev_start)
            if ev_attempt is not None and derived_ep_start < ev_attempt <= entry["attempt_number"]:
                derived_ep_start = ev_attempt
        else:
            # No earlier exhausted item: fall back to evidence-derived episode_start.
            derived_ep_start = ev_start
        history_row["episode_start"] = derived_ep_start
        history[segment] = history_row

    return history, skip_count


def _as_attempt_number(value) -> Optional[int]:
    """An attempt number from evidence: an int, or an integral Decimal (DynamoDB N)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        from decimal import Decimal  # noqa: PLC0415
        if isinstance(value, Decimal) and value == value.to_integral_value():
            return int(value)
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_health_diagnosis_source(finding: dict) -> Optional[str]:
    """Extract diagnosis_source from a fleet-health Finding's evidence."""
    for entry in finding.get("evidence") or []:
        if entry.get("kind") == "health.diagnosis_source":
            return entry.get("value")
        if "health.diagnosis_source" in entry:
            return entry["health.diagnosis_source"]
    return None


# ---------------------------------------------------------------------------
# _plan_health_attempt
# ---------------------------------------------------------------------------

def _plan_health_attempt(
    history_entry: Optional[dict],
    severity: str,
    now: datetime.datetime,
    current_need_ids: Optional[frozenset] = None,
) -> str:
    """Decide what to write for one (vehicle, subject_segment) slot.

    Returns one of: "write_first", "write_current", "write_next",
    "write_exhausted", "skip".

    Maps Stage C's plan_attempt logic to the fleet-health severity/timing rules.

    Parameters
    ----------
    current_need_ids:
        Frozen set of need_ids from the current run's vehicle row.  Used by the
        C2 episode-boundary check: a closing status starts a new episode when
        the current needs differ from those stored in the closing item.
    """
    if history_entry is None:
        return "write_first"

    qs = history_entry["queue_status"]
    attempt_n = history_entry["attempt_number"]
    # G3F3.1 (a1 C3): per-episode cap — count only attempts since the last closing one.
    # episode_start is derived by read_attempt_history_health from the full history scan
    # (exhausted items + health.episode evidence); when absent (e.g. probe tests that
    # call _plan_health_attempt directly), treat this attempt as the first of a new
    # episode (ep_cap=1) so the cap does not fire on stale/missing tracking data.
    # Mutation guard: default to 1 → episode_start=None → ep_cap=attempt_n → cap fires
    # at MAX_ATTEMPTS regardless of episode → test_per_episode_cap_restarts_after fails.
    _ep_start = history_entry.get("episode_start")
    if _ep_start is not None:
        ep_cap: int = attempt_n - int(_ep_start) + 1
    else:
        ep_cap = 1  # no episode_start: treat as first attempt of current episode

    # Pending/undecided: refresh the current attempt (keep it live).
    if qs in ("none", "pending"):
        return "write_current"

    # C2 (design answer 1): closing statuses end an episode.  A new episode
    # starts when the current needs differ from the closing item's needs OR when
    # the decline window has passed since the closing decision.
    # Mutation guard: removing this block → a completed booking is never
    # superseded even when new faults appear → TestC2EpisodeManagement fails.
    if qs in _CLOSING_STATUSES:
        if _is_new_episode_needed(
            history_entry=history_entry,
            severity=severity,
            now=now,
            current_need_ids=current_need_ids,
        ):
            return "write_next"
        return "skip"

    # Statuses that never re-raise: in_progress, unknown, stalled, blocked.
    if qs in _NO_RETRY_STATUSES:
        return "skip"

    # For rejected: compute days since decision.
    days_since = 0
    if qs == "rejected":
        decided_at = history_entry.get("decided_at")
        if not decided_at:
            return "skip"
        decided_dt = datetime.datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
        days_since = (now - decided_dt).days

    # G3F3.1 (a1 C3): pass ep_cap (not absolute attempt_n) so the cap is per-episode.
    # Mutation guard: pass attempt_n instead → episode 2 gets only (MAX_ATTEMPTS - prev_n)
    # additional attempts → test_per_episode_cap_restarts_after_closing fails.
    can_retry = should_retry_health(
        prev_status=qs,
        severity=severity,
        days_since_decision=days_since,
        attempt=ep_cap,
    )
    if can_retry:
        return "write_next"

    # Cap check for exhausted item
    if ep_cap >= MAX_ATTEMPTS:
        high = {"p0", "attention"}
        threshold = 14 if severity in high else 30
        from agents.fi_recall_warranty.attempts import _IMMEDIATE_RETRY as _C_IMMEDIATE
        if qs in _C_IMMEDIATE or (qs == "rejected" and days_since >= threshold):
            return "write_exhausted"

    return "skip"


def _is_new_episode_needed(
    *,
    history_entry: dict,
    severity: str,
    now: datetime.datetime,
    current_need_ids: Optional[frozenset],
) -> bool:
    """Return True when a closing status should start a new episode (C2).

    A new episode starts when:
    - At least one current need_id is NOT in the closing item's needs, OR
    - The decline window (14 days for p0/attention, 30 otherwise) has passed
      since the closing decision.
    """
    closing_needs: frozenset = history_entry.get("closing_needs") or frozenset()
    cur_needs: frozenset = current_need_ids or frozenset()

    # New need present that was not in the closing item → new episode.
    if cur_needs - closing_needs:
        return True

    # Decline window elapsed → new episode regardless of needs.
    decided_at = history_entry.get("decided_at")
    if not decided_at:
        return False
    decided_dt = datetime.datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
    days_since = (now - decided_dt).days
    high = {"p0", "attention"}
    threshold = 14 if severity in high else 30
    return days_since >= threshold


# ---------------------------------------------------------------------------
# _build_evidence
# ---------------------------------------------------------------------------

def _build_evidence(
    *,
    needs: list[dict],
    rooftops: list[dict],
    position: Optional[dict],
    utilisation: Optional[dict],
    recall_visit: Optional[dict],
    grounding_reason: Optional[dict],
    diagnosis_source: str,
    computed_at: str,
    vin: str,
) -> list[dict]:
    """Build the evidence list for a fleet-health Finding (contract § 1.6).

    Follows Stage C's two-phase pattern:
    1. Build RAW entries (domain values + vin + provenance, no computed_at/source).
       health.diagnosis_source is NOT in RAW (W1: excluded from hash).
    2. Call compute_inputs_hash(raw_entries) → stable hash.
    3. Convert to schema entries with source/source_ref/computed_at added,
       then append the health.diagnosis_source schema entry.

    Returns the schema-compliant entries (step 3).  The raw entries used for
    hashing are produced by ``_build_raw_evidence``.
    C6: diagnosis_source lives ONLY in evidence, not at the Finding top level.
    """
    raw = _build_raw_evidence(
        needs=needs, rooftops=rooftops, position=position,
        utilisation=utilisation, recall_visit=recall_visit,
        grounding_reason=grounding_reason, diagnosis_source=diagnosis_source,
        vin=vin,
    )
    return _to_schema_evidence(raw, computed_at=computed_at, diagnosis_source=diagnosis_source)


def _build_raw_evidence(
    *,
    needs: list[dict],
    rooftops: list[dict],
    position: Optional[dict],
    utilisation: Optional[dict],
    recall_visit: Optional[dict],
    grounding_reason: Optional[dict],
    diagnosis_source: str,
    vin: str,
) -> list[dict]:
    """Build raw evidence entries without computed_at/source/source_ref.

    Hash is computed over these entries (Stage C's pattern — compute_inputs_hash
    is called before computed_at is added so the hash is stable across runs).

    W1 (C1 fix): ``health.diagnosis_source`` is NOT included here so that
    rules-path and model-path raw lists hash identically — it is added as a
    schema evidence entry in ``_to_schema_evidence`` after hashing.

    W1 (C1 fix): ``vehicle.utilisation`` is bucketed to the nearest 10 miles/day
    before hashing so a vehicle that is driven daily does not produce a new hash
    on each run.  The exact value is added to the schema evidence by the caller
    after hashing.
    """
    raw: list[dict] = []

    for need in needs:
        raw.append({"kind": "health.need", "value": need,
                    "vin": vin, "provenance": "live"})

    if rooftops:
        # W3c: contract §1.6 names the field 'parts_in_stock' (not 'in_stock').
        #      rank_rooftops returns 'in_stock'; rename at evidence-build time so
        #      Stage C's rooftops.py is unchanged.
        def _rename_in_stock(r: dict) -> dict:
            result = dict(r)
            if "in_stock" in result:
                result["parts_in_stock"] = result.pop("in_stock")
            return result

        raw.append({"kind": "health.rooftops", "value": [_rename_in_stock(r) for r in rooftops[:3]],
                    "vin": vin, "provenance": "live"})

    if position:
        raw.append({"kind": "health.position", "value": position,
                    "vin": vin, "provenance": "live"})

    if utilisation:
        # W1: bucket daily_average_miles_30d to nearest 10 for hash stability.
        # The exact value goes in the schema evidence (added separately after hashing).
        # Mutation guard: using exact value → hash changes daily for driven vehicles
        #   → skip_unchanged never fires → test_utilisation_bucket_same_hash fails.
        raw_util = dict(utilisation)
        exact_miles = raw_util.get("daily_average_miles_30d")
        if exact_miles is not None:
            try:
                raw_util["daily_average_miles_30d"] = round(float(exact_miles) / 10) * 10
            except (TypeError, ValueError):
                pass
        raw.append({"kind": "vehicle.utilisation", "value": raw_util,
                    "vin": vin, "provenance": "live"})

    if recall_visit:
        raw.append({"kind": "health.recall_visit", "value": recall_visit,
                    "vin": vin, "provenance": "live"})

    if grounding_reason:
        raw.append({"kind": "health.grounding_reason", "value": grounding_reason,
                    "vin": vin, "provenance": "live"})

    # NOTE: health.diagnosis_source is intentionally NOT included here.
    # It is excluded from the hash (W1) and added to schema evidence separately
    # so that a rules run and a model run over identical vehicle data produce
    # the same inputs_hash (enabling the skip-unchanged optimisation across paths).
    return raw


def _to_schema_evidence(
    raw: list[dict],
    *,
    computed_at: str,
    diagnosis_source: Optional[str] = None,
    episode_start: Optional[int] = None,
    exact_utilisation: Optional[dict] = None,
) -> list[dict]:
    """Convert raw evidence entries to schema-compliant entries.

    Adds source, source_ref and computed_at to each entry.
    ``additionalProperties: false`` on the finding schema means extra fields are
    not allowed; the schema evidence_entry allows exactly:
    {kind, source, source_ref, value, computed_at, provenance, [vin]}.

    W1 (C1 fix): ``diagnosis_source`` is added as a separate
    ``health.diagnosis_source`` schema entry AFTER the hash is computed so the
    hash is stable across rules/model paths.  Pass ``diagnosis_source`` to
    include it in the schema evidence.

    W2 (cycle 3): ``exact_utilisation`` carries the un-bucketed
    ``daily_average_miles_30d`` value.  When provided, ``vehicle.utilisation``
    entries in the schema evidence carry the exact value while the hash was
    computed over the bucketed copy (from ``_build_raw_evidence``).
    Mutation guard: omit exact_utilisation → stored value is bucketed (40 for
    42.7 input) → TestW2ExactUtilisationStored fails.
    """
    result: list[dict] = []
    _meta = frozenset({"kind", "value", "vin", "provenance"})
    for entry in raw:
        # W2: substitute exact utilisation for storage (hash used bucketed copy)
        if entry.get("kind") == "vehicle.utilisation" and exact_utilisation is not None:
            stored_value = exact_utilisation
        else:
            stored_value = entry["value"]
        schema_entry: dict[str, Any] = {
            "kind": entry["kind"],
            "source": "agent-fi-fleet-health",
            "source_ref": "fi-fleet-health-run",
            "value": stored_value,
            "computed_at": computed_at,
            "provenance": entry.get("provenance", "live"),
        }
        if "vin" in entry:
            schema_entry["vin"] = entry["vin"]
        result.append(schema_entry)
    # Add diagnosis_source schema entry (excluded from hash for cross-path stability)
    if diagnosis_source is not None:
        result.append({
            "kind": "health.diagnosis_source",
            "source": "agent-fi-fleet-health",
            "source_ref": "fi-fleet-health-run",
            "value": diagnosis_source,
            "computed_at": computed_at,
            "provenance": "live",
        })
    # G3F3.1 (a1 C3): add health.episode entry (excluded from hash) so the next
    # run can read episode_start back from evidence.
    # Mutation guard: drop this entry → _episode_start=None → ep_cap defaults to 1
    # per-run → cap never advances → test_per_episode_cap_restarts_after_closing fails.
    if episode_start is not None:
        result.append({
            "kind": "health.episode",
            "source": "agent-fi-fleet-health",
            "source_ref": "fi-fleet-health-run",
            "value": {"started_at_attempt": int(episode_start)},
            "computed_at": computed_at,
            "provenance": "live",
        })
    return result


# ---------------------------------------------------------------------------
# _build_exhausted_finding
# ---------------------------------------------------------------------------

def _build_exhausted_finding(
    *,
    vin: str,
    fleet_id: Optional[str],
    subject_segment: str,
    attempt: int,
    computed_at: str,
    evidence: list[dict],
    inputs_hash: str,
) -> dict:
    """Build the informational Finding when MAX_ATTEMPTS is exhausted (spec D9).

    ``evidence`` must already be schema-compliant entries (with source/source_ref/computed_at).
    The caller calls ``_to_schema_evidence`` before passing here.
    The hash is computed by the caller on the raw evidence (Stage C's pattern).

    C3: writes to a SEPARATE subject segment (``service_exhausted`` /
    ``ground_exhausted``) so ``read_attempt_history_health`` never reads it back
    as an attempt.  Contract § 1.2 and § 6 list the exhausted subject forms.

    An exhausted item carries NO recommended_action (contract § 1.7, D9).
    Mutation guard: adding recommended_action to this item → test fails.
    """
    # G3F3.1 (a1 C3): write to service_exhausted/<attempt> (not /1) so each
    # episode's exhausted item is distinct and readable as a closing record.
    # Mutation guard: use /1 → all episodes share the same key → new episode after
    # exhaustion never gets a separate closing record → test_per_episode_cap fails.
    exhausted_segment = f"{subject_segment}_exhausted"
    subject_key = f"{exhausted_segment}/{attempt}"
    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )
    # W9: headline is the fixed contract §6 text; detail is the per-segment
    #     human-readable sentence.  Using the detail text as the headline
    #     (M7) loses the contracted headline that tests check.
    from agents.fi_fleet_health.attempts import _INFORMATIONAL_HEADLINE
    headline = _INFORMATIONAL_HEADLINE           # "Bookings keep failing — manual follow-up needed."
    detail = _EXHAUSTED_DETAIL.get(subject_segment, headline)  # longer per-segment sentence
    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": "informational",
        "computed_at": computed_at,
        "expires_at": _iso_add_days(computed_at, _EXPIRY_DAYS),
        "confidence": "low",
        "finding_kind": FINDING_KIND,
        "headline": headline,
        "detail": detail,
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
        # C6: diagnosis_source is in evidence only, not at Finding top level.
        # NO fleet_id — writer projects it from standing.
        # NO recommended_action — informational only
    }
    return finding


def _iso_add_days(iso: str, days: int) -> str:
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt + datetime.timedelta(days=days)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# _process_one_vehicle
# ---------------------------------------------------------------------------

def _process_one_vehicle(
    *,
    vin: str,
    vehicle_row: dict,
    stock_rows: list[dict],
    computed_at: str,
    now: datetime.datetime,
    needs_config: dict,
    grounded_fn: Callable[[str], tuple[bool, int]],
    history_fn: Callable[[str], tuple[dict, int]],
    standing_fn: Callable[[str], Any],
    lookup_live: Callable[[str], Optional[dict]],
    write_finding: Callable[..., Any],
    recall_visit_fn: Optional[Callable[[str], Optional[dict]]],
    fault_grounding_enabled: bool,
    stats: dict[str, Any],
    model_queue: Optional[list] = None,
) -> None:
    """Process all needs for one VIN and write Findings."""
    fleet_id: Optional[str] = vehicle_row.get("fleet_id")
    vehicle_id: Optional[str] = vehicle_row.get("vehicle_id")

    # --- Grounding check (fail-closed: error → not grounded) ---------------
    already_grounded = False
    if vehicle_id is not None and fault_grounding_enabled:
        is_grounded, err_count = grounded_fn(vehicle_id)
        already_grounded = is_grounded
        stats["grounding_read_errors"] += err_count

    # --- Attempt history ---------------------------------------------------
    history, skip_count = history_fn(vin)
    stats["vins_refused"] += skip_count

    # --- Standing -----------------------------------------------------------
    # Mutation guard (f): passing None breaks write_finding(finding, standing, ...)
    # The loop MUST pass the real standing object, never None.
    standing = standing_fn(vin)

    # --- Recall visit (live Stage C booking item) ---------------------------
    recall_visit: Optional[dict] = None
    if recall_visit_fn is not None:
        try:
            recall_visit = recall_visit_fn(vin)
        except Exception as exc:  # noqa: BLE001
            log.warning("health_loop: recall_visit read error vin=<redacted>: %s", exc)

    # --- Collect needs from vehicle row ------------------------------------
    # W5: sort open_faults deterministically before classify_needs so the
    #     needs list — and thus the evidence hash — is order-independent.
    #     Primary sort: band severity (p0 first), then code (asc).
    _BAND_RANK = {"p0": 0, "attention": 1, "soon": 2, "routine": 3}
    open_faults: list[dict] = sorted(
        vehicle_row.get("open_faults") or [],
        key=lambda f: (_BAND_RANK.get(f.get("band", ""), 99), f.get("code") or ""),
    )
    agent2_findings: list[dict] = vehicle_row.get("agent2_findings") or []
    pm_tasks: list[dict] = vehicle_row.get("pm_tasks") or []

    # --- Classify needs ----------------------------------------------------
    try:
        band_result = classify_needs(
            open_faults=open_faults,
            agent2_findings=agent2_findings,
            pm_tasks=pm_tasks,
            already_grounded=already_grounded,
            fault_grounding_enabled=fault_grounding_enabled,
        )
    except (ValueError, KeyError) as exc:
        log.warning("health_loop: classify_needs error vin=<redacted>: %s", exc)
        stats["vins_refused"] += 1
        return

    if not band_result.items:
        # No actionable needs (e.g. PM task 'current' landed here)
        return

    # --- Rooftops ----------------------------------------------------------
    # Collect all part numbers needed for this vehicle's needs
    all_parts: list[str] = []
    for need in band_result.needs:
        mapped = map_need_to_labor_op(need, needs_config)
        all_parts.extend(mapped.get("parts") or [])

    # Position from vehicle row (fleet home or telemetry)
    position: Optional[dict] = vehicle_row.get("position")
    origin: Optional[tuple[float, float]] = None
    if position:
        try:
            origin = (float(position["latitude"]), float(position["longitude"]))
        except (KeyError, TypeError, ValueError):
            origin = None

    rooftops = rank_rooftops(all_parts, stock_rows, origin)

    # Default rooftop: recall visit's rooftop if one is live (spec D8 step 4 / D8 "with a recall visit")
    default_dealer_id: Optional[str] = None
    if recall_visit:
        default_dealer_id = recall_visit.get("rooftop_dealer_id")
    if not default_dealer_id and rooftops:
        default_dealer_id = rooftops[0]["dealer_id"]

    # W8: no candidate rooftop → skip the booking slot for non-p0 vehicles only.
    # W6 (design answer 3): for a p0 vehicle, the booking slot is still written
    #     even without a rooftop — the headline becomes
    #     "Stop-driving fault <code>: choose a rooftop to book service" and
    #     the operator picks a dealer at approve time (contract § 1.4).
    #     Mirrors Stage C's ``book_items_without_rooftops`` stat for non-p0.
    _skip_booking_no_rooftop = not default_dealer_id and band_result.severity != "p0"
    if _skip_booking_no_rooftop:
        log.warning(
            "health_loop: no candidate rooftop (non-p0) — skipping booking slot. "
            "Mutation: skip check → dealer_id=None in booking headline → test fails."
        )
        stats["book_items_without_rooftops"] += 1
    elif not default_dealer_id and band_result.severity == "p0":
        # p0 with no rooftop: booking written with no dealer_id (operator must choose).
        log.warning(
            "health_loop: p0 vehicle has no candidate rooftop — booking with no dealer_id "
            "(operator picks a roster rooftop at approve time, contract § 1.4, W6)."
        )
        stats["book_items_without_rooftops"] += 1

    # Utilisation
    utilisation: Optional[dict] = vehicle_row.get("utilisation")

    # Catalog entries for disclosure_txt
    catalog_entries: dict[str, dict] = vehicle_row.get("catalog_entries") or {}

    # W6: p0 fault with no catalog directive → use the pullover_stop_engine
    #     fallback instead of refusing the vehicle (contract § 1.5).  Returning
    #     here was the defect: a stop-driving vehicle got no item at all.
    if band_result.severity == "p0":
        _fallback_directive: Optional[str] = None
        _fallback_injected = False
        for fault in open_faults:
            if fault.get("band") == "p0":
                code = fault.get("code") or ""
                entry = catalog_entries.get(code) if code else None
                if not entry or not entry.get("directive"):
                    if _fallback_directive is None:
                        # Read the template sentence once per vehicle (never copy the text).
                        from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES
                        _raw = RESPONSE_TEMPLATES["pullover_stop_engine"]["first_sentence"]
                        _fallback_directive = _raw[0].upper() + _raw[1:]
                        # Copy catalog_entries before mutation so the vehicle_row is
                        # not modified in place.
                        catalog_entries = dict(catalog_entries)
                    _base = catalog_entries.get(code) or {"band": "p0", "code": code}
                    catalog_entries[code] = {**_base, "directive": _fallback_directive}
                    if not _fallback_injected:
                        stats["p0_directive_fallbacks"] += 1
                        _fallback_injected = True
                    log.warning(
                        "health_loop: p0 fault %s has no catalog directive — "
                        "using pullover_stop_engine fallback (contract § 1.5). "
                        "Mutation: drop fallback → no disclosure_txt on p0 item → test fails.",
                        code,
                    )

    # --- Write booking slot ------------------------------------------------
    if not _skip_booking_no_rooftop:
        _write_slot(
            segment="service",
            vin=vin,
            fleet_id=fleet_id,
            band_result=band_result,
            open_faults=open_faults,
            catalog_entries=catalog_entries,
            agent2_findings=agent2_findings,
            pm_tasks=pm_tasks,
            rooftops=rooftops,
            position=position,
            utilisation=utilisation,
            recall_visit=recall_visit,
            default_dealer_id=default_dealer_id,
            needs_config=needs_config,
            history=history,
            computed_at=computed_at,
            now=now,
            standing=standing,
            lookup_live=lookup_live,
            write_finding=write_finding,
            fault_grounding_enabled=fault_grounding_enabled,
            stats=stats,
            model_queue=model_queue,
        )

    # --- Write grounding slot (only when p0 and flag on and not already grounded) ---
    if "grounding" in band_result.items:
        _write_slot(
            segment="ground",
            vin=vin,
            fleet_id=fleet_id,
            band_result=band_result,
            open_faults=open_faults,
            catalog_entries=catalog_entries,
            agent2_findings=agent2_findings,
            pm_tasks=pm_tasks,
            rooftops=[],          # grounding items don't rank rooftops
            position=position,
            utilisation=None,
            recall_visit=None,    # grounding items don't mention the recall visit
            default_dealer_id=None,
            needs_config=needs_config,
            history=history,
            computed_at=computed_at,
            now=now,
            standing=standing,
            lookup_live=lookup_live,
            write_finding=write_finding,
            fault_grounding_enabled=fault_grounding_enabled,
            stats=stats,
        )


def _write_slot(
    *,
    segment: str,
    vin: str,
    fleet_id: Optional[str],
    band_result: Any,
    open_faults: list,
    catalog_entries: dict,
    agent2_findings: list,
    pm_tasks: list,
    rooftops: list,
    position: Optional[dict],
    utilisation: Optional[dict],
    recall_visit: Optional[dict],
    default_dealer_id: Optional[str],
    needs_config: dict,
    history: dict,
    computed_at: str,
    now: datetime.datetime,
    standing: Any,
    lookup_live: Callable,
    write_finding: Callable,
    fault_grounding_enabled: bool,
    stats: dict,
    model_queue: Optional[list] = None,
) -> None:
    """Write (or skip) one Finding slot ("service" or "ground").

    With ``model_queue`` (task 4.1 model path), an eligible booking item is queued for
    phase 2 instead of written; ``None`` keeps the rules path exactly as before.
    """
    history_entry = history.get(segment)
    # C2: pass current need_ids to _plan_health_attempt for episode-boundary check.
    current_need_ids = frozenset(
        n.get("need_id") for n in (band_result.needs or []) if n.get("need_id")
    )
    decision = _plan_health_attempt(
        history_entry, band_result.severity, now,
        current_need_ids=current_need_ids,
    )

    if decision == "skip":
        return

    if decision == "write_exhausted":
        # Exhausted item: informational, NO recommended_action.
        # G3F3.1 (a1 C3): use the actual attempt_n from history (not MAX_ATTEMPTS+1)
        # so each episode's exhausted item gets a distinct subject_key.
        attempt = history_entry["attempt_number"]  # type: ignore[index]
        ep_start = history_entry.get("episode_start") or 1   # type: ignore[index]
        raw_ev = _build_raw_evidence(
            needs=band_result.needs,
            rooftops=[],
            position=position,
            utilisation=None,
            recall_visit=None,
            grounding_reason=None,
            diagnosis_source=_RULES_SOURCE,
            vin=vin,
        )
        ev = _to_schema_evidence(raw_ev, computed_at=computed_at,
                                 diagnosis_source=_RULES_SOURCE,
                                 episode_start=ep_start)
        finding = _build_exhausted_finding(
            vin=vin,
            fleet_id=fleet_id,
            subject_segment=segment,
            attempt=attempt,
            computed_at=computed_at,
            evidence=ev,
            inputs_hash=compute_inputs_hash(raw_ev),
        )
        # Mutation guard (e): exhausted item must have NO recommended_action.
        assert "recommended_action" not in finding, (
            "Exhausted items must carry no recommended_action (spec D9 / contract § 1.7)."
        )
        # Mutation guard (f): pass standing, never None.
        write_finding(finding, standing, refresh_on_noop=True)
        stats["items_written"] += 1
        stats["exhausted_items_written"] += 1
        stats["by_item_type"][f"{segment}_exhausted"] = (
            stats["by_item_type"].get(f"{segment}_exhausted", 0) + 1
        )
        return

    # Compute attempt number
    if decision == "write_first":
        attempt = 1
    elif decision == "write_current":
        attempt = history_entry["attempt_number"]  # type: ignore[index]
    else:  # write_next
        attempt = history_entry["attempt_number"] + 1  # type: ignore[index]

    # G3F3.1 (a1 C3): determine episode_start for this attempt.
    # write_first: episode starts at 1.
    # write_current: copy episode_start from the stored item (defaults to 1 for old items).
    # write_next after closing: new episode starts at this attempt (n+1 of the closed one).
    # write_next after non-closing (retry): continue the episode.
    if decision == "write_first":
        episode_start: int = 1
    elif decision == "write_current":
        episode_start = int(history_entry.get("episode_start") or 1)  # type: ignore[index]
    else:  # write_next
        prev_qs = (history_entry or {}).get("queue_status", "")  # type: ignore[index]
        if prev_qs in _CLOSING_STATUSES:
            episode_start = attempt        # new episode: starts at this attempt
        else:
            episode_start = int((history_entry or {}).get("episode_start") or 1)  # type: ignore[index]

    # --- Build the item dicts for this slot --------------------------------
    if segment == "service":
        items = _build_booking_item(
            vin=vin,
            attempt=attempt,
            band_result=band_result,
            open_faults=open_faults,
            catalog_entries=catalog_entries,
            agent2_findings=agent2_findings,
            pm_tasks=pm_tasks,
            needs_config=needs_config,
            default_dealer_id=default_dealer_id,
            recall_visit=recall_visit,
        )
    else:
        items = _build_grounding_item_for_slot(
            vin=vin,
            attempt=attempt,
            band_result=band_result,
            open_faults=open_faults,
            catalog_entries=catalog_entries,
        )

    if not items:
        return

    item = items[0]

    # --- Grounding reason for evidence -------------------------------------
    grounding_reason: Optional[dict] = None
    if segment == "ground":
        # W5: sort p0 codes deterministically (latest last_seen_at desc, then code asc)
        sorted_gnd_faults = sorted(
            [f for f in open_faults if f.get("band") == "p0"],
            key=lambda f: f.get("code") or "",
        )
        sorted_gnd_faults.sort(key=lambda f: f.get("last_seen_at") or "", reverse=True)
        p0_codes = [f.get("code") for f in sorted_gnd_faults]
        last_sighting = [
            {"code": f.get("code"), "last_seen_at": f.get("last_seen_at", "")}
            for f in sorted_gnd_faults
        ]
        grounding_reason = {
            "fault_codes": p0_codes,
            "last_sighting": last_sighting,
        }

    # --- Build evidence and inputs_hash (Stage C's two-phase pattern) ------
    # Phase 1: raw entries without computed_at → stable hash across runs (C4).
    #   C1/W1: raw_ev excludes health.diagnosis_source (added to schema evidence
    #          later so rules/model paths hash identically); utilisation is bucketed.
    #   C1: item signature (directive text, headline, severity, dealer_id, agent_version)
    #       is added to raw_ev BEFORE hashing so a catalog change changes the hash.
    raw_ev = _build_raw_evidence(
        needs=band_result.needs,
        rooftops=rooftops[:3],
        position=position,
        utilisation=utilisation,
        recall_visit=recall_visit if segment == "service" else None,
        grounding_reason=grounding_reason,
        diagnosis_source=_RULES_SOURCE,
        vin=vin,
    )
    # C1: Add item signature to raw_ev so directive/headline changes change the hash.
    raw_ev_for_hash = raw_ev + [_build_item_signature_entry(item, vin=vin)]
    inputs_hash = compute_inputs_hash(raw_ev_for_hash)
    # Phase 2: schema entries with computed_at/source/source_ref for storage.
    #   diagnosis_source is appended here (excluded from hash per W1).
    #   episode_start is appended here (excluded from hash per C3).
    #   W2: exact_utilisation replaces the bucketed copy in stored evidence.
    evidence = _to_schema_evidence(raw_ev, computed_at=computed_at,
                                   diagnosis_source=_RULES_SOURCE,
                                   episode_start=episode_start,
                                   exact_utilisation=utilisation)

    # --- Skip-unchanged rule (spec D8 step 5) ------------------------------
    subject_key = f"{segment}/{attempt}"
    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )
    live = lookup_live(finding_key)

    # --- Model path (task 4.1): booking items only; grounding keeps fixed text ---------
    if model_queue is not None and segment == "service":
        rules_finding = build_finding(
            vin=vin, fleet_id=fleet_id, item=item, computed_at=computed_at,
            inputs_hash=inputs_hash, evidence=evidence, confidence="low",
        )
        rules_finding["subject_key"] = subject_key
        pending = {
            "vin": vin, "item": item, "rules_finding": rules_finding,
            "raw_ev": raw_ev, "computed_at": computed_at,
            "episode_start": episode_start, "utilisation": utilisation,
            "recall_visit": recall_visit, "standing": standing,
            "severity": item["severity"], "band_result": band_result,
            "same_hash_rules": live is not None and live.get("inputs_hash") == inputs_hash,
        }
        if _skip_unchanged_health(live, inputs_hash):
            # Same hash, model-written: no model call, but the live model content is
            # written again so the 3-day item never expires unrefreshed (Stage C call 52).
            live_plan = _plan_from_live(live, item)
            if live_plan is not None:
                finding = _finding_with_plan(pending=pending, plan=live_plan, usage=None)
                write_finding(finding, standing, refresh_on_noop=True)
                stats["items_skipped_unchanged"] += 1
                stats["items_refreshed_from_live"] += 1
                return
            pending["same_hash_rules"] = True  # unreadable live plan: let the model rewrite
        candidates = _model_candidates(rooftops, recall_visit)
        if candidates:
            pending["candidates"] = candidates
            pending["context"] = _service_context(
                item=item, band_result=band_result, candidates=candidates,
                recall_visit=recall_visit, utilisation=utilisation,
                evidence_kinds=[k for k in _SERVICE_EVIDENCE_KINDS
                                if any(e.get("kind") == k for e in raw_ev)],
            )
            model_queue.append(pending)
            return
        # No candidate rooftop (a p0 vehicle; the operator picks one): rules item.

    if _skip_unchanged_health(live, inputs_hash):
        stats["items_skipped_unchanged"] += 1
        return

    # --- Build and write the Finding ---------------------------------------
    finding = build_finding(
        vin=vin,
        fleet_id=fleet_id,
        item=item,
        computed_at=computed_at,
        inputs_hash=inputs_hash,
        evidence=evidence,
        confidence="low",
    )
    # Override subject_key (build_finding uses item["subject_key"])
    finding["subject_key"] = subject_key
    # C6: diagnosis_source is in evidence only, not at Finding top level.

    # Mutation guard (f): standing MUST NOT be None.
    write_finding(finding, standing, refresh_on_noop=True)
    stats["items_written"] += 1
    stats["by_item_type"][segment] = stats["by_item_type"].get(segment, 0) + 1


# ---------------------------------------------------------------------------
# _skip_unchanged_health
# ---------------------------------------------------------------------------

def _skip_unchanged_health(live: Optional[dict], inputs_hash: str) -> bool:
    """Skip rule: same hash AND model-written → skip (rules-written stays eligible)."""
    if live is None:
        return False
    existing_hash = live.get("inputs_hash")
    source = _extract_health_diagnosis_source(live)
    return same_hash_model_written(existing_hash, inputs_hash, source)


# ---------------------------------------------------------------------------
# _build_item_signature_entry — C1 fix
# ---------------------------------------------------------------------------

def _build_item_signature_entry(item: dict, *, vin: str) -> dict:
    """Return a raw evidence entry encoding the item's key textual fields for hashing.

    C1 (design answer 2): including directive text, headline, severity, dealer_id,
    agent_version and the mapped needs-config lines in the hash ensures that any
    catalog change (e.g. new directive wording, updated labor_op) causes the next
    run to produce a different hash and write the updated text to the stored Finding,
    instead of extending expires_at on a stale row.

    W1 (cycle 3): the four previously-surviving mutants that dropped headline,
    severity, dealer_id or agent_version individually are caught by
    TestW1SignatureFields. The needs-config (labor_op) mutant is caught by
    TestW1SignatureFields.test_labor_op_change_changes_hash.

    Mutation guard: removing this entry → catalog change keeps same hash →
    write_finding returns "refresh" → TestC1DirectiveChangeDetected fails.
    Mutation guard per-field: removing any one of headline/severity/dealer_id/
    agent_version/needs from the value → respective TestW1SignatureFields test fails.
    """
    ra_params = (item.get("recommended_action") or {}).get("parameters") or {}
    return {
        "kind": "health.item_signature",
        "value": {
            "severity": item.get("severity"),
            "headline": item.get("headline"),
            "disclosure_txt": item.get("disclosure_txt"),  # None for non-p0
            "dealer_id": ra_params.get("dealer_id"),
            "urgency": ra_params.get("urgency"),
            "agent_version": AGENT_VERSION,
            # W1 (cycle 3): hash the needs-config mapped lines so a labor_op change
            # (needs_config update) produces a new hash and refreshes the stored row.
            # Mutation guard: omit needs → labor_op change keeps same hash →
            # DMS receives a stale RO line → TestW1SignatureFields.test_labor_op fails.
            "needs": ra_params.get("needs"),
        },
        "vin": vin,
        "provenance": "live",
    }


# ---------------------------------------------------------------------------
# Item builders for individual slots
# ---------------------------------------------------------------------------

def _build_booking_item(
    *,
    vin: str,
    attempt: int,
    band_result: Any,
    open_faults: list,
    catalog_entries: dict,
    agent2_findings: list,
    pm_tasks: list,
    needs_config: dict,
    default_dealer_id: Optional[str],
    recall_visit: Optional[dict],
) -> list[dict]:
    """Build the booking item for the 'service' slot.

    When a recall_visit is present, the default rooftop is the recall visit's
    rooftop, and the detail text is 'Book with recall <id> at <rooftop>'
    (spec D8 "with a recall visit").
    """
    from agents.fi_fleet_health.writer import (
        build_headline,
        build_disclosure_txt,
    )

    severity = band_result.severity

    # W5: sort p0 faults deterministically (latest sighting desc, then code asc)
    #     so the headline code, directive, and hash are stable regardless of
    #     Athena row order.  Without sorting, two runs of the same data in
    #     different row order produce different headlines and hashes.
    sorted_p0_faults = sorted(
        [f for f in open_faults if f.get("band") == "p0"],
        key=lambda f: (f.get("last_seen_at") or "", f.get("code") or ""),
        reverse=True,  # latest first; reverse=False for code falls back on tie
    )
    # stable sort: first by last_seen_at desc, then by code asc on ties
    sorted_p0_faults.sort(key=lambda f: f.get("code") or "", reverse=False)
    sorted_p0_faults.sort(key=lambda f: f.get("last_seen_at") or "", reverse=True)

    # Primary p0 code/entry for disclosure_txt and headline
    p0_code: Optional[str] = None
    p0_entry: Optional[dict] = None
    for fault in sorted_p0_faults:
        code = fault.get("code")
        if code:
            p0_code = code
            p0_entry = catalog_entries.get(code)
            break

    chosen_dealer = default_dealer_id

    # Recall-visit default: spec D8 "with a recall visit"
    recall_detail: Optional[str] = None
    if recall_visit:
        recall_id = recall_visit.get("recall_id", "")
        recall_rooftop = recall_visit.get("rooftop_dealer_id", "")
        if recall_rooftop:
            chosen_dealer = recall_rooftop
        if recall_id and recall_rooftop:
            recall_detail = f"Book with recall {recall_id} at {recall_rooftop}"

    headline = build_headline(
        item_type="booking",
        severity=severity,
        fault_code=p0_code,
        need_description=None,
        rooftop_dealer_id=chosen_dealer,
    )

    disclosure: Optional[str] = None
    if severity == "p0" and p0_entry:
        disclosure = build_disclosure_txt(p0_entry)

    # Build needs list for the action's server-set `needs` parameter (contract §1.7).
    # W2: carry need_id so 4.1 model can defer specific needs via defer_need_ids
    #     and the validator can reference them (contract §9.1).
    need_lines = []
    for need in band_result.needs:
        mapped = map_need_to_labor_op(need, needs_config)
        need_lines.append({
            "need_id": need.get("need_id") or "",  # W2: deterministic id
            "labor_op": mapped["labor_op"],
            # W8: use finding_kind (component name) as the description for
            # Agent 2 needs so the dealer's RO line shows a meaningful text.
            # Mutation guard: leaving "agent2_finding" → test_agent2_need_description fails.
            "description": (
                need.get("task_code") or need.get("code")
                or (lambda fk: fk.split(".")[-1] if "." in fk else fk)(need.get("finding_kind") or "")
                or str(need.get("kind", ""))
            ),
            "pay_type": "customer_pay",
        })

    item: dict[str, Any] = {
        "item_type": "booking",
        "vin": vin,
        "attempt": attempt,
        "subject_key": f"service/{attempt}",
        "severity": severity,
        "headline": headline,
        "detail": recall_detail or "",
        "needs": list(band_result.needs),
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": vin,
                "needs": need_lines,
                "dealer_id": chosen_dealer,
                "urgency": "this_week" if severity == "p0" else "this_month",
            },
        },
    }
    if disclosure is not None:
        item["disclosure_txt"] = disclosure
    # Task 4.1: the headline is rebuilt from this code when the model picks another rooftop.
    item["p0_code"] = p0_code
    return [item]


def _build_grounding_item_for_slot(
    *,
    vin: str,
    attempt: int,
    band_result: Any,
    open_faults: list,
    catalog_entries: dict,
) -> list[dict]:
    """Build the grounding item for the 'ground' slot (fixed text, no model call)."""
    from agents.fi_fleet_health.writer import build_grounding_item

    # W5: sort p0 codes deterministically (latest last_seen_at desc, then code asc)
    sorted_p0_faults = sorted(
        [f for f in open_faults if f.get("band") == "p0"],
        key=lambda f: f.get("code") or "",
    )
    sorted_p0_faults.sort(key=lambda f: f.get("last_seen_at") or "", reverse=True)

    p0_codes = [f.get("code") for f in sorted_p0_faults]
    last_sighting = [
        {"code": f.get("code"), "last_seen_at": f.get("last_seen_at", "")}
        for f in sorted_p0_faults
    ]

    # Pick the primary catalog entry for disclosure_txt
    p0_entry: dict = {}
    for code in p0_codes:
        if code and code in catalog_entries:
            p0_entry = catalog_entries[code]
            break
    if not p0_entry and open_faults:
        for fault in open_faults:
            if fault.get("band") == "p0":
                p0_entry = {"band": "p0", "code": fault.get("code"), "directive": None}
                break

    item = build_grounding_item(
        vin=vin,
        attempt=attempt,
        fault_codes=[c for c in p0_codes if c],
        last_sighting=last_sighting,
        catalog_entry=p0_entry,
        model_fn=None,  # never called — spec D8 step 6
    )
    item["subject_key"] = f"ground/{attempt}"
    return [item]


# ---------------------------------------------------------------------------
# Model path (task 4.1): candidates, context, plan application, phase 2
# ---------------------------------------------------------------------------

#: Evidence kinds a booking item's ``evidence_refs`` may name (contract § 1.6).
_SERVICE_EVIDENCE_KINDS: tuple[str, ...] = (
    "health.need", "health.rooftops", "health.position", "vehicle.utilisation",
    "health.recall_visit",
)
_CONTEXT_STR_MAX = 120


def _clip(value: Any) -> Any:
    return value[:_CONTEXT_STR_MAX] if isinstance(value, str) else value


def _model_candidates(rooftops: list[dict], recall_visit: Optional[dict]) -> list[dict]:
    """Rooftops the model may choose: the top three, plus the recall visit's rooftop.

    The recall rooftop is the item's default (spec D8 "with a recall visit"), so it is a
    candidate even when it is not among the three nearest.
    """
    candidates: list[dict] = []
    recall_dealer = (recall_visit or {}).get("rooftop_dealer_id") or None
    for r in rooftops[:3]:
        dealer_id = r.get("dealer_id")
        if not dealer_id:
            continue
        candidates.append({
            "dealer_id": dealer_id,
            "distance_miles": r.get("distance_miles"),
            "in_stock": r.get("in_stock"),
            "recall_visit": dealer_id == recall_dealer,
        })
    if recall_dealer and all(c["dealer_id"] != recall_dealer for c in candidates):
        candidates.insert(0, {"dealer_id": recall_dealer, "distance_miles": None,
                              "in_stock": None, "recall_visit": True})
    return candidates


def _service_context(*, item: dict, band_result: Any, candidates: list[dict],
                     recall_visit: Optional[dict], utilisation: Optional[dict],
                     evidence_kinds: list[str]) -> dict:
    """The model's view of one booking item: allowlisted evidence values only.

    No VIN, coordinates, fleet id or raw ADP/DMS row reaches the prompt.
    """
    from agents.fi_fleet_health.validator import NON_DEFERRABLE_BANDS, allowed_urgencies

    lines = {ln.get("need_id"): ln for ln in
             ((item.get("recommended_action") or {}).get("parameters") or {}).get("needs") or []}
    needs = []
    for need in band_result.needs:
        need_id = need.get("need_id") or ""
        line = lines.get(need_id) or {}
        needs.append({
            "need_id": _clip(need_id),
            "kind": _clip(need.get("kind")),
            "severity_band": _clip(need.get("severity_band")),
            "code": _clip(need.get("code") or need.get("task_code")
                          or need.get("finding_kind")),
            "labor_op": _clip(line.get("labor_op")),
            "deferrable": need.get("severity_band") not in NON_DEFERRABLE_BANDS,
        })
    miles = (utilisation or {}).get("daily_average_miles_30d")
    return {
        "severity": item.get("severity"),
        "allowed_urgencies": list(allowed_urgencies(item.get("severity"))),
        "needs": needs,
        "candidates": [{k: _clip(v) for k, v in c.items()} for c in candidates],
        "recall_visit": (
            {"recall_id": _clip(recall_visit.get("recall_id")),
             "rooftop_dealer_id": _clip(recall_visit.get("rooftop_dealer_id"))}
            if recall_visit else None
        ),
        "daily_average_miles_30d": miles if isinstance(miles, (int, float)) else None,
        "evidence_kinds": list(evidence_kinds),
    }


def _plan_from_live(live: dict, item: dict) -> Optional[dict]:
    """The live model-written row's plan (skip rule: no model call), or None if unreadable."""
    params = (live.get("recommended_action") or {}).get("parameters")
    if not isinstance(params, dict) or not isinstance(live.get("detail"), str):
        return None
    rules_ids = [ln.get("need_id") for ln in
                 ((item.get("recommended_action") or {}).get("parameters") or {}).get("needs") or []]
    live_ids = {ln.get("need_id") for ln in params.get("needs") or [] if isinstance(ln, dict)}
    return {
        "dealer_id": params.get("dealer_id"),
        "urgency": params.get("urgency"),
        "defer_need_ids": [i for i in rules_ids if i not in live_ids],
        "detail": live["detail"],
        "confidence": live.get("confidence") or "low",
        "evidence_refs": [],
        "diagnosis_source": "model",
        "_detail_is_final": True,
    }


def _finding_with_plan(*, pending: dict, plan: dict, usage: Optional[dict]) -> dict:
    """The booking Finding with a plan applied.  Only the plan's five fields are read.

    Severity, headline template, ``disclosure_txt``, needs content and ``inputs_hash``
    stay the deterministic rules item's; the model chooses among them, never writes them.
    """
    from agents.fi_fleet_health.writer import build_headline

    rules_finding: dict = pending["rules_finding"]
    item: dict = pending["item"]
    finding = copy.deepcopy(rules_finding)
    params = finding["recommended_action"]["parameters"]
    deferred = set(plan.get("defer_need_ids") or [])
    params["needs"] = [ln for ln in params["needs"] if ln.get("need_id") not in deferred]
    params["dealer_id"] = plan.get("dealer_id")
    params["urgency"] = plan.get("urgency")
    finding["headline"] = build_headline(
        item_type="booking", severity=item["severity"], fault_code=item.get("p0_code"),
        need_description=None, rooftop_dealer_id=plan.get("dealer_id"),
    )
    detail = plan.get("detail") or ""
    recall_visit = pending.get("recall_visit") or {}
    recall_id = recall_visit.get("recall_id")
    recall_rooftop = recall_visit.get("rooftop_dealer_id")
    if (not plan.get("_detail_is_final") and recall_id and recall_rooftop
            and plan.get("dealer_id") == recall_rooftop):
        detail = f"Book with recall {recall_id} at {recall_rooftop}. {detail}"
    finding["detail"] = detail
    finding["confidence"] = plan.get("confidence") or "low"
    finding["evidence"] = _to_schema_evidence(
        pending["raw_ev"], computed_at=pending["computed_at"],
        diagnosis_source=plan.get("diagnosis_source", _RULES_SOURCE),
        episode_start=pending["episode_start"],
        exact_utilisation=pending["utilisation"],
    )
    if usage:
        from agents.fi_fleet_health.llm import cost_usd
        n_in, n_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        finding["tokens_used"] = n_in + n_out
        finding["cost_usd"] = cost_usd(n_in, n_out)
    return finding


def _write_model_queue(*, model_queue: list, bedrock_client: Any, model_config: Any,
                       budget: Any, remaining_seconds: Optional[Callable[[], float]],
                       write_finding: Callable, stats: dict) -> None:
    """Phase 2: model calls for the queued booking items, most severe first, capped."""
    from agents.fi_fleet_health.runner import apply_health_cap

    within, over = apply_health_cap(model_queue, cap=model_config.max_model_calls)
    for pending in over:
        stats["model_deferred"] += 1
        _guarded(lambda p=pending: _write_pending_rules(p, write_finding, stats), stats)
    for pending in within:
        _guarded(lambda p=pending: _plan_and_write(
            p, bedrock_client=bedrock_client, model_config=model_config, budget=budget,
            remaining_seconds=remaining_seconds, write_finding=write_finding, stats=stats,
        ), stats)


def _guarded(fn: Callable[[], None], stats: dict) -> None:
    """A bad item is counted and skipped, not fatal (as in phase 1)."""
    try:
        fn()
    except (ValueError, KeyError) as exc:
        log.warning("health_loop: model-path item skipped error=%s", type(exc).__name__)
        stats["vins_refused"] += 1


def _plan_and_write(pending: dict, *, bedrock_client: Any, model_config: Any, budget: Any,
                    remaining_seconds: Optional[Callable[[], float]],
                    write_finding: Callable, stats: dict) -> None:
    """Plan one queued booking item with the model (or defer it) and write it."""
    from agents.fi_fleet_health.llm import plan_service
    from agents.fi_fleet_health.validator import NON_DEFERRABLE_BANDS

    needs = pending["band_result"].needs
    non_deferrable = [n["need_id"] for n in needs
                      if n.get("severity_band") in NON_DEFERRABLE_BANDS]
    deferrable = [n["need_id"] for n in needs
                  if n.get("severity_band") not in NON_DEFERRABLE_BANDS]
    kinds = [k for k in _SERVICE_EVIDENCE_KINDS
             if any(e.get("kind") == k for e in pending["raw_ev"])]
    rules_finding = pending["rules_finding"]
    rules_params = rules_finding["recommended_action"]["parameters"]
    result = plan_service(
        bedrock_client=bedrock_client, budget=budget, config=model_config,
        context=pending["context"], severity=pending["severity"],
        candidates=pending["candidates"],
        non_deferrable_need_ids=non_deferrable, deferrable_need_ids=deferrable,
        evidence_kinds=kinds,
        rules_fn=lambda p=rules_params: {"dealer_id": p.get("dealer_id"),
                                         "urgency": p.get("urgency")},
        remaining_seconds=remaining_seconds,
    )
    if result is None:
        stats["model_deferred"] += 1
        _write_pending_rules(pending, write_finding, stats)
        return
    plan, usage = result
    stats["model_items"] += 1
    finding = _finding_with_plan(pending=pending, plan=plan, usage=usage)
    if pending["same_hash_rules"] and plan.get("diagnosis_source") == "model":
        write_finding(finding, pending["standing"], update_on_same_hash=True)
    else:
        write_finding(finding, pending["standing"], refresh_on_noop=True)
    stats["items_written"] += 1
    stats["by_item_type"]["service"] = stats["by_item_type"].get("service", 0) + 1


def _write_pending_rules(pending: dict, write_finding: Callable, stats: dict) -> None:
    """Write a queued item exactly as the rules path would have (no model call made)."""
    write_finding(pending["rules_finding"], pending["standing"], refresh_on_noop=True)
    stats["items_written"] += 1
    stats["by_item_type"]["service"] = stats["by_item_type"].get("service", 0) + 1


# ---------------------------------------------------------------------------
# run_health_loop — public entry point
# ---------------------------------------------------------------------------

def run_health_loop(
    *,
    vehicle_rows: list[dict[str, Any]],
    stock_rows: list[dict[str, Any]],
    computed_at: str,
    grounded_fn: Callable[[str], tuple[bool, int]],
    history_fn: Callable[[str], tuple[dict, int]],
    standing_fn: Callable[[str], Any],
    lookup_live: Callable[[str], Optional[dict]],
    write_finding: Callable[..., Any],
    recall_visit_fn: Optional[Callable[[str], Optional[dict]]] = None,
    fault_grounding_enabled: bool = False,
    needs_config: Optional[dict] = None,
    now: Optional[datetime.datetime] = None,
    bedrock_client: Optional[Any] = None,
    model_config: Optional[Any] = None,
    model_budget: Optional[Any] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Run loop for the fleet-health component (spec D8 steps 1–8).

    Model path (task 4.1): with ``bedrock_client`` and ``model_config``
    (:class:`agents.fi_fleet_health.llm.ModelConfig`), eligible booking items are
    queued in phase 1 and planned in phase 2, most severe first, under
    ``model_config.max_model_calls`` Converse calls and ``model_config.max_tokens``
    tokens for the run (``model_budget`` may carry a :class:`ModelBudget` the caller
    reads afterwards).  Without ``bedrock_client`` the rules path runs unchanged.

    Parameters
    ----------
    vehicle_rows:
        Pre-selected rows from selection.select_vehicles_with_needs(), each a
        dict with at least:
          vin (str), fleet_id (str|None), vehicle_id (str|None),
          open_faults (list), agent2_findings (list), pm_tasks (list),
          position (dict|None), utilisation (dict|None),
          catalog_entries (dict — code → catalog entry),
          [optional] open_faults[*].last_seen_at
    stock_rows:
        DMS stock rows (from rooftops.rank_rooftops).
    computed_at:
        ISO-8601 UTC timestamp string for this run.
    grounded_fn:
        ``vehicle_id -> (is_grounded, error_count)`` (Stage C check_grounded shape).
        Mutation guard (d): if error_count is ignored → grounding_read_errors stays
        at 0 on an error → stat test fails.
    history_fn:
        ``vin -> (history_dict, skip_count)`` via read_attempt_history_health.
    standing_fn:
        ``vin -> StandingFacts`` — passed to write_finding.
        Mutation guard (f): passing None breaks write_finding's standing arg.
    lookup_live:
        ``finding_key -> live_row | None`` (skip rule).
    write_finding:
        ``(finding, standing, refresh_on_noop=True) -> result``.
        Mutation guard (a): omitting refresh_on_noop=True → test fails.
    recall_visit_fn:
        Optional ``vin -> {"recall_id", "rooftop_dealer_id", ...} | None``.
        Mutation guard (c): ignoring → default rooftop wrong on recall visit → fails.
    fault_grounding_enabled:
        Rollout flag (default False) — no grounding items until the CMS
        groundings route supports the fault form (Group 7).
    needs_config:
        Pre-loaded YAML config dict.  If None, loaded from needs_config.yaml.
    now:
        Current UTC datetime.  When None (default), set to
        ``datetime.datetime.now(datetime.timezone.utc)`` at run start.
        Inject a fixed datetime in tests to make ``_plan_health_attempt``
        and retry-window checks deterministic.

    Returns
    -------
    dict with keys:
      vins_selected, vins_processed, items_written, items_skipped_unchanged,
      vins_refused, grounding_read_errors, exhausted_items_written, by_item_type,
      book_items_without_rooftops, p0_directive_fallbacks.
    """
    if needs_config is None:
        needs_config = load_needs_config()

    stats: dict[str, Any] = {
        "vins_selected": len(vehicle_rows),
        "vins_processed": 0,
        "items_written": 0,
        "items_skipped_unchanged": 0,
        "vins_refused": 0,
        "grounding_read_errors": 0,
        "exhausted_items_written": 0,
        "by_item_type": {},
        "book_items_without_rooftops": 0,
        "p0_directive_fallbacks": 0,
    }

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    model_queue: Optional[list] = None
    if bedrock_client is not None:
        from agents.fi_fleet_health.llm import ModelBudget, ModelConfig
        if not isinstance(model_config, ModelConfig):
            raise ValueError("the model path needs a ModelConfig (guardrail, caps)")
        if model_budget is None:
            model_budget = ModelBudget(max_calls=model_config.max_model_calls,
                                       max_tokens=model_config.max_tokens)
        model_queue = []
        stats.update({"model_items": 0, "model_deferred": 0,
                      "items_refreshed_from_live": 0})

    for vehicle_row in vehicle_rows:
        vin = vehicle_row.get("vin")
        if not vin:
            stats["vins_refused"] += 1
            continue

        try:
            _process_one_vehicle(
                vin=vin,
                vehicle_row=vehicle_row,
                stock_rows=stock_rows,
                computed_at=computed_at,
                now=now,
                needs_config=needs_config,
                grounded_fn=grounded_fn,
                history_fn=history_fn,
                standing_fn=standing_fn,
                lookup_live=lookup_live,
                write_finding=write_finding,
                recall_visit_fn=recall_visit_fn,
                fault_grounding_enabled=fault_grounding_enabled,
                stats=stats,
                model_queue=model_queue,
            )
            stats["vins_processed"] += 1
        except (ValueError, KeyError) as exc:
            log.warning(
                "health_loop: skipping bad vehicle vin=<redacted> error=%s",
                type(exc).__name__,
            )
            stats["vins_refused"] += 1

    if model_queue is not None:
        _write_model_queue(
            model_queue=model_queue, bedrock_client=bedrock_client,
            model_config=model_config, budget=model_budget,
            remaining_seconds=remaining_seconds, write_finding=write_finding,
            stats=stats,
        )
        stats.update({
            "model_calls": model_budget.calls,
            "input_tokens": model_budget.input_tokens,
            "output_tokens": model_budget.output_tokens,
            "tokens_used": model_budget.tokens_used,
            "cost_usd": model_budget.cost_usd(),
            "guardrail_interventions": model_budget.guardrail_interventions,
            "guardrail_errors": model_budget.guardrail_errors,
            "rules_fallbacks": model_budget.rules_fallbacks,
        })

    return stats
