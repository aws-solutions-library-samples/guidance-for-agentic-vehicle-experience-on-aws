#!/usr/bin/env python3
"""Validate an acquire-catalog JSON file against the CVX Acquire catalog schema.

Usage:
    python3 scripts/lib/validate_acquire_catalog.py <catalog.json> [...]

Exit codes:
    0  — all files valid
    1  — one or more violations found (per-violation lines written to stderr
         as "<file>:<jsonpath>: <error>")

Schema reference:
    connected-mobility-guidance-on-aws/docs/acquire-catalog-schema.md § 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Valid item-key prefixes per schema § 2
# ---------------------------------------------------------------------------

VALID_PREFIXES = (
    "META#",
    "CAT#",
    "MODEL#",
    "VAR#",
    "COLOR#",
    "ACC#",
    "ACC-COMPAT#",
    "OFFER#",
    # Zone-2 additions (spec 2026-07-31-zone2-order-backend-and-kiosk):
    "INTERIOR#",   # INTERIOR#<modelId>#<intId>  — per-model interior option
    "IVE#",        # IVE#<packageId>              — in-vehicle experience package
)

# Required top-level fields in every catalog document.
REQUIRED_TOP_LEVEL = ("tenantId", "catalogVersion", "items")

# Required fields per item-key prefix.
REQUIRED_BY_PREFIX: dict[str, list[str]] = {
    "META#": ["value"],
    # META#tenantConfig has deliveryCadence instead of value (Zone-2, spec 2026-07-31):
    "META#tenantConfig": ["deliveryCadence"],
    "CAT#": ["tenantId", "itemKey", "displayName"],
    "MODEL#": ["tenantId", "itemKey", "categoryKey", "displayName", "basePrice"],
    "VAR#": ["tenantId", "itemKey", "displayName"],
    "COLOR#": ["tenantId", "itemKey", "displayName", "hexSwatch"],
    "ACC#": ["tenantId", "itemKey", "displayName", "price"],
    "ACC-COMPAT#": ["tenantId", "itemKey"],
    "OFFER#": ["tenantId", "itemKey", "displayName"],
    # Zone-2 additions (spec 2026-07-31-zone2-order-backend-and-kiosk):
    "INTERIOR#": ["tenantId", "itemKey", "displayName"],
    "IVE#": ["tenantId", "itemKey", "displayName"],
}


def _prefix(item_key: str) -> str | None:
    """Return the matched prefix or None."""
    for p in VALID_PREFIXES:
        if item_key.startswith(p):
            return p
    return None


def validate_catalog(path: Path) -> list[str]:
    """Validate a single catalog JSON file.

    Returns a list of violation strings.  Empty list means clean.
    """
    violations: list[str] = []
    filename = str(path)

    # --- parse JSON -----------------------------------------------------------
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{filename}:$: cannot read file: {exc}"]

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"{filename}:$: JSON parse error: {exc}"]

    if not isinstance(doc, dict):
        return [f"{filename}:$: root must be a JSON object"]

    # --- required top-level fields -------------------------------------------
    for field in REQUIRED_TOP_LEVEL:
        if field not in doc:
            violations.append(f"{filename}:$.{field}: required field missing")

    if violations:
        # Can't inspect items without tenantId / items being present.
        return violations

    tenant_id = doc["tenantId"]
    items = doc["items"]

    if not isinstance(tenant_id, str) or not tenant_id:
        violations.append(f"{filename}:$.tenantId: must be a non-empty string")

    if not isinstance(items, list):
        violations.append(f"{filename}:$.items: must be an array")
        return violations

    if len(items) == 0:
        violations.append(f"{filename}:$.items: must contain at least one item")

    # --- validate each item --------------------------------------------------
    for idx, item in enumerate(items):
        path_prefix = f"{filename}:$.items[{idx}]"

        if not isinstance(item, dict):
            violations.append(f"{path_prefix}: item must be a JSON object")
            continue

        item_key = item.get("itemKey")
        if not isinstance(item_key, str) or not item_key:
            violations.append(f"{path_prefix}.itemKey: required string field missing or empty")
            continue

        matched_prefix = _prefix(item_key)
        if matched_prefix is None:
            violations.append(
                f"{path_prefix}.itemKey: invalid prefix in {item_key!r}; "
                f"must start with one of {VALID_PREFIXES}"
            )
            continue

        # Check required fields for this prefix.
        # Special case: META#tenantConfig uses 'deliveryCadence' instead of 'value'.
        effective_prefix = matched_prefix
        if item_key == "META#tenantConfig":
            effective_prefix = "META#tenantConfig"
        for req_field in REQUIRED_BY_PREFIX.get(effective_prefix, REQUIRED_BY_PREFIX.get(matched_prefix, [])):
            if req_field not in item:
                violations.append(
                    f"{path_prefix}.{req_field}: required for {matched_prefix!r} items but missing"
                )
                continue
            # Non-empty string where applicable.
            val = item[req_field]
            if isinstance(val, str) and not val:
                violations.append(
                    f"{path_prefix}.{req_field}: must be a non-empty string"
                )

        # tenantId consistency check: every item (except META# which may omit it)
        # must have a tenantId that matches the catalog-level tenantId.
        if matched_prefix != "META#":
            item_tenant = item.get("tenantId")
            if item_tenant is None:
                violations.append(
                    f"{path_prefix}.tenantId: required but missing"
                )
            elif item_tenant != tenant_id:
                violations.append(
                    f"{path_prefix}.tenantId: {item_tenant!r} does not match "
                    f"catalog tenantId {tenant_id!r}"
                )

        # Numeric field checks for specific prefixes.
        if matched_prefix == "MODEL#":
            base_price = item.get("basePrice")
            if base_price is not None and not isinstance(base_price, (int, float)):
                violations.append(f"{path_prefix}.basePrice: must be a number")
            elif isinstance(base_price, (int, float)) and base_price < 0:
                violations.append(f"{path_prefix}.basePrice: must be >= 0")

        if matched_prefix == "ACC#":
            price = item.get("price")
            if price is not None and not isinstance(price, (int, float)):
                violations.append(f"{path_prefix}.price: must be a number")
            elif isinstance(price, (int, float)) and price < 0:
                violations.append(f"{path_prefix}.price: must be >= 0")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate acquire-catalog JSON files against the CVX Acquire schema."
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="One or more catalog JSON file paths to validate.",
    )
    args = parser.parse_args(argv)

    all_violations: list[str] = []
    for raw_path in args.files:
        path = Path(raw_path)
        if not path.exists():
            all_violations.append(f"{raw_path}:$: file not found")
            continue
        violations = validate_catalog(path)
        all_violations.extend(violations)

    if all_violations:
        for v in all_violations:
            print(v, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
