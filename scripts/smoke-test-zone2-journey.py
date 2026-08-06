#!/usr/bin/env python3
"""Smoke-test the Zone 2 order journey end-to-end.

Walks Beats 1-5 as a kiosk device:
  Beat 1 — GET /acquire/catalog/{tenantId}       (fetch catalog)
  Beat 2 — POST /acquire/reservations             (place reservation with interior + IVE package)
  Beat 3 — GET /acquire/orders/{orderId}          (verify initial stage = orderPlaced)
  Beat 4 — Poll GET /acquire/orders/{orderId} until stage reaches readyForDelivery
  Beat 5 — Assert all 10 stages appear in `stages` in the canonical order,
            assert total elapsed time is inside the demo budget

Exits 0 on PASS, non-zero on FAIL with a diagnostic naming the failing beat.

Usage:
    # Live mode — requires a deployed staging stack and kiosk credentials
    python3 scripts/smoke-test-zone2-journey.py \\
        --stage staging \\
        --api-url https://<id>.execute-api.us-west-2.amazonaws.com/staging \\
        --tenant-id meridian

    # Offline self-check — no AWS calls, exercises assertion logic against fixtures
    python3 scripts/smoke-test-zone2-journey.py --self-check

Credential inputs (live mode only — never hard-coded):
    --id-token       Cognito IdToken (or VSA_KIOSK_ID_TOKEN env var)
    --sub            Caller sub claim (or VSA_KIOSK_SUB env var); required for
                     the owner-match assertion on Beat 5
    --tenant-id      Tenant id string (default: meridian)

Env vars:
    VSA_API_URL              Base URL of the deployed VSA REST API
    VSA_KIOSK_ID_TOKEN       Kiosk device IdToken (Cognito)
    VSA_KIOSK_SUB            sub claim for the kiosk device user
    VSA_ZONE2_DEMO_BUDGET_S  Max allowed elapsed seconds for the full journey
                             (default: 180 — three-minute demo budget)
    AWS_REGION               Region for STS calls (default: us-west-2)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib import error as urlerr
from urllib import request as urlreq

# ---------------------------------------------------------------------------
# Constants mirrored from lambdas/acquire/order_progressor/handler.py
# These are the canonical stage names and their required ordering.
# The smoke test validates against these rather than re-deriving them from
# the live response, so a handler regression that skips or reorders a stage
# is immediately caught.
# ---------------------------------------------------------------------------

STAGES = [
    "orderPlaced",
    "paymentReceived",
    "assignedToPlant",
    "subAssembly",
    "paint",
    "finalAssembly",
    "qualityControl",
    "shipped",
    "atDealer",
    "readyForDelivery",
]
TERMINAL_STAGE = "readyForDelivery"

# Demo budget — total journey must complete within this many seconds.
# Meridian's deliveryCadence gives near-zero orderPlacedToPayment and ~12 s
# per subsequent stage (9 transitions × 12 s ≈ 108 s).  180 s gives 50 %
# headroom without letting a runaway poll spin forever.
_DEFAULT_DEMO_BUDGET_S = int(os.environ.get("VSA_ZONE2_DEMO_BUDGET_S", "180"))

# Poll interval — how often to re-check order status while waiting for
# readyForDelivery.  12 s matches the target cadence; polling faster wastes
# API calls without helping.
_POLL_INTERVAL_S = 6

# Maximum poll iterations before declaring a timeout.
_MAX_POLLS = 40


# ---------------------------------------------------------------------------
# Assertion helpers (pure functions — no I/O, testable in isolation)
# ---------------------------------------------------------------------------

def assert_stages_in_order(stages: list[dict]) -> list[str]:
    """Validate that `stages` (the wire `stages` array from GET /acquire/orders)
    contains every canonical stage in the correct order with no duplicates.

    Each entry in `stages` is expected to have a `stage` key (matching the
    DDB `stageHistory` element's `stage` field).

    Returns the list of stage names found.
    Raises AssertionError with a descriptive message on any violation.
    """
    if not stages:
        raise AssertionError("stages array is empty — no stage history recorded")

    found: list[str] = []
    for i, entry in enumerate(stages):
        stage_name = entry.get("stage") if isinstance(entry, dict) else str(entry)
        if not stage_name:
            raise AssertionError(
                f"stages[{i}] has no 'stage' key: {entry!r}"
            )
        found.append(stage_name)

    # Every stage must be a member of STAGES
    unknown = [s for s in found if s not in STAGES]
    if unknown:
        raise AssertionError(
            f"Unknown stage name(s) in response: {unknown!r}. "
            f"Valid stages: {STAGES}"
        )

    # Stages must appear in canonical order (no skips allowed, no reversals)
    canonical_indices = [STAGES.index(s) for s in found]
    for i in range(1, len(canonical_indices)):
        if canonical_indices[i] <= canonical_indices[i - 1]:
            raise AssertionError(
                f"Stage ordering violation: '{found[i - 1]}' (idx {canonical_indices[i - 1]}) "
                f"followed by '{found[i]}' (idx {canonical_indices[i]}) — "
                f"stages must appear in canonical order with no reversals"
            )

    return found


def assert_all_stages_present(found: list[str]) -> None:
    """Assert that every canonical stage from STAGES[0] through TERMINAL_STAGE
    is represented in `found`.

    Raises AssertionError naming each missing stage.
    """
    missing = [s for s in STAGES if s not in found]
    if missing:
        raise AssertionError(
            f"Missing stage(s) in completed order: {missing!r}. "
            f"Found: {found!r}"
        )


def assert_elapsed_within_budget(elapsed_s: float, budget_s: float) -> None:
    """Assert that the journey completed within the demo time budget.

    Raises AssertionError with timing details if the budget was exceeded.
    """
    if elapsed_s > budget_s:
        raise AssertionError(
            f"Journey took {elapsed_s:.1f}s — exceeds demo budget of {budget_s}s. "
            f"Check Meridian deliveryCadence in META#tenantConfig; "
            f"each stage transition should be ~12 s."
        )


def assert_catalog_shape(catalog: dict) -> None:
    """Assert that the catalog response has the required top-level arrays.

    decisions.md Q4: `interiors` and `ivePackages` are always-present even
    when empty.  The Swift client decodes them as non-optional arrays.
    """
    required_keys = ["categories", "models", "variants", "colors",
                     "accessories", "interiors", "ivePackages"]
    missing = [k for k in required_keys if k not in catalog]
    if missing:
        raise AssertionError(
            f"CatalogResponse missing required top-level key(s): {missing!r}"
        )
    # Each value must be a list (may be empty)
    not_list = [k for k in required_keys if not isinstance(catalog.get(k), list)]
    if not_list:
        raise AssertionError(
            f"CatalogResponse key(s) {not_list!r} must be arrays, got: "
            + ", ".join(f"{k}={type(catalog.get(k)).__name__}" for k in not_list)
        )


def assert_reservation_response(resp: dict) -> None:
    """Assert that the reservation (POST /acquire/reservations) response has
    the required fields.

    decisions.md Q3: `orderNumber` is non-optional in Swift; its absence
    makes every reservation fail to decode.
    """
    required = ["orderId", "orderNumber", "stage", "nextTransitionAt"]
    missing = [k for k in required if k not in resp]
    if missing:
        raise AssertionError(
            f"ReservationResponse missing required field(s): {missing!r}"
        )
    if resp.get("stage") != "orderPlaced":
        raise AssertionError(
            f"Initial order stage should be 'orderPlaced', got {resp.get('stage')!r}"
        )


def assert_order_response_shape(order: dict) -> None:
    """Assert that GET /acquire/orders/{orderId} returns the correct wire shape.

    decisions.md Q5: DDB `stage` → wire `currentStage`, DDB `stageHistory`
    → wire `stages`.  Both Swift fields are non-optional.
    """
    if "currentStage" not in order:
        raise AssertionError(
            "Order response missing 'currentStage' (decisions.md Q5 wire rename "
            "of DDB 'stage' → 'currentStage')"
        )
    if "stages" not in order:
        raise AssertionError(
            "Order response missing 'stages' (decisions.md Q5 wire rename of "
            "DDB 'stageHistory' → 'stages')"
        )
    # Internal DDB fields must not bleed through
    _FORBIDDEN_FIELDS = {
        "configuration", "correlation", "financing", "nextTransitionAt",
        "orderPlacedAt", "payment", "plantId", "tenantOwnerKey", "ownerSub",
        "stage", "stageHistory",
    }
    leaked = _FORBIDDEN_FIELDS & set(order.keys())
    if leaked:
        raise AssertionError(
            f"Order response leaks internal DDB field(s): {leaked!r} — "
            f"these must be stripped by _CLIENT_FIELDS in the handler"
        )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http(method: str, url: str, id_token: str,
          body: dict | None = None, timeout_s: int = 30) -> tuple[int, dict]:
    """Make an authenticated HTTP request.  Returns (status_code, body_dict).

    Raises on connection/timeout errors.  HTTP 4xx/5xx are returned, not raised,
    so callers can produce beat-specific diagnostics.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
    }
    req = urlreq.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urlreq.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urlerr.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"_raw": "<non-JSON error body>"}
        return e.code, payload


