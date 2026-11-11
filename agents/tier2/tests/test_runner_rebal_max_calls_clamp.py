"""Tests — FI_REBAL_MAX_MODEL_CALLS clamped to [0, 100] (T1.3 item 9).

``runner.py`` clamped to [0, 100] via ``max(0, min(parsed, 100))``:
    negative  → 0
    > 100     → 100
    non-int   → 12 (default)
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.tier2 import artifact_writer, runner


# ---------------------------------------------------------------------------
# Shared test fixture helpers (mirrors test_run_rebalance.py pattern)
# ---------------------------------------------------------------------------

class _FakeBoto3:
    @staticmethod
    def client(service: str, **_kw: Any) -> object:
        return MagicMock(name=service)


class _FakeWriter:
    def __init__(self, *, table_name: str, dynamodb_client: Any) -> None:
        pass
    def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                      update_on_same_hash=False):
        return {}


def _run_with_max_calls(monkeypatch, env_value: str) -> dict:
    captured: dict[str, Any] = {}

    def fake_run_rebalance(stage, **kw):
        captured.update(kw)
        return {"status": "ok", "move_items_written": 0, "gap_items_written": 0,
                "items_unchanged": 0, "model_calls": 0, "tokens_used": 0, "cost_usd": 0.0,
                "grounding_read_errors": 0, "items_refused": 0, "vehicles_checked": 0,
                "open_items_read_errors": 0, "locations_incomplete": 0,
                "gap_items_without_vehicle": 0, "forecast_days_missing": 0,
                "candidates_forecast_days_missing": 0,
                "metros_skipped_unchanged": 0}

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _FakeWriter)
    monkeypatch.setattr(runner, "run_rebalance", fake_run_rebalance)
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
    monkeypatch.setenv("FI_REBAL_MAX_MODEL_CALLS", env_value)
    monkeypatch.delenv("FI_COST_GUARDRAIL_ID", raising=False)

    runner._run_rebalance_from_env("staging")
    return captured


def test_max_model_calls_clamped_upper_bound(monkeypatch):
    """Value 150 (> 100) must be clamped to 100."""
    captured = _run_with_max_calls(monkeypatch, "150")
    assert captured.get("max_model_calls_per_run") == 100, (
        f"Expected 100 after clamping 150; got {captured.get('max_model_calls_per_run')}"
    )


def test_max_model_calls_clamped_at_101(monkeypatch):
    """Value 101 (one over upper bound) must be clamped to 100."""
    captured = _run_with_max_calls(monkeypatch, "101")
    assert captured.get("max_model_calls_per_run") == 100


def test_max_model_calls_clamped_negative(monkeypatch):
    """Negative value (-1) must be clamped to 0."""
    captured = _run_with_max_calls(monkeypatch, "-1")
    assert captured.get("max_model_calls_per_run") == 0, (
        f"Expected 0 after clamping -1; got {captured.get('max_model_calls_per_run')}"
    )


def test_max_model_calls_clamped_large_negative(monkeypatch):
    """Large negative value must be clamped to 0."""
    captured = _run_with_max_calls(monkeypatch, "-100")
    assert captured.get("max_model_calls_per_run") == 0


def test_max_model_calls_100_passes_through(monkeypatch):
    """Value 100 (at upper bound) passes through unchanged."""
    captured = _run_with_max_calls(monkeypatch, "100")
    assert captured.get("max_model_calls_per_run") == 100


def test_max_model_calls_0_passes_through(monkeypatch):
    """Value 0 (at lower bound) passes through unchanged."""
    captured = _run_with_max_calls(monkeypatch, "0")
    assert captured.get("max_model_calls_per_run") == 0


def test_max_model_calls_default_12(monkeypatch):
    """Default (env var not set) → 12."""
    captured: dict[str, Any] = {}

    def fake_run_rebalance(stage, **kw):
        captured.update(kw)
        return {"status": "ok", "move_items_written": 0, "gap_items_written": 0,
                "items_unchanged": 0, "model_calls": 0, "tokens_used": 0, "cost_usd": 0.0,
                "grounding_read_errors": 0, "items_refused": 0, "vehicles_checked": 0,
                "open_items_read_errors": 0, "locations_incomplete": 0,
                "gap_items_without_vehicle": 0, "forecast_days_missing": 0,
                "candidates_forecast_days_missing": 0,
                "metros_skipped_unchanged": 0}

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _FakeWriter)
    monkeypatch.setattr(runner, "run_rebalance", fake_run_rebalance)
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
    monkeypatch.delenv("FI_REBAL_MAX_MODEL_CALLS", raising=False)
    monkeypatch.delenv("FI_COST_GUARDRAIL_ID", raising=False)

    runner._run_rebalance_from_env("staging")
    assert captured.get("max_model_calls_per_run") == 12


def test_max_model_calls_non_int_falls_back_to_12(monkeypatch):
    """Non-integer value ('abc') → ValueError → default 12."""
    captured = _run_with_max_calls(monkeypatch, "abc")
    assert captured.get("max_model_calls_per_run") == 12
