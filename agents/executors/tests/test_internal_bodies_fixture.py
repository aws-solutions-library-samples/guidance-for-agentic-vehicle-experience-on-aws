# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The DMS internal routes rebuild AVX's request body from the Action (FI spec D10, 11.3c/11.5).

``fixtures/fi_internal_bodies.json`` holds Actions and the exact body AVX's DMS executor sends
for each. DMS keeps a byte-identical copy (``tests/fixtures/fi_internal_bodies.json``) and its
internal routes refuse a body that differs from the one they rebuild. Both repos pin the same
sha256, so changing one copy without the other fails a test in that repo.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agents.executors import dms

FIXTURE = Path(__file__).parent / "fixtures" / "fi_internal_bodies.json"
#: Same literal in DMS tests/unit/test_avx_internal_bodies.py.
FIXTURE_SHA256 = "5518d0052283a202e1286f49bf22559cb2264b1fca85dee7be42b855b0007948"

_BUILDERS = {
    "book_fleet_service": dms._fleet_service_body,
    "book_recall_service": dms._request_body,
    "submit_warranty_claim": dms._warranty_claim_body,
}
_CASES = json.loads(FIXTURE.read_text())["cases"]


def test_fixture_bytes_are_pinned() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_fixture_covers_every_internal_route_kind() -> None:
    assert {c["action"]["action_kind"] for c in _CASES} == set(_BUILDERS)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_executor_sends_exactly_the_fixture_body(case: dict) -> None:
    action = case["action"]
    assert _BUILDERS[action["action_kind"]](action) == case["body"]
