"""Validator fallback runner for fi_rebalancing (spec D8 step 7).

Copied from ``agents/fi_recall_warranty/runner.py`` (decisions call 46 — the Stage
B wrapper pattern is copied, not imported, so each stage can evolve its validator
without churn in the sister stages).  Only the one function belongs here; the
sort/cap/skip helpers in fi_recall_warranty's runner.py are specific to that
stage's recall/warranty split.
"""
from __future__ import annotations

from typing import Any, Callable


def run_with_validator_fallback(
    *,
    model_fn: Callable[..., Any],
    rules_fn: Callable[[Exception], Any],
    max_retries: int = 1,
) -> Any:
    """Call *model_fn* up to ``max_retries + 1`` times, then fall back to *rules_fn*.

    First call: ``model_fn(prev_error=None)``.
    Each retry:  ``model_fn(prev_error=<last exception>)``.
    If every model attempt raises, ``rules_fn(last_exception)`` is returned.

    Spec D8 step 7 ("one retry with the validation error, then the rules
    fallback") → ``max_retries=1`` → two total model attempts.
    """
    last_exc: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            return model_fn(prev_error=last_exc)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    return rules_fn(last_exc)
