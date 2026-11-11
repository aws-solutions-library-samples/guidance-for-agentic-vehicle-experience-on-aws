"""Unit tests for parts_intel orchestrator tool — RED phase (Group 2).

All 12 tests correspond byte-for-byte to spec § "Test strategy" items 1–12.
This file is the RED phase: `agents.supervisor.tools.parts_intel` does not
exist yet; every test imports it locally so pytest --collect-only can discover
all 12 names (T2.1 Verify), while actually running any test fails with
ModuleNotFoundError naming agents.supervisor.tools.parts_intel (T2.2 Verify).

Tests are numbered in spec order:
  1  test_candidate_parts_happy_path
  2  test_candidate_parts_kb_unavailable
  3  test_recall_check_disclosure_is_verbatim
  4  test_recall_check_never_asserts_absence
  5  test_warranty_summary_verdict_is_none
  6  test_dispatch_skipped_without_fleet_operator
  7  test_dispatch_forwards_jwt
  8  test_dispatch_surfaces_endpoint_body_verbatim
  9  test_dispatch_surfaces_400_verbatim
 10  test_tool_not_registered_on_bidi_catalog
 11  test_source_category_filter_gated
 12  test_recall_and_warranty_authoritative_flags
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (mirroring test_kb_tools.py style)
# ---------------------------------------------------------------------------


def _fake_config(
    kb_id: str = "test-kb-id",
    region: str = "us-east-1",
    dms_api_endpoint: str = "https://dms.example.test",
    dms_stage: str = "staging",
) -> SimpleNamespace:
    """Minimal config stub for parts_intel factory."""
    return SimpleNamespace(
        adp_kb_id=kb_id,
        adp_region=region,
        dms_api_endpoint=dms_api_endpoint,
        dms_stage=dms_stage,
    )


def _fake_session(
    persona_id: str = "service_advisor",
    groups: list[str] | None = None,
    jwt: str = "test.jwt.token",
) -> SimpleNamespace:
    """Minimal session stub with Cognito groups."""
    if groups is None:
        groups = ["fleet-operator", "fleet-viewer"]
    return SimpleNamespace(
        persona_id=persona_id,
        vin="1FMCU0J91KUB12345",
        tenant_id="fleet",
        customer_id="cust-123",
        jwt=jwt,
        cognito_groups=groups,
    )


def _retrieve_response(*texts: str) -> dict:
    """Build a minimal bedrock-agent-runtime:Retrieve response."""
    return {
        "retrievalResults": [
            {
                "content": {"text": t},
                "location": {"s3Location": {"uri": f"s3://bucket/doc-{i}.md"}},
                "score": 0.9 - i * 0.1,
            }
            for i, t in enumerate(texts)
        ]
    }


def _dispatch_201_body() -> dict:
    """Mock 201 response body with ALL ELEVEN fields from spec § D-5.

    The eleven fields are:
      ro_id, vehicle_vin, dealer_id, dealer_name, status, opened_at,
      created_at, updated_at, description, initiated_by, recall_id.

    A ten-field mock (omitting dealer_name) would pass this test while
    failing to exercise the shape the live endpoint actually returns.
    See spec § D-5: dealer_name is appended by _project_narrow, not a
    member of _NARROW_RO_FIELDS, but present on every successful 201.
    """
    return {
        "ro_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "vehicle_vin": "1FMCU0J91KUB12345",
        "dealer_id": "meridian-of-denver",
        "dealer_name": "Test Rooftop North",           # 11th field — must be present
        "status": "Draft",
        "opened_at": "2026-09-10T12:00:00Z",
        "created_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z",
        "description": "Recall service — P0420 inspection",
        "initiated_by": "cms_booking",
        "recall_id": "recall-24V-234",
    }


# ---------------------------------------------------------------------------
# Test 1: candidate_parts happy path
# ---------------------------------------------------------------------------


def test_candidate_parts_happy_path(monkeypatch):
    """Retrieve is called with source_category=parts_catalog; parts list returned.

    Arrange: mock bedrock-agent-runtime Retrieve; filter gate ON.
    Act:     call the candidate-parts step of parts_intel_orchestrate.
    Assert:  Retrieve called with correct filter; result.found is True.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    session = _fake_session()
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        '{"partNumber": "BRK-4721", "name": "Front Brake Pad Set", "supersession": "BRK-4700"}'
    )

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result = tool_fn(
            dtc="P0420",
            vehicle_make="Ford",
            vehicle_model="Transit",
            vehicle_year="2022",
            vin="1FMCU0J91KUB12345",
        )

    assert result["candidate_parts"]["found"] is True
    assert len(result["candidate_parts"]["parts"]) >= 1

    # Confirm at least one Retrieve call used parts_catalog filter
    calls = mock_boto3.client.return_value.retrieve.call_args_list
    parts_catalog_call = next(
        (c for c in calls
         if c[1].get("retrievalConfiguration", {})
             .get("vectorSearchConfiguration", {})
             .get("filter", {})
             .get("equals", {})
             .get("value") == "parts_catalog"),
        None,
    )
    assert parts_catalog_call is not None, "expected a Retrieve call with parts_catalog filter"


