"""policy.py — FI rebalancing's U9 thresholds, read from configuration, never from code.

The values live in ``deployment/config/avx-policy-defaults.json`` under the key
``/vsa/{stage}/avx/fi-rebalancing/policy`` and are seeded into SSM Parameter Store by
``deployment/scripts/seed_avx_policy_params.sh``, as stage B's cost cap is
(``agents/tier2/runner.FI_COST_MAX_DIAGNOSES_PARAM``). The Tier 2 runner loads the parameter at
the start of every ``rebalance`` run (:func:`load_from_ssm`) and installs it for that run
(:func:`active`); agent code reads :func:`current`.

Nothing here holds a default value. An absent, unreadable or malformed parameter stops the run
before any read or write (:class:`PolicyUnavailable`), and the run record says ``aborted``: a run
on thresholds nobody chose would recommend moves nobody approved. (Stage B falls back to a
built-in default instead; here a default would be a literal in code, which
``tests/test_policy_no_literals.py`` forbids.)

Keys (exactly these; contract ``docs/fi-rebalancing-contract.md`` § 11):

* ``target_utilization`` — a source must stay at or under this forecast utilisation after its
  moves (spec D8 step 7); also the level ADP's view counts vehicles short/spare against.
* ``deficit_above`` — forecast utilisation above this marks a deficit day (first deficit day).
* ``max_utilization`` — the most of a vehicle's operating hours that can be on rent (D9).
* ``min_net_benefit_usd`` — move candidates under this net benefit are dropped (D8 step 2).
* ``max_vehicles_per_move`` — the most vehicles one move item names.

``surplus_below`` is not here: only ADP's ``supply_demand_gaps`` view applies it (from ADP's
``fleet_bookings_parameters`` table); the agent reads each location's status from the view's
snapshot. ``deficit_above`` and ``target_utilization`` are applied on both sides, so ADP's
parameters row and this parameter must agree; the live check compares them.
"""
from __future__ import annotations

import contextlib
import json
import math
from typing import Any, Iterator

#: The SSM parameter (``{stage}`` substituted) and the config file's ``_key_template``.
POLICY_PARAM: str = "/vsa/{stage}/avx/fi-rebalancing/policy"

_RATIO_KEYS: tuple[str, ...] = ("target_utilization", "deficit_above", "max_utilization")
POLICY_KEYS: frozenset[str] = frozenset(
    {*_RATIO_KEYS, "min_net_benefit_usd", "max_vehicles_per_move"}
)

#: Upper bounds that turn a typo into a refusal rather than a surprise.
_NET_BENEFIT_CEILING_USD: int = 1_000_000
_VEHICLES_CEILING: int = 100


class PolicyUnavailable(RuntimeError):
    """The rebalancing policy is absent, unreadable or malformed; the run must not proceed."""


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyUnavailable(f"policy {key} must be a number; got {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise PolicyUnavailable(f"policy {key} must be finite")
    return out


def parse(value: Any) -> dict[str, Any]:
    """The validated policy dict from a JSON string or a dict, else :class:`PolicyUnavailable`.

    Exactly :data:`POLICY_KEYS`; ratios in (0, 1] with
    ``target_utilization < deficit_above <= max_utilization``; ``min_net_benefit_usd`` from 0 to
    1,000,000; ``max_vehicles_per_move`` an integer from 1 to 100.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise PolicyUnavailable(f"policy is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyUnavailable(f"policy must be a JSON object; got {type(value).__name__}")
    missing, extra = POLICY_KEYS - value.keys(), value.keys() - POLICY_KEYS
    if missing or extra:
        raise PolicyUnavailable(
            f"policy keys must be exactly {sorted(POLICY_KEYS)}; "
            f"missing {sorted(missing)}, extra {sorted(extra)}"
        )
    out: dict[str, Any] = {}
    for key in _RATIO_KEYS:
        ratio = _number(value[key], key)
        if not 0 < ratio <= 1:
            raise PolicyUnavailable(f"policy {key} must be in (0, 1]; got {ratio}")
        out[key] = ratio
    if not out["target_utilization"] < out["deficit_above"] <= out["max_utilization"]:
        raise PolicyUnavailable(
            "policy must have target_utilization < deficit_above <= max_utilization"
        )
    net = _number(value["min_net_benefit_usd"], "min_net_benefit_usd")
    if not 0 <= net <= _NET_BENEFIT_CEILING_USD:
        raise PolicyUnavailable(
            f"policy min_net_benefit_usd must be from 0 to {_NET_BENEFIT_CEILING_USD}; got {net}"
        )
    out["min_net_benefit_usd"] = net
    veh = value["max_vehicles_per_move"]
    if isinstance(veh, bool) or not isinstance(veh, int) or not 1 <= veh <= _VEHICLES_CEILING:
        raise PolicyUnavailable(
            f"policy max_vehicles_per_move must be an integer from 1 to {_VEHICLES_CEILING}"
        )
    out["max_vehicles_per_move"] = veh
    return out


def param_name(stage: str) -> str:
    """The SSM parameter name for *stage*."""
    if not stage or "/" in stage or "{" in stage:
        raise PolicyUnavailable(f"bad stage {stage!r}")
    return POLICY_PARAM.replace("{stage}", stage)


def load_from_ssm(stage: str, ssm_client: Any) -> dict[str, Any]:
    """Read and validate the stage's policy parameter, or raise :class:`PolicyUnavailable`."""
    name = param_name(stage)
    try:
        raw = ssm_client.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 — any read failure stops the run (fail closed)
        raise PolicyUnavailable(f"{name}: {type(exc).__name__}") from exc
    try:
        return parse(raw)
    except PolicyUnavailable as exc:
        raise PolicyUnavailable(f"{name}: {exc}") from exc


_ACTIVE: dict[str, Any] | None = None


@contextlib.contextmanager
def active(policy: Any) -> Iterator[dict[str, Any]]:
    """Install a validated *policy* for the duration of the block, then restore the previous one."""
    global _ACTIVE
    parsed = parse(policy)
    previous = _ACTIVE
    _ACTIVE = parsed
    try:
        yield dict(parsed)
    finally:
        _ACTIVE = previous


def current() -> dict[str, Any]:
    """A copy of the installed policy, or :class:`PolicyUnavailable` when none is installed."""
    if _ACTIVE is None:
        raise PolicyUnavailable(
            "no FI rebalancing policy is installed; the runner loads it from SSM "
            f"({POLICY_PARAM}) at the start of each run"
        )
    return dict(_ACTIVE)