def _get(api_url: str, path: str, id_token: str) -> tuple[int, dict]:
    return _http("GET", api_url.rstrip("/") + path, id_token)


def _post(api_url: str, path: str, id_token: str, body: dict) -> tuple[int, dict]:
    return _http("POST", api_url.rstrip("/") + path, id_token, body)


# ---------------------------------------------------------------------------
# Beat implementations (live mode)
# ---------------------------------------------------------------------------

def beat1_fetch_catalog(api_url: str, id_token: str,
                        tenant_id: str) -> dict:
    """Beat 1 — GET /acquire/catalog/{tenantId}"""
    print(f"  Beat 1: GET /acquire/catalog/{tenant_id}")
    status, body = _get(api_url, f"/acquire/catalog/{tenant_id}", id_token)
    if status != 200:
        raise AssertionError(
            f"Beat 1 FAILED: expected 200, got {status}. Body: {body}"
        )
    assert_catalog_shape(body)
    model_count = len(body.get("models", []))
    ive_count = len(body.get("ivePackages", []))
    interior_count = len(body.get("interiors", []))
    print(f"  ✓ Beat 1 PASS — {model_count} model(s), "
          f"{interior_count} interior(s), {ive_count} IVE package(s)")
    return body


def beat2_place_reservation(api_url: str, id_token: str,
                             tenant_id: str, catalog: dict) -> dict:
    """Beat 2 — POST /acquire/reservations (with interior + IVE package).

    Picks the first model, variant, color, interior, and IVE package from the
    catalog.  Falls back to placeholder strings if the catalog fixture is thin.
    """
    print("  Beat 2: POST /acquire/reservations")

    # Resolve configuration items from catalog (gracefully fall back to stubs)
    models = catalog.get("models", [])
    variants = catalog.get("variants", [])
    colors = catalog.get("colors", [])
    interiors = catalog.get("interiors", [])
    ive_packages = catalog.get("ivePackages", [])

    model_id = (models[0].get("modelId") if models else None) or "MODEL-PLACEHOLDER"
    variant_id = (variants[0].get("variantId") if variants else None) or "VAR-PLACEHOLDER"
    color_id = (colors[0].get("colorId") if colors else None) or "COLOR-PLACEHOLDER"
    interior_id = (interiors[0].get("interiorId") if interiors else None) or "INTERIOR-PLACEHOLDER"
    ive_package = (ive_packages[0].get("packageId") if ive_packages else None) or "IVE-PLACEHOLDER"

    payload = {
        "tenantId": tenant_id,
        "configuration": {
            "modelId": model_id,
            "variantId": variant_id,
            "colorId": color_id,
            "interiorId": interior_id,
            "ivePackage": ive_package,
        },
    }
    status, body = _post(api_url, "/acquire/reservations", id_token, payload)
    if status not in (200, 201):
        raise AssertionError(
            f"Beat 2 FAILED: expected 201, got {status}. Body: {body}"
        )
    assert_reservation_response(body)
    order_id = body["orderId"]
    print(f"  ✓ Beat 2 PASS — orderId={order_id}")
    return body


