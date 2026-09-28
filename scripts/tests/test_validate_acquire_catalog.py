"""Tests for scripts/lib/validate_acquire_catalog.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/lib importable regardless of how pytest is invoked.
LIB_DIR = Path(__file__).parent.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from validate_acquire_catalog import validate_catalog, main  # type: ignore[import]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_catalog(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _minimal_catalog(**overrides) -> dict:
    """Return a minimal valid catalog, applying any keyword overrides."""
    base = {
        "tenantId": "test-tenant",
        "catalogVersion": "test-2026-07",
        "items": [
            {
                "tenantId": "test-tenant",
                "itemKey": "META#catalogVersion",
                "value": "test-2026-07",
            },
            {
                "tenantId": "test-tenant",
                "itemKey": "CAT#sport",
                "categoryKey": "CAT#sport",
                "displayName": "Sport",
            },
            {
                "tenantId": "test-tenant",
                "itemKey": "MODEL#sport#alpha-500",
                "categoryKey": "CAT#sport",
                "displayName": "Alpha 500",
                "basePrice": 6999.00,
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_minimal_catalog_is_valid(self, tmp_path):
        """A well-formed minimal catalog produces no violations."""
        path = _write_catalog(tmp_path, _minimal_catalog())
        violations = validate_catalog(path)
        assert violations == []

    def test_genericmoto_catalog_is_valid(self):
        """The committed GenericMoto seed catalog passes validation."""
        catalog_path = (
            Path(__file__).parent.parent
            / "seed_data"
            / "acquire_catalog"
            / "genericmoto-catalog.json"
        )
        assert catalog_path.exists(), f"GenericMoto catalog not found at {catalog_path}"
        violations = validate_catalog(catalog_path)
        assert violations == [], "\n".join(violations)

    def test_acc_compat_item_valid(self, tmp_path):
        """ACC-COMPAT# items with required fields are valid."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "ACC-COMPAT#rack#sport#alpha-500",
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert violations == []

    def test_color_item_valid(self, tmp_path):
        """COLOR# items with required fields are valid."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "COLOR#sport#alpha-500#standard#crimson",
            "displayName": "Crimson",
            "hexSwatch": "#DC143C",
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert violations == []


# ---------------------------------------------------------------------------
# Missing required-field tests
# ---------------------------------------------------------------------------

class TestMissingRequiredFields:
    def test_missing_tenant_id(self, tmp_path):
        """Catalog without tenantId reports a violation."""
        doc = _minimal_catalog()
        del doc["tenantId"]
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("tenantId" in v for v in violations)

    def test_missing_catalog_version(self, tmp_path):
        """Catalog without catalogVersion reports a violation."""
        doc = _minimal_catalog()
        del doc["catalogVersion"]
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("catalogVersion" in v for v in violations)

    def test_missing_items(self, tmp_path):
        """Catalog without items array reports a violation."""
        doc = _minimal_catalog()
        del doc["items"]
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("items" in v for v in violations)

    def test_model_missing_base_price(self, tmp_path):
        """MODEL# item missing basePrice reports a violation."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "MODEL#sport#beta-200",
            "categoryKey": "CAT#sport",
            "displayName": "Beta 200",
            # basePrice deliberately omitted
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("basePrice" in v for v in violations)

    def test_category_missing_display_name(self, tmp_path):
        """CAT# item missing displayName reports a violation."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "CAT#electric",
            "categoryKey": "CAT#electric",
            # displayName deliberately omitted
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("displayName" in v for v in violations)


# ---------------------------------------------------------------------------
# Invalid item-key prefix tests
# ---------------------------------------------------------------------------

class TestInvalidItemKeyPrefix:
    def test_invalid_prefix_rejected(self, tmp_path):
        """An item with an unrecognized prefix reports a violation."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "UNKNOWN#foo",
            "displayName": "Bad Item",
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("invalid prefix" in v.lower() for v in violations)

    def test_empty_item_key_rejected(self, tmp_path):
        """An item with an empty itemKey reports a violation."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "test-tenant",
            "itemKey": "",
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("itemKey" in v for v in violations)


# ---------------------------------------------------------------------------
# Tenant consistency tests
# ---------------------------------------------------------------------------

class TestTenantConsistency:
    def test_mismatched_tenant_id_in_item(self, tmp_path):
        """An item whose tenantId doesn't match the catalog tenantId is flagged."""
        doc = _minimal_catalog()
        doc["items"].append({
            "tenantId": "different-tenant",
            "itemKey": "CAT#road",
            "categoryKey": "CAT#road",
            "displayName": "Road",
        })
        violations = validate_catalog(_write_catalog(tmp_path, doc))
        assert any("does not match" in v for v in violations)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    def test_cli_valid_catalog_exits_0(self, tmp_path):
        """main() exits 0 for a valid catalog."""
        path = _write_catalog(tmp_path, _minimal_catalog())
        rc = main([str(path)])
        assert rc == 0

    def test_cli_invalid_catalog_exits_1(self, tmp_path, capsys):
        """main() exits 1 and writes violations to stderr."""
        doc = _minimal_catalog()
        del doc["tenantId"]
        path = _write_catalog(tmp_path, doc)
        rc = main([str(path)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "tenantId" in captured.err

    def test_cli_missing_file_exits_1(self, tmp_path, capsys):
        """main() exits 1 when the file does not exist."""
        rc = main([str(tmp_path / "does-not-exist.json")])
        assert rc == 1
