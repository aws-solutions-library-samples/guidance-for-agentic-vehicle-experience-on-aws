"""
T2.5 — ``subject_key`` in ``finding_key``; byte-identical when absent;
re-emit keeps FI fields current.

Spec source:
    .kiro/specs/2026-10-02-fi-action-queue-stage-a/tasks.md § Group 2
    → task "`subject_key` in `finding_key`, byte-identical when absent;
      re-emit keeps the FI fields current"

Contract reference:
    docs/fi-action-queue-contract.md § 1.1

Acceptance criteria tested here:
    A. ``compute_finding_key()`` accepts an optional ``subject_key``.
    B. Without it (None or empty string), the digest is byte-identical to the
       four-field form — pinned by literal against the existing fleet-scoped
       fixture.
    C. With it, two Findings of one kind on one fleet get distinct keys.
    D. ``write_finding()`` passes ``subject_key`` through (it is included in
       the Finding dict written to DDB).
    E. ``_update_item`` SETs ``recommended_action``, ``location``,
       and ``subject_key`` on re-emit for ALL Findings; also SETs/REMOVEs
       ``severity`` and ``disclosure_txt`` for agent-fi- Findings only
       (G3-1 follow-up to 1d22ea5).
    F. ``_update_item`` REMOVEs ``recommended_action``, ``location`` and
       ``subject_key`` when the new artifact omits them (so the override
       baseline stays current).

Mutation guards (run in scratch copy; recorded in decisions-T2.5-fragment.md):
    M1. Include ``subject_key: None`` in the canonical JSON when absent →
        the byte-identity test fails.
    M2. Drop ``recommended_action`` from the update SET → a re-emit test fails.

All tests use a DDB Stubber (botocore.stub.Stubber); no network calls.

Design rules:
    1. Never read prose in a guard — compare structured data.
    2. Mutation values come from OUTSIDE the assertion's own vocabulary.
    3. "Covered by construction" is a claim; measure and say what was measured.
    4. Restore from cp copy, NEVER git checkout.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from agents.tier2.writer import (
    _FI_ESCALATION_ATTRS,
    _FI_REEMIT_ATTRS,
    compute_finding_key,
    write_finding,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE_NAME = "vsa-test-avx-findings"
AGENT_ID = "agent-fi-cost"
FLEET_ID = "FLEET-MRD-001"
FINDING_KIND = "fleet.rebalance.move"

# Two subject_key values for a move-pair discriminator.
SUBJECT_KEY_AB = "loc-airport-02|loc-downtown-01"
SUBJECT_KEY_BC = "loc-downtown-01|loc-central-03"

# Literal digest for the four-field form (no subject_key).
# This pin IS the mutation guard for M1: if subject_key=None is included in
# the canonical JSON the hash changes and the test fails.
# Derived once from compute_finding_key(agent_id=AGENT_ID,
#     scope_field="fleet_id", scope_value=FLEET_ID, finding_kind=FINDING_KIND)
# and recorded in decisions-T2.5-fragment.md.
_EXPECTED_NO_SUBJECT_KEY_DIGEST = (
    "d82da94eb718110b7f2ddb7673e42fc632d76380268c4b3f17c54befc408cc60"
)

# Two deterministic inputs hashes — different from each other and from any
# value the assertions use (Rule 2).
_HASH_RUN_1 = hashlib.sha256(b"fi-run-1-xyzzy-fleet").hexdigest()
_HASH_RUN_2 = hashlib.sha256(b"fi-run-2-plugh-fleet").hexdigest()

assert _HASH_RUN_1 != _HASH_RUN_2

_SERIALIZER = TypeSerializer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ddb_client():
    client = boto3.client("dynamodb", region_name="us-west-2")
    return client, Stubber(client)


def _stub_query_no_results(stubber: Stubber) -> None:
    stubber.add_response(
        "query",
        {"Items": [], "Count": 0},
        expected_params={
            "TableName": TABLE_NAME,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _stub_query_live_row(stubber: Stubber, row: dict) -> None:
    """Stub a Query returning one live DDB row."""

    def _ser(v):
        if isinstance(v, float):
            v = Decimal(str(v))
        return _SERIALIZER.serialize(v)

    ddb_item: dict = {}
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            from agents.tier2.writer import _coerce_floats
            ddb_item[k] = _SERIALIZER.serialize(_coerce_floats(v))
        else:
            ddb_item[k] = _ser(v)

    stubber.add_response(
        "query",
        {"Items": [ddb_item], "Count": 1},
        expected_params={
            "TableName": TABLE_NAME,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _stub_put_item(stubber: Stubber) -> None:
    stubber.add_response(
        "put_item",
        {},
        expected_params={"TableName": TABLE_NAME, "Item": ANY},
    )


def _stub_update_item(stubber: Stubber) -> None:
    stubber.add_response(
        "update_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Key": ANY,
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
        },
    )


def _make_fleet_finding(
    *,
    subject_key: str | None = None,
    recommended_action: dict | None = None,
    location: dict | None = None,
    severity: str = "routine",
    inputs_hash: str = _HASH_RUN_1,
    finding_id: str | None = None,
    computed_at: str = "2026-10-02T02:00:00Z",
    expires_at: str = "2026-10-10T02:00:00Z",
    audit_trail: list | None = None,
) -> dict:
    """Build a minimal fleet-scoped Finding for T2.5 tests."""
    fk = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="fleet_id",
        scope_value=FLEET_ID,
        finding_kind=FINDING_KIND,
        subject_key=subject_key,
    )
    if finding_id is None:
        finding_id = f"FIND-{uuid.uuid4()}"
    if audit_trail is None:
        audit_trail = []

    finding: dict = {
        "finding_id": finding_id,
        "finding_key": fk,
        "agent_id": AGENT_ID,
        "agent_version": "1.0.0",
        "scope": {"fleet_id": FLEET_ID},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": "medium",
        "finding_kind": FINDING_KIND,
        "headline": "Fleet rebalance move opportunity.",
        "detail": "Three vehicles at loc-airport-02 with low utilisation.",
        "evidence": [
            {
                "kind": "fi.rebalance.utilisation",
                "source": "fleet_intelligence",
                "source_ref": "athena-query-fi-001",
                "value": {"utilisation_pct": 22},
                "computed_at": computed_at,
                "provenance": "live",
            }
        ],
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": audit_trail,
    }

    # Optional FI fields — only present when provided.
    if subject_key is not None:
        finding["subject_key"] = subject_key
    if recommended_action is not None:
        finding["recommended_action"] = recommended_action
    if location is not None:
        finding["location"] = location

    return finding


# A sample recommended_action for FI queue item tests.
_SAMPLE_RECOMMENDED_ACTION = {
    "action_kind": "move_vehicles",
    "target_system": "cms",
    "parameters": {
        "vins": ["MRDTEST00000000001"],
        "from_location_id": "loc-airport-02",
        "to_location_id": "loc-downtown-01",
    },
}

_SAMPLE_LOCATION = {
    "from_location_id": "loc-airport-02",
    "to_location_id": "loc-downtown-01",
}


# ---------------------------------------------------------------------------
# A. compute_finding_key() accepts an optional subject_key
# ---------------------------------------------------------------------------

class TestComputeFindingKeySubjectKey:
    """Unit tests for the ``subject_key`` extension of ``compute_finding_key``."""

    def test_accepts_subject_key_kwarg(self):
        """``compute_finding_key`` must accept ``subject_key`` without raising."""
        key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key=SUBJECT_KEY_AB,
        )
        assert len(key) == 64
        assert key == key.lower()


# ---------------------------------------------------------------------------
# B. Byte-identical when absent — pinned by literal (Mutation M1 guard)
# ---------------------------------------------------------------------------

class TestByteIdenticalWhenAbsent:
    """Without ``subject_key`` the digest must equal the four-field form.

    This is the mutation M1 guard: if the implementation includes
    ``subject_key: None`` in the canonical JSON the hash changes and these
    tests fail.
    """

    def test_omitting_subject_key_equals_literal_pin(self):
        """Digest without subject_key equals the pinned four-field form."""
        key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
        )
        assert key == _EXPECTED_NO_SUBJECT_KEY_DIGEST, (
            f"Digest without subject_key must equal the pinned four-field value.\n"
            f"  Expected: {_EXPECTED_NO_SUBJECT_KEY_DIGEST}\n"
            f"  Got:      {key}\n"
            "If these differ, the implementation has changed the key derivation for "
            "existing rows — that is a breaking change."
        )

    def test_explicit_none_equals_four_field_form(self):
        """``subject_key=None`` must produce the same digest as the four-field form."""
        key_implicit = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
        )
        key_explicit_none = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key=None,
        )
        assert key_implicit == key_explicit_none == _EXPECTED_NO_SUBJECT_KEY_DIGEST, (
            "subject_key=None must be byte-identical to omitting subject_key.\n"
            f"  No arg:        {key_implicit}\n"
            f"  Explicit None: {key_explicit_none}\n"
            f"  Pin:           {_EXPECTED_NO_SUBJECT_KEY_DIGEST}"
        )

    def test_empty_string_equals_four_field_form(self):
        """``subject_key=''`` (empty string) must equal the four-field form."""
        key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key="",
        )
        assert key == _EXPECTED_NO_SUBJECT_KEY_DIGEST, (
            "An empty subject_key must be treated as absent (byte-identical to "
            "the four-field form).\n"
            f"  Expected: {_EXPECTED_NO_SUBJECT_KEY_DIGEST}\n"
            f"  Got:      {key}"
        )


# ---------------------------------------------------------------------------
# C. With subject_key — two Findings of one kind on one fleet get distinct keys
# ---------------------------------------------------------------------------

class TestDistinctKeysWithSubjectKey:
    """Two Findings of the same kind on the same fleet get distinct keys."""

    def test_different_subject_keys_produce_different_hashes(self):
        """Different ``subject_key`` values must yield different ``finding_key``s."""
        key_ab = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key=SUBJECT_KEY_AB,
        )
        key_bc = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key=SUBJECT_KEY_BC,
        )
        assert key_ab != key_bc, (
            "Different subject_key values must produce different finding_keys.\n"
            f"  AB key: {key_ab[:16]}…\n"
            f"  BC key: {key_bc[:16]}…\n"
            "If they are equal, the subject_key is not part of the hash."
        )

    def test_subject_key_differs_from_no_subject_key(self):
        """A Finding with subject_key must have a different key from one without."""
        key_with = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
            subject_key=SUBJECT_KEY_AB,
        )
        key_without = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="fleet_id",
            scope_value=FLEET_ID,
            finding_kind=FINDING_KIND,
        )
        assert key_with != key_without, (
            "A Finding with subject_key must not share a finding_key with one "
            "without subject_key on the same fleet/kind.\n"
            f"  With:    {key_with[:16]}…\n"
            f"  Without: {key_without[:16]}…"
        )

    def test_two_findings_same_kind_same_fleet_distinct_keys(self):
        """Two fleet-scoped Findings of the same kind with different subject_keys
        have different finding_keys — they are treated as separate open conditions."""
        finding_1 = _make_fleet_finding(subject_key=SUBJECT_KEY_AB)
        finding_2 = _make_fleet_finding(subject_key=SUBJECT_KEY_BC)

        # Both have the same agent/kind/fleet.
        assert finding_1["agent_id"] == finding_2["agent_id"]
        assert finding_1["finding_kind"] == finding_2["finding_kind"]
        assert finding_1["scope"] == finding_2["scope"]

        # But different finding_keys.
        assert finding_1["finding_key"] != finding_2["finding_key"], (
            "Two Findings of the same kind on the same fleet must have distinct "
            "finding_keys when they carry different subject_key values.\n"
            f"  Finding 1 key: {finding_1['finding_key'][:16]}…\n"
            f"  Finding 2 key: {finding_2['finding_key'][:16]}…"
        )


# ---------------------------------------------------------------------------
# D. write_finding() passes subject_key through
# ---------------------------------------------------------------------------

class TestWriteFindingPassesSubjectKey:
    """``write_finding`` stores a Finding that carries ``subject_key``."""

    def test_put_item_includes_subject_key(self):
        """On the PutItem path, the ``subject_key`` field is in the DDB item."""
        client, stubber = _make_ddb_client()
        finding = _make_fleet_finding(subject_key=SUBJECT_KEY_AB)

        captured: list[dict] = []

        with stubber:
            _stub_query_no_results(stubber)

            _orig_put = client.put_item

            def _capturing_put(**kwargs):
                captured.append(kwargs)
                return {}

            client.put_item = _capturing_put

            result = write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-10-02T02:01:00Z",
            )

        assert result["action"] == "put"
        assert len(captured) == 1

        item = captured[0]["Item"]
        # The stored item must carry subject_key as a DDB string attribute.
        assert "subject_key" in item, (
            "write_finding must store subject_key in the DDB item on the PutItem path."
        )
        assert item["subject_key"] == {"S": SUBJECT_KEY_AB}, (
            f"subject_key attribute must equal the Finding's value.\n"
            f"  Expected: {{'S': {SUBJECT_KEY_AB!r}}}\n"
            f"  Got:      {item.get('subject_key')}"
        )


# ---------------------------------------------------------------------------
# E. _update_item SETs recommended_action, location, subject_key (not severity)
#    on re-emit (Mutation M2 guard)
# ---------------------------------------------------------------------------

class TestUpdateItemSetsFIFields:
    """On UpdateItem (re-emit), FI fields must be SET from the new artifact.

    This is the mutation M2 guard: if ``recommended_action`` is dropped from
    the update SET, the stored item keeps the stale value and the test fails.
    """

    def _run_update(
        self,
        finding: dict,
        prior_row: dict | None = None,
    ) -> dict:
        """Run write_finding on the update path; capture and return UpdateItem kwargs."""
        client, stubber = _make_ddb_client()

        if prior_row is None:
            # Build a minimal prior row to simulate an existing live finding.
            prior_row = {
                **finding,
                "inputs_hash": _HASH_RUN_2,   # differs so update path is taken
            }

        captured: list[dict] = []

        with stubber:
            _stub_query_live_row(stubber, prior_row)

            _orig_update = client.update_item

            def _capturing_update(**kwargs):
                captured.append(kwargs)
                return {}

            client.update_item = _capturing_update

            write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-10-02T02:01:00Z",
            )

        assert len(captured) == 1, (
            f"Expected exactly one UpdateItem call; got {len(captured)}"
        )
        return captured[0]

    def test_recommended_action_is_set_on_update(self):
        """``recommended_action`` must appear in the UpdateExpression SET clause."""
        finding = _make_fleet_finding(
            subject_key=SUBJECT_KEY_AB,
            recommended_action=_SAMPLE_RECOMMENDED_ACTION,
            location=_SAMPLE_LOCATION,
        )
        call = self._run_update(finding)

        expr = call["UpdateExpression"]
        # Must contain a SET entry for recommended_action (via the #fi_recommended_action
        # placeholder).
        assert "#fi_recommended_action = :fi_recommended_action" in expr, (
            "UpdateExpression must SET recommended_action on re-emit.\n"
            "If absent, a stale recommended_action stays on the row after a re-emit.\n"
            f"UpdateExpression: {expr!r}"
        )
        # The value must be in ExpressionAttributeValues.
        assert ":fi_recommended_action" in call["ExpressionAttributeValues"], (
            "ExpressionAttributeValues must include the recommended_action value."
        )
        assert call["ExpressionAttributeNames"]["#fi_recommended_action"] == "recommended_action"

    def test_location_is_set_on_update(self):
        """``location`` must appear in the UpdateExpression SET clause."""
        finding = _make_fleet_finding(
            subject_key=SUBJECT_KEY_AB,
            recommended_action=_SAMPLE_RECOMMENDED_ACTION,
            location=_SAMPLE_LOCATION,
        )
        call = self._run_update(finding)

        expr = call["UpdateExpression"]
        assert "#fi_location = :fi_location" in expr, (
            "UpdateExpression must SET location on re-emit.\n"
            f"UpdateExpression: {expr!r}"
        )
        assert call["ExpressionAttributeNames"]["#fi_location"] == "location"

    def test_subject_key_is_set_on_update(self):
        """``subject_key`` must appear in the UpdateExpression SET clause."""
        finding = _make_fleet_finding(
            subject_key=SUBJECT_KEY_AB,
            recommended_action=_SAMPLE_RECOMMENDED_ACTION,
        )
        call = self._run_update(finding)

        expr = call["UpdateExpression"]
        assert "#fi_subject_key = :fi_subject_key" in expr, (
            "UpdateExpression must SET subject_key on re-emit.\n"
            f"UpdateExpression: {expr!r}"
        )
        assert call["ExpressionAttributeNames"]["#fi_subject_key"] == "subject_key"
        assert call["ExpressionAttributeValues"][":fi_subject_key"] == {
            "S": SUBJECT_KEY_AB
        }

    def test_severity_is_not_updated_in_place(self):
        """Decision D-G2-1 superseded by Group 3 design answer 1: severity IS refreshed
        on re-emit so an escalation (routine → p0) is always stored correctly.
        The avx_broker tripwire follow-on is tracked separately."""
        finding = _make_fleet_finding(severity="high")
        call = self._run_update(finding)
        assert "severity" in call["UpdateExpression"], (
            "severity must be in the UpdateExpression on re-emit (C1 fix). "
            "Mutation: drop from _FI_REEMIT_ATTRS → not present → fails."
        )

    def test_fi_reemit_attrs_constant_covers_expected_fields(self):
        """``_FI_REEMIT_ATTRS`` covers exactly the three FI fields refreshed (SET or
        REMOVE) for ALL Findings.  ``disclosure_txt`` lives in ``_FI_ESCALATION_ATTRS``
        and is refreshed only for agent-fi- Findings (G3-1 follow-up).

        ``severity`` is in neither tuple since issue
        2026-10-23-tier2-writer-frozen-severity: it is required, so it is SET on every
        in-place update of every Finding and has no REMOVE arm. Putting it back in
        ``_FI_REEMIT_ATTRS`` would REMOVE it from a row whose artifact lacks it.
        """
        assert frozenset(_FI_REEMIT_ATTRS) == frozenset(
            ("recommended_action", "location", "subject_key")), (
            "C1: _FI_REEMIT_ATTRS must contain exactly the three all-Finding fields."
        )
        assert frozenset(_FI_ESCALATION_ATTRS) == frozenset(("disclosure_txt",)), (
            "_FI_ESCALATION_ATTRS must contain exactly disclosure_txt (severity is SET "
            "for every Finding since issue 2026-10-23-tier2-writer-frozen-severity)."
        )


# ---------------------------------------------------------------------------
# F. _update_item REMOVEs FI optional fields when the new artifact omits them
# ---------------------------------------------------------------------------

class TestUpdateItemRemovesFIFieldsWhenAbsent:
    """When a re-emitted Finding omits FI optional fields, they must be REMOVED.

    This prevents a stale ``recommended_action`` or ``location`` from staying
    on the row after a re-emit that drops the field.
    """

    def _run_update_capture_expr(
        self,
        finding: dict,
        prior_row: dict | None = None,
    ) -> str:
        """Run write_finding on the update path; return the UpdateExpression."""
        client, stubber = _make_ddb_client()

        if prior_row is None:
            prior_row = {**finding, "inputs_hash": _HASH_RUN_2}

        captured: list[str] = []

        with stubber:
            _stub_query_live_row(stubber, prior_row)

            def _capturing_update(**kwargs):
                captured.append(kwargs["UpdateExpression"])
                return {}

            client.update_item = _capturing_update

            write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-10-02T02:01:00Z",
            )

        return captured[0]

    def test_recommended_action_removed_when_absent(self):
        """A re-emitted Finding without ``recommended_action`` must REMOVE it."""
        # New artifact has no recommended_action (informational finding only).
        finding = _make_fleet_finding(subject_key=SUBJECT_KEY_AB)
        assert "recommended_action" not in finding, "test fixture must not have recommended_action"

        expr = self._run_update_capture_expr(finding)

        assert "REMOVE" in expr, (
            "UpdateExpression must contain a REMOVE clause when recommended_action "
            "is absent from the re-emitted Finding.\n"
            f"UpdateExpression: {expr!r}"
        )
        assert "#fi_recommended_action" in expr.split("REMOVE")[1], (
            "REMOVE clause must include #fi_recommended_action.\n"
            f"UpdateExpression: {expr!r}"
        )

    def test_location_removed_when_absent(self):
        """A re-emitted Finding without ``location`` must REMOVE it."""
        finding = _make_fleet_finding(subject_key=SUBJECT_KEY_AB)
        assert "location" not in finding

        expr = self._run_update_capture_expr(finding)

        assert "#fi_location" in expr.split("REMOVE")[1], (
            "REMOVE clause must include #fi_location.\n"
            f"UpdateExpression: {expr!r}"
        )

    def test_subject_key_removed_when_absent(self):
        """A re-emitted Finding without ``subject_key`` must REMOVE it."""
        finding = _make_fleet_finding()   # no subject_key
        assert "subject_key" not in finding

        expr = self._run_update_capture_expr(finding)

        assert "#fi_subject_key" in expr.split("REMOVE")[1], (
            "REMOVE clause must include #fi_subject_key.\n"
            f"UpdateExpression: {expr!r}"
        )

    def test_fi_fields_present_not_removed(self):
        """FI fields that ARE on the re-emitted Finding must NOT be in the REMOVE clause."""
        finding = _make_fleet_finding(
            subject_key=SUBJECT_KEY_AB,
            recommended_action=_SAMPLE_RECOMMENDED_ACTION,
            location=_SAMPLE_LOCATION,
        )
        expr = self._run_update_capture_expr(finding)

        # If there is a REMOVE clause, it must not name these present fields.
        if "REMOVE" in expr:
            remove_part = expr.split("REMOVE")[1]
            assert "#fi_recommended_action" not in remove_part, (
                "recommended_action is present — must NOT be in REMOVE clause.\n"
                f"UpdateExpression: {expr!r}"
            )
            assert "#fi_location" not in remove_part, (
                "location is present — must NOT be in REMOVE clause.\n"
                f"UpdateExpression: {expr!r}"
            )
            assert "#fi_subject_key" not in remove_part, (
                "subject_key is present — must NOT be in REMOVE clause.\n"
                f"UpdateExpression: {expr!r}"
            )



# ---------------------------------------------------------------------------
# W5 regression guard — moto-backed re-emit per scope (W5, Fix Group 2c)
# ---------------------------------------------------------------------------
import pytest  # noqa: E402 — appended section needs its own import for moto tests


class TestOwnerScopedReEmitSucceeds:
    """Moto-backed: UpdateItem must not fail when an owner-scoped Finding has
    no FI optional fields.

    W5 root cause: ``_update_item`` registered ``#fi_recommended_action`` etc.
    in ``ExpressionAttributeNames`` for ALL scopes, even when neither SET nor
    REMOVE fired (owner_id scope with absent FI fields). DynamoDB rejects this
    with ValidationException: "Value provided in ExpressionAttributeNames
    unused in expressions."

    These tests exercise write_finding() end-to-end against moto so the real
    DynamoDB validation logic runs — a Stubber cannot catch this because
    Stubbers never validate ExpressionAttributeNames consistency.

    Mutation guard: reverting the W5 fix (registering expr_names[ph]=attr
    unconditionally) causes DynamoDB to raise ValidationException; the test
    must fail in that state.
    """

    _TABLE = "vsa-moto-test-avx-findings"
    _AGENT_ID = "agent-2-vehicle-health"
    _OWNER_ID = "owner-uuid-reemit-001"

    def _make_moto_table(self, ddb_client):
        """Create the AVX Findings table under moto."""
        ddb_client.create_table(
            TableName=self._TABLE,
            KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "finding_id", "AttributeType": "S"},
                {"AttributeName": "finding_key", "AttributeType": "S"},
                {"AttributeName": "expires_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_finding_key",
                    "KeySchema": [
                        {"AttributeName": "finding_key", "KeyType": "HASH"},
                        {"AttributeName": "expires_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )

    def _make_finding(self, inputs_hash: str, *, with_fi_fields: bool = False) -> dict:
        import datetime
        import uuid
        finding_key = compute_finding_key(
            agent_id=self._AGENT_ID,
            scope_field="owner_id",
            scope_value=self._OWNER_ID,
            finding_kind="health.battery",
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = (now + datetime.timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
        now_str = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        finding: dict = {
            "finding_id": str(uuid.uuid4()),
            "finding_key": finding_key,
            "agent_id": self._AGENT_ID,
            "agent_version": "0.3.0",
            "finding_kind": "health.battery",
            "scope": {"owner_id": self._OWNER_ID},
            "inputs_hash": inputs_hash,
            "computed_at": now_str,
            "expires_at": expires,
            "headline": "Battery headline",
            "detail": "Battery detail",
            "evidence": [],
            "audit_trail": [],
        }
        if with_fi_fields:
            finding["recommended_action"] = "book_recall_service"
            finding["location"] = "front-left"
            finding["subject_key"] = "sub-key-01"
        return finding

    @pytest.mark.parametrize("with_fi_fields", [False, True], ids=["no_fi_fields", "with_fi_fields"])
    def test_owner_scoped_reemit_succeeds(self, with_fi_fields):
        """owner_id re-emit must complete without ValidationException (W5)."""
        try:
            from moto import mock_aws
        except ImportError:
            pytest.skip("moto not available")

        import hashlib

        hash_1 = hashlib.sha256(b"owner-reemit-run-1-xyzzy").hexdigest()
        hash_2 = hashlib.sha256(b"owner-reemit-run-2-plugh").hexdigest()

        with mock_aws():
            import boto3
            client = boto3.client("dynamodb", region_name="us-west-2")
            self._make_moto_table(client)

            # First write: PutItem (no live row yet)
            finding_1 = self._make_finding(hash_1, with_fi_fields=with_fi_fields)
            result_1 = write_finding(finding_1, table_name=self._TABLE, dynamodb_client=client)
            assert result_1["action"] == "put", f"Expected put on first write, got {result_1}"

            # Second write: UpdateItem with different inputs_hash (the re-emit path)
            finding_2 = self._make_finding(hash_2, with_fi_fields=with_fi_fields)
            # Reuse the same finding_key so the GSI query returns the live row
            finding_2["finding_key"] = finding_1["finding_key"]

            # This must NOT raise ValidationException (W5 regression)
            result_2 = write_finding(finding_2, table_name=self._TABLE, dynamodb_client=client)
            assert result_2["action"] == "update", (
                f"Expected update on re-emit, got {result_2['action']!r}. "
                "If 'put', the GSI query may not have returned the live row."
            )
            assert result_2["finding_id"] == result_1["finding_id"], (
                "Re-emit must preserve finding_id (in-place update)"
            )

    def test_vin_scoped_reemit_removes_absent_fi_fields(self):
        """Non-owner scopes must still REMOVE absent FI fields on re-emit."""
        try:
            from moto import mock_aws
        except ImportError:
            pytest.skip("moto not available")

        import hashlib

        hash_1 = hashlib.sha256(b"vin-reemit-run-1-xyzzy").hexdigest()
        hash_2 = hashlib.sha256(b"vin-reemit-run-2-plugh").hexdigest()

        with mock_aws():
            import boto3
            client = boto3.client("dynamodb", region_name="us-west-2")
            self._make_moto_table(client)

            fk = compute_finding_key(
                agent_id=self._AGENT_ID,
                scope_field="vin",
                scope_value="VIN-MOTO-TEST-001",
                finding_kind="health.battery",
            )
            import datetime, uuid as _uuid

            def _make_vin_finding(ih, *, with_fi):
                now = datetime.datetime.now(datetime.timezone.utc)
                exp = (now + datetime.timedelta(days=8)).isoformat(timespec="seconds").replace("+00:00", "Z")
                now_str = now.isoformat(timespec="seconds").replace("+00:00", "Z")
                f: dict = {
                    "finding_id": str(_uuid.uuid4()),
                    "finding_key": fk,
                    "agent_id": self._AGENT_ID,
                    "agent_version": "0.3.0",
                    "finding_kind": "health.battery",
                    "scope": {"vin": "VIN-MOTO-TEST-001"},
                    "inputs_hash": ih,
                    "computed_at": now_str,
                    "expires_at": exp,
                    "headline": "h", "detail": "d",
                    "evidence": [], "audit_trail": [],
                }
                if with_fi:
                    f["recommended_action"] = "book_recall_service"
                    f["location"] = "front-left"
                    f["subject_key"] = "sub-01"
                return f

            # First write WITH fi fields
            r1 = write_finding(_make_vin_finding(hash_1, with_fi=True),
                               table_name=self._TABLE, dynamodb_client=client)
            assert r1["action"] == "put"

            # Second write WITHOUT fi fields → must succeed AND remove the FI attrs
            f2 = _make_vin_finding(hash_2, with_fi=False)
            f2["finding_key"] = fk
            r2 = write_finding(f2, table_name=self._TABLE, dynamodb_client=client)
            assert r2["action"] == "update", (
                f"Expected update on second vin-scoped write, got {r2['action']!r}"
            )
            # Confirm the FI fields were removed
            item = client.get_item(
                TableName=self._TABLE,
                Key={"finding_id": {"S": r2["finding_id"]}},
            )["Item"]
            assert "recommended_action" not in item, (
                "recommended_action must be REMOVEd when absent on re-emit"
            )
            assert "location" not in item, (
                "location must be REMOVEd when absent on re-emit"
            )
            assert "subject_key" not in item, (
                "subject_key must be REMOVEd when absent on re-emit (S9)"
            )
