"""Mutation-caught tests for the auto-approver — pin that `_move_within_bounds`
treats cost == 0 as a valid lower bound.

See ``agents/executors/tests/test_mutation_catches_p100.py`` for the broader
framing and the sweep (prompt 100, 2026-10-24) that identified this survivor.

Pick 10
-------
    file: lambdas/avx-auto-approver/handler.py:142
    kind: NUM_OFFBYONE_PLUS
    mutation: ``cost < 0`` -> ``cost < 1``

The transfer_cost_usd lower bound at line 142 reads
``if cost is None or cost < 0 or cost > bounds["max_limit_usd"]: return None``.
Flipping the literal ``0`` to ``1`` turns the predicate into ``cost < 1``,
which refuses any zero-cost move. A zero-cost move is legitimate — e.g.
rebalancing within the same yard where carrier fees do not apply — and the
rule-decided path should still approve it under the ceiling.

The test pins the boundary by asserting the function returns the exact cost
(0.0) rather than None.
"""
from __future__ import annotations

import os
import sys


# ── sys.path setup (follows test_handler.py convention) ────────────────────
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


_VIN = "MRDVEH00000000015"
_FLEET = "fleet-meridian-01"

_VALID_PARAMS = {
    "fleet_id": _FLEET,
    "from_location_id": "LAX-1",
    "to_location_id": "SFO-2",
    "forecast_week": "2026-W01",
    "vehicle_vins": [_VIN],
    "predicted_on_rent_hours_gain": 10.0,
    "predicted_turnaways_avoided": 2.0,
    # transfer_cost_usd filled by each test case.
}

_BOUNDS = {
    "max_limit_usd": 10000.0,
    "max_vehicles_per_auto_move": 5,
}


def test_move_within_bounds_accepts_zero_cost():
    """A move with transfer_cost_usd == 0 is within bounds.

    Pins ``handler.py:142`` lower-bound literal. If flipped to
    ``cost < 1``, the function returns None for a zero-cost move. The
    rule-approved path would never auto-approve a legitimately-free move.
    """
    import handler as h  # noqa: PLC0415

    params = dict(_VALID_PARAMS)
    params["transfer_cost_usd"] = 0.0

    result = h._move_within_bounds(_FLEET, params, _BOUNDS)
    assert result == 0.0, (
        f"zero-cost move must be accepted (returning the cost); got {result!r}"
    )


def test_move_within_bounds_rejects_negative_cost():
    """The lower bound is strictly ``cost >= 0`` (not ``> 0``).

    This companion test pins the other half of the boundary: ``cost < 0``
    continues to refuse, so a mutation to ``cost <= 0`` (which would make the
    lower bound exclusive of 0) would still be caught. The two tests together
    pin the exact half-open interval ``[0, max_limit_usd]``.
    """
    import handler as h  # noqa: PLC0415

    params = dict(_VALID_PARAMS)
    params["transfer_cost_usd"] = -0.01

    result = h._move_within_bounds(_FLEET, params, _BOUNDS)
    assert result is None, (
        f"negative-cost move must be refused; got {result!r}"
    )