# ---------------------------------------------------------------------------
# Test 2: candidate_parts KB unavailable
# ---------------------------------------------------------------------------


def test_candidate_parts_kb_unavailable():
    """When adp_kb_id is empty, candidate_parts returns found:false, reason 'KB unavailable'.

    Arrange: config with no adp_kb_id.
    Act:     call parts_intel_orchestrate.
    Assert:  candidate_parts.found is False; reason present.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    session = _fake_session()
    config = _fake_config(kb_id="")

    tool_fn = make_parts_intel_tool(session=session, config=config)
    result = tool_fn(dtc="P0420", vehicle_make="Ford")

    assert result["candidate_parts"]["found"] is False
    assert "reason" in result["candidate_parts"]
    assert "unavailable" in result["candidate_parts"]["reason"].lower()


# ---------------------------------------------------------------------------
# Test 3: recall_check disclosure is verbatim (seam-preservation test)
# ---------------------------------------------------------------------------


def test_recall_check_disclosure_is_verbatim(monkeypatch):
    """_RECALL_DISCLOSURE_MSG appears byte-identical in every recall_check response.

    This is a deterministic-seam narration-preservation test per spec § D-1 and
    § D-5. The constant must contain the word 'authoritative'.
    Full-string equality — no 'in' or fuzzy match.

    Arrange: two scenarios — (a) Retrieve returns a hit, (b) Retrieve returns empty.
    Act:     call parts_intel_orchestrate in both scenarios.
    Assert:  recall_check.disclosure == _RECALL_DISCLOSURE_MSG byte-identical in both.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        make_parts_intel_tool,
        _RECALL_DISCLOSURE_MSG,
    )

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    # Verify the constant itself contains "authoritative" (mutation gate)
    assert "authoritative" in _RECALL_DISCLOSURE_MSG, (
        "_RECALL_DISCLOSURE_MSG must contain the word 'authoritative'"
    )

    session = _fake_session()
    config = _fake_config()

    # Scenario (a): Retrieve returns a recall hit
    mock_client_hit = MagicMock()
    mock_client_hit.retrieve.return_value = _retrieve_response(
        "NHTSA recall 24V-234: Brake caliper seizure affecting Transit MY2021-2023."
    )

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_hit
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result_hit = tool_fn(dtc="P0420", vehicle_make="Ford", vehicle_year="2022")

    assert result_hit["recall_check"]["disclosure"] == _RECALL_DISCLOSURE_MSG, (
        "recall_check.disclosure must be exactly _RECALL_DISCLOSURE_MSG (full string equality)"
    )

    # Scenario (b): Retrieve returns empty results
    mock_client_empty = MagicMock()
    mock_client_empty.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_empty
        tool_fn2 = make_parts_intel_tool(session=session, config=config)
        result_empty = tool_fn2(dtc="P9999", vehicle_make="Ford")

    assert result_empty["recall_check"]["disclosure"] == _RECALL_DISCLOSURE_MSG, (
        "recall_check.disclosure must be exactly _RECALL_DISCLOSURE_MSG even with no hits"
    )


# ---------------------------------------------------------------------------
# Test 4: recall_check never asserts absence of a recall
# ---------------------------------------------------------------------------


