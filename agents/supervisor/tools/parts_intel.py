"""parts_intel — cross-repo parts intelligence orchestrator (CVX-only, v1).

Orchestrates four sequential KB + dispatch steps for a single chat turn:
  1. candidate_parts  — KB Retrieve filtered to source_category=parts_catalog
  2. recall_check     — KB Retrieve filtered to source_category=tsb_recall
  3. warranty_summary — KB Retrieve filtered to source_category=warranty_policy
  4. dispatch         — POST /api/dms/fleet/repair-orders (fleet-operator gated)

NOT registered on the bidi persona catalog (voice out of scope — the
fitment-narrative step can approach the ~1s Nova Sonic ceiling; see spec
§ Constraint 6 and test_tool_not_registered_on_bidi_catalog).

See spec: DMS/.kiro/specs/2026-09-10-dms-parts-intel/spec.md § Design.
Prior art: agents/supervisor/tools/parts_lookup.py (KB Retrieve pattern).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
import requests
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.tools.kb_filter import apply_source_category_filter

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=10)

# ---------------------------------------------------------------------------
# Deterministic-seam narration-preservation constants (spec § D-3 and § D-4).
# These are mutation-test gates — do NOT soften, paraphrase, or inline.
# ---------------------------------------------------------------------------

_RECALL_DISCLOSURE_MSG: str = (
    "Recall status is inferred from a narrative knowledge-base corpus of 9 documents "
    "(4 NHTSA recalls + 5 TSBs) and is not authoritative — it is not a real-time NHTSA "
    "feed. Confirm with official NHTSA records before acting on any recall finding."
)
"""Verbatim disclosure attached to every recall_check response per spec § D-3.

Must contain the word 'authoritative'. Pinned by tests 3 and 12.
"""

# ---------------------------------------------------------------------------
# Post-narration seam constants — added 2026-09-11 to close the T4.4 finding.
#
# Background: review Cycle 3 mutation-tested the four seam contracts and found
# them guarded at the tool boundary. T4.4 then found the boundary the user reads
# is downstream of that. When _recall_check returned the correct empty-path
# dict, the LLM rendered it as "✅ No matching active recall was found" — an
# absence claim forbidden by spec § D-3 that no boundary-adjacent test could
# see. And on the hits path, the LLM adjudicated applicability the corpus
# cannot support ("none of the returned recalls apply to this vehicle's
# profile"), a determination `~/.kiro/steering/agentic-tiers.md` requires to
# stay deterministic.
#
# Fix: options 1 + 2 from the issue, both moving forbidden phrasing inside a
# test-pinned boundary — issues/2026-09-11-parts-intel-narration-asserts-recall-absence/.
# The tool now emits verbatim narration text on every path and a data field
# (`applicability: "undetermined"`) on the hits path. The persona prompt
# instructs the LLM to surface both verbatim, but the guardrail is here.
# ---------------------------------------------------------------------------

_RECALL_NO_HIT_NARRATION: str = (
    "The parts_intel recall knowledge base (a 9-document seeded corpus, not "
    "authoritative) did not return a matching document for this query. That "
    "absence of a match does not establish that the vehicle is unaffected — "
    "recall status cannot be established from this corpus. Confirm directly "
    "with NHTSA at nhtsa.gov/recalls before drawing a conclusion."
)
"""Verbatim narration attached to every empty / exception recall_check response.

This constant carries the phrasing the LLM must surface to the user. It is
worded to make an absence claim inexpressible without paraphrasing away the
disclosure. Pinned by test_recall_check_narration_is_verbatim_on_no_hit
(mutation gate: shortening or softening breaks the test).

MUST NOT contain any string from the forbidden absence-assertion set
(pinned by test_recall_narration_constants_forbid_absence_phrases):
  'no active recall', 'no recall found', 'not subject to any recall',
  'all clear', 'no recall'.
MUST NOT contain the ✅ (U+2705) glyph or any other all-clear glyph.
"""

_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG: str = (
    "Applicability to this specific vehicle cannot be determined from the "
    "seeded corpus, which carries no VIN-range or make/model/year join. The "
    "returned recalls are surfaced verbatim; determining whether any of them "
    "apply to the caller's vehicle requires a service advisor or the official "
    "NHTSA VIN lookup at nhtsa.gov/recalls."
)
"""Verbatim explanation attached to every recall_check hit-path response.

Present in the response as `applicability_reason`, alongside
`applicability: "undetermined"`. Together they encode the determination the
tool cannot make into data the narration must carry, rather than a gap the
LLM will fill. Pinned by test_recall_check_applicability_undetermined_on_hits
(mutation gate).

