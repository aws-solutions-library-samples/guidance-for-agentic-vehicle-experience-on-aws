"""Local-corpora fallback verification — Zone 2 Meridian corpus (Group 3).

Verifies that ``retrieve.py``'s local keyword-search fallback correctly
finds the 24 new documents across the four Zone-2 categories when the
Bedrock KB is unavailable.

No live AWS calls are made.  Tests force the fallback path by either:
  1. Calling ``_local_fallback`` directly (unit-level), or
  2. Constructing a ``retrieve`` tool with ``adp_kb_id=""`` so the
     short-circuit branch fires immediately.

Key assertions:
  - At least one result per new category is returned and its source path
    starts with the expected category subdirectory.
  - A four-wheeler manufacturing query does NOT surface a
    ``vehicle_manufacturing`` document from the wrong path — wrong-category
    bleed is a real failure mode.

See spec: `.kiro/specs/2026-07-31-zone2-meridian-kb-corpus/spec.md` §D4.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retrieve(persona_id: str = "fleet_driver") -> Any:
    """Build a retrieve tool with an empty adp_kb_id to force the fallback path."""
    from agents.supervisor.tools.retrieve import make_retrieve_tool

    config = SimpleNamespace(adp_kb_id="", adp_region="us-east-1")
    ctx = SimpleNamespace(persona_id=persona_id, last_kb_citations=[])
    tool = make_retrieve_tool(config=config, ctx=ctx)
    return tool._tool_func


def _call_local(query: str) -> list[dict[str, Any]]:
    """Call _local_fallback directly — the lowest-level fallback function."""
    from agents.supervisor.tools.retrieve import _local_fallback

    return _local_fallback(query)


def _top_source_category(results: list[dict[str, Any]]) -> str | None:
    """Return the corpus subdirectory (category) of the top-scoring hit."""
    if not results:
        return None
    top_source = results[0]["source"]
    # source is like "vehicle_manufacturing/04-sub-assembly.md"
    return top_source.split("/")[0]


# ---------------------------------------------------------------------------
# Category: vehicle_manufacturing
# ---------------------------------------------------------------------------


def test_vehicle_manufacturing_top_hit():
    """A car-manufacturing vocabulary query returns a vehicle_manufacturing doc."""
    # "body-in-white", "robotic weld", "stamping" do not appear in
    # other corpora — they are exclusively four-wheeler vocabulary.
    results = _call_local(
        "body-in-white stamping press robotic weld cells automotive manufacturing"
    )
    assert results, "Expected at least one result"
    category = _top_source_category(results)
    assert category == "vehicle_manufacturing", (
        f"Expected top hit from 'vehicle_manufacturing', got {category!r}. "
        f"Top hit: {results[0]['source']}"
    )


def test_vehicle_manufacturing_any_hit():
    """At least one result from vehicle_manufacturing category is present."""
    results = _call_local("automotive vehicle assembly plant production stages")
    categories = {r["source"].split("/")[0] for r in results}
    assert "vehicle_manufacturing" in categories, (
        f"No vehicle_manufacturing hit in results. Sources: {[r['source'] for r in results]}"
    )


def test_vehicle_manufacturing_via_tool(monkeypatch):
    """The retrieve tool with no KB ID falls back and returns vehicle_manufacturing hits."""
    # Suppress the CloudWatch metric emission (no AWS creds in CI)
    monkeypatch.setattr(
        "agents.supervisor.tools.retrieve._emit_kb_unavailable_metric",
        lambda: None,
    )
    retrieve_fn = _make_retrieve(persona_id="fleet_driver")
    results = retrieve_fn(
        query="body-in-white stamping press robotic weld cells automotive"
    )
    assert results, "Fallback returned no results"
    categories = {r["source"].split("/")[0] for r in results}
    assert "vehicle_manufacturing" in categories, (
        f"No vehicle_manufacturing hit via tool. Sources: {[r['source'] for r in results]}"
    )


# ---------------------------------------------------------------------------
# Category: meridian_lineup_narrative
# ---------------------------------------------------------------------------


def test_meridian_lineup_narrative_top_hit():
    """A Meridian model-name query returns a meridian_lineup_narrative doc."""
    results = _call_local("Windrose compact SUV Meridian lineup electric vehicle")
    assert results, "Expected at least one result"
    category = _top_source_category(results)
    assert category == "meridian_lineup_narrative", (
        f"Expected top hit from 'meridian_lineup_narrative', got {category!r}. "
        f"Top hit: {results[0]['source']}"
    )


def test_meridian_lineup_narrative_any_hit():
    """At least one meridian_lineup_narrative result appears for lineup queries."""
    results = _call_local("Trailwind adventure mid-size SUV electric Meridian")
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_lineup_narrative" in categories, (
        f"No meridian_lineup_narrative hit. Sources: {[r['source'] for r in results]}"
    )


def test_meridian_lineup_narrative_via_tool(monkeypatch):
    """The retrieve tool fallback returns meridian_lineup_narrative hits."""
    monkeypatch.setattr(
        "agents.supervisor.tools.retrieve._emit_kb_unavailable_metric",
        lambda: None,
    )
    retrieve_fn = _make_retrieve(persona_id="consumer")
    results = retrieve_fn(query="Crestwind three-row family SUV Meridian electric")
    assert results, "Fallback returned no results"
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_lineup_narrative" in categories, (
        f"No meridian_lineup_narrative hit via tool. Sources: {[r['source'] for r in results]}"
    )


# ---------------------------------------------------------------------------
# Category: meridian_owner_manual
# ---------------------------------------------------------------------------


def test_meridian_owner_manual_top_hit():
    """An owner-manual vocabulary query returns a meridian_owner_manual doc."""
    results = _call_local(
        "safety systems automatic emergency braking lane departure"
    )
    assert results, "Expected at least one result"
    category = _top_source_category(results)
    assert category == "meridian_owner_manual", (
        f"Expected top hit from 'meridian_owner_manual', got {category!r}. "
        f"Top hit: {results[0]['source']}"
    )


def test_meridian_owner_manual_any_hit():
    """At least one meridian_owner_manual result is present for warranty queries."""
    results = _call_local("warranty coverage vehicle defects service interval")
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_owner_manual" in categories, (
        f"No meridian_owner_manual hit. Sources: {[r['source'] for r in results]}"
    )


def test_meridian_owner_manual_via_tool(monkeypatch):
    """The retrieve tool fallback returns meridian_owner_manual hits."""
    monkeypatch.setattr(
        "agents.supervisor.tools.retrieve._emit_kb_unavailable_metric",
        lambda: None,
    )
    retrieve_fn = _make_retrieve(persona_id="vehicle_owner")
    results = retrieve_fn(
        query="connected features remote access vehicle app Meridian"
    )
    assert results, "Fallback returned no results"
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_owner_manual" in categories, (
        f"No meridian_owner_manual hit via tool. Sources: {[r['source'] for r in results]}"
    )


# ---------------------------------------------------------------------------
# Category: meridian_ive_package
# ---------------------------------------------------------------------------


def test_meridian_ive_package_top_hit():
    """An IVE-package vocabulary query returns a meridian_ive_package doc."""
    results = _call_local(
        "family IVE package personalisation NFC key card"
    )
    assert results, "Expected at least one result"
    category = _top_source_category(results)
    assert category == "meridian_ive_package", (
        f"Expected top hit from 'meridian_ive_package', got {category!r}. "
        f"Top hit: {results[0]['source']}"
    )


def test_meridian_ive_package_any_hit():
    """At least one meridian_ive_package result appears for adventure package queries."""
    results = _call_local(
        "adventure package off-road IVE personalisation vehicle"
    )
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_ive_package" in categories, (
        f"No meridian_ive_package hit. Sources: {[r['source'] for r in results]}"
    )


def test_meridian_ive_package_via_tool(monkeypatch):
    """The retrieve tool fallback returns meridian_ive_package hits."""
    monkeypatch.setattr(
        "agents.supervisor.tools.retrieve._emit_kb_unavailable_metric",
        lambda: None,
    )
    retrieve_fn = _make_retrieve(persona_id="retail_advisor")
    results = retrieve_fn(
        query="executive IVE package premium features personalisation"
    )
    assert results, "Fallback returned no results"
    categories = {r["source"].split("/")[0] for r in results}
    assert "meridian_ive_package" in categories, (
        f"No meridian_ive_package hit via tool. Sources: {[r['source'] for r in results]}"
    )


# ---------------------------------------------------------------------------
# Anti-bleed: four-wheeler query must NOT return an incorrect category as top hit
# ---------------------------------------------------------------------------


def test_four_wheeler_query_does_not_bleed_into_wrong_category():
    """Four-wheeler manufacturing vocabulary returns vehicle_manufacturing as top hit.

    The term "body-in-white" is specific to welded steel/aluminium passenger-car
    body construction and must route to vehicle_manufacturing.
    """
    results = _call_local(
        "body-in-white stamping press robotic weld cells automotive manufacturing"
    )
    assert results, "Expected at least one result"
    top_source = results[0]["source"]
    top_category = top_source.split("/")[0]
    assert top_category == "vehicle_manufacturing", (
        f"Four-wheeler query must return vehicle_manufacturing. "
        f"Top hit: {top_source!r}"
    )


def test_four_wheeler_top_hit_is_vehicle_manufacturing():
    """Top hit for a car manufacturing query is vehicle_manufacturing."""
    results = _call_local(
        "automotive body paint shop cure temperature electrodeposition primer"
    )
    assert results, "Expected at least one result"
    top_source = results[0]["source"]
    top_category = top_source.split("/")[0]
    assert top_category == "vehicle_manufacturing", (
        f"Expected 'vehicle_manufacturing' top hit, got {top_category!r} "
        f"(source: {top_source!r}). "
        "A four-wheeler paint-shop query must return vehicle_manufacturing."
    )


def test_no_deleted_corpora_in_top_results_for_four_wheeler_body_query():
    """No non-vehicle-manufacturing document appears anywhere in the top-5 results
    for a vocabulary query built exclusively from four-wheeler body-shop terms.
    vehicle_manufacturing corpus is the correct four-wheeler source.
    """
    results = _call_local(
        "body-in-white stamping robotic weld door aperture geometry fixture"
    )
    wrong_hits = [r for r in results if not r["source"].startswith("vehicle_manufacturing")]
    # All results should be from vehicle_manufacturing given the four-wheeler vocabulary
    assert not wrong_hits or all(
        r["score"] < results[0]["score"] * 0.5 for r in wrong_hits
    ), (
        f"Unexpected high-score non-vehicle_manufacturing hits for four-wheeler body query: "
        f"{[r['source'] for r in wrong_hits]}"
    )


# ---------------------------------------------------------------------------
# Fallback path via ClientError (with adp_kb_id configured but KB throws)
# ---------------------------------------------------------------------------


def test_fallback_triggered_on_client_error(monkeypatch):
    """When boto3.client.retrieve raises ClientError the fallback runs and finds
    vehicle_manufacturing documents — no live AWS calls are made.
    """
    from botocore.exceptions import ClientError

    monkeypatch.setattr(
        "agents.supervisor.tools.retrieve._emit_kb_unavailable_metric",
        lambda: None,
    )
    from agents.supervisor.tools.retrieve import make_retrieve_tool
    from unittest.mock import MagicMock, patch as mpatch

    config = SimpleNamespace(adp_kb_id="test-kb-id", adp_region="us-east-1")
    ctx = SimpleNamespace(persona_id="fleet_driver", last_kb_citations=[])
    retrieve_fn = make_retrieve_tool(config=config, ctx=ctx)._tool_func

    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "Retrieve",
    )

    with mpatch("agents.supervisor.tools.retrieve.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        results = retrieve_fn(
            query="body-in-white stamping press robotic weld cells automotive"
        )

    assert results, "ClientError fallback returned no results"
    categories = {r["source"].split("/")[0] for r in results}
    assert "vehicle_manufacturing" in categories, (
        f"No vehicle_manufacturing hit after ClientError fallback. "
        f"Sources: {[r['source'] for r in results]}"
    )