def test_recall_check_never_asserts_absence(monkeypatch):
    """When Retrieve returns empty, recall_check never emits absence-assertion language.

    Spec § D-3: 'The v1 tool never asserts the absence of a recall.'
    Strings checked: 'no active recall', 'no recall found', 'not subject to any recall',
    'all clear', 'no recall'.

    Arrange: mock Retrieve → empty retrievalResults.
    Act:     call parts_intel_orchestrate.
    Assert:  found is False; repr of the full recall_check sub-dict contains none of
             the forbidden absence-assertion strings (case-insensitive).
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    session = _fake_session()
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result = tool_fn(dtc="P9999", vehicle_make="BrandX", vehicle_year="2025")

    recall = result["recall_check"]
    assert recall["found"] is False

    recall_repr = repr(recall).lower()
    forbidden_phrases = [
        "no active recall",
        "no recall found",
        "not subject to any recall",
        "all clear",
        "no recall",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in recall_repr, (
            f"recall_check must not assert absence; found forbidden phrase: {phrase!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: warranty_summary verdict is None (seam-preservation test)
# ---------------------------------------------------------------------------


def test_warranty_summary_verdict_is_none(monkeypatch):
    """warranty_summary.verdict is always None; verdict_reason == _COVERAGE_NOT_AUTOMATED_MSG.

    This is the deterministic-seam narration-preservation test per spec § D-4.
    The constant must contain 'service advisor can confirm'.
    Full-string equality — no 'in' or fuzzy match.
    Warranty Retrieve uses source_category=warranty_policy (NOT service_policy).

    Arrange: mock Retrieve returns warranty content.
    Act:     call parts_intel_orchestrate.
    Assert:  warranty_summary.verdict is None; verdict_reason == _COVERAGE_NOT_AUTOMATED_MSG.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        make_parts_intel_tool,
        _COVERAGE_NOT_AUTOMATED_MSG,
    )

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    # Verify the constant itself contains "service advisor can confirm" (mutation gate)
    assert "service advisor can confirm" in _COVERAGE_NOT_AUTOMATED_MSG, (
        "_COVERAGE_NOT_AUTOMATED_MSG must contain 'service advisor can confirm'"
    )

    session = _fake_session()
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "Warranty policy: Powertrain coverage 5 years / 60,000 miles. "
        "Brakes and rotors excluded from bumper-to-bumper after 12 months."
    )

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result = tool_fn(
            dtc="P0420", vehicle_make="Ford", vehicle_model="Transit", mileage=45000
        )

    warranty = result["warranty_summary"]
    assert warranty["verdict"] is None, (
        "warranty_summary.verdict must be None in v1 — coverage adjudication deferred to v1.1"
    )
    assert warranty["verdict_reason"] == _COVERAGE_NOT_AUTOMATED_MSG, (
        "warranty_summary.verdict_reason must be exactly _COVERAGE_NOT_AUTOMATED_MSG "
        "(full string equality)"
    )

    # Confirm warranty Retrieve used warranty_policy filter (NOT service_policy)
    calls = mock_boto3.client.return_value.retrieve.call_args_list
    warranty_call = next(
        (c for c in calls
         if c[1].get("retrievalConfiguration", {})
             .get("vectorSearchConfiguration", {})
             .get("filter", {})
             .get("equals", {})
             .get("value") == "warranty_policy"),
        None,
    )
    assert warranty_call is not None, (
        "expected a Retrieve call with source_category=warranty_policy (NOT service_policy)"
    )


# ---------------------------------------------------------------------------
# Test 6: dispatch skipped without fleet-operator
# ---------------------------------------------------------------------------


def test_dispatch_skipped_without_fleet_operator():
    """When caller lacks fleet-operator, dispatch step is skipped; no HTTPS call made.

    Arrange: session with groups=["fleet-viewer"] (no fleet-operator); mock requests.post.
    Act:     call parts_intel_orchestrate with a dispatch request.
    Assert:  dispatched is False; requests.post never called.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    session = _fake_session(groups=["fleet-viewer"])  # no fleet-operator
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        with patch("agents.supervisor.tools.parts_intel.requests") as mock_requests:
            tool_fn = make_parts_intel_tool(session=session, config=config)
            result = tool_fn(
                dtc="P0420",
                vehicle_make="Ford",
                vin="1FMCU0J91KUB12345",
                dealer_id="meridian-of-denver",
                dispatch_requested=True,
            )

    dispatch = result["dispatch"]
    assert dispatch["dispatched"] is False
    assert (
        "service advisor" in dispatch["reason"].lower()
        or "fleet-operator" in dispatch["reason"].lower()
    ), "reason should mention fleet-operator requirement or service advisor"
    mock_requests.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: dispatch forwards JWT in Authorization header
# ---------------------------------------------------------------------------


def test_dispatch_forwards_jwt(monkeypatch):
    """With fleet-operator group, POST body matches D-5 contract; JWT forwarded.

    Arrange: session with fleet-operator; mock requests.post to return 201.
    Act:     call parts_intel_orchestrate with dispatch_requested=True.
    Assert:  POST was called with Authorization: Bearer <jwt>; body contains
             required fields from spec § D-5.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    jwt_token = "eyJhbGciOiJSUzI1NiJ9.test.signature"
    session = _fake_session(groups=["fleet-operator", "fleet-viewer"], jwt=jwt_token)
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = _dispatch_201_body()

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        with patch("agents.supervisor.tools.parts_intel.requests") as mock_requests:
            mock_requests.post.return_value = mock_response
            tool_fn = make_parts_intel_tool(session=session, config=config)
            result = tool_fn(
                dtc="P0420",
                vehicle_make="Ford",
                vin="1FMCU0J91KUB12345",
                dealer_id="meridian-of-denver",
                description="Brake inspection",
                recall_id="recall-24V-234",
                dispatch_requested=True,
            )

    mock_requests.post.assert_called_once()
    call_args = mock_requests.post.call_args

    # Check Authorization header (may be positional or keyword)
    headers = (
        call_args.kwargs.get("headers")
        or (call_args.args[1] if len(call_args.args) > 1 else {})
        or {}
    )
    assert headers.get("Authorization") == f"Bearer {jwt_token}", (
        "dispatch must forward caller JWT as Bearer token"
    )

    # Check required body fields per spec § D-5
    body = call_args.kwargs.get("json") or {}
    assert body.get("vehicle_vin") == "1FMCU0J91KUB12345"
    assert body.get("dealer_id") == "meridian-of-denver"


