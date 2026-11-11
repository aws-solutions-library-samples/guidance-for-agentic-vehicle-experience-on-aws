"""Validator for the fi_rebalancing model tool output (spec D8 step 7).

Fail-closed throughout: any input that does not satisfy every constraint
raises :class:`ValidationError`.  The validator never writes to the environment,
makes no AWS calls, and does not import anything from the stack.

The spec D8 step 7 constraints the validator enforces are:

* Schema (strict): top-level keys ``moves`` and ``holds`` only; each move has
  exactly ``candidate_id``, ``vehicle_vins``, ``timing``, ``detail``,
  ``evidence_refs``; each hold has exactly ``gap_id``, ``reason``, ``detail``.
* Every ``candidate_id`` is one of the ids in the bundle; every ``gap_id`` is
  one of the gap ids in the bundle.
* ``moves[i].vehicle_vins`` is a non-empty subset (not multiset) of the named
  candidate's ``eligible_vins``, length between 1 and the candidate's ``count``.
* Each VIN appears in at most one move across the whole plan, and in no move
  that is already in ``used_vins_global``.
* Each ``source`` (``from_location_id``) is still at or under
  ``source_util_cap`` (default: the policy's ``target_utilization``) forecast utilisation
  after all its moves.
* ``moves[i].detail`` is a string of length 1..600; ``holds[i].detail`` is a
  string of length 1..300.
* Every entry in ``moves[i].evidence_refs`` is in the bundle's
  ``evidence_kinds`` allowlist.
* ``timing`` is ``today`` or ``within_3_days``.
* ``reason`` is one of ``temporary_event``, ``returns_expected``,
  ``not_worth_cost``, ``unclear``.
* A ``temporary_event`` hold must have at least one announced event in the
  bundle for its gap's location and ISO forecast week.
* Something actionable must exist: both ``moves`` and ``holds`` empty is a
  ``ValidationError`` (D8 step 6 means the model is asked to say something).
* No location gets both a move into it and a hold on its deficit: the hold would
  never reach a gap item, so the operator would not see it (Group 5 review cycle 1
  Suggestion 2; user's answer 2026-10-14). The retry, then the rules fallback, run.

The recomputed net-benefit check (D8 step 7, "net benefit recomputed for the
chosen vehicles still at or over the minimum") is enforced in ``agent.py`` at
the plan-build step via :func:`agents.fi_rebalancing.candidates.money_for`,
which raises :class:`ValueError` if the required per-day inputs are missing
and whose returned ``net_benefit_usd`` is compared against
``min_net_benefit_usd``.  Keeping the money recomputation in the agent (where
``money_for`` already lives) avoids a second import of the money code in the
validator.
"""
from __future__ import annotations

from typing import Any, Iterable

from agents.fi_rebalancing import policy as rebal_policy


class ValidationError(Exception):
    """Raised when model output fails one of D8 step 7's rules."""


# --- Allowed enums per spec D8 step 6 -----------------------------------
_MOVE_TIMINGS: frozenset[str] = frozenset({"today", "within_3_days"})
_HOLD_REASONS: frozenset[str] = frozenset(
    {"temporary_event", "returns_expected", "not_worth_cost", "unclear"}
)

# --- Schema (strict: no extras, no missing) -----------------------------
_TOP_REQUIRED: frozenset[str] = frozenset({"moves", "holds"})
_MOVE_REQUIRED: frozenset[str] = frozenset(
    {"candidate_id", "vehicle_vins", "timing", "detail", "evidence_refs"}
)
_HOLD_REQUIRED: frozenset[str] = frozenset({"gap_id", "reason", "detail"})

_DETAIL_MAX_MOVE: int = 600
_DETAIL_MAX_HOLD: int = 300


