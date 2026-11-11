"""Rules fallback for the FI quality investigation agent — Stage D task 3.3.

Spec D10 step 4: when both the model call and its one retry fail validation,
the rules fallback fires.  The fallback is deterministic:

  1. Select the top-ranked (``rank=1``) candidate from ``bundle["candidates"]``.
  2. Find the failure mode whose ``symptom_codes`` overlap the pattern's
     ``code_family`` (a code starting with the code_family prefix counts).
  3. If no failure mode matches → ``failure_mode_id = "unknown"``.
  4. If a failure mode is found → pick the first remedy option in catalog order.
  5. Return ``confidence="low"``, ``diagnosis_source="rules"``.

Public API
----------
investigation_fallback(bundle) -> dict
"""
from __future__ import annotations

from typing import Any, Optional


def investigation_fallback(bundle: dict[str, Any]) -> dict[str, Any]:
    """Run the rules fallback for the investigation agent.

    Returns a result dict with the same shape the model would produce
    (minus free-text fields), using deterministic selection rules.

    Parameters
    ----------
    bundle:
        Investigation bundle built by ``bundle.build_bundle()``.

    Returns
    -------
    dict
        ``{candidate_id, failure_mode_id, recommended_option_id,
           confidence, diagnosis_source}``

        ``confidence`` is always ``"low"``.
        ``diagnosis_source`` is always ``"rules"``.
        ``failure_mode_id`` is ``"unknown"`` when no failure mode's
        symptom codes overlap the pattern's code_family.
        ``recommended_option_id`` is the first option in catalog order
        for the chosen failure mode, or ``"monitor"`` when unknown.
    """
    candidates: list[dict[str, Any]] = bundle.get("candidates") or []
    failure_modes: list[dict[str, Any]] = bundle.get("failure_modes") or []
    pattern: dict[str, Any] = bundle.get("pattern") or {}
    code_family: str = pattern.get("code_family", "")

    # Step 1: top-ranked candidate (rank=1; first in list by convention).
    top_candidate = _find_top_candidate(candidates)
    candidate_id = top_candidate.get("candidate_id", "") if top_candidate else ""

    # Step 2: best-matching failure mode (symptom codes overlap code_family).
    # W6 b fix: pass candidate_components map so only failure modes whose component
    # is a candidate cause are considered, ranked by candidate rank.
    candidate_components: dict[str, int] = bundle.get("candidate_components") or {}
    failure_mode = _match_failure_mode(failure_modes, code_family, candidate_components)

    if failure_mode is None:
        # No matching failure mode — unknown-cause path.
        return {
            "candidate_id": candidate_id,
            "failure_mode_id": "unknown",
            "recommended_option_id": "monitor",
            "confidence": "low",
            "diagnosis_source": "rules",
        }

    # Step 3: first option in catalog order (the bundle's list order).
    failure_mode_id = failure_mode.get("failure_mode_id", "unknown")
    remedy_options: list[dict[str, Any]] = failure_mode.get("remedy_options") or []
    recommended_option_id = (
        remedy_options[0].get("remedy_option_id", "monitor")
        if remedy_options else "monitor"
    )

    return {
        "candidate_id": candidate_id,
        "failure_mode_id": failure_mode_id,
        "recommended_option_id": recommended_option_id,
        "confidence": "low",
        "diagnosis_source": "rules",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_top_candidate(
    candidates: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return the candidate with ``rank == 1``, or the first in list if absent."""
    for c in candidates:
        if c.get("rank") == 1:
            return c
    return candidates[0] if candidates else None


def _match_failure_mode(
    failure_modes: list[dict[str, Any]],
    code_family: str,
    candidate_components: Optional[dict[str, int]] = None,
) -> Optional[dict[str, Any]]:
    """Find the failure mode whose symptom codes best match code_family (W6 fix).

    Ranking (D10: "the failure mode whose symptoms match best"):
    1. Exact code match (code == code_family) — highest priority.
    2. Prefix match (code starts with code_family, or code[:4] == code_family[:4]).
    3. Within each match tier: sort by candidate rank (lower = better), then
       by failure_mode_id for stability.

    W6 b fix: if ``candidate_components`` is provided, filter failure modes to
    those whose ``component_id`` appears among the candidate causes before
    ranking.  This prevents the fallback from recommending a recall for a
    component that is not a candidate cause of the pattern.

    W6 fix: an empty ``code_family`` now returns None instead of silently
    naming ``failure_modes[0]``, which could be unrelated to the actual fault.
    Returns None when no failure mode matches (triggering the unknown path).
    """
    # W6 fix: empty code_family → unknown (no arbitrary first-row pick).
    if not code_family:
        return None

    # W6 b fix: filter to candidate-component failure modes when provided.
    # If candidate_components is non-empty, only consider FMs whose component is
    # in the candidate causes.  An empty candidate_components (candidates all being
    # plant/build_window with no component mapping) means no filter is applied.
    fms = failure_modes
    if candidate_components:
        fms = [
            fm for fm in fms
            if fm.get("component_id", "") in candidate_components
        ]
        # Do NOT fall back to all when the filter yields nothing: an empty pool
        # means no candidate-cause FM exists for this code family, so the
        # fallback should return None (→ unknown path).  The job.py code
        # already falls back to all component_ids when candidate_components is
        # empty (when no candidate has a component dimension).

    prefix4 = code_family[:4]
    exact_matches: list[dict[str, Any]] = []
    prefix_matches: list[dict[str, Any]] = []

    for fm in fms:
        symptom_codes: list = fm.get("symptom_codes") or []
        matched_exact = False
        matched_prefix = False
        for code in symptom_codes:
            if not isinstance(code, str):
                continue
            if code == code_family:
                matched_exact = True
                break
            if code.startswith(code_family) or code[:4] == prefix4:
                matched_prefix = True
        if matched_exact:
            exact_matches.append(fm)
        elif matched_prefix:
            prefix_matches.append(fm)

    best_pool = exact_matches or prefix_matches
    if not best_pool:
        return None

    # W6 b fix: rank by exact>prefix (captured by best_pool) then by candidate rank
    # (lower rank = higher priority) then by failure_mode_id for stability.
    def _rank_key(fm: dict[str, Any]) -> tuple:
        comp_id = fm.get("component_id", "")
        crank = (candidate_components or {}).get(comp_id, 9999)
        return (crank, fm.get("failure_mode_id", ""))

    return sorted(best_pool, key=_rank_key)[0]
