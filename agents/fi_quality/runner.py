"""Retry-then-fallback wrapper for the investigation agent's model calls (spec D10 step 4).

One attempt plus one retry per call; any exception is a failed attempt, and the
``ModelBudgetStop`` raised when the investigation has no budget left ends the attempts at
once.  The fallback result always carries ``confidence: low`` and
``diagnosis_source: rules``, whatever ``fallback_fn`` returned.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from agents.fi_fleet_health.runner import ModelBudgetStop

log = logging.getLogger(__name__)


def run_with_investigation_fallback(
    *,
    model_fn: Callable[[], Any],
    fallback_fn: Callable[[], dict[str, Any]],
    max_retries: int = 1,
) -> dict[str, Any]:
    """Return ``{"result": <model_fn() value>, "diagnosis_source": "model"}`` merged for a
    dict result, or the forced-rules ``fallback_fn()`` result after the attempts fail."""
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    for _attempt in range(1 + max_retries):
        try:
            value = model_fn()
        except ModelBudgetStop:
            break
        except Exception as exc:  # noqa: BLE001 — every model failure is a failed attempt
            log.info("fi_quality: model attempt failed (%s)", type(exc).__name__)
            continue
        if isinstance(value, dict):
            return {**value, "diagnosis_source": "model"}
        return {"result": value, "diagnosis_source": "model"}
    result = dict(fallback_fn())
    result["confidence"] = "low"
    result["diagnosis_source"] = "rules"
    return result
