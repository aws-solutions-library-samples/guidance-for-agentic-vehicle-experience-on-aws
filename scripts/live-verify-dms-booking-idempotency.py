#!/usr/bin/env python3
"""Live check: DMS staging deduplicates AVX bookings on ``avx_action_id``.

The deploy-order gate in
``.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/HANDOFF-g10-ready.md``: CVX
approve dispatch retries 5xx and timeouts, which is safe only if DMS returns the same
repair order for a repeated ``avx_action_id`` (DMS ``e2085e1`` / ``216a8d6``). Run
this after deploying DMS and before deploying CVX dispatch.

Checks, against real DMS staging, with a fresh random ``avx_action_id``:
  1. first POST                          -> 201, an ``ro_id``
  2. same body again                     -> 200, the SAME ``ro_id``
  3. same id, different dealer           -> 409

This creates ONE real Draft repair order in DMS staging (printed). The principal is the
ephemeral ``platform-admin`` + ``fleet-operator`` CMS-pool user from
``scripts/provision-avx-e2e-principal.py``, deleted afterwards. The token is never
printed.

Usage: ``LIVE_TESTS=1 .venv/bin/python scripts/live-verify-dms-booking-idempotency.py [--vin VIN]``
Exit 0 on pass, 1 on failure, 0 with SKIPPED when ``LIVE_TESTS`` is unset.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "provision_avx_e2e_principal", _HERE / "provision-avx-e2e-principal.py")
_prov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prov)  # type: ignore[union-attr]


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 (creates one Draft RO in DMS staging and an "
              "ephemeral Cognito user).")
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--vin", default="MRDCW01H8PC00V7RD")
    ap.add_argument("--dealer", default="dealer-seattle")
    args = ap.parse_args()

    url = _prov._ssm(_prov.DMS_ENDPOINT_SSM).rstrip("/") + "/api/dms/fleet/repair-orders"
    avx_id = str(uuid.uuid4())
    body = {"vehicle_vin": args.vin, "dealer_id": args.dealer, "avx_action_id": avx_id}
    failures: list[str] = []

    with _prov.provision() as (token, _username, _claims):
        s1, b1 = _post(url, token, body)
        s2, b2 = _post(url, token, body)
        s3, _ = _post(url, token, {**body, "dealer_id": "dealer-boston"})

    ro1, ro2 = b1.get("ro_id"), b2.get("ro_id")
    print(f"\navx_action_id : {avx_id}")
    print(f"1 first POST  : {s1} ro_id={ro1}")
    print(f"2 replay      : {s2} ro_id={ro2}")
    print(f"3 other dealer: {s3}")
    if s1 != 201 or not ro1:
        failures.append(f"first POST expected 201 with ro_id, got {s1}")
    if s2 != 200 or ro2 != ro1:
        failures.append(f"replay expected 200 with the same ro_id, got {s2} ro_id={ro2}")
    if s3 != 409:
        failures.append(f"different dealer expected 409, got {s3}")
    if failures:
        print("\nFAIL:\n  " + "\n  ".join(failures))
        return 1
    print(f"\nPASS — DMS deduplicates AVX bookings. Test RO left in staging: {ro1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