# ---------------------------------------------------------------------------
# Test 8: dispatch surfaces 201 endpoint body verbatim (all 11 fields)
# ---------------------------------------------------------------------------


def test_dispatch_surfaces_endpoint_body_verbatim(monkeypatch):
    """Mock 201 body — all ELEVEN fields including dealer_name — surfaces in dispatch.ro.

    The mocked body MUST carry all ELEVEN fields (spec § D-5, review Cycle 1 Warning 2).
    A ten-field mock would pass while failing to exercise dealer_name, which is
    appended by _project_narrow on every successful 201.

    Arrange: mock requests.post → 201 with _dispatch_201_body() (11 fields).
    Act:     call parts_intel_orchestrate with fleet-operator and dispatch_requested.
    Assert:  dispatch.dispatched is True; dispatch.ro matches mocked body key-for-key.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    session = _fake_session(groups=["fleet-operator"])
    config = _fake_config()
    expected_body = _dispatch_201_body()

    # Assert the fixture carries all eleven expected fields
    expected_fields = {
        "ro_id", "vehicle_vin", "dealer_id", "dealer_name", "status",
        "opened_at", "created_at", "updated_at", "description", "initiated_by", "recall_id",
    }
    assert expected_fields == set(expected_body.keys()), (
        f"_dispatch_201_body fixture must carry exactly 11 fields; "
        f"got {set(expected_body.keys())}"
    )

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = expected_body

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        with patch("agents.supervisor.tools.parts_intel.requests") as mock_requests:
            mock_requests.post.return_value = mock_response
            tool_fn = make_parts_intel_tool(session=session, config=config)
            result = tool_fn(
                dtc="P0420",
                vehicle_make="Ford",
                vin="1FMCU0J91KUB12345",
                dealer_id="meridian-of-denver",
                dispatch_requested=True,
            )

    dispatch = result["dispatch"]
    assert dispatch["dispatched"] is True
    # Every key-value in the mocked body must appear in dispatch.ro verbatim
    for key, value in expected_body.items():
        assert dispatch["ro"][key] == value, (
            f"dispatch.ro[{key!r}] must match mocked 201 body verbatim"
        )

    # Explicitly assert dealer_name is present (the 11th field)
    assert dispatch["ro"]["dealer_name"] == "Test Rooftop North", (
        "dealer_name must be present in dispatch.ro — it is the 11th field per spec § D-5"
    )


# ---------------------------------------------------------------------------
# Test 9: dispatch surfaces 400 verbatim
# ---------------------------------------------------------------------------


def test_dispatch_surfaces_400_verbatim(monkeypatch):
    """Mock 400 'dealer_id not in roster' flows through as dispatched:false + error body.

    Arrange: mock requests.post → 400 with roster error body.
    Act:     call parts_intel_orchestrate with fleet-operator and invalid dealer_id.
    Assert:  dispatched is False; error body from endpoint flows through unchanged.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    session = _fake_session(groups=["fleet-operator"])
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    error_body = {"error": "dealer_id is not a valid DMS rooftop", "code": "INVALID_DEALER"}
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = error_body

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        with patch("agents.supervisor.tools.parts_intel.requests") as mock_requests:
            mock_requests.post.return_value = mock_response
            tool_fn = make_parts_intel_tool(session=session, config=config)
            result = tool_fn(
                dtc="P0420",
                vehicle_make="Ford",
                vin="1FMCU0J91KUB12345",
                dealer_id="not-a-real-dealer",
                dispatch_requested=True,
            )

    dispatch = result["dispatch"]
    assert dispatch["dispatched"] is False
    # Error body from endpoint should flow through, not be swallowed
    assert dispatch.get("error_body") == error_body or (
        "dealer_id is not a valid" in str(dispatch)
    ), "400 error body must be surfaced verbatim, not swallowed"


