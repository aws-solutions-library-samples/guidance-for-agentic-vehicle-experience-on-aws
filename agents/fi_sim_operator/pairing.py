"""Ground-and-book pairing logic for the simulated fleet operator (spec D10b, task 11.2c).

Pure port of CMS ``fi_ground_book.pair_booking`` (spec D1,
``modules/cms_ui/source/handlers/main_api/fi_ground_book.py``).
No boto3, no AWS calls.  The caller supplies all candidates and the set of
decided finding_ids.

Public API
----------
pair_booking(grounding, candidates, decided_ids, now_iso) -> (finding | None, reason | None)
    Pair an FH or recall grounding Finding with a booking Finding.

Reasons (mirroring CMS spec D1)
--------------------------------
``None``              — booking found and paired.
``"not_fleet_health"``  — grounding is not from the FH agent (FH-class only).
``"no_booking_item"``   — no structurally valid booking candidate found.
``"already_decided"``   — valid candidates exist but all are already decided.
``"no_dealer"``         — best candidate has no dealer_id.
``"disclosure_differs"``— best candidate's disclosure_txt differs from grounding.
``"not_recall_agent"``  — recall grounding not from the recall-warranty agent.
``"recall_campaign_mismatch"`` — no recall booking for same VIN + campaign.
"""
from __future__ import annotations

from typing import Optional

from agents.fi_sim_operator.select import FH_AGENT, RW_AGENT

# Re-export so tests can spell the reasons without depending on string literals.
REASON_NOT_FLEET_HEALTH: str = "not_fleet_health"
REASON_NO_BOOKING_ITEM: str = "no_booking_item"
REASON_ALREADY_DECIDED: str = "already_decided"
REASON_NO_DEALER: str = "no_dealer"
REASON_DISCLOSURE_DIFFERS: str = "disclosure_differs"
REASON_NOT_RECALL_AGENT: str = "not_recall_agent"
REASON_RECALL_CAMPAIGN_MISMATCH: str = "recall_campaign_mismatch"


# ── internal helpers ──────────────────────────────────────────────────────────

def _get_vin(finding: dict) -> Optional[str]:
    scope = finding.get("scope")
    if isinstance(scope, dict):
        v = scope.get("vin")
        if isinstance(v, str) and v:
            return v
    return None


def _get_fleet(finding: dict) -> Optional[str]:
    """Fleet id from top-level attr or scope."""
    top = finding.get("fleet_id")
    if isinstance(top, str) and top:
        return top
    scope = finding.get("scope")
    if isinstance(scope, dict):
        fid = scope.get("fleet_id")
        if isinstance(fid, str) and fid:
            return fid
    return None


def _is_expired(finding: dict, now_iso: str) -> bool:
    """True when expires_at is missing, malformed, naive, or <= now (fail closed).

    Fix (b) D10 11.3: same semantics as select._is_expired. String comparison fails
    for second-precision Z-suffixed timestamps. Missing or malformed → expired (fail
    closed), matching the auto-approver (F1.5 S2).

    Mutation guard (mutant = old string comparison): a missing expires_at would return
    False (live), allowing a booking with no expiry to be paired — caught by
    test_pairing_expired_candidate_skipped_with_missing_expires_at.
    """
    import datetime as _dt  # local alias avoids collision with datetime imported above
    ea = finding.get("expires_at")
    if not isinstance(ea, str) or not ea:
        return True  # missing → expired (fail closed)
    try:
        dt_ea = _dt.datetime.fromisoformat(ea.replace("Z", "+00:00"))
        dt_now = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return dt_ea <= dt_now
    except (ValueError, AttributeError, TypeError):
        return True  # malformed or naive → expired (fail closed)


def _sort_key(finding: dict) -> tuple:
    """Latest computed_at + finding_id first (mirrors CMS `_key`)."""
    return (finding.get("computed_at", ""), finding.get("finding_id", ""))


# ── public API ────────────────────────────────────────────────────────────────

