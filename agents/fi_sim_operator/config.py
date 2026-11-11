"""Configuration parser for the simulated fleet operator (spec D10a, task 11.2a).

Pure: no boto3, no AWS calls.  The Lambda handler (task 11.3) reads the SSM
parameter and passes the parsed JSON dict here.

Public API
----------
parse_config(raw) -> OperatorConfig | ConfigError
    Parse the SSM JSON dict.  Returns a typed error on any malformed field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.executors.simulated_operator import DEMO_FLEET_IDS

_MAX_CAP: int = 20


@dataclass(frozen=True)
class OperatorConfig:
    """Validated configuration for one Lambda invocation."""

    #: Demo fleet ids whose ``enabled`` is ``True``.
    enabled_fleets: frozenset[str]
    #: Decisions per fleet per UTC day, clamped to ``_MAX_CAP``.
    daily_cap: int
    #: VINs that must never be decided (held by id).
    hold_vins: frozenset[str]
    #: Vehicle ids that must never be decided (held by vehicle id).
    hold_vehicle_ids: frozenset[str]
    #: Fleet ids present in the parameter but not in ``DEMO_FLEET_IDS``.
    #: Logged by the run; never acted on.
    non_demo_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConfigError:
    """A typed refusal: the parameter is missing or a field is malformed."""

    reason: str


def parse_config(raw: Any) -> OperatorConfig | ConfigError:
    """Parse the SSM JSON dict; return a :class:`ConfigError` on any bad field.

    Rules (spec D10a):
    - ``fleets``: a dict; each value must be a dict.  A fleet runs only when
      its id is in :data:`~agents.executors.simulated_operator.DEMO_FLEET_IDS`
      **and** ``enabled is True`` (the boolean ``True``; the string ``"true"``
      is **off**).  Non-demo ids are collected into ``non_demo_ids`` and never
      acted on.
    - ``daily_cap``: an ``int`` (not a ``bool``, not a ``float``, not a
      string).  Clamped to ``_MAX_CAP`` (20) from above.
    - ``hold_vins``, ``hold_vehicle_ids``: optional lists of strings; absent
      means empty.
    """
    if not isinstance(raw, dict):
        return ConfigError("parameter must be a JSON object")

    # ── fleets ──────────────────────────────────────────────────────────────
    fleets = raw.get("fleets")
    if not isinstance(fleets, dict):
        return ConfigError("'fleets' must be a JSON object")

    enabled: list[str] = []
    non_demo: list[str] = []
    for fleet_id, fleet_cfg in fleets.items():
        if not isinstance(fleet_cfg, dict):
            return ConfigError(
                f"fleet entry {fleet_id!r} must be a JSON object; got {type(fleet_cfg).__name__}"
            )
        if fleet_id not in DEMO_FLEET_IDS:
            non_demo.append(fleet_id)
            continue
        # Only the boolean True enables a fleet; "true", 1, truthy non-bool all fail closed.
        if fleet_cfg.get("enabled") is True:
            enabled.append(fleet_id)

    # ── daily_cap ────────────────────────────────────────────────────────────
    cap = raw.get("daily_cap")
    if cap is None:
        return ConfigError("'daily_cap' is required")
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(cap, bool) or not isinstance(cap, int):
        return ConfigError(
            f"'daily_cap' must be an integer; got {type(cap).__name__}"
        )
    # S2: D10a says "an int 0-20"; negative values are invalid.
    if cap < 0:
        return ConfigError("'daily_cap' must be 0 or greater")

    # ── hold lists ───────────────────────────────────────────────────────────
    hold_vins_raw = raw.get("hold_vins", [])
    if not isinstance(hold_vins_raw, list):
        return ConfigError("'hold_vins' must be a list")
    if not all(isinstance(v, str) for v in hold_vins_raw):
        return ConfigError("'hold_vins' entries must be strings")

    hold_ids_raw = raw.get("hold_vehicle_ids", [])
    if not isinstance(hold_ids_raw, list):
        return ConfigError("'hold_vehicle_ids' must be a list")
    if not all(isinstance(v, str) for v in hold_ids_raw):
        return ConfigError("'hold_vehicle_ids' entries must be strings")

    return OperatorConfig(
        enabled_fleets=frozenset(enabled),
        daily_cap=min(cap, _MAX_CAP),
        hold_vins=frozenset(hold_vins_raw),
        hold_vehicle_ids=frozenset(hold_ids_raw),
        non_demo_ids=tuple(non_demo),
    )