def beat3_verify_initial_stage(api_url: str, id_token: str,
                                order_id: str) -> dict:
    """Beat 3 — GET /acquire/orders/{orderId} and check initial stage."""
    print(f"  Beat 3: GET /acquire/orders/{order_id}")
    status, body = _get(api_url, f"/acquire/orders/{order_id}", id_token)
    if status != 200:
        raise AssertionError(
            f"Beat 3 FAILED: expected 200, got {status}. Body: {body}"
        )
    assert_order_response_shape(body)
    current_stage = body.get("currentStage")
    if current_stage != "orderPlaced":
        raise AssertionError(
            f"Beat 3 FAILED: expected currentStage='orderPlaced', "
            f"got {current_stage!r}"
        )
    print(f"  ✓ Beat 3 PASS — currentStage={current_stage}")
    return body


def beat4_poll_to_terminal(api_url: str, id_token: str,
                            order_id: str,
                            demo_budget_s: float,
                            t0: float) -> dict:
    """Beat 4 — Poll until currentStage == readyForDelivery or timeout."""
    print(f"  Beat 4: polling {order_id} → {TERMINAL_STAGE}")
    for attempt in range(_MAX_POLLS):
        elapsed = time.time() - t0
        if elapsed > demo_budget_s:
            raise AssertionError(
                f"Beat 4 FAILED: demo budget {demo_budget_s}s exceeded "
                f"({elapsed:.1f}s) before reaching '{TERMINAL_STAGE}'"
            )
        status, body = _get(api_url, f"/acquire/orders/{order_id}", id_token)
        if status != 200:
            raise AssertionError(
                f"Beat 4 FAILED: GET /acquire/orders returned {status} "
                f"on poll #{attempt + 1}. Body: {body}"
            )
        assert_order_response_shape(body)
        current_stage = body.get("currentStage", "")
        stage_count = len(body.get("stages", []))
        print(f"    poll #{attempt + 1:02d} ({elapsed:5.1f}s) — "
              f"currentStage={current_stage}  stages={stage_count}")
        if current_stage == TERMINAL_STAGE:
            print(f"  ✓ Beat 4 PASS — reached '{TERMINAL_STAGE}' "
                  f"after {elapsed:.1f}s")
            return body
        # Order progression is driven by an async Step Functions execution, so
        # the terminal stage is only observable by polling. The loop is bounded
        # at `_MAX_POLLS` and raises below on exhaustion; this sleep paces it.
        # Keep `nosemgrep` as the last comment line before the target.
        # nosemgrep: arbitrary-sleep
        time.sleep(_POLL_INTERVAL_S)

    raise AssertionError(
        f"Beat 4 FAILED: reached poll limit ({_MAX_POLLS} attempts) without "
        f"reaching '{TERMINAL_STAGE}'"
    )