def validate_rebalance_plan(
    tool_input: dict,
    *,
    metro_bundle: dict,
    used_vins_global: Iterable[str] = (),
    source_util_cap: float | None = None,
) -> None:
    """Raise :class:`ValidationError` if *tool_input* fails any D8 step 7 rule.

    The caller supplies *metro_bundle*, the context the model saw.  It must have:

    * ``candidates``: list of dicts, each with ``candidate_id``,
      ``from_location_id``, ``to_location_id``, ``count``, ``eligible_vins``
      (list[str]).
    * ``gap_ids``: iterable of valid ``gap_id`` strings for this metro.
    * ``gap_lookup``: ``{gap_id: (location_id, forecast_week)}`` for every
      ``gap_id`` in ``gap_ids`` — the forecast week is the gap item's ISO week.
    * ``sources_by_id``: ``{from_location_id: {supply_vehicles, op_hours,
      forecast_hours}}`` for each source referenced by any candidate.
    * ``events``: list of announced events, each at least
      ``{location_id, forecast_week}``.
    * ``evidence_kinds``: iterable of allowed strings for every
      ``evidence_refs`` entry.

    *used_vins_global* is the set of VINs already assigned to a move earlier in
    the same run (across previously processed metros).  A VIN that appears
    there cannot be re-used in this metro.
    """
    if source_util_cap is None:
        source_util_cap = rebal_policy.current()["target_utilization"]
    _require_type(tool_input, dict, "tool_input")
    _check_keys(tool_input, _TOP_REQUIRED, "tool_input")

    moves = tool_input["moves"]
    holds = tool_input["holds"]
    _require_type(moves, list, "moves")
    _require_type(holds, list, "holds")

    if not moves and not holds:
        raise ValidationError(
            "tool_input has neither moves nor holds — the model must say something "
            "when it is called (spec D8 step 6)"
        )

    cands_by_id: dict[str, dict] = {c["candidate_id"]: c for c in metro_bundle["candidates"]}
    gap_ids: set[str] = set(metro_bundle["gap_ids"])
    gap_lookup: dict[str, tuple[str, str]] = dict(metro_bundle["gap_lookup"])
    sources_by_id: dict[str, dict] = dict(metro_bundle["sources_by_id"])
    events: list[dict] = list(metro_bundle["events"])
    allowed_evidence: set[str] = set(metro_bundle["evidence_kinds"])

    # --- moves ---------------------------------------------------------
    seen_vins: set[str] = set(used_vins_global)
    moved_per_source: dict[str, int] = {}

    for i, mv in enumerate(moves):
        path = f"moves[{i}]"
        _require_type(mv, dict, path)
        _check_keys(mv, _MOVE_REQUIRED, path)

        cand = cands_by_id.get(mv["candidate_id"])
        if cand is None:
            raise ValidationError(
                f"{path}.candidate_id {mv['candidate_id']!r} is not in the bundle"
            )

        vins = mv["vehicle_vins"]
        _require_type(vins, list, f"{path}.vehicle_vins")
        if any(not isinstance(v, str) for v in vins):
            raise ValidationError(f"{path}.vehicle_vins must be a list of strings")

        count_cap = int(cand["count"])
        if not (1 <= len(vins) <= count_cap):
            raise ValidationError(
                f"{path}.vehicle_vins must have 1..{count_cap} entries; got {len(vins)}"
            )
        if len(set(vins)) != len(vins):
            raise ValidationError(f"{path}.vehicle_vins contains a duplicate VIN")

        eligible = set(cand["eligible_vins"])
        for vin in vins:
            if vin not in eligible:
                raise ValidationError(
                    f"{path}.vehicle_vins contains {vin!r}, which is not in "
                    f"candidate {cand['candidate_id']!r}'s eligible list"
                )
            if vin in seen_vins:
                raise ValidationError(
                    f"{path}.vehicle_vins reuses VIN {vin!r} already assigned"
                )
            seen_vins.add(vin)

        if mv["timing"] not in _MOVE_TIMINGS:
            raise ValidationError(
                f"{path}.timing {mv['timing']!r} must be one of {sorted(_MOVE_TIMINGS)}"
            )
        _check_string(mv["detail"], max_len=_DETAIL_MAX_MOVE, path=f"{path}.detail")
        _check_evidence_refs(mv["evidence_refs"], allowed_evidence, path)

        src_id = cand["from_location_id"]
        moved_per_source[src_id] = moved_per_source.get(src_id, 0) + len(vins)

    # --- forecast utilisation cap per source ---------------------------
    for src_id, moved in moved_per_source.items():
        src = sources_by_id.get(src_id)
        if src is None:
            # If a source referenced by a candidate is not in the bundle, that
            # is a bundle construction bug, not a model error.  Fail closed:
            # refusing here still keeps the production surface safe.
            raise ValidationError(
                f"candidate source {src_id!r} missing from the bundle "
                "(sources_by_id)"
            )
        util_after = _util_after_removal(src, moved)
        if util_after > source_util_cap:
            raise ValidationError(
                f"source {src_id!r} forecast utilisation after moves is "
                f"{util_after:.3f} > cap {source_util_cap}"
            )

    # --- holds ---------------------------------------------------------
    for i, h in enumerate(holds):
        path = f"holds[{i}]"
        _require_type(h, dict, path)
        _check_keys(h, _HOLD_REQUIRED, path)
        if h["gap_id"] not in gap_ids:
            raise ValidationError(
                f"{path}.gap_id {h['gap_id']!r} is not in the bundle"
            )
        if h["reason"] not in _HOLD_REASONS:
            raise ValidationError(
                f"{path}.reason {h['reason']!r} must be one of {sorted(_HOLD_REASONS)}"
            )
        _check_string(h["detail"], max_len=_DETAIL_MAX_HOLD, path=f"{path}.detail")
        if h["reason"] == "temporary_event":
            gap_loc, gap_week = gap_lookup[h["gap_id"]]
            if not any(
                e.get("location_id") == gap_loc and e.get("forecast_week") == gap_week
                for e in events
            ):
                raise ValidationError(
                    f"{path} reason=temporary_event, but no event is announced "
                    f"for location {gap_loc!r} in week {gap_week!r}"
                )

    # --- a move and a hold on the same deficit -------------------------
    # A hold says why a deficit gets no move. The agent writes a hold only on a deficit
    # left without a move, so a hold on a location that also gets a move vanishes without
    # a trace. Refuse the combination; the caller's retry and then the rules run.
    move_destinations = {cands_by_id[mv["candidate_id"]]["to_location_id"] for mv in moves}
    held_locations: set[str] = set()
    for h in holds:
        looked_up = gap_lookup.get(h["gap_id"])
        if looked_up is None:
            raise ValidationError(
                f"gap_id {h['gap_id']!r} is missing from the bundle's gap_lookup"
            )
        held_locations.add(looked_up[0])
    both = sorted(move_destinations & held_locations)
    if both:
        raise ValidationError(
            f"plan has both a move into and a hold on {both}; a deficit gets moves or a "
            "hold, not both"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_keys(obj: dict, required: frozenset[str], path: str) -> None:
    missing = set(required) - obj.keys()
    extra = obj.keys() - set(required)
    if missing:
        raise ValidationError(f"{path} missing required keys: {sorted(missing)}")
    if extra:
        raise ValidationError(f"{path} has extra keys: {sorted(extra)}")


def _require_type(v: Any, t: type, path: str) -> None:
    if not isinstance(v, t):
        raise ValidationError(
            f"{path} must be {t.__name__}; got {type(v).__name__}"
        )


def _check_string(v: Any, *, max_len: int, path: str) -> None:
    if not isinstance(v, str):
        raise ValidationError(f"{path} must be a string")
    if not (1 <= len(v) <= max_len):
        raise ValidationError(f"{path} length {len(v)} outside [1, {max_len}]")


def _check_evidence_refs(refs: Any, allowed: set[str], path: str) -> None:
    _require_type(refs, list, f"{path}.evidence_refs")
    for r in refs:
        if not isinstance(r, str):
            raise ValidationError(
                f"{path}.evidence_refs must be a list of strings"
            )
        if r not in allowed:
            raise ValidationError(
                f"{path}.evidence_refs contains {r!r}, which is not in the "
                f"bundle's allowed evidence_kinds"
            )


def _util_after_removal(src: dict, moved: int) -> float:
    """Forecast utilisation at *src* after *moved* vehicles leave.

    utilisation = forecast_hours / ((supply_vehicles - moved) * op_hours).

    If every vehicle is removed and forecast hours remain, utilisation is
    infinite (we would fail the cap).  If both numerator and denominator are
    zero, utilisation is zero (nothing left to serve, no demand to serve).
    """
    supply_after = max(0, int(src["supply_vehicles"]) - moved)
    forecast = float(src.get("forecast_hours", 0.0))
    op_hours = float(src.get("op_hours", 0.0))
    denom = supply_after * op_hours
    if denom <= 0:
        return float("inf") if forecast > 0 else 0.0
    return forecast / denom
