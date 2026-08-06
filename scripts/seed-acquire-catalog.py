#!/usr/bin/env python3
"""Seed an acquire-catalog JSON file into the DynamoDB vsa-acquire-catalog table.

Each item in the catalog JSON's ``items`` array is written via put_item.
Floats are coerced to Decimal before writing (DynamoDB does not accept
Python floats).

Usage:
    python3 scripts/seed-acquire-catalog.py \\
        --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json

    # Override table and region:
    VSA_ACQUIRE_CATALOG_TABLE=my-table AWS_REGION=eu-west-1 \\
        python3 scripts/seed-acquire-catalog.py \\
        --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json

    # Dry-run (validate + count, no AWS write):
    python3 scripts/seed-acquire-catalog.py \\
        --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json \\
        --dry-run

Environment variables (all optional):
    VSA_ACQUIRE_CATALOG_TABLE  DynamoDB table name. Default: vsa-<STAGE>-acquire-catalog
    STAGE                      Deployment stage for default table name. Default: dev
    AWS_REGION                 AWS region. Default: us-east-1

Exit codes:
    0  — success (or dry-run with zero validation errors)
    1  — validation error in the catalog file
    2  — boto3 / DynamoDB error during write
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_STAGE = os.environ.get("STAGE", "dev")
_DEFAULT_TABLE = os.environ.get(
    "VSA_ACQUIRE_CATALOG_TABLE", f"vsa-{_STAGE}-acquire-catalog"
)
_DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Lazy boto3 resource — not imported at module level so that unit tests
# that don't have AWS credentials can still import this module safely.
_ddb_resource: Any = None


def _get_ddb(region: str) -> Any:
    global _ddb_resource
    if _ddb_resource is None:
        import boto3
        _ddb_resource = boto3.resource("dynamodb", region_name=region)
    return _ddb_resource


# ---------------------------------------------------------------------------
# Decimal coercion — mirrors agents/supervisor/tools/book.py `_to_ddb_safe`
# ---------------------------------------------------------------------------

def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal for DDB. All other types pass through."""
    if isinstance(value, float):
        # str() conversion avoids the Decimal(float) precision trap.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Validation shim
# ---------------------------------------------------------------------------

def _validate_catalog(catalog_path: Path) -> list[str]:
    """Call the validator and return violation strings.

    We import from the sibling module rather than re-implementing the
    logic, so the two tools stay in sync.
    """
    # Add scripts/lib to path when running from the repo root.
    lib_dir = Path(__file__).parent / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    from validate_acquire_catalog import validate_catalog  # type: ignore[import]

    return validate_catalog(catalog_path)


# ---------------------------------------------------------------------------
# Core seeding function
# ---------------------------------------------------------------------------

def seed_catalog(
    *,
    catalog_path: Path,
    table_name: str,
    region: str,
    dry_run: bool,
) -> int:
    """Load, validate, and write a catalog to DynamoDB.

    Returns an exit code (0, 1, or 2).
    """
    # --- load JSON -----------------------------------------------------------
    if not catalog_path.exists():
        log.error("Catalog file not found: %s", catalog_path)
        return 1

    try:
        doc = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to parse catalog JSON: %s", exc)
        return 1

    # --- validate ------------------------------------------------------------
    violations = _validate_catalog(catalog_path)
    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1

    items = doc.get("items", [])
    item_count = len(items)

    if dry_run:
        print(
            f"[dry-run] Catalog valid. {item_count} item(s) would be written to "
            f"table '{table_name}' in region '{region}'."
        )
        return 0

    # --- write to DynamoDB ---------------------------------------------------
    try:
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import]
    except ImportError as exc:
        log.error("botocore not available: %s", exc)
        return 2

    try:
        table = _get_ddb(region).Table(table_name)
    except Exception as exc:  # noqa: BLE001 — surface any resource init error
        log.error("Failed to acquire DynamoDB resource: %s", exc)
        return 2

    written = 0
    for item in items:
        safe_item = _to_ddb_safe(item)
        try:
            table.put_item(Item=safe_item)
            item_key = item.get("itemKey", "<unknown>")
            print(f"Written: {item_key}")
            written += 1
        except (BotoCoreError, ClientError) as exc:
            log.error("DynamoDB write failed for item %r: %s", item.get("itemKey"), exc)
            return 2

    print(f"\nDone. {written} item(s) written to table '{table_name}' (region '{region}').")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a CVX acquire-catalog JSON file into DynamoDB.",
    )
    parser.add_argument(
        "--catalog",
        required=True,
        metavar="PATH",
        help="Path to the catalog JSON file to seed.",
    )
    parser.add_argument(
        "--table",
        default=_DEFAULT_TABLE,
        metavar="NAME",
        help=(
            f"DynamoDB table name. "
            f"Default: env var VSA_ACQUIRE_CATALOG_TABLE or vsa-${{STAGE:-dev}}-acquire-catalog "
            f"(currently '{_DEFAULT_TABLE}')."
        ),
    )
    parser.add_argument(
        "--region",
        default=_DEFAULT_REGION,
        metavar="NAME",
        help=f"AWS region. Default: env var AWS_REGION or us-east-1 (currently '{_DEFAULT_REGION}').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the catalog and print the item count without writing to DynamoDB.",
    )
    args = parser.parse_args(argv)

    return seed_catalog(
        catalog_path=Path(args.catalog),
        table_name=args.table,
        region=args.region,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