# ---------------------------------------------------------------------------
# Test 10: parts_intel not registered on bidi catalog (voice-scope guard)
# ---------------------------------------------------------------------------


def test_tool_not_registered_on_bidi_catalog():
    """parts_intel must NOT be in bidi_app_canonical's tool list.

    Voice out of scope per spec Constraint 6: the fitment-narrative step
    can approach the ~1s Nova Sonic ceiling and MUST NOT be registered on
    the bidi persona catalog.

    Arrange: read bidi_app_canonical.py source; parse with ast.
    Act:     walk AST for Name nodes containing 'parts_intel'.
    Assert:  no such node exists; string 'parts_intel' absent from source.
    """
    # RED — implemented in T3.1 (this test does NOT import parts_intel itself)
    import ast
    import pathlib

    bidi_path = pathlib.Path(__file__).parent.parent / "bidi_app_canonical.py"
    source = bidi_path.read_text()

    # AST-level guard: check for Name nodes containing 'parts_intel'
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and "parts_intel" in node.id:
            pytest.fail(
                f"bidi_app_canonical.py contains a reference to {node.id!r} at line "
                f"{node.lineno} — parts_intel must NOT be registered on the bidi catalog "
                f"(spec Constraint 6: voice latency guard)"
            )

    # String-search guard (catches string references AST misses)
    assert "parts_intel" not in source, (
        "bidi_app_canonical.py must not contain 'parts_intel' — "
        "voice registration is forbidden per spec Constraint 6"
    )


# ---------------------------------------------------------------------------
# Test 11: source_category filter gated by VSA_KB_SOURCE_CATEGORY_FILTER
# ---------------------------------------------------------------------------