MUST contain the substring 'cannot be determined' (pinned).
MUST NOT contain any absence phrase or the ✅ glyph (pinned).
"""

_COVERAGE_NOT_AUTOMATED_MSG: str = (
    "Coverage for this specific repair isn't in our automated ruleset — "
    "a service advisor can confirm."
)
"""Verbatim reason attached to every warranty_summary response per spec § D-4.

Must contain 'service advisor can confirm'. Pinned by tests 5 and 12.
"""

# Absence-phrase and all-clear-glyph denylist used both by tests and by a
# module-load-time assertion below. Kept module-private so a test-time monkey
# patch cannot silently widen the guard.
_RECALL_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "no active recall",
    "no recall found",
    "not subject to any recall",
    "all clear",
    "no recall",
    "no matching active recall",
)
_RECALL_FORBIDDEN_GLYPHS: tuple[str, ...] = (
    "\u2705",  # ✅ WHITE HEAVY CHECK MARK
    "\u2714",  # ✔ HEAVY CHECK MARK
    "\u2611",  # ☑ BALLOT BOX WITH CHECK
)


def _assert_recall_narration_constants_are_safe() -> None:
    """Load-time guard on the two narration constants.

    Fires on module import. A future edit that inlines an absence phrase or
    an all-clear glyph into either constant crashes the module before any
    caller can read the broken text. Complements the unit-test guard: the
    test catches the mistake in CI; this catches it at deploy-time even if
    the test file is skipped.
    """
    for name, text in (
        ("_RECALL_NO_HIT_NARRATION", _RECALL_NO_HIT_NARRATION),
        ("_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG",
         _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG),
    ):
        lower = text.lower()
        for phrase in _RECALL_FORBIDDEN_PHRASES:
            if phrase in lower:
                raise AssertionError(
                    f"{name} contains forbidden absence phrase {phrase!r} — "
                    "see spec § D-3 and issue "
                    "2026-09-11-parts-intel-narration-asserts-recall-absence"
                )
        for glyph in _RECALL_FORBIDDEN_GLYPHS:
            if glyph in text:
                raise AssertionError(
                    f"{name} contains forbidden all-clear glyph U+{ord(glyph):04X} — "
                    "a green check on a recall question reads as all-clear "
                    "independently of the words. See spec § D-3."
                )


_assert_recall_narration_constants_are_safe()

# ---------------------------------------------------------------------------
# Dispatch env var (no default — graceful degrade when unset per T3.4)
# ---------------------------------------------------------------------------
_DMS_API_ENDPOINT_ENV = "VSA_DMS_API_ENDPOINT"
_DISPATCH_PATH = "/api/dms/fleet/repair-orders"
_DISPATCH_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Internal KB-query helpers
# ---------------------------------------------------------------------------


def _candidate_parts(
    client: Any,
    kb_id: str,
    dtc: str = "",
    vehicle_make: str = "",
    vehicle_model: str = "",
    vehicle_year: str = "",
) -> dict[str, Any]:
    """Step 1: KB Retrieve filtered to source_category=parts_catalog.

    Returns sub-dict matching spec § Design return shape for candidate_parts.
    """
    query_parts = ["parts catalog"]
    if dtc:
        query_parts.append(dtc)
    if vehicle_make:
        query_parts.append(vehicle_make)
    if vehicle_model:
        query_parts.append(vehicle_model)
    if vehicle_year:
        query_parts.append(vehicle_year)
    query = " ".join(query_parts)

    vsc: dict = {"numberOfResults": 5}
    apply_source_category_filter(
        vsc, {"equals": {"key": "source_category", "value": "parts_catalog"}}
    )

    try:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vsc},
        )
    except (ClientError, Exception) as exc:  # noqa: BLE001
        log.warning("parts_intel _candidate_parts: KB Retrieve failed (%s)", exc)
        return {"found": False, "parts": [], "reason": "KB unavailable"}

    results = resp.get("retrievalResults") or []
    if not results:
        return {"found": False, "parts": []}

    parts = []
    for r in results:
        text = r.get("content", {}).get("text", "")
        parts.append({
            "partNumber": _extract_field(text, "partNumber")
                or _extract_field(text, "part_number") or "unknown",
            "name": _extract_field(text, "name")
                or _extract_field(text, "description") or text[:100],
            "supersession": _extract_field(text, "supersession")
                or _extract_field(text, "supersedes") or "",
            "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
            "score": r.get("score", 0),
        })
    return {"found": True, "parts": parts}


def _recall_check(
    client: Any,
    kb_id: str,
    dtc: str = "",
    vehicle_make: str = "",
    vehicle_model: str = "",
    vehicle_year: str = "",
    vin: str = "",
) -> dict[str, Any]:
    """Step 2: KB Retrieve filtered to source_category=tsb_recall.

    ALWAYS returns authoritative=False.
    NEVER asserts the absence of a recall — only presence within the corpus.
    Disclosure text is _RECALL_DISCLOSURE_MSG, byte-identical in every response.
    """
    query_parts = ["recall TSB"]
    if dtc:
        query_parts.append(dtc)
    if vehicle_make:
        query_parts.append(vehicle_make)
    if vehicle_model:
        query_parts.append(vehicle_model)
    if vehicle_year:
        query_parts.append(vehicle_year)
    if vin:
        query_parts.append(vin)
    query = " ".join(query_parts)

    vsc: dict = {"numberOfResults": 5}
    apply_source_category_filter(
        vsc, {"equals": {"key": "source_category", "value": "tsb_recall"}}
    )

    try:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vsc},
        )
    except (ClientError, Exception) as exc:  # noqa: BLE001
        log.warning("parts_intel _recall_check: KB Retrieve failed (%s)", exc)
        return {
            "found": False,
            "authoritative": False,
            "disclosure": _RECALL_DISCLOSURE_MSG,
            "narration": _RECALL_NO_HIT_NARRATION,
            "hits": [],
        }

    results = resp.get("retrievalResults") or []
    if not results:
        # Not asserting absence — only that no matching recall was found in corpus.
        # `narration` is the verbatim text the LLM MUST surface. It is worded to
        # make an absence claim inexpressible without paraphrasing away the
        # disclosure. See spec § D-3 and issue
        # 2026-09-11-parts-intel-narration-asserts-recall-absence.
        return {
            "found": False,
            "authoritative": False,
            "disclosure": _RECALL_DISCLOSURE_MSG,
            "narration": _RECALL_NO_HIT_NARRATION,
            "hits": [],
            "reason": "no matching recall in the seeded corpus — recall status is not established",
        }

    hits = []
    for r in results:
        text = r.get("content", {}).get("text", "")
        hits.append({
            "recall_id": _extract_field(text, "recall_id")
                or _extract_field(text, "recallId") or "",
            "summary": text[:300],
            "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
            "score": r.get("score", 0),
        })

    return {
        "found": True,
        "authoritative": False,
        "disclosure": _RECALL_DISCLOSURE_MSG,
        # The 9-doc KB has no VIN-range / make-model-year join, so the tool
        # cannot decide whether any hit applies to the caller's vehicle. Encode
        # that as a data field the narration MUST carry, rather than a gap the
        # LLM will fill (which is the T4.4 defect — see spec § D-3 Risk 1).
        "applicability": "undetermined",
        "applicability_reason": _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG,
        "hits": hits,
    }


def _warranty_summary(
    client: Any,
    kb_id: str,
    vehicle_make: str = "",
    vehicle_model: str = "",
    mileage: int | None = None,
) -> dict[str, Any]:
    """Step 3: KB Retrieve filtered to source_category=warranty_policy.

    ALWAYS returns verdict=None.
    verdict_reason is _COVERAGE_NOT_AUTOMATED_MSG, byte-identical in every response.
    Coverage adjudication via warranty_rules.py deferred to v1.1 (spec § D-4).
    """
    query_parts = ["warranty policy coverage"]
    if vehicle_make:
        query_parts.append(vehicle_make)
    if vehicle_model:
        query_parts.append(vehicle_model)
    if mileage is not None:
        query_parts.append(f"{mileage} miles")
    query = " ".join(query_parts)

    vsc: dict = {"numberOfResults": 5}
    apply_source_category_filter(
        vsc, {"equals": {"key": "source_category", "value": "warranty_policy"}}
    )

    try:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vsc},
        )
    except (ClientError, Exception) as exc:  # noqa: BLE001
        log.warning("parts_intel _warranty_summary: KB Retrieve failed (%s)", exc)
        return {
            "narrative": "",
            "verdict": None,
            "verdict_reason": _COVERAGE_NOT_AUTOMATED_MSG,
        }

    results = resp.get("retrievalResults") or []
    narrative_parts = [r.get("content", {}).get("text", "") for r in results]
    narrative = " ".join(narrative_parts[:2])  # top 2 chunks for brevity

    return {
        "narrative": narrative,
        "verdict": None,
        "verdict_reason": _COVERAGE_NOT_AUTOMATED_MSG,
    }


def _dispatch(
    session: Any,
    config: Any,
    vin: str = "",
    dealer_id: str = "",
    description: str = "",
    recall_id: str = "",
) -> dict[str, Any]:
    """Step 4: POST /api/dms/fleet/repair-orders with Cognito JWT.

    Gates on caller having fleet-operator in Cognito groups.
    10s timeout; returns dispatched=false rather than raising on timeout.
    Surfaces endpoint 4xx/5xx bodies verbatim — does not swallow errors.
    Never retries.
    """
    # Gate: caller must have fleet-operator group
    cognito_groups: list[str] = getattr(session, "cognito_groups", []) or []
    if "fleet-operator" not in cognito_groups:
        return {
            "dispatched": False,
            "ro": None,
            "reason": (
                "I can surface the finding, but you don't have permission to open a "
                "repair order (fleet-operator group required); a service advisor can."
            ),
        }

    # Degrade gracefully when DMS endpoint not configured (T3.4 — v1.1 wiring)
    dms_endpoint: str = (
        getattr(config, "dms_api_endpoint", None)
        or os.environ.get(_DMS_API_ENDPOINT_ENV, "")
    )
    if not dms_endpoint:
        return {
            "dispatched": False,
            "ro": None,
            "reason": "DMS API endpoint not configured",
        }

    url = dms_endpoint.rstrip("/") + _DISPATCH_PATH
    jwt: str = getattr(session, "jwt", "") or ""
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "vehicle_vin": vin,
        "dealer_id": dealer_id,
    }
    if description:
        body["description"] = description
    if recall_id:
        body["recall_id"] = recall_id

    try:
        resp = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=_DISPATCH_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        return {
            "dispatched": False,
            "ro": None,
            "reason": "dispatch endpoint timed out",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("parts_intel _dispatch: HTTP error (%s)", exc)
        return {
            "dispatched": False,
            "ro": None,
            "reason": f"dispatch failed: {exc}",
        }

    if resp.status_code == 201:
        try:
            ro_body = resp.json()
        except ValueError:
            ro_body = {"raw": resp.text}
        return {
            "dispatched": True,
            "ro": ro_body,
            "reason": None,
        }

    # 4xx / 5xx: surface verbatim, do not swallow
    try:
        error_body = resp.json()
    except ValueError:
        error_body = {"raw": resp.text}

    return {
        "dispatched": False,
        "ro": None,
        "reason": f"dispatch endpoint returned {resp.status_code}",
        "error_body": error_body,
    }


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def make_parts_intel_tool(session: Any = None, config: Any = None):
    """Build the parts_intel orchestrator tool.

    Args:
        session: SessionContext (used for caller JWT + Cognito groups).
        config:  Config with adp_kb_id, adp_region, dms_api_endpoint, dms_stage.

    Returns a @tool-decorated callable named parts_intel_orchestrate.
    NOT registered on the bidi persona catalog (see module docstring).
    """
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"

    @tool
    def parts_intel_orchestrate(
        dtc: str = "",
        vehicle_make: str = "",
        vehicle_model: str = "",
        vehicle_year: str = "",
        vin: str = "",
        mileage: int | None = None,
        dealer_id: str = "",
        description: str = "",
        recall_id: str = "",
        dispatch_requested: bool = False,
    ) -> dict[str, Any]:
        """Orchestrate parts intelligence: candidate parts, recall check, warranty summary, optional dispatch.

        Prefer this tool over `retrieve` when the question concerns any of:
        recalls, TSBs, active recall campaigns, parts availability or
        supersession, warranty coverage on a specific repair, or opening a
        repair order at a DMS rooftop.

        Use this when the driver or service advisor needs to understand:
        - Which parts are likely involved for a given DTC or symptom
        - Whether any active recall or TSB applies to their vehicle
        - What warranty coverage may apply
        - (Service advisors with fleet-operator) Open a repair order at a DMS rooftop

        Steps run in sequence; each returns a sub-dict in the response.
        recall_check.authoritative is always false in v1 (KB corpus, not NHTSA feed).
        warranty_summary.verdict is always null in v1 (coverage adjudication deferred to v1.1).
        dispatch only fires when dispatch_requested=true AND caller holds fleet-operator.

        NARRATION CONTRACT (spec § D-3, issue 2026-09-11-parts-intel-narration-asserts-recall-absence):

        The response carries pinned text you MUST surface verbatim. Do not
        paraphrase, do not omit, do not decorate with ✅ or other all-clear
        glyphs, do not compress into a bullet:

        - `recall_check.disclosure` — always surface verbatim.
        - `recall_check.narration` — present on every no-hit/unavailable path;
           surface verbatim. Do NOT emit any of: "no active recall", "no recall
           found", "not subject to any recall", "all clear", "no recall", "no
           matching active recall". The narration itself says the corpus did
           not match; do not restate that in your own words.
        - `recall_check.applicability_reason` — present on the hits path when
           `recall_check.applicability == "undetermined"`; surface verbatim.
           Do NOT decide whether any returned recall applies to the caller's
           vehicle. That determination is `undetermined` by contract of this
           tool; assert only what the returned hit `summary` states.

        Args:
            dtc: DTC code (e.g. "P0420").
            vehicle_make: Vehicle manufacturer (e.g. "Ford").
            vehicle_model: Vehicle model (e.g. "Transit").
            vehicle_year: Model year (e.g. "2022").
            vin: Vehicle Identification Number.
            mileage: Current odometer reading in miles.
            dealer_id: DMS rooftop slug (required for dispatch).
            description: Repair description for the RO (optional).
            recall_id: Recall campaign ID to attach to the RO (optional).
            dispatch_requested: Set to true to attempt repair-order creation.

        Returns:
            {
                "candidate_parts": {"found": bool, "parts": [...], "reason": str|None},
                "recall_check":    {
                    "found": bool,
                    "authoritative": False,
                    "disclosure": str,                       # verbatim, always
                    "narration": str,                        # verbatim on no-hit / unavailable
                    "applicability": "undetermined",         # only on hits
                    "applicability_reason": str,             # verbatim on hits
                    "hits": [...],
                },
                "warranty_summary": {"narrative": str, "verdict": None,
                                     "verdict_reason": str},
                "dispatch":        {"dispatched": bool, "ro": {...}|None, "reason": str|None},
            }
        """
        if not adp_kb_id:
            # KB unavailable — degrade all three KB steps; dispatch still gated separately
            kb_unavailable = {"found": False, "parts": [], "reason": "KB unavailable"}
            recall_unavailable = {
                "found": False,
                "authoritative": False,
                "disclosure": _RECALL_DISCLOSURE_MSG,
                "narration": _RECALL_NO_HIT_NARRATION,
                "hits": [],
            }
            warranty_unavailable = {
                "narrative": "",
                "verdict": None,
                "verdict_reason": _COVERAGE_NOT_AUTOMATED_MSG,
            }
            dispatch_result: dict[str, Any]
            if dispatch_requested:
                dispatch_result = _dispatch(
                    session=session,
                    config=config,
                    vin=vin,
                    dealer_id=dealer_id,
                    description=description,
                    recall_id=recall_id,
                )
            else:
                dispatch_result = {"dispatched": False, "ro": None, "reason": None}
            return {
                "candidate_parts": kb_unavailable,
                "recall_check": recall_unavailable,
                "warranty_summary": warranty_unavailable,
                "dispatch": dispatch_result,
            }

        client = boto3.client(
            "bedrock-agent-runtime",
            region_name=adp_region,
            config=_BOTOCORE_CFG,
        )

        # Step 1: candidate parts
        candidates = _candidate_parts(
            client=client,
            kb_id=adp_kb_id,
            dtc=dtc,
            vehicle_make=vehicle_make,
            vehicle_model=vehicle_model,
            vehicle_year=vehicle_year,
        )

        # Step 2: recall check
        recall = _recall_check(
            client=client,
            kb_id=adp_kb_id,
            dtc=dtc,
            vehicle_make=vehicle_make,
            vehicle_model=vehicle_model,
            vehicle_year=vehicle_year,
            vin=vin,
        )

        # Step 3: warranty summary
        warranty = _warranty_summary(
            client=client,
            kb_id=adp_kb_id,
            vehicle_make=vehicle_make,
            vehicle_model=vehicle_model,
            mileage=mileage,
        )

        # Step 4: dispatch (optional, fleet-operator gated)
        if dispatch_requested:
            dispatch_result = _dispatch(
                session=session,
                config=config,
                vin=vin,
                dealer_id=dealer_id,
                description=description,
                recall_id=recall_id,
            )
        else:
            dispatch_result = {"dispatched": False, "ro": None, "reason": None}

        return {
            "candidate_parts": candidates,
            "recall_check": recall,
            "warranty_summary": warranty,
            "dispatch": dispatch_result,
        }

    return parts_intel_orchestrate


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_field(text: str, key: str) -> str:
    """Best-effort extraction of a field from KB text chunk."""
    import re
    pattern = rf'"{key}"\s*:\s*"([^"]+)"'
    m = re.search(pattern, text)
    if m:
        return m.group(1)
    m2 = re.search(rf"{key}:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    return m2.group(1).strip() if m2 else ""