def beat5_assert_stage_history(final_order: dict,
                                t0: float,
                                demo_budget_s: float) -> None:
    """Beat 5 — Assert all 10 stages are present in order, budget met."""
    print("  Beat 5: asserting stage history and elapsed time")
    stages_array = final_order.get("stages", [])
    found = assert_stages_in_order(stages_array)
    assert_all_stages_present(found)
    elapsed = time.time() - t0
    assert_elapsed_within_budget(elapsed, demo_budget_s)
    print(f"  ✓ Beat 5 PASS — all {len(found)} stages in order, "
          f"elapsed={elapsed:.1f}s ≤ budget={demo_budget_s}s")


# ---------------------------------------------------------------------------
# Self-check mode — offline, no AWS calls
# ---------------------------------------------------------------------------

def _self_check_catalog_shape() -> None:
    """Assert assert_catalog_shape accepts a well-formed fixture and rejects bad ones."""
    good = {k: [] for k in ["categories", "models", "variants",
                             "colors", "accessories", "interiors", "ivePackages"]}
    good["tenantId"] = "meridian"
    assert_catalog_shape(good)  # must not raise

    # Missing interiors → should raise
    bad = dict(good)
    del bad["interiors"]
    try:
        assert_catalog_shape(bad)
        raise AssertionError("assert_catalog_shape should have raised on missing 'interiors'")
    except AssertionError as e:
        assert "interiors" in str(e), f"Unexpected message: {e}"


def _self_check_reservation_shape() -> None:
    """Assert assert_reservation_response accepts good and rejects bad."""
    good = {
        "orderId": "ORD-20261101-abc123",
        "orderNumber": "ORD-20261101-abc123",
        "stage": "orderPlaced",
        "nextTransitionAt": "2026-11-01T10:00:00+00:00",
    }
    assert_reservation_response(good)

    # Missing orderNumber → should raise
    bad = dict(good)
    del bad["orderNumber"]
    try:
        assert_reservation_response(bad)
        raise AssertionError("Should have raised on missing orderNumber")
    except AssertionError as e:
        assert "orderNumber" in str(e), f"Unexpected message: {e}"