def test_source_category_filter_gated(monkeypatch):
    """With gate OFF, no source_category filter is sent; with gate ON, filter is present.

    Mirrors parts_lookup's existing behavior via kb_filter.py's env-var gate.

    Arrange: two runs — (a) gate OFF (default), (b) gate ON.
    Act:     call parts_intel_orchestrate in both.
    Assert:  (a) no 'filter' key in Retrieve vsc; (b) 'filter' key present with
             source_category value for the first step.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    session = _fake_session()
    config = _fake_config()

    # --- Gate OFF ---
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "false")

    mock_client_off = MagicMock()
    mock_client_off.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_off
        tool_fn = make_parts_intel_tool(session=session, config=config)
        tool_fn(dtc="P0420", vehicle_make="Ford")

    for call in mock_boto3.client.return_value.retrieve.call_args_list:
        vsc = call[1].get("retrievalConfiguration", {}).get("vectorSearchConfiguration", {})
        assert "filter" not in vsc, (
            "With VSA_KB_SOURCE_CATEGORY_FILTER=false, no filter should be sent"
        )

    # --- Gate ON ---
    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    mock_client_on = MagicMock()
    mock_client_on.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_on
        tool_fn2 = make_parts_intel_tool(session=session, config=config)
        tool_fn2(dtc="P0420", vehicle_make="Ford")

    filter_found = any(
        "filter" in call[1].get("retrievalConfiguration", {}).get("vectorSearchConfiguration", {})
        for call in mock_boto3.client.return_value.retrieve.call_args_list
    )
    assert filter_found, (
        "With VSA_KB_SOURCE_CATEGORY_FILTER=true, at least one Retrieve call must carry filter"
    )


# ---------------------------------------------------------------------------
# Test 12: recall and warranty authoritative flags — mutation gate
# ---------------------------------------------------------------------------


def test_recall_and_warranty_authoritative_flags(monkeypatch):
    """recall_check.authoritative is ALWAYS False; warranty_summary.verdict is ALWAYS None.

    Mutation gate (spec § "Test strategy" item 12): a code change that flips
    recall_check.authoritative to True or warranty_summary.verdict to non-None
    MUST fail this test.

    This test is structured so that:
    - assert recall["authoritative"] is False  (not == False — catches bool subclass bugs)
    - assert warranty["verdict"] is None       (not == None — catches any truthy override)

    Arrange: two Retrieve responses — one with results, one empty.
    Act:     call parts_intel_orchestrate in both.
    Assert:  authoritative is False and verdict is None in all four combinations.
    """
    # RED — implemented in T3.1
    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    session = _fake_session()
    config = _fake_config()

    scenarios = [
        _retrieve_response("NHTSA recall 24V-112: Seat-belt fraying MY2020."),
        {"retrievalResults": []},
    ]

    for mock_response_data in scenarios:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = mock_response_data

        with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_client
            tool_fn = make_parts_intel_tool(session=session, config=config)
            result = tool_fn(dtc="P0420", vehicle_make="Ford", vehicle_year="2021")

        recall = result["recall_check"]
        warranty = result["warranty_summary"]

        # Mutation gate: these must be the exact boolean/None values, not just falsy
        assert recall["authoritative"] is False, (
            f"recall_check.authoritative MUST be False (not just falsy) — "
            f"got {recall['authoritative']!r}"
        )
        assert warranty["verdict"] is None, (
            f"warranty_summary.verdict MUST be None (not just falsy) — "
            f"got {warranty['verdict']!r}"
        )



# ---------------------------------------------------------------------------
# Tests 13–17: T4.4 narration seam guards (2026-09-11).
#
# Added after T4.4 caught a defect the 12 tests above could not: `_recall_check`'s
# tool contract held, but the LLM's narration of the (correct) return value
# emitted "✅ No matching active recall was found" (absence assertion + all-clear
# glyph) and "none of the returned recalls apply to this vehicle's profile"
# (applicability adjudication the corpus cannot support).
#
# Fix implements options 1 + 2 from
# issues/2026-09-11-parts-intel-narration-asserts-recall-absence/report.md —
# both move forbidden phrasing INSIDE the test-pinned tool boundary rather
# than leaving it to the persona prompt. The doctrine is explicit that a
# prompt-only boundary will be crossed.
#
# These tests are the mutation gate on the two new constants and the
# per-path narration/applicability fields. They mirror the review Cycle 3
# pattern: assert byte-identical constant equality (not `in`, not fuzzy).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 13: recall_check narration is verbatim on the no-hit path
# ---------------------------------------------------------------------------


def test_recall_check_narration_is_verbatim_on_no_hit(monkeypatch):
    """recall_check.narration is byte-identical to _RECALL_NO_HIT_NARRATION on empty results,
    and the constant itself carries the properties T4.4 requires.

    Mutation gate:
      - byte-identical check catches divergence between the tool's return path
        and the constant (e.g., inlining a hardcoded string).
      - literal-string checks catch softening of the constant itself; per review
        Cycle 3's pattern, a byte-identical assertion alone would pass under any
        mutation to the constant because the test imports it symbolically.

    Arrange: mock Retrieve → empty retrievalResults.
    Act:     call parts_intel_orchestrate.
    Assert:  recall_check.narration == _RECALL_NO_HIT_NARRATION exactly, AND
             the constant contains "cannot be established" and "NHTSA".
    """
    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        _RECALL_NO_HIT_NARRATION,
        make_parts_intel_tool,
    )

    # Literal-string property gates on the constant itself. A mutation that
    # softens the text (e.g. drops the NHTSA pointer or the "cannot be
    # established" clause) MUST fail one of these. Byte-identical assertions
    # against an imported constant cannot catch that class of mutation on
    # their own.
    assert "cannot be established" in _RECALL_NO_HIT_NARRATION, (
        "_RECALL_NO_HIT_NARRATION must state that recall status cannot be "
        "established from this corpus — spec § D-3 forbids the tool asserting "
        "absence, so the narration must state the epistemic limit explicitly"
    )
    assert "NHTSA" in _RECALL_NO_HIT_NARRATION, (
        "_RECALL_NO_HIT_NARRATION must point the reader at NHTSA — the "
        "authoritative source the tool is not"
    )

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    session = _fake_session()
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result = tool_fn(dtc="P9999", vehicle_make="BrandX", vehicle_year="2025")

    recall = result["recall_check"]
    assert recall["found"] is False
    assert "narration" in recall, (
        "recall_check must carry a narration field on the no-hit path — this is "
        "the verbatim text the LLM MUST surface"
    )
    assert recall["narration"] == _RECALL_NO_HIT_NARRATION, (
        "recall_check.narration must be exactly _RECALL_NO_HIT_NARRATION "
        "(full string equality — catches divergence between the tool return "
        "path and the constant, e.g. an inlined hardcoded string)"
    )


# ---------------------------------------------------------------------------
# Test 14: recall_check narration is verbatim on KB-unavailable and exception paths
# ---------------------------------------------------------------------------


def test_recall_check_narration_is_verbatim_on_kb_unavailable_and_exception():
    """recall_check.narration is byte-identical to _RECALL_NO_HIT_NARRATION on both
    the KB-unavailable fast path (adp_kb_id empty) and the Retrieve-exception path.

    Both paths must emit the same verbatim narration — otherwise an LLM planner
    can rationalise a different rendering for each and re-introduce absence
    claims through the back door.

    Arrange: two scenarios — (a) empty kb_id, (b) Retrieve raises ClientError.
    Act:     call parts_intel_orchestrate in both.
    Assert:  recall_check.narration == _RECALL_NO_HIT_NARRATION in both.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        _RECALL_NO_HIT_NARRATION,
        make_parts_intel_tool,
    )

    session = _fake_session()

    # Scenario (a): KB unavailable (adp_kb_id empty) — pre-Retrieve fast path
    config_no_kb = _fake_config(kb_id="")
    tool_fn = make_parts_intel_tool(session=session, config=config_no_kb)
    result_no_kb = tool_fn(dtc="P0420", vehicle_make="Ford")

    recall_no_kb = result_no_kb["recall_check"]
    assert recall_no_kb["found"] is False
    assert recall_no_kb.get("narration") == _RECALL_NO_HIT_NARRATION, (
        "KB-unavailable path must emit _RECALL_NO_HIT_NARRATION verbatim"
    )

    # Scenario (b): Retrieve raises — exception path in _recall_check
    config = _fake_config()
    mock_client = MagicMock()
    mock_client.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Retrieve",
    )

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn2 = make_parts_intel_tool(session=session, config=config)
        result_exc = tool_fn2(dtc="P0420", vehicle_make="Ford")

    recall_exc = result_exc["recall_check"]
    assert recall_exc["found"] is False
    assert recall_exc.get("narration") == _RECALL_NO_HIT_NARRATION, (
        "Retrieve-exception path must emit _RECALL_NO_HIT_NARRATION verbatim"
    )


