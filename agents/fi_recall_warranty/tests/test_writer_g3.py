"""Plain tests for writer.py (T3.1b — FI stage C task 3.1 part B).

S1 — build_recall_finding produces a schema-valid Finding for all three item
     types (ground, book, parts_expected).
S2 — a model_plan with headline/severity/ground keys changes nothing in the
     Finding (build_items_from_band ignores model_plan).

Mutation guards (no xfail — these must run against the live implementation):
  N(a) build_items_from_band returns model_plan.get("items", …)
       → test_model_plan_items_key_ignored (existing test also covers this)
  N(b) build_disclosure_txt strips trailing whitespace from remedy
       → test_disclosure_txt_preserves_trailing_whitespace
  N(c) recommended_action.vehicle_vin read from a model_plan key (not scope vin)
       → test_book_recommended_action_vehicle_vin_is_scope_vin
  N(d) grounding item gains a dealer_id in parameters
       → test_ground_recommended_action_has_no_dealer_id

Run:
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \\
        .venv/bin/python -m pytest agents/fi_recall_warranty/tests -q
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import jsonschema
import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_CAMPAIGN_ID = "MRD-2024-001"
_COMPUTED_AT = "2024-01-01T00:00:00Z"

_CATALOG = {
    "campaign_id": _CAMPAIGN_ID,
    "recall_class": "safety",
    "do_not_drive": True,
    "summary": "The brake caliper may seize due to a manufacturing defect.",
    "consequence": "Reduced braking ability increases risk of a crash.",
    "remedy": "Dealers will replace the affected brake caliper at no charge.",
    "remedy_available_from": None,
}

# Catalog with trailing whitespace in remedy — catches the .strip() mutation (N(b)).
_CATALOG_TRAILING_REMEDY = dict(_CATALOG, remedy="Dealers will replace at no charge.  ")

# Minimal evidence entry satisfying the schema (vin + provenance required).
_EVIDENCE = [
    {
        "kind": "recall.campaign",
        "source": "adp",
        "source_ref": "adp-recall-2024-001",
        "value": {"campaign_id": _CAMPAIGN_ID, "recall_class": "safety"},
        "computed_at": _COMPUTED_AT,
        "provenance": "live",
        "vin": _VIN,
    }
]

_INPUTS_HASH = hashlib.sha256(b"test-inputs-g3").hexdigest()

# Band results used across tests.
# _BAND_P0: Row 1 — safety, do_not_drive=True, CONFIRMED → p0, [ground, book]
# _BAND_ATTENTION: Row 3 — safety, do_not_drive=False, POPULATION → attention, [book]
from agents.fi_recall_warranty import band as _band_mod
_BAND_P0 = _band_mod.classify("safety", True, "CONFIRMED")
_BAND_ATTENTION = _band_mod.classify("safety", False, "POPULATION")

# Load the schema once at module import time.
_SCHEMA_PATH = (
    pathlib.Path(__file__).parent.parent.parent / "tier2" / "schemas" / "finding.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())


def _validate(finding: dict) -> None:
    """Assert finding validates against the AVX Finding schema."""
    jsonschema.validate(finding, _SCHEMA)


def _make_finding(item_type: str, band_result=None, dealer_id=None, remedy_date=None,
                  catalog=None):
    """Helper: build a Finding for the given item_type."""
    from agents.fi_recall_warranty import writer

    if band_result is None:
        band_result = _BAND_P0

    if item_type == "parts_expected":
        headline = writer.build_headline(
            item_type="parts_expected",
            campaign_id=_CAMPAIGN_ID,
            dealer_id=None,
            remedy_date=remedy_date or "2025-06-01",
        )
    elif remedy_date is not None:
        headline = writer.build_headline(
            item_type="parts_expected",
            campaign_id=_CAMPAIGN_ID,
            dealer_id=None,
            remedy_date=remedy_date,
        )
    else:
        headline = writer.build_headline(
            item_type=item_type,
            campaign_id=_CAMPAIGN_ID,
            dealer_id=dealer_id,
            remedy_date=None,
        )
    return writer.build_recall_finding(
        vin=_VIN,
        campaign_id=_CAMPAIGN_ID,
        item_type=item_type,
        attempt=1,
        band_result=band_result,
        headline=headline,
        detail="Model-written detail.",
        catalog_entry=catalog or _CATALOG,
        dealer_id=dealer_id,
        computed_at=_COMPUTED_AT,
        evidence=_EVIDENCE,
        confidence="high",
        inputs_hash=_INPUTS_HASH,
    )


# ============================================================================
# S1 — schema validation for all three item types
# ============================================================================

class TestFindingSchemaValid:

    def test_ground_item_validates(self):
        """S1.1: ground item validates against finding.schema.json."""
        finding = _make_finding("ground", _BAND_P0)
        _validate(finding)

    def test_book_item_validates(self):
        """S1.2: book item (parts available) validates against finding.schema.json."""
        finding = _make_finding("book", _BAND_P0, dealer_id="dealer-seattle")
        _validate(finding)

    def test_parts_expected_item_validates(self):
        """S1.3: parts_expected informational item validates against finding.schema.json."""
        finding = _make_finding("parts_expected", _BAND_ATTENTION)
        _validate(finding)


# ============================================================================
# S2 — model_plan with headline/severity/ground keys changes nothing
# ============================================================================

class TestModelPlanIsolation:

    def test_model_plan_items_key_ignored(self):
        """S2: model_plan["items"] that contradicts the band result is ignored.

        Mutation N(a): build_items_from_band returns model_plan.get("items", ...)
        → items becomes ["book"] instead of ["ground", "book"] → assertion fails.
        """
        from agents.fi_recall_warranty import band, writer

        band_result = band.classify("safety", True, "CONFIRMED")
        # Band Row 1: ["ground", "book"]. model_plan tries to drop "ground".
        model_plan = {
            "items": ["book"],
            "headline": "Do not drive",
            "severity": "routine",
            "ground": False,
        }
        items = writer.build_items_from_band(band_result, model_plan)
        assert items == ["ground", "book"], (
            f"build_items_from_band must return band_result.items unchanged; got {items!r}. "
            "N(a): returning model_plan.get('items') would give ['book']."
        )

    def test_model_plan_headline_changes_nothing_in_finding(self):
        """S2: build_recall_finding does not accept model_plan; headline comes from caller."""
        from agents.fi_recall_warranty import writer

        caller_headline = writer.build_headline(
            item_type="ground",
            campaign_id=_CAMPAIGN_ID,
            dealer_id=None,
            remedy_date=None,
        )
        finding = writer.build_recall_finding(
            vin=_VIN,
            campaign_id=_CAMPAIGN_ID,
            item_type="ground",
            attempt=1,
            band_result=_BAND_P0,
            headline=caller_headline,
            detail="Detail.",
            catalog_entry=_CATALOG,
            dealer_id=None,
            computed_at=_COMPUTED_AT,
            evidence=_EVIDENCE,
            confidence="high",
            inputs_hash=_INPUTS_HASH,
        )
        assert finding["severity"] == "p0"
        assert finding["headline"] == caller_headline
        _validate(finding)


# ============================================================================
# Mutation guards
# ============================================================================

class TestMutationGuards:

    def test_disclosure_txt_preserves_trailing_whitespace(self):
        """N(b): build_disclosure_txt must NOT strip trailing whitespace from remedy.

        Mutation: return summary + consequence + remedy.strip()
        → txt.endswith(remedy) fails when remedy has trailing spaces.
        """
        from agents.fi_recall_warranty import writer

        remedy = _CATALOG_TRAILING_REMEDY["remedy"]
        assert remedy.endswith("  "), "fixture must have trailing spaces"
        txt = writer.build_disclosure_txt(_CATALOG_TRAILING_REMEDY)
        assert txt.endswith(remedy), (
            "disclosure_txt must end with the remedy byte for byte. "
            "N(b): strip() would drop trailing spaces, failing this assertion."
        )

    def test_book_recommended_action_vehicle_vin_is_scope_vin(self):
        """N(c): vehicle_vin in recommended_action.parameters must be the scope vin.

        Mutation: vehicle_vin = dealer_id (or some other parameter) instead of vin.
        → vehicle_vin != vin → assertion fails.
        """
        finding = _make_finding("book", _BAND_P0, dealer_id="dealer-seattle")
        params = finding["recommended_action"]["parameters"]
        assert params["vehicle_vin"] == _VIN, (
            f"vehicle_vin must equal scope vin {_VIN!r}; got {params['vehicle_vin']!r}. "
            "N(c): an implementation reading dealer_id as vehicle_vin fails here."
        )
        assert params["vehicle_vin"] != "dealer-seattle", (
            "vehicle_vin must not be the dealer_id. N(c) guard."
        )

    def test_ground_recommended_action_has_no_dealer_id(self):
        """N(d): grounding item's recommended_action.parameters must not carry dealer_id.

        Contract § 1.7: ground_vehicle parameters are {vehicle_vin, recall_id} only.
        Mutation: parameters includes dealer_id → assertion fails.
        """
        finding = _make_finding("ground", _BAND_P0)
        params = finding["recommended_action"]["parameters"]
        assert "dealer_id" not in params, (
            "Ground item recommended_action.parameters must not contain dealer_id. "
            "N(d): an implementation that copies dealer_id into ground params fails here."
        )
        # Ensure the required fields are present.
        assert params["vehicle_vin"] == _VIN
        assert params["recall_id"] == _CAMPAIGN_ID

    def test_parts_expected_has_no_recommended_action(self):
        """Parts_expected (informational) item must carry no recommended_action.

        This is the prerequisite for N(c)/(d) not firing on the informational path.
        """
        finding = _make_finding("parts_expected", _BAND_ATTENTION)
        assert "recommended_action" not in finding, (
            "parts_expected informational item must not carry recommended_action."
        )



# ============================================================================
# G2a addition: item_type='ground' requires 'ground' in band_result.items
# ============================================================================

class TestGroundRequiresGroundInBandItems:

    def test_ground_for_row3_band_raises(self):
        """Mutation guard (c): writer accepts ground for a row-3 band → TypeError.

        Row 3 band (safety, POPULATION, not do_not_drive) has items=['book'].
        Calling build_recall_finding(item_type='ground', band_result=row3_band)
        must raise ValueError — a ground item for a band that does not include
        grounding is a logic error that should fail closed.

        Mutation: remove the 'ground' not in items check → no raise → test fails.
        """
        from agents.fi_recall_warranty import writer

        row3_band = _band_mod.classify("safety", False, "POPULATION")
        assert "ground" not in row3_band.items, "fixture must not have ground"

        headline = writer.build_headline(
            item_type="ground",
            campaign_id=_CAMPAIGN_ID,
            dealer_id=None,
            remedy_date=None,
        )
        with pytest.raises(ValueError, match="ground"):
            writer.build_recall_finding(
                vin=_VIN,
                campaign_id=_CAMPAIGN_ID,
                item_type="ground",
                attempt=1,
                band_result=row3_band,
                headline=headline,
                detail="Detail.",
                catalog_entry=_CATALOG,
                dealer_id=None,
                computed_at=_COMPUTED_AT,
                evidence=_EVIDENCE,
                confidence="high",
                inputs_hash=_INPUTS_HASH,
            )