def _self_check_order_response_shape() -> None:
    """Assert assert_order_response_shape accepts good wire shape and rejects bad."""
    good = {
        "orderId": "ORD-20261101-abc123",
        "tenantId": "meridian",
        "currentStage": "orderPlaced",
        "stages": [{"stage": "orderPlaced", "enteredAt": "2026-11-01T10:00:00+00:00"}],
        "modelId": "windrose",
    }
    assert_order_response_shape(good)

    # DDB field 'stage' leaking through → should raise
    bad = dict(good)
    bad["stage"] = "orderPlaced"  # leaked DDB attribute name
    try:
        assert_order_response_shape(bad)
        raise AssertionError("Should have raised on leaked 'stage' field")
    except AssertionError as e:
        assert "stage" in str(e), f"Unexpected message: {e}"

    # Missing currentStage → should raise
    bad2 = dict(good)
    del bad2["currentStage"]
    try:
        assert_order_response_shape(bad2)
        raise AssertionError("Should have raised on missing 'currentStage'")
    except AssertionError as e:
        assert "currentStage" in str(e), f"Unexpected message: {e}"


def _self_check_stages_in_order() -> None:
    """Assert assert_stages_in_order accepts the canonical sequence and rejects violations."""
    all_entries = [{"stage": s, "enteredAt": "2026-11-01T10:00:00+00:00"} for s in STAGES]

    # Full canonical sequence → OK
    found = assert_stages_in_order(all_entries)
    assert found == STAGES, f"Expected {STAGES}, got {found}"

    # Partial sequence (first 5 stages) — still in order → OK
    partial = assert_stages_in_order(all_entries[:5])
    assert partial == STAGES[:5]

    # Empty list → raises
    try:
        assert_stages_in_order([])
        raise AssertionError("Should have raised on empty stages")
    except AssertionError as e:
        assert "empty" in str(e).lower(), f"Unexpected message: {e}"

    # Reversed order → raises
    try:
        assert_stages_in_order(list(reversed(all_entries)))
        raise AssertionError("Should have raised on reversed order")
    except AssertionError as e:
        assert "ordering" in str(e).lower() or "order" in str(e).lower(), \
            f"Unexpected message: {e}"

    # Unknown stage name → raises
    try:
        assert_stages_in_order([{"stage": "unknownStage", "enteredAt": "2026-11-01T10:00:00"}])
        raise AssertionError("Should have raised on unknown stage")
    except AssertionError as e:
        assert "Unknown" in str(e) or "unknown" in str(e).lower(), \
            f"Unexpected message: {e}"


def _self_check_all_stages_present() -> None:
    """Assert assert_all_stages_present raises on a missing stage."""
    # Complete list → OK
    assert_all_stages_present(list(STAGES))

    # Missing last stage → raises
    try:
        assert_all_stages_present(STAGES[:-1])
        raise AssertionError("Should have raised on missing readyForDelivery")
    except AssertionError as e:
        assert "readyForDelivery" in str(e), f"Unexpected message: {e}"

    # Multiple missing → raises and names them
    try:
        assert_all_stages_present(STAGES[:3])
        raise AssertionError("Should have raised on multiple missing stages")
    except AssertionError as e:
        assert "Missing" in str(e) or "missing" in str(e), f"Unexpected message: {e}"


def _self_check_elapsed_budget() -> None:
    """Assert assert_elapsed_within_budget passes within and fails outside budget."""
    assert_elapsed_within_budget(100.0, 180.0)   # well within → OK
    assert_elapsed_within_budget(180.0, 180.0)   # at limit → OK (≤ not <)

    try:
        assert_elapsed_within_budget(181.0, 180.0)
        raise AssertionError("Should have raised on budget exceeded")
    except AssertionError as e:
        assert "budget" in str(e).lower() or "exceeds" in str(e).lower(), \
            f"Unexpected message: {e}"


def run_self_check() -> int:
    """Run all offline self-checks.  Returns 0 on pass, 1 on fail."""
    checks = [
        ("catalog shape", _self_check_catalog_shape),
        ("reservation shape", _self_check_reservation_shape),
        ("order response shape", _self_check_order_response_shape),
        ("stages in order", _self_check_stages_in_order),
        ("all stages present", _self_check_all_stages_present),
        ("elapsed budget", _self_check_elapsed_budget),
    ]

    failures: list[str] = []
    for name, fn in checks:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}: {e}", file=sys.stderr)
            failures.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {name}: unexpected error: {e}", file=sys.stderr)
            failures.append(name)

    if failures:
        print(f"\n✗ Self-check FAILED — {len(failures)} check(s) failed: {failures}",
              file=sys.stderr)
        return 1

    print(f"\n🎉 Self-check PASSED — {len(checks)} assertion helper(s) verified "
          f"(zero AWS calls).")
    return 0