# ---------------------------------------------------------------------------
# Test 15: recall_check emits applicability=undetermined + verbatim reason on hits
# ---------------------------------------------------------------------------


def test_recall_check_applicability_undetermined_on_hits(monkeypatch):
    """On the hits path, recall_check emits `applicability: "undetermined"` and
    `applicability_reason` byte-identical to _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG.

    This is the harder half of the T4.4 defect: the LLM had adjudicated
    applicability ("none of the returned recalls apply to this vehicle's
    profile") from a corpus with no VIN-range join. The fix encodes the tool's
    inability to decide into a data field the narration must carry.

    Mutation gate:
    - `applicability` MUST be the string "undetermined" (not None, not the
      absence of the field, not a truthy override)
    - `applicability_reason` MUST equal _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG
      byte-identical

    Arrange: mock Retrieve → returns a hit.
    Act:     call parts_intel_orchestrate.
    Assert:  applicability and applicability_reason present + correct.
    """
    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG,
        make_parts_intel_tool,
    )

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")

    # The pinned constant must contain "cannot be determined" — otherwise the
    # data field's text does not carry the property (like the disclosure
    # constant's "authoritative" pin).
    assert "cannot be determined" in _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG, (
        "_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG must contain "
        "'cannot be determined'"
    )
    # Second property assertion: the constant must name the specific reason
    # (no VIN-range or make/model/year join). A mutation that drops the reason
    # while keeping the "cannot be determined" phrase would still pass the
    # first check; this one catches that.
    assert "VIN-range" in _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG, (
        "_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG must name the specific "
        "corpus limitation (no VIN-range join) — otherwise the applicability "
        "verdict looks like an opinion rather than a data-limitation"
    )
    assert "NHTSA" in _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG, (
        "_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG must point the reader at "
        "NHTSA as the authoritative applicability check"
    )

    session = _fake_session()
    config = _fake_config()

    mock_client = MagicMock()
    mock_client.retrieve.return_value = _retrieve_response(
        "NHTSA recall 24V-234: Brake caliper seizure affecting Transit MY2021-2023.",
        "NHTSA recall 24V-456: Airbag inflator degradation on select light-truck platforms.",
    )

    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result = tool_fn(dtc="P0420", vehicle_make="Ford", vehicle_year="2022")

    recall = result["recall_check"]
    assert recall["found"] is True
    assert recall.get("applicability") == "undetermined", (
        "recall_check.applicability MUST be exactly 'undetermined' on the hits "
        "path — the corpus has no VIN-range join, so applicability cannot be "
        "decided; this field encodes that determination into data the "
        "narration must carry"
    )
    assert (
        recall.get("applicability_reason") == _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG
    ), (
        "recall_check.applicability_reason must be exactly "
        "_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG (full string equality)"
    )


# ---------------------------------------------------------------------------
# Test 16: narration constants themselves forbid absence phrases and all-clear glyphs
# ---------------------------------------------------------------------------


