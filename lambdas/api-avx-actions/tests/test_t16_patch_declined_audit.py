"""
T1.6 red tests — PATCH declined audit entry, note cap, iOS-excluded extension.

RED phase: all tests in TestPatchDeclinedAudit and TestNoteCap and
TestIOSExcludedExtension FAIL before the fix is applied.

Run:
    python3 -m pytest lambdas/api-avx-actions/tests/test_t16_patch_declined_audit.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDA_ROOT = os.path.dirname(_TESTS_DIR)       # lambdas/api-avx-actions
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)    # lambdas
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)      # repo root
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402

import jsonschema as _jsonschema  # noqa: E402

# ---------------------------------------------------------------------------
# Schema loaded once
# ---------------------------------------------------------------------------
_ACTION_SCHEMA_PATH = (
    pathlib.Path(_REPO_ROOT) / "agents" / "tier2" / "schemas" / "action.schema.json"
)
_ACTION_SCHEMA: dict = json.loads(_ACTION_SCHEMA_PATH.read_text())

_OPENAPI_PATH = (
    pathlib.Path(_REPO_ROOT) / "docs" / "api" / "avx-findings-actions.openapi.yaml"
)

# D13 list: operationIds whose handler applies the FI exclusion filter.
# Derived from exclusion call sites in both handler files (F1.1 W4 + G2.3):
#   api-avx-findings/handler.py:
#     ~1897  listFindingsForCaller     /findings/me — _is_fi_finding() in _admit()
#     ~2302  listFindingsByVehicle     /findings/vehicle/{vin} owner branch
#     ~2415  listFindingsByOwner       /findings/owner/{owner_id}
#     ~2440  listFindingsByOwnerFleet  /findings/owner-fleet/{owner_id}
#   api-avx-actions/handler.py:
#     ~834   listActionsByVehicle      /actions/vehicle/{vin} owner branch (G2.3 sec S3)
#     ~917   listActionsByOwner        /actions/owner/{owner_id}
#     ~946   listActionsByOwnerFleet   /actions/owner-fleet/{owner_id}
_D13_IOS_EXCLUDED_OPERATION_IDS: frozenset[str] = frozenset({
    "listFindingsForCaller",
    "listFindingsByVehicle",
    "listFindingsByOwner",
    "listFindingsByOwnerFleet",
    "listActionsByVehicle",
    "listActionsByOwner",
    "listActionsByOwnerFleet",
})

# Handler note cap constant (T1.6 spec: 2000 characters, no pre-existing cap).
_EXPECTED_NOTE_MAX_LEN = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claims(sub="user-sub-123", groups=None):
    return {
        "sub": sub,
        "cognito:groups": (
            json.dumps(groups) if isinstance(groups, list) else (groups or "")
        ),
    }


def _patch_event(action_id: str, body: dict, claims_dict: dict | None = None) -> dict:
    return {
        "httpMethod": "PATCH",
        "resource": "/actions/{action_id}",
        "pathParameters": {"action_id": action_id},
        "queryStringParameters": {},
        "requestContext": {
            "authorizer": {"claims": claims_dict or _claims()}
        },
        "body": json.dumps(body),
    }


def _sample_action(
    status: str = "in_progress",
    target_system: str = "dms",
    created_by: str = "owner-user-sub-123",
) -> dict:
    return {
        "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
        "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
        "created_by": created_by,
        "created_at": "2026-10-26T12:00:00Z",
        "updated_at": "2026-10-26T12:00:00Z",
        "target_system": target_system,
        "status": status,
        "action_kind": "book_combined_visit",
        "parameters": {},
        "evidence": [],
        "audit_trail": [],
    }


def _mock_update_and_get_audit_entry(
    from_status: str = "in_progress",
    to_status: str = "declined",
    target_system: str = "dms",
    actor: str = "owner-user-sub-123",
    **extra_kwargs,
) -> dict:
    """Call _update_action_status with a mocked DDB table and extract the audit entry."""
    import botocore.exceptions

    mock_table = MagicMock()
    mock_table.update_item.return_value = {"Attributes": {
        "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
        "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
        "created_by": actor,
        "created_at": "2026-10-26T12:00:00Z",
        "updated_at": "2026-10-26T12:01:00Z",
        "target_system": target_system,
        "status": to_status,
        "action_kind": "book_combined_visit",
        "parameters": {},
        "evidence": [],
        "audit_trail": [],
    }}
    mock_table.meta.client.exceptions.ConditionalCheckFailedException = type(
        "ConditionalCheckFailedException",
        (botocore.exceptions.ClientError,),
        {},
    )

    with patch.object(h, "_get_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value = mock_table
        h._update_action_status(
            action_id="AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            **extra_kwargs,
        )

    call_kwargs = mock_table.update_item.call_args.kwargs
    entries = call_kwargs["ExpressionAttributeValues"][":entry"]
    assert len(entries) == 1, f"Expected 1 audit entry, got {len(entries)}"
    return entries[0]


def _handler_patch_declined_and_get_stored_audit_entry(
    target_system: str = "dms",
) -> dict:
    """Call handler PATCH to declined via mocked DDB and extract the stored audit entry.

    Uses the same mock pattern as _mock_update_and_get_audit_entry but goes
    through the full handler so failure_kind/failure_reason are built from the action.
    """
    import botocore.exceptions

    action = _sample_action(status="in_progress", target_system=target_system)
    mock_table = MagicMock()

    stored_entry: dict = {}

    def fake_update_item(**kwargs):
        entries = kwargs["ExpressionAttributeValues"][":entry"]
        stored_entry.update(entries[0])
        return {"Attributes": {**action, "status": "declined", "audit_trail": [entries[0]]}}

    mock_table.update_item.side_effect = fake_update_item
    mock_table.get_item.return_value = {"Item": action}
    mock_table.meta.client.exceptions.ConditionalCheckFailedException = type(
        "ConditionalCheckFailedException",
        (botocore.exceptions.ClientError,),
        {},
    )

    with patch.object(h, "_get_ddb") as mock_ddb, \
         patch.object(h, "_ddb", None):
        mock_ddb.return_value.Table.return_value = mock_table
        resp = h.handler(
            _patch_event("AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                         {"status": "declined"}),
            None,
        )

    assert resp["statusCode"] == 200, (
        f"Expected 200, got {resp['statusCode']}: {resp['body']}"
    )
    return stored_entry


# ---------------------------------------------------------------------------
# TestPatchDeclinedAudit
# ---------------------------------------------------------------------------

class TestPatchDeclinedAudit:
    """
    RED: all tests FAIL before T1.6 fix (handler does not write failure_kind or
    failure_reason to the audit entry for PATCH → declined transitions).

    GREEN: all tests PASS after _patch_action builds failure_kind="target_refused"
    and failure_reason from failure_reason.py, and passes them to
    _update_action_status.
    """

    def test_declined_audit_entry_has_failure_kind_target_refused(self):
        """Audit entry for a declined transition must carry failure_kind=target_refused.

        RED: _update_action_status builds no failure_kind → entry has no failure_kind key.
        GREEN: handler passes failure_kind="target_refused" from _patch_action.

        Mutation guard: PATCH decline without failure_kind → this test fails.
        """
        entry = _handler_patch_declined_and_get_stored_audit_entry(target_system="dms")
        assert "failure_kind" in entry, (
            f"Audit entry for 'declined' must have 'failure_kind'. Entry: {entry!r}"
        )
        assert entry["failure_kind"] == "target_refused", (
            f"failure_kind must be 'target_refused', got {entry['failure_kind']!r}"
        )

    def test_declined_audit_entry_has_failure_reason(self):
        """Audit entry for a declined transition must carry failure_reason from vocabulary.

        RED: no failure_reason is written → entry lacks failure_reason.
        GREEN: handler calls failure_reason.failure_reason() with the action's
        target_system and failure_kind="target_refused".
        """
        entry = _handler_patch_declined_and_get_stored_audit_entry(target_system="dms")
        assert "failure_reason" in entry, (
            f"Audit entry for 'declined' must have 'failure_reason'. Entry: {entry!r}"
        )
        assert isinstance(entry["failure_reason"], str) and entry["failure_reason"], (
            f"failure_reason must be a non-empty string; got {entry['failure_reason']!r}"
        )

    def test_declined_audit_entry_validates_against_action_schema(self):
        """The full audit entry for a declined transition must pass action.schema.json.

        RED: schema requires failure_kind=target_refused for to_status=declined
        (via allOf if/then at $defs/audit_entry). Without failure_kind the entry
        fails schema validation.
        GREEN: handler adds both fields; schema validation passes.

        Mutation guard: PATCH decline without failure_kind → schema validation
        raises ValidationError.
        """
        entry = _handler_patch_declined_and_get_stored_audit_entry(target_system="dms")
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        try:
            _jsonschema.validate(entry, audit_entry_schema)
        except _jsonschema.ValidationError as exc:
            raise AssertionError(
                f"declined audit entry fails schema validation: {exc.message}\n"
                f"  entry = {entry!r}"
            ) from exc

    def test_declined_audit_entry_failure_reason_matches_pattern(self):
        """failure_reason must match the schema's fixed-vocabulary pattern.

        Verifies the value built by failure_reason.py actually matches the
        pattern in action.schema.json (not just that a string is present).
        """
        entry = _handler_patch_declined_and_get_stored_audit_entry(target_system="dms")
        # Run schema validation which enforces the pattern
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        _jsonschema.validate(entry, audit_entry_schema)  # raises if pattern mismatch

    def test_declined_audit_entry_for_cms_target(self):
        """CMS declined audit entry also carries target_refused and failure_reason.

        CMS target with no error_status → 'CMS cannot carry out this action yet.'
        (failure_reason.py row 11: Stage A CmsExecutor special case).
        """
        entry = _handler_patch_declined_and_get_stored_audit_entry(target_system="cms")
        assert entry.get("failure_kind") == "target_refused"
        assert "failure_reason" in entry
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        _jsonschema.validate(entry, audit_entry_schema)


# ---------------------------------------------------------------------------
# TestNoteCap
# ---------------------------------------------------------------------------

class TestNoteCap:
    """
    RED tests: note_over_limit and openapi_maxlength FAIL before fix.
    note_at_limit passes both before and after (already 200 with no cap).
    """

    def test_note_over_limit_returns_400(self):
        """A note longer than _NOTE_MAX_LEN characters → 400 note_too_long.

        RED: handler does not read the note field → returns 200 regardless.
        GREEN: handler reads note, enforces cap, returns 400.

        Mutation guard: cap removed → returns 200 → test fails.
        """
        action = _sample_action(status="in_progress")
        too_long_note = "x" * (_EXPECTED_NOTE_MAX_LEN + 1)
        # Also mock _update_action_status so the test does not reach DDB even
        # when the mutation removes the cap (which would let the handler proceed
        # past the note check and hit _update_action_status).
        updated = {**action, "status": "cancelled"}

        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                _patch_event(
                    "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                    {"status": "cancelled", "note": too_long_note},
                ),
                None,
            )
        assert resp["statusCode"] == 400, (
            f"Expected 400 for note>{_EXPECTED_NOTE_MAX_LEN} chars, got {resp['statusCode']}"
        )
        body = json.loads(resp["body"])
        assert body.get("error") == "note_too_long", (
            f"Expected error='note_too_long', got {body!r}"
        )

    def test_note_non_string_returns_400(self):
        """A non-string note value → 400 note_invalid (F1.1 W3).

        RED: handler checks len(note) but not isinstance(note, str) → 200 (list,
        dict, int all pass the length gate).
        GREEN: handler rejects non-str note with note_invalid before the length check.

        Mutation guard: remove the isinstance check → non-string passes → test fails.
        """
        action = _sample_action(status="in_progress")
        updated = {**action, "status": "cancelled"}

        for bad_note in ([], {"key": "val"}, 42, True):
            with patch.object(h, "_get_action", return_value=action), \
                 patch.object(h, "_update_action_status", return_value=updated):
                resp = h.handler(
                    _patch_event(
                        "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                        {"status": "cancelled", "note": bad_note},
                    ),
                    None,
                )
            assert resp["statusCode"] == 400, (
                f"Expected 400 for non-string note={bad_note!r}, got {resp['statusCode']}"
            )
            body = json.loads(resp["body"])
            assert body.get("error") == "note_invalid", (
                f"Expected error='note_invalid' for note={bad_note!r}, got {body!r}"
            )

    def test_note_is_not_persisted(self):
        """A valid PATCH note is rejected or ignored — never written to the audit trail.

        RED: handler passes `audit_note=note if note else None` → a non-empty string
        note IS stored in the DynamoDB item.
        GREEN: handler passes `audit_note=None` regardless of input note.

        Mutation guard (persist note again): change `audit_note=None` to `audit_note=note`
        → _update_action_status receives a non-None audit_note → test fails.
        """
        action = _sample_action(status="in_progress")
        updated = {**action, "status": "cancelled"}

        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated) as mock_update:
            resp = h.handler(
                _patch_event(
                    "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                    {"status": "cancelled", "note": "a valid non-empty note"},
                ),
                None,
            )
        assert resp["statusCode"] == 200, (
            f"Expected 200 for valid note, got {resp['statusCode']}: {resp['body']}"
        )
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs.get("audit_note") is None, (
            f"note must NOT be persisted in the audit trail; "
            f"got audit_note={call_kwargs.get('audit_note')!r}"
        )

    def test_note_at_limit_is_allowed(self):
        """A note of exactly _NOTE_MAX_LEN characters → 200 (boundary is inclusive).

        Passes both before and after fix (boundary test, not a red test).

        Mutation guard (off-by-one): cap at 1999 → 2000-char note fails →
        this test fails.
        """
        action = _sample_action(status="in_progress")
        exact_note = "x" * _EXPECTED_NOTE_MAX_LEN
        updated = {**action, "status": "cancelled"}

        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                _patch_event(
                    "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                    {"status": "cancelled", "note": exact_note},
                ),
                None,
            )
        assert resp["statusCode"] == 200, (
            f"Expected 200 for note={_EXPECTED_NOTE_MAX_LEN} chars (boundary), "
            f"got {resp['statusCode']}: {resp['body']}"
        )

    def test_openapi_note_maxlength_in_update_request_matches_handler_cap(self):
        """UpdateActionRequest.note.maxLength in the OpenAPI must equal _NOTE_MAX_LEN.

        RED: UpdateActionRequest.note has no maxLength → KeyError / AssertionError.
        GREEN: OpenAPI updated with maxLength: 2000.

        Mutation guard: OpenAPI maxLength set to a different value → test fails.
        """
        doc = yaml.safe_load(_OPENAPI_PATH.read_text())
        schemas = doc["components"]["schemas"]
        update_req = schemas["UpdateActionRequest"]
        note_prop = update_req.get("properties", {}).get("note", {})
        assert "maxLength" in note_prop, (
            "UpdateActionRequest.note must have a maxLength constraint matching the "
            f"handler cap ({_EXPECTED_NOTE_MAX_LEN})."
        )
        assert note_prop["maxLength"] == _EXPECTED_NOTE_MAX_LEN, (
            f"UpdateActionRequest.note.maxLength must be {_EXPECTED_NOTE_MAX_LEN}, "
            f"got {note_prop['maxLength']!r}."
        )

    def test_all_openapi_note_properties_have_same_maxlength(self):
        """Every 'note' property in the OpenAPI must have maxLength == _NOTE_MAX_LEN.

        This enforces 'same number as maxLength in every OpenAPI note property'
        (T1.6 spec).

        RED: at least one note property lacks maxLength (UpdateActionRequest.note,
        FindingAuditEntry.note, ActionAuditEntry.note).
        GREEN: all note properties updated to maxLength: 2000.
        """
        doc = yaml.safe_load(_OPENAPI_PATH.read_text())
        schemas = doc.get("components", {}).get("schemas", {})

        def _collect_note_maxlengths(schema_name: str, schema: dict) -> list[tuple]:
            """Return (schema_name, maxLength or None) for top-level 'note' properties."""
            results = []
            props = schema.get("properties", {})
            if "note" in props:
                ml = props["note"].get("maxLength")
                results.append((schema_name, ml))
            return results

        mismatches = []
        for name, schema in schemas.items():
            for schema_name, ml in _collect_note_maxlengths(name, schema):
                if ml != _EXPECTED_NOTE_MAX_LEN:
                    mismatches.append(
                        f"  {schema_name}.note: maxLength={ml!r} "
                        f"(expected {_EXPECTED_NOTE_MAX_LEN})"
                    )

        assert not mismatches, (
            "These OpenAPI note properties do not match the handler cap "
            f"({_EXPECTED_NOTE_MAX_LEN}):\n" + "\n".join(mismatches)
        )


# ---------------------------------------------------------------------------
# TestIOSExcludedExtension
# ---------------------------------------------------------------------------

class TestIOSExcludedExtension:
    """
    RED: both tests FAIL before the OpenAPI is updated (no x-fi-ios-excluded extension).
    GREEN: both tests PASS after the extension is added to exactly the D13 operations.

    D13 list (spec 2026-10-02-fi-action-queue-stage-a § D13, decisions.md T2.3):
      - listActionsByOwner (GET /actions/owner/{owner_id}): handler applies
        _is_ios_excluded_action; iOS client directly calls this route.
      - listActionsByOwnerFleet (GET /actions/owner-fleet/{owner_id}): handler
        applies same exclusion (security review Cycle 3 authz S2).
    """

    def _collect_ios_excluded_operation_ids(self) -> set[str]:
        """Return operationIds where x-fi-ios-excluded: true in the OpenAPI."""
        doc = yaml.safe_load(_OPENAPI_PATH.read_text())
        marked = set()
        for path_item in doc.get("paths", {}).values():
            for method_obj in path_item.values():
                if not isinstance(method_obj, dict):
                    continue
                if method_obj.get("x-fi-ios-excluded") is True:
                    op_id = method_obj.get("operationId")
                    if op_id:
                        marked.add(op_id)
        return marked

    def test_ios_excluded_operations_match_d13_list(self):
        """x-fi-ios-excluded: true must be on exactly the D13-excluded operations.

        RED: no extension in OpenAPI → marked set is empty → fails.
        GREEN: extension on listActionsByOwner and listActionsByOwnerFleet.

        Mutation guard (extra): add extension to listActionsByFleet → extra in marked →
        test fails.
        Mutation guard (drop): remove extension from listActionsByOwner → missing in
        marked → test fails.
        """
        marked = self._collect_ios_excluded_operation_ids()
        assert marked == _D13_IOS_EXCLUDED_OPERATION_IDS, (
            f"x-fi-ios-excluded set mismatch.\n"
            f"  Expected: {sorted(_D13_IOS_EXCLUDED_OPERATION_IDS)}\n"
            f"  Actual:   {sorted(marked)}\n\n"
            "Extra operations: "
            f"{sorted(marked - _D13_IOS_EXCLUDED_OPERATION_IDS)}\n"
            "Missing operations: "
            f"{sorted(_D13_IOS_EXCLUDED_OPERATION_IDS - marked)}"
        )

    def test_ios_excluded_extension_value_is_true(self):
        """x-fi-ios-excluded must be boolean true, not a string or other value.

        Guards against accidentally writing 'true' (YAML string) instead of true.
        """
        doc = yaml.safe_load(_OPENAPI_PATH.read_text())
        for path_item in doc.get("paths", {}).values():
            for method_obj in path_item.values():
                if not isinstance(method_obj, dict):
                    continue
                if "x-fi-ios-excluded" in method_obj:
                    val = method_obj["x-fi-ios-excluded"]
                    op_id = method_obj.get("operationId", "?")
                    assert val is True, (
                        f"x-fi-ios-excluded on {op_id} must be boolean true, "
                        f"not {val!r}"
                    )

    def test_d13_handler_call_site_count_matches_set(self):
        """Count of D13 exclusion call sites in both handlers must equal the expected set size.

        Catches: adding an exclusion call site to a handler without updating
        _D13_IOS_EXCLUDED_OPERATION_IDS and the OpenAPI mark (F1.1 W4 mutant guard).

        Excluded from the count:
        - Function definition lines (def _is_fi_finding, def _drop_fi_findings, ...)
        - The _drop_fi_findings helper body: 'return [item for item in items if not _is_fi_finding(item)]'
        """
        import pathlib as _pl
        repo = _pl.Path(_REPO_ROOT)
        handler_files = [
            repo / "lambdas" / "api-avx-findings" / "handler.py",
            repo / "lambdas" / "api-avx-actions" / "handler.py",
        ]
        # Patterns that mark an FI exclusion call site in a route handler
        call_patterns = (
            "_is_fi_finding(",
            "_is_ios_excluded_action(",
            "_drop_fi_findings(",
        )
        # Lines to skip — function definitions and the helper body expression
        exclude_fragments = (
            "def _is_fi_finding(",
            "def _drop_fi_findings(",
            "def _is_ios_excluded_action(",
            "return [item for item in items if not _is_fi_finding(item)]",
        )

        count = 0
        found_lines = []
        for hf in handler_files:
            for lineno, line in enumerate(hf.read_text().splitlines(), 1):
                stripped = line.strip()
                if any(p in stripped for p in call_patterns):
                    if not any(ex in stripped for ex in exclude_fragments):
                        count += 1
                        found_lines.append(f"{hf.name}:{lineno}: {stripped[:80]}")

        expected = len(_D13_IOS_EXCLUDED_OPERATION_IDS)
        assert count == expected, (
            f"D13 exclusion call site count ({count}) != expected operationId count ({expected}).\n"
            "Found call sites:\n" + "\n".join(f"  {l}" for l in found_lines) + "\n\n"
            "If a new exclusion call site was added, also update _D13_IOS_EXCLUDED_OPERATION_IDS "
            "and add x-fi-ios-excluded: true to the corresponding operationId in the OpenAPI."
        )
