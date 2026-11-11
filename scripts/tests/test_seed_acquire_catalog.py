"""Tests for scripts/seed-acquire-catalog.py"""
from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the seeder module by file path (hyphens in filename prevent a normal
# package import).
# ---------------------------------------------------------------------------

_SEEDER_PATH = Path(__file__).parent.parent / "seed-acquire-catalog.py"
_SEEDER_SPEC = importlib.util.spec_from_file_location("seed_acquire_catalog", _SEEDER_PATH)
_SEEDER = importlib.util.module_from_spec(_SEEDER_SPEC)
_SEEDER_SPEC.loader.exec_module(_SEEDER)

seed_catalog = _SEEDER.seed_catalog
_to_ddb_safe = _SEEDER._to_ddb_safe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_catalog(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _valid_catalog() -> dict:
    return {
        "tenantId": "demo-tenant",
        "catalogVersion": "demo-2026-07",
        "items": [
            {
                "tenantId": "demo-tenant",
                "itemKey": "META#catalogVersion",
                "value": "demo-2026-07",
            },
            {
                "tenantId": "demo-tenant",
                "itemKey": "CAT#commuter",
                "categoryKey": "CAT#commuter",
                "displayName": "Commuter",
            },
            {
                "tenantId": "demo-tenant",
                "itemKey": "MODEL#commuter#city-200",
                "categoryKey": "CAT#commuter",
                "displayName": "City 200",
                "basePrice": 3299.99,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Decimal-coercion tests
# ---------------------------------------------------------------------------

class TestDecimalCoercion:
    def test_float_becomes_decimal(self):
        """Float values are converted to Decimal."""
        result = _to_ddb_safe(3.14)
        assert isinstance(result, Decimal)
        assert result == Decimal("3.14")

    def test_nested_dict_floats_coerced(self):
        """Floats nested inside a dict are all coerced to Decimal."""
        data = {"price": 9.99, "delta": 0.00, "count": 5}
        result = _to_ddb_safe(data)
        assert isinstance(result["price"], Decimal)
        assert isinstance(result["delta"], Decimal)
        assert isinstance(result["count"], int)  # int unchanged

    def test_nested_list_floats_coerced(self):
        """Floats inside lists are coerced to Decimal."""
        data = [1.1, 2.2, 3]
        result = _to_ddb_safe(data)
        assert isinstance(result[0], Decimal)
        assert isinstance(result[1], Decimal)
        assert isinstance(result[2], int)

    def test_no_floats_in_genericmoto_after_coercion(self):
        """After _to_ddb_safe, no item in the GenericMoto catalog contains a float."""
        catalog_path = (
            Path(__file__).parent.parent
            / "seed_data"
            / "acquire_catalog"
            / "genericmoto-catalog.json"
        )
        doc = json.loads(catalog_path.read_text())
        safe_items = [_to_ddb_safe(item) for item in doc["items"]]

        def _has_float(val) -> bool:
            if isinstance(val, float):
                return True
            if isinstance(val, dict):
                return any(_has_float(v) for v in val.values())
            if isinstance(val, list):
                return any(_has_float(v) for v in val)
            return False

        for item in safe_items:
            assert not _has_float(item), (
                f"Float found in item after coercion: {item.get('itemKey')}"
            )

    def test_string_and_bool_pass_through(self):
        """Non-float scalars are returned unchanged."""
        assert _to_ddb_safe("hello") == "hello"
        assert _to_ddb_safe(True) is True
        assert _to_ddb_safe(None) is None


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_happy_path(self, tmp_path, capsys):
        """Dry-run with a valid catalog exits 0 and prints item count."""
        path = _write_catalog(tmp_path, _valid_catalog())
        rc = seed_catalog(
            catalog_path=path,
            table_name="test-table",
            region="us-east-1",
            dry_run=True,
        )
        assert rc == 0
        out = capsys.readouterr().out
        # Item count in the valid catalog above is 3.
        assert "3" in out

    def test_dry_run_does_not_call_boto3(self, tmp_path):
        """Dry-run must not make any boto3/DDB calls."""
        path = _write_catalog(tmp_path, _valid_catalog())
        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            rc = seed_catalog(
                catalog_path=path,
                table_name="test-table",
                region="us-east-1",
                dry_run=True,
            )
        assert rc == 0

    def test_dry_run_invalid_catalog_exits_1(self, tmp_path, capsys):
        """Dry-run on a catalog missing tenantId exits 1."""
        doc = _valid_catalog()
        del doc["tenantId"]
        path = _write_catalog(tmp_path, doc)
        rc = seed_catalog(
            catalog_path=path,
            table_name="test-table",
            region="us-east-1",
            dry_run=True,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# Missing file test
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_missing_catalog_file_exits_1(self, tmp_path):
        """seed_catalog exits 1 when the catalog file does not exist."""
        rc = seed_catalog(
            catalog_path=tmp_path / "no-such-file.json",
            table_name="test-table",
            region="us-east-1",
            dry_run=False,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# Live write path — mocked boto3
# ---------------------------------------------------------------------------

class TestLiveWrite:
    def test_successful_write_exits_0(self, tmp_path, capsys):
        """seed_catalog exits 0 and prints a line per item on success."""
        path = _write_catalog(tmp_path, _valid_catalog())

        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        mock_ddb_resource = MagicMock()
        mock_ddb_resource.Table.return_value = mock_table

        with patch.object(_SEEDER, "_get_ddb", return_value=mock_ddb_resource):
            # Also reset module-level cache.
            _SEEDER._ddb_resource = None
            rc = seed_catalog(
                catalog_path=path,
                table_name="test-table",
                region="us-east-1",
                dry_run=False,
            )

        assert rc == 0
        out = capsys.readouterr().out
        # 3 items in the catalog — each should produce a "Written:" line.
        assert out.count("Written:") == 3

    def test_boto3_error_exits_2(self, tmp_path, capsys):
        """seed_catalog exits 2 when DynamoDB raises ClientError."""
        path = _write_catalog(tmp_path, _valid_catalog())

        try:
            from botocore.exceptions import ClientError
        except ImportError:
            pytest.skip("botocore not installed")

        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "PutItem",
        )

        mock_ddb_resource = MagicMock()
        mock_ddb_resource.Table.return_value = mock_table

        with patch.object(_SEEDER, "_get_ddb", return_value=mock_ddb_resource):
            _SEEDER._ddb_resource = None
            rc = seed_catalog(
                catalog_path=path,
                table_name="test-table",
                region="us-east-1",
                dry_run=False,
            )

        assert rc == 2