def test_recall_narration_constants_forbid_absence_phrases_and_glyphs():
    """Both narration constants and the disclosure are free of forbidden text
    and all-clear glyphs.

    This test is the mutation gate on the constants themselves. It caught the
    exact defect T4.4 found: the LLM's rendered response asserted "no matching
    active recall was found" with a ✅ prefix. A tool-side text that emits the
    phrase or the glyph would surface it verbatim through the LLM, so both
    are forbidden here at the source.

    A load-time assertion in parts_intel.py also enforces this — the test
    catches a mistake in CI; the module-load guard catches it at deploy-time.
    Both are cheap and both are load-bearing (steering doctrine: mutation
    testing at the green boundary).
    """
    from agents.supervisor.tools.parts_intel import (  # noqa: PLC0415
        _RECALL_DISCLOSURE_MSG,
        _RECALL_FORBIDDEN_GLYPHS,
        _RECALL_FORBIDDEN_PHRASES,
        _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG,
        _RECALL_NO_HIT_NARRATION,
    )

    # Sanity: the denylists themselves must not be empty (guard against a
    # future edit that trivially widens the guard to nothing).
    assert len(_RECALL_FORBIDDEN_PHRASES) >= 5, (
        "forbidden-phrase list must not be trivially widened to <5 entries — "
        "T4.4 depends on this set"
    )
    assert "\u2705" in _RECALL_FORBIDDEN_GLYPHS, (
        "the ✅ glyph MUST be in the forbidden-glyph set — it was the "
        "all-clear signal T4.4 caught"
    )

    for name, text in [
        ("_RECALL_DISCLOSURE_MSG", _RECALL_DISCLOSURE_MSG),
        ("_RECALL_NO_HIT_NARRATION", _RECALL_NO_HIT_NARRATION),
        (
            "_RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG",
            _RECALL_HITS_APPLICABILITY_UNDETERMINED_MSG,
        ),
    ]:
        lower = text.lower()
        for phrase in _RECALL_FORBIDDEN_PHRASES:
            assert phrase not in lower, (
                f"{name} contains forbidden absence-assertion phrase "
                f"{phrase!r} — spec § D-3 forbids this"
            )
        for glyph in _RECALL_FORBIDDEN_GLYPHS:
            assert glyph not in text, (
                f"{name} contains forbidden all-clear glyph U+{ord(glyph):04X} "
                "— T4.4 defect"
            )


# ---------------------------------------------------------------------------
# Test 17: applicability field is absent on non-hit paths (structural guard)
# ---------------------------------------------------------------------------


def test_recall_check_applicability_field_absent_on_non_hit_paths(monkeypatch):
    """`applicability` and `applicability_reason` are present ONLY on the hits
    path — they are the tool's answer to "does this recall apply?" and they
    have no meaning on empty/exception/kb-unavailable paths.

    A response where `applicability` bleeds onto a no-hit path would let the
    LLM narrate an applicability determination on a query that returned
    nothing to apply — a distinct failure mode from the T4.4 defect but the
    same category. Pin the shape.

    Arrange: three scenarios — empty results, KB unavailable, Retrieve
             exception.
    Act:     call parts_intel_orchestrate in each.
    Assert:  applicability/applicability_reason are NOT present in any of them.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    from agents.supervisor.tools.parts_intel import make_parts_intel_tool  # noqa: PLC0415

    monkeypatch.setenv("VSA_KB_SOURCE_CATEGORY_FILTER", "true")
    session = _fake_session()

    # (a) empty retrievalResults
    config = _fake_config()
    mock_client_empty = MagicMock()
    mock_client_empty.retrieve.return_value = {"retrievalResults": []}
    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_empty
        tool_fn = make_parts_intel_tool(session=session, config=config)
        result_empty = tool_fn(dtc="P9999")

    # (b) KB unavailable
    config_no_kb = _fake_config(kb_id="")
    tool_fn2 = make_parts_intel_tool(session=session, config=config_no_kb)
    result_no_kb = tool_fn2(dtc="P9999")

    # (c) Retrieve exception
    mock_client_exc = MagicMock()
    mock_client_exc.retrieve.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "boom"}},
        "Retrieve",
    )
    with patch("agents.supervisor.tools.parts_intel.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client_exc
        tool_fn3 = make_parts_intel_tool(session=session, config=config)
        result_exc = tool_fn3(dtc="P9999")

    for label, result in [
        ("empty-results", result_empty),
        ("kb-unavailable", result_no_kb),
        ("exception", result_exc),
    ]:
        recall = result["recall_check"]
        assert "applicability" not in recall, (
            f"{label}: `applicability` must not appear on non-hit paths "
            f"(present in the response, which would let the LLM narrate an "
            f"applicability determination on a query that returned nothing)"
        )
        assert "applicability_reason" not in recall, (
            f"{label}: `applicability_reason` must not appear on non-hit paths"
        )