def pair_booking(
    grounding: dict,
    candidates: list[dict],
    decided_ids: set[str],
    now_iso: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Pair a grounding Finding with a booking Finding.

    Handles both FH (``fh_ground_book``) and recall (``recall_ground_book``)
    groundings.

    FH grounding (``parameters.fault_codes`` present)
    --------------------------------------------------
    Ported from CMS ``pair_booking``:
    1. Grounding must be from :data:`~.select.FH_AGENT`.
    2. Booking candidates must: same FH agent, same VIN, same fleet, action_kind
       ``book_fleet_service``, not expired, fault_codes subset covered by needs.
    3. Split eligible (undecided) from decided.
    4. If no eligible: return the latest decided candidate with
       ``"already_decided"``.
    5. Best eligible: latest ``computed_at`` / ``finding_id``.
    6. No dealer_id → ``(best, "no_dealer")``.
    7. ``disclosure_txt`` mismatch → ``(best, "disclosure_differs")``.
    8. All checks pass → ``(best, None)``.

    Recall grounding (``parameters.recall_id`` present)
    ----------------------------------------------------
    Grounding must be from :data:`~.select.RW_AGENT`.  Match a
    ``book_recall_service`` candidate with the same VIN and same ``recall_id``
    in parameters.  Eligibility checks (expiry, decided) still apply; dealer
    and disclosure checks are NOT applied (recall bookings carry the dealer in
    recommended_action parameters; disclosure is campaign-level).

    Parameters
    ----------
    grounding:
        A Finding with ``recommended_action.action_kind == "ground_vehicle"``.
    candidates:
        All undecided-or-decided Findings for the same fleet that might be
        the paired booking.
    decided_ids:
        Set of finding_ids for which a decision Action exists.
    now_iso:
        Current run time as ISO-8601 UTC string.

    Returns
    -------
    tuple[Finding | None, reason | None]
        ``(None, reason)`` when pairing failed; ``(booking, None)`` on success.
    """
    rec = grounding.get("recommended_action") or {}
    parameters = rec.get("parameters") or {}
    agent_id = grounding.get("agent_id", "")

    recall_id = parameters.get("recall_id") if isinstance(parameters, dict) else None
    fault_codes = parameters.get("fault_codes") if isinstance(parameters, dict) else None

    # ── Dispatch to FH or recall path ─────────────────────────────────────────
    if isinstance(fault_codes, list) and fault_codes:
        return _pair_fh(grounding, candidates, decided_ids, now_iso)
    if isinstance(recall_id, str) and recall_id:
        return _pair_recall(grounding, candidates, decided_ids, now_iso)

    # Neither fault_codes nor recall_id: treat as FH (will fail at step 1)
    return _pair_fh(grounding, candidates, decided_ids, now_iso)


def _pair_fh(
    grounding: dict,
    candidates: list[dict],
    decided_ids: set[str],
    now_iso: str,
) -> tuple[Optional[dict], Optional[str]]:
    """FH pairing: port of CMS ``pair_booking``."""
    # ── Step 1: grounding must be from the FH agent ───────────────────────────
    if grounding.get("agent_id") != FH_AGENT:
        return (None, REASON_NOT_FLEET_HEALTH)

    g_vin = _get_vin(grounding)
    g_fleet = _get_fleet(grounding)
    if not (isinstance(g_vin, str) and g_vin and g_fleet):
        return (None, REASON_NO_BOOKING_ITEM)

    # Fault codes the booking's needs must cover.
    g_rec = grounding.get("recommended_action")
    g_params = g_rec.get("parameters") if isinstance(g_rec, dict) else None
    raw_codes = g_params.get("fault_codes", []) if isinstance(g_params, dict) else []
    fault_codes = raw_codes if isinstance(raw_codes, list) else []
    fault_needs = {f"fault:{c}" for c in fault_codes if isinstance(c, str) and c}
    if not fault_needs:
        return (None, REASON_NO_BOOKING_ITEM)

    # ── Step 2: structurally valid candidates ──────────────────────────────────
    valid: list[dict] = []
    for cand in candidates:
        try:
            if not isinstance(cand, dict):
                continue
            if cand.get("agent_id") != FH_AGENT:
                continue
            if _get_vin(cand) != g_vin:
                continue
            if _get_fleet(cand) != g_fleet:
                continue
            c_rec = cand.get("recommended_action")
            if not isinstance(c_rec, dict):
                continue
            if c_rec.get("action_kind") != "book_fleet_service":
                continue
            if not isinstance(cand.get("computed_at"), str):
                continue
            if not (isinstance(cand.get("finding_id"), str) and cand["finding_id"]):
                continue
            if _is_expired(cand, now_iso):
                continue
            c_params = c_rec.get("parameters")
            if not isinstance(c_params, dict):
                continue
            needs = c_params.get("needs")
            if not isinstance(needs, list):
                continue
            need_ids: set[str] = set()
            for n in needs:
                if isinstance(n, dict) and isinstance(n.get("need_id"), str):
                    need_ids.add(n["need_id"])
            if not fault_needs.issubset(need_ids):
                continue
            valid.append(cand)
        except Exception:  # noqa: BLE001
            continue

    if not valid:
        return (None, REASON_NO_BOOKING_ITEM)

    # ── Step 3: split eligible (undecided) from decided ────────────────────────
    eligible = [c for c in valid if c["finding_id"] not in decided_ids]
    decided = [c for c in valid if c["finding_id"] in decided_ids]

    if not eligible:
        return (max(decided, key=_sort_key), REASON_ALREADY_DECIDED)

    best = max(eligible, key=_sort_key)

    # ── Step 4: best-match checks ──────────────────────────────────────────────
    b_params = best["recommended_action"]["parameters"]
    dealer = b_params.get("dealer_id") if isinstance(b_params, dict) else None
    if not (isinstance(dealer, str) and dealer.strip()):
        return (best, REASON_NO_DEALER)

    if grounding.get("disclosure_txt") != best.get("disclosure_txt"):
        return (best, REASON_DISCLOSURE_DIFFERS)

    return (best, None)


def _pair_recall(
    grounding: dict,
    candidates: list[dict],
    decided_ids: set[str],
    now_iso: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Recall pairing: match by VIN, fleet and campaign_id.

    Fix (c) D10 11.3: require the same fleet as the grounding. FH pairing already
    checked fleet via ``_get_fleet(cand) != g_fleet``; recall pairing omitted this,
    allowing a booking from a *different* fleet for the same VIN+campaign to pair.
    This would approve a booking whose ``fleet_id`` differs from the grounding's,
    violating the one-fleet invariant on both the CMS groundings route and DMS.

    Mutation guard (mutant = no fleet check): a booking from a different fleet
    pairs successfully — test_recall_pairing_different_fleet_rejected catches this.
    """
    if grounding.get("agent_id") != RW_AGENT:
        return (None, REASON_NOT_RECALL_AGENT)

    g_vin = _get_vin(grounding)
    g_fleet = _get_fleet(grounding)
    g_rec = grounding.get("recommended_action") or {}
    g_params = g_rec.get("parameters") or {}
    campaign_id = g_params.get("recall_id")

    if not (isinstance(g_vin, str) and g_vin and isinstance(campaign_id, str) and campaign_id):
        return (None, REASON_RECALL_CAMPAIGN_MISMATCH)

    valid: list[dict] = []
    for cand in candidates:
        try:
            if not isinstance(cand, dict):
                continue
            if cand.get("agent_id") != RW_AGENT:
                continue
            if _get_vin(cand) != g_vin:
                continue
            # Fix (c): fleet must match the grounding's fleet (same as FH pairing).
            if _get_fleet(cand) != g_fleet:
                continue
            c_rec = cand.get("recommended_action")
            if not isinstance(c_rec, dict):
                continue
            if c_rec.get("action_kind") != "book_recall_service":
                continue
            if _is_expired(cand, now_iso):
                continue
            c_params = c_rec.get("parameters")
            if not isinstance(c_params, dict):
                continue
            if c_params.get("recall_id") != campaign_id:
                continue
            if not (isinstance(cand.get("finding_id"), str) and cand["finding_id"]):
                continue
            valid.append(cand)
        except Exception:  # noqa: BLE001
            continue

    if not valid:
        return (None, REASON_RECALL_CAMPAIGN_MISMATCH)

    eligible = [c for c in valid if c["finding_id"] not in decided_ids]
    decided_valid = [c for c in valid if c["finding_id"] in decided_ids]

    if not eligible:
        return (max(decided_valid, key=_sort_key), REASON_ALREADY_DECIDED)

    best = max(eligible, key=_sort_key)
    return (best, None)
