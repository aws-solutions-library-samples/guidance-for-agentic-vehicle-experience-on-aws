#!/usr/bin/env python3
"""Assert that expected REST routes exist in a synthesized API Gateway template.

Reconstructs full resource paths from the CloudFormation template's
`AWS::ApiGateway::Resource` tree, so it cannot be satisfied by a coincidental
path-part collision. `/tenants/{tenantId}` already contributes a `{tenantId}`
part, for example, so asserting on bare path parts would pass vacuously for
`/acquire/catalog/{tenantId}`.

Written 2026-07-31 for spec `2026-07-31-zone2-order-backend-and-kiosk`, whose
premise is that the six `/acquire/*` routes the iOS client expects are not
provisioned. Doubles as a regression gate afterwards.

Usage (non-interactive):

    make synth
    python3 scripts/check-api-routes.py --expect-acquire
    python3 scripts/check-api-routes.py --list
    python3 scripts/check-api-routes.py --expect /acquire/reservations /book

Exit status: 0 when every expected path is present, 1 otherwise (missing paths
are printed). 2 on usage or template-discovery errors.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys

# The six routes `AcquireCatalogClient.swift` is written against.
ACQUIRE_ROUTES = [
    "/acquire/catalog/{tenantId}",
    "/acquire/reservations",
    "/acquire/orders/{orderId}",
    "/acquire/offers/{modelId}",
    "/acquire/leads",
    "/acquire/leads/{leadId}",
]

DEFAULT_GLOB = "infrastructure/cdk.out/vsa-*-api.template.json"


def _resource_paths(template: dict) -> set[str]:
    """Reconstruct every full resource path from the API Gateway resource tree."""
    resources = {
        logical_id: body
        for logical_id, body in template.get("Resources", {}).items()
        if body.get("Type") == "AWS::ApiGateway::Resource"
    }

    def path_of(logical_id: str, seen: frozenset[str] = frozenset()) -> str:
        if logical_id in seen:  # defensive: malformed template cycle
            return ""
        props = resources[logical_id].get("Properties", {})
        part = props.get("PathPart", "")
        parent = props.get("ParentId")
        if isinstance(parent, dict) and parent.get("Ref") in resources:
            return path_of(parent["Ref"], seen | {logical_id}) + "/" + part
        return "/" + part

    return {path_of(logical_id) for logical_id in resources}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-glob", default=DEFAULT_GLOB,
        help=f"glob for the synthesized API template (default: {DEFAULT_GLOB})",
    )
    parser.add_argument(
        "--expect", nargs="*", default=None, metavar="PATH",
        help="explicit paths that must be present",
    )
    parser.add_argument(
        "--expect-acquire", action="store_true",
        help="expect the six /acquire/* routes",
    )
    parser.add_argument(
        "--list", action="store_true", help="print all reconstructed paths and exit 0",
    )
    args = parser.parse_args()

    matches = sorted(glob.glob(args.template_glob))
    if not matches:
        print(
            f"ERROR: no template matched {args.template_glob!r}. Run `make synth` first.",
            file=sys.stderr,
        )
        return 2

    template_path = matches[0]
    with open(template_path, encoding="utf-8") as handle:
        paths = _resource_paths(json.load(handle))

    if args.list:
        print(f"{template_path}: {len(paths)} resource paths")
        for path in sorted(paths):
            print(f"  {path}")
        return 0

    expected: list[str] = list(args.expect or [])
    if args.expect_acquire:
        expected += ACQUIRE_ROUTES
    if not expected:
        print("ERROR: pass --expect, --expect-acquire, or --list", file=sys.stderr)
        return 2

    missing = [path for path in expected if path not in paths]
    print(f"{template_path}: {len(paths)} resource paths, "
          f"{len(expected) - len(missing)}/{len(expected)} expected present")
    if missing:
        print("MISSING:")
        for path in missing:
            print(f"  {path}")
        return 1
    print("all expected routes present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
