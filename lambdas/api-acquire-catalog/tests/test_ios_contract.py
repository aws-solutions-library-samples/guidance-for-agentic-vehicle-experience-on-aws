"""Cross-repo contract test: AcquireCatalogClient.swift DTOs vs this Lambda's
response shapes.

WHY THIS EXISTS
---------------
The `/acquire/*` routes are new. The iOS client (`AcquireCatalogClient.swift`)
declares four Swift Codable structs against these routes:

  - `CatalogResponse`   — GET /acquire/catalog/{tenantId}
  - `AcquireOrder`      — GET /acquire/orders/{orderId}
  - `ReservationRequest` — POST /acquire/reservations (client → server)
  - `ReservationResponse` — POST /acquire/reservations (server → client)

Nothing linked the server contract to the client until this test. A field
mismatch silently compiles, deploys, and fails at runtime in one of two
invisible ways: the decode fails silently (Swift optional decodes as nil)
or the field is absent from the response and the UI shows a wrong default.

This test pins those four contracts by parsing the Swift source. It is the
same technique used for `api-assistant-chat` and `api-tenant-config` — the
only mechanism in this codebase that has caught cross-repo drift.

APPROACH
--------
Source-parsing: reads AcquireCatalogClient.swift, extracts property names
from each Codable struct, and asserts alignment with the handler response
shapes. No AWS credentials, no network, no simulator.

The CMS repo is expected as a sibling of the CVX repo (same assumption
`scripts/update-staging-xcconfig.py` makes). When it is absent the test
SKIPS rather than fails, so CVX-only checkouts remain green.

LOADING PATTERN NOTE
--------------------
This test lives under lambdas/api-acquire-catalog/tests/ alongside
test_catalog_handler.py. It asserts the DTO field contracts; the handler
skeleton tests (test_catalog_handler.py) assert the HTTP contract. Both
tests are intentionally in separate files so they can fail independently —
a missing handler does not block DTO contract checking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CVX_ROOT = Path(__file__).resolve().parents[3]
_CMS_ROOT = _CVX_ROOT.parent / "connected-mobility-guidance-on-aws"
_SWIFT_CLIENT = _CMS_ROOT / "clients/ios/VSACompanion/Api/AcquireCatalogClient.swift"

# Handler files that must contain these field names in their response logic.
# When a handler does not exist yet, we test only that the Swift contract is
# internally coherent and properly parsed.
_CATALOG_HANDLER = Path(__file__).resolve().parents[1] / "handler.py"
_ORDER_HANDLER = _CVX_ROOT / "lambdas/api-acquire-order/handler.py"


def _require_swift_file() -> None:
    if not _SWIFT_CLIENT.exists():
        pytest.skip(
            f"CMS repo not present at {_CMS_ROOT} — cross-repo contract test skipped"
        )


def _swift_src() -> str:
    return _SWIFT_CLIENT.read_text()


# ---------------------------------------------------------------------------
# Swift DTO field extraction
#
# Strategy: find the struct by name, collect property names from the body.
# Handles both plain `let x: T` and `CodingKeys` remapping (wire name
# is the rhs of `case x = "y"`; bare case name otherwise).
# ---------------------------------------------------------------------------

def _extract_struct_properties(src: str, struct_name: str) -> set[str]:
    """Return the set of PROPERTY NAMES declared in the named struct.

    For structs WITHOUT CodingKeys, these are also the wire keys.
    For structs WITH CodingKeys, wire keys may differ — call
    _extract_coding_keys() for those.
    """
    m = re.search(
        rf"struct\s+{re.escape(struct_name)}\s*:\s*[^{{]*\{{(.*?)\n\}}",
        src,
        re.DOTALL,
    )
    assert m, f"struct {struct_name} not found in AcquireCatalogClient.swift"
    body = m.group(1)

    # Strip nested type bodies so their properties are not double-counted.
    depth = 0
    cleaned_lines = []
    for line in body.splitlines():
        depth += line.count("{") - line.count("}")
        if depth == 0:
            cleaned_lines.append(line)

    props: set[str] = set()
    for line in cleaned_lines:
        line = line.split("//", 1)[0]
        hit = re.match(r"\s*(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
        if hit:
            props.add(hit.group(1))
    return props


def _extract_wire_keys(src: str, struct_name: str) -> set[str]:
    """Return the wire keys a struct will use when encoding / decoding.

    If the struct has a CodingKeys enum, the wire key is the rhs of
    `case x = "y"` when present, or the bare case name otherwise.
    If no CodingKeys, wire keys are the property names.
    """
    m = re.search(
        rf"struct\s+{re.escape(struct_name)}\s*:\s*[^{{]*\{{(.*?)\n\}}",
        src,
        re.DOTALL,
    )
    assert m, f"struct {struct_name} not found in AcquireCatalogClient.swift"
    body = m.group(1)

    enum = re.search(r"enum\s+CodingKeys[^{{]*\{{(.*?)\}}", body, re.DOTALL)
    if not enum:
        return _extract_struct_properties(src, struct_name)

    keys: set[str] = set()
    for raw_line in enum.group(1).splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line.startswith("case "):
            continue
        for item in line[len("case "):].split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                _, wire = item.split("=", 1)
                keys.add(wire.strip().strip('"'))
            else:
                keys.add(item)
    return keys


# ---------------------------------------------------------------------------
# Sanity guard: verify the parser produces plausible results
# ---------------------------------------------------------------------------

class TestParserSanity:
    """These tests guard the parser itself.

    A contract test that parses zero fields would pass forever while checking
    nothing — the failure mode this whole file exists to prevent (per the
    api-assistant-chat post-mortem on 2026-07-28).
    """

    def test_catalog_response_parses_enough_fields(self):
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "CatalogResponse")
        assert len(fields) >= 5, (
            f"CatalogResponse parsed implausibly few fields: {fields}. "
            "The regex is probably broken."
        )

    def test_acquire_order_parses_enough_fields(self):
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "AcquireOrder")
        assert len(fields) >= 5, (
            f"AcquireOrder parsed implausibly few fields: {fields}. "
            "The regex is probably broken."
        )

    def test_reservation_request_parses_enough_fields(self):
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "ReservationRequest")
        assert len(fields) >= 4, (
            f"ReservationRequest parsed implausibly few fields: {fields}. "
            "The regex is probably broken."
        )

    def test_reservation_response_parses_enough_fields(self):
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "ReservationResponse")
        assert len(fields) >= 2, (
            f"ReservationResponse parsed implausibly few fields: {fields}. "
            "The regex is probably broken."
        )


# ---------------------------------------------------------------------------
# CatalogResponse contract
# ---------------------------------------------------------------------------

class TestCatalogResponseContract:
    """GET /acquire/catalog/{tenantId} → CatalogResponse"""

    # Required fields the Swift struct declares as non-optional.
    # Optional fields (catalogVersion) may be absent from the response.
    _REQUIRED_FIELDS = {"tenantId", "categories", "models", "variants", "colors", "accessories"}

    def test_swift_struct_declares_required_fields(self):
        """Pin the specific required fields the server MUST return."""
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "CatalogResponse")
        for field in self._REQUIRED_FIELDS:
            assert field in declared, (
                f"Swift CatalogResponse must declare '{field}'. "
                f"Declared fields: {sorted(declared)}"
            )

    def test_handler_produces_all_required_response_fields(self):
        """When handler exists, verify it references each required field key."""
        if not _CATALOG_HANDLER.exists():
            pytest.skip("Catalog handler does not exist yet — DTO shape check deferred to Group 2")
        handler_src = _CATALOG_HANDLER.read_text()
        for field in self._REQUIRED_FIELDS:
            assert f'"{field}"' in handler_src or f"'{field}'" in handler_src, (
                f"Catalog handler must reference response field '{field}'. "
                f"CatalogResponse.{field} is a required Swift property — "
                f"the response will silently omit it if not explicitly included."
            )

    def test_tenant_id_field_name(self):
        """Pin the exact wire key — 'tenantId' not 'tenant_id'."""
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "CatalogResponse")
        assert "tenantId" in fields, (
            "CatalogResponse.tenantId must be 'tenantId' (camelCase). "
            "A snake_case response key 'tenant_id' will decode to nil in Swift."
        )
        assert "tenant_id" not in fields


# ---------------------------------------------------------------------------
# AcquireOrder contract
# ---------------------------------------------------------------------------

class TestAcquireOrderContract:
    """GET /acquire/orders/{orderId} → AcquireOrder"""

    # Required (non-optional) fields in the Swift struct.
    _REQUIRED_FIELDS = {"orderId", "tenantId", "currentStage", "stages"}

    # Swift uses `currentStage` — the server must NOT return `stage` alone.
    # If it does, the Swift property decodes as nil and Beat 4/5 UX breaks.
    _FIELD_NAME_PINS = {
        "currentStage": (
            "AcquireOrder.currentStage is the wire key the Swift client reads. "
            "The DDB item uses `stage` internally; the handler must remap it to "
            "`currentStage`. Returning `stage` makes currentStage nil on the client."
        ),
        "stages": (
            "AcquireOrder.stages is the Swift array of OrderStageRecord. "
            "The DDB item stores this as `stageHistory`; the handler must remap it. "
            "Returning `stageHistory` leaves `stages` nil on the client."
        ),
    }

    def test_swift_struct_declares_required_fields(self):
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "AcquireOrder")
        for field in self._REQUIRED_FIELDS:
            assert field in declared, (
                f"Swift AcquireOrder must declare '{field}'. "
                f"Declared: {sorted(declared)}"
            )

    def test_current_stage_field_name_is_camel_case(self):
        """currentStage not `stage` — critical rename from DDB internal key."""
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "AcquireOrder")
        assert "currentStage" in fields, self._FIELD_NAME_PINS["currentStage"]

    def test_stages_field_name_not_stage_history(self):
        """stages not `stageHistory` — critical rename from DDB internal key."""
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "AcquireOrder")
        assert "stages" in fields, self._FIELD_NAME_PINS["stages"]
        # stageHistory is the DDB name; it must NOT be the wire key.
        # Swift will decode stageHistory to nil if the struct declares `stages`.
        assert "stageHistory" not in fields, (
            "Swift AcquireOrder must NOT declare 'stageHistory'. "
            "The wire key must be 'stages'. "
            f"Declared: {sorted(fields)}"
        )

    def test_handler_remaps_stage_to_current_stage(self):
        """When handler exists, verify it maps DDB `stage` → response `currentStage`."""
        if not _ORDER_HANDLER.exists():
            pytest.skip("Order handler does not exist yet — remap check deferred to Group 2")
        handler_src = _ORDER_HANDLER.read_text()
        assert '"currentStage"' in handler_src or "'currentStage'" in handler_src, (
            "api-acquire-order/handler.py must produce a 'currentStage' response key. "
            "The DDB item uses `stage`; map it to `currentStage` before returning."
        )

    def test_handler_remaps_stage_history_to_stages(self):
        """When handler exists, verify it maps DDB `stageHistory` → response `stages`."""
        if not _ORDER_HANDLER.exists():
            pytest.skip("Order handler does not exist yet — remap check deferred to Group 2")
        handler_src = _ORDER_HANDLER.read_text()
        assert '"stages"' in handler_src or "'stages'" in handler_src, (
            "api-acquire-order/handler.py must produce a 'stages' response key. "
            "The DDB item uses `stageHistory`; map it to `stages` before returning."
        )


# ---------------------------------------------------------------------------
# ReservationRequest contract (client → server)
# ---------------------------------------------------------------------------

class TestReservationRequestContract:
    """POST /acquire/reservations body — what iOS sends."""

    _EXPECTED_FIELDS = {
        "tenantId", "modelId", "variantId", "colorId", "selectedAccessoryIds"
    }
    # Optional fields (Swift `?` suffix) — server must tolerate their absence.
    _OPTIONAL_FIELDS = {
        "depositRef", "qualificationTier", "discoverSessionId", "leadId"
    }

    def test_swift_struct_declares_expected_fields(self):
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "ReservationRequest")
        for field in self._EXPECTED_FIELDS:
            assert field in declared, (
                f"Swift ReservationRequest must declare '{field}'. "
                f"Declared: {sorted(declared)}"
            )

    def test_swift_struct_declares_optional_fields(self):
        """Optional fields must also be declared (Swift may send nil, server must accept)."""
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "ReservationRequest")
        for field in self._OPTIONAL_FIELDS:
            assert field in declared, (
                f"Swift ReservationRequest should declare optional '{field}'. "
                f"Declared: {sorted(declared)}"
            )

    def test_no_owner_sub_in_reservation_request(self):
        """ownerSub must NOT be in the request body — it is derived server-side from
        the Cognito claim. Putting it in the body is a security issue (client override)
        and was the pre-spec behaviour this spec corrects (Group 2 Task 1)."""
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "ReservationRequest")
        assert "ownerSub" not in declared, (
            "Swift ReservationRequest must NOT send ownerSub. "
            "The server derives it from the Cognito claim. "
            "A client-supplied ownerSub would allow identity spoofing."
        )


# ---------------------------------------------------------------------------
# ReservationResponse contract (server → client)
# ---------------------------------------------------------------------------

class TestReservationResponseContract:
    """POST /acquire/reservations → ReservationResponse"""

    _REQUIRED_FIELDS = {"orderId", "orderNumber"}

    def test_swift_struct_declares_required_fields(self):
        _require_swift_file()
        declared = _extract_struct_properties(_swift_src(), "ReservationResponse")
        for field in self._REQUIRED_FIELDS:
            assert field in declared, (
                f"Swift ReservationResponse must declare '{field}'. "
                f"Declared: {sorted(declared)}"
            )

    def test_order_id_field_name_is_camel_case(self):
        """orderId not `order_id`."""
        _require_swift_file()
        fields = _extract_struct_properties(_swift_src(), "ReservationResponse")
        assert "orderId" in fields
        assert "order_id" not in fields

    def test_existing_reservation_handoff_returns_order_id(self):
        """reservation_handoff handler (which handles POST /acquire/reservations)
        must already return `orderId` in its response body. This handler exists
        today, so we can assert immediately without a skip guard."""
        rh_handler = _CVX_ROOT / "lambdas/acquire/reservation_handoff/handler.py"
        if not rh_handler.exists():
            pytest.skip("reservation_handoff handler not found")
        src = rh_handler.read_text()
        assert '"orderId"' in src or "'orderId'" in src, (
            "reservation_handoff/handler.py must include 'orderId' in its response. "
            "Swift ReservationResponse.orderId is a required non-optional field."
        )

    def test_reservation_handoff_returns_order_number(self):
        """reservation_handoff MUST return `orderNumber` in its response.

        Swift ReservationResponse.orderNumber is non-optional (no `?`), so its
        absence makes JSONDecoder throw and every reservation fails to decode
        client-side.

        Gap closed in Group 2 (decisions.md Q3).  Previously this test asserted
        the field was ABSENT to document the known gap; it now asserts PRESENCE
        to guard against regression.
        """
        rh_handler = _CVX_ROOT / "lambdas/acquire/reservation_handoff/handler.py"
        if not rh_handler.exists():
            pytest.skip("reservation_handoff handler not found")
        src = rh_handler.read_text()
        has_order_number = '"orderNumber"' in src or "'orderNumber'" in src
        assert has_order_number, (
            "reservation_handoff/handler.py must include 'orderNumber' in its "
            "response body.  Swift ReservationResponse.orderNumber is "
            "non-optional — its absence makes every reservation fail to decode "
            "client-side (decisions.md Q3)."
        )