# ---------------------------------------------------------------------------
# Live journey runner
# ---------------------------------------------------------------------------

def run_live_journey(api_url: str, id_token: str, tenant_id: str,
                     demo_budget_s: float) -> int:
    """Walk Beats 1-5 against a deployed stack.  Returns 0 on pass, 1 on fail."""
    t0 = time.time()
    print(f"→ API URL:       {api_url}")
    print(f"→ Tenant:        {tenant_id}")
    print(f"→ Demo budget:   {demo_budget_s}s")
    print()

    try:
        # Beat 1 — fetch catalog
        catalog = beat1_fetch_catalog(api_url, id_token, tenant_id)

        # Beat 2 — place reservation
        reservation = beat2_place_reservation(api_url, id_token, tenant_id, catalog)
        order_id = reservation["orderId"]

        # Beat 3 — verify initial stage
        beat3_verify_initial_stage(api_url, id_token, order_id)

        # Beat 4 — poll to terminal
        final_order = beat4_poll_to_terminal(
            api_url, id_token, order_id, demo_budget_s, t0
        )

        # Beat 5 — assert all 10 stages in order + within budget
        beat5_assert_stage_history(final_order, t0, demo_budget_s)

    except AssertionError as e:
        print(f"\n✗ Zone 2 journey smoke test FAILED: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ Zone 2 journey smoke test ERROR: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    elapsed = time.time() - t0
    print()
    print(f"🎉 Zone 2 journey smoke test PASSED — {elapsed:.1f}s total.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        default=False,
        help=(
            "Run offline self-check only: exercise all assertion helpers "
            "against in-process fixtures with zero AWS calls. "
            "Exits 0 on pass. Use this to verify assertion logic before deploy."
        ),
    )
    parser.add_argument(
        "--stage",
        default=os.environ.get("VSA_STAGE", "staging"),
        help="Deployment stage label (default: staging). "
             "Used in help text only; the actual URL is --api-url.",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("VSA_API_URL", ""),
        help="Base URL of the deployed VSA REST API "
             "(e.g. https://<id>.execute-api.us-west-2.amazonaws.com/staging). "
             "Required in live mode. Can also be set via VSA_API_URL env var.",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("VSA_TENANT_ID", "meridian"),
        help="Tenant id to use for the journey (default: meridian). "
             "Can also be set via VSA_TENANT_ID env var.",
    )
    parser.add_argument(
        "--id-token",
        default=None,
        help=(
            "Kiosk device Cognito IdToken. "
            "Prefer VSA_KIOSK_ID_TOKEN env var — CLI args appear in shell "
            "history and `ps`. Required in live mode."
        ),
    )
    parser.add_argument(
        "--demo-budget",
        type=float,
        default=_DEFAULT_DEMO_BUDGET_S,
        metavar="SECONDS",
        help=(
            f"Maximum allowed elapsed seconds for the full journey "
            f"(default: {_DEFAULT_DEMO_BUDGET_S}). "
            "Can also be set via VSA_ZONE2_DEMO_BUDGET_S env var."
        ),
    )
    args = parser.parse_args()

    # ── Self-check mode ──────────────────────────────────────────────────
    if args.self_check:
        print("Zone 2 journey smoke test — offline self-check")
        print("=" * 50)
        return run_self_check()

    # ── Live mode — validate required inputs ─────────────────────────────
    api_url = args.api_url.strip()
    if not api_url:
        print(
            "✗ --api-url (or VSA_API_URL env var) is required in live mode.\n"
            "  Use --self-check to run offline assertion checks without AWS.",
            file=sys.stderr,
        )
        return 1

    # IdToken resolution: CLI flag > env var (env preferred to avoid shell history)
    id_token: str | None = args.id_token or os.environ.get("VSA_KIOSK_ID_TOKEN")
    if not id_token:
        print(
            "✗ Kiosk IdToken is required in live mode.\n"
            "  Provide via VSA_KIOSK_ID_TOKEN env var (preferred) or --id-token flag.\n"
            "  Never hard-code credentials — see non-interactive.md.",
            file=sys.stderr,
        )
        return 1

    print(f"Zone 2 journey smoke test — {args.stage}")
    print("=" * 50)
    return run_live_journey(
        api_url=api_url,
        id_token=id_token,
        tenant_id=args.tenant_id,
        demo_budget_s=args.demo_budget,
    )


if __name__ == "__main__":
    sys.exit(main())
