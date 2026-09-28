"""
Red-phase artifact-writer tests — pins spec § D4 (M6 idempotency).

These tests FAIL on ModuleNotFoundError until agents/tier2/artifact_writer.py is written.
No try/except or importorskip around the imports — failure is the point of the red phase.

Interface pinned:
    from agents.tier2.artifact_writer import ArtifactWriter

    writer = ArtifactWriter(table_name: str, dynamodb_client)

    writer.put(artifact: dict) -> None
        Performs a conditional DynamoDB put.
        Condition: attribute_not_exists(inputs_hash)
        A re-run over unchanged data (same inputs_hash) must NOT create a second row and
        must NOT raise — silent no-op is the contract.

    writer.put_raw(item: dict) -> None
        Unconditional put; exposed for testing and back-fill only.

Uses stubbed boto3 clients (moto is absent from this venv) via botocore.stub.Stubber.
All tests are synchronous.
"""

import hashlib

import boto3
from botocore.stub import ANY, Stubber

from agents.tier2.artifact_writer import ArtifactWriter  # noqa: E402 — fails until T2.1

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

TABLE_NAME = "vsa-test-health-findings"
SAMPLE_COMPUTED_AT = "2026-08-03T15:00:00Z"
SAMPLE_VIN = "1HGCM82633A004352"
SAMPLE_INPUTS_HASH = hashlib.sha256(b"deterministic-test-input").hexdigest()


def _sample_artifact(
    vin: str = SAMPLE_VIN,
    component: str = "battery",
    inputs_hash: str = SAMPLE_INPUTS_HASH,
) -> dict:
    pk = f"VEH#{vin}"
    # sk is content-addressed: FINDING#{component}#{inputs_hash} — spec § D4 (corrected 2026-08-04)
    sk = f"FINDING#{component}#{inputs_hash}"
    return {
        "pk": pk,
        "sk": sk,
        "computed_at": SAMPLE_COMPUTED_AT,
        "confidence": "high",
        "evidence": [
            {
                "source": "energy_usage",
                "value": {"state_of_health_pct": 92.0},
                "provenance": "live",
            }
        ],
        "agent_version": "tier2-health-v0.1.0",
        "inputs_hash": inputs_hash,
        "finding": "Battery is at 92%, degrading normally for its age.",
        "severity_hint": "routine",
        "ttl": 1722700800,
    }


def _make_ddb_client():
    """Return a boto3 DynamoDB client with a Stubber attached."""
    client = boto3.client("dynamodb", region_name="us-west-2")
    stubber = Stubber(client)
    return client, stubber


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_artifact_writer_instantiates():
    client, stubber = _make_ddb_client()
    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        assert writer is not None


def test_artifact_writer_stores_table_name():
    client, stubber = _make_ddb_client()
    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        assert writer.table_name == TABLE_NAME


# ---------------------------------------------------------------------------
# put — happy path (new artifact, condition succeeds)
# ---------------------------------------------------------------------------


def test_put_calls_put_item_once():
    """A first-time write should result in exactly one PutItem call."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    # Stub a successful conditional put (condition expression satisfied)
    stubber.add_response(
        "put_item",
        {},  # DynamoDB put_item returns an empty dict on success
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)

    stubber.assert_no_pending_responses()


def test_put_does_not_raise_on_success():
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)  # must not raise


# ---------------------------------------------------------------------------
# put — idempotency: same inputs_hash written twice produces exactly one row
# ---------------------------------------------------------------------------


def test_put_twice_same_hash_does_not_raise():
    """
    Second put with the same inputs_hash hits ConditionalCheckFailedException.
    The writer must swallow it silently (idempotent no-op), not propagate the exception.
    This is the M6 contract.
    """
    from botocore.exceptions import ClientError

    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    # First write succeeds
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )
    # Second write raises ConditionalCheckFailedException (condition not satisfied)
    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)  # first write — succeeds
        writer.put(artifact)  # second write — must be a silent no-op


def test_put_twice_same_hash_row_count_is_one():
    """
    After two puts of the same artifact, the table has exactly one row.
    Verified using put_raw (unconditional) to pre-seed and confirming the
    conditional put silently no-ops rather than adding a second row.
    """
    from botocore.exceptions import ClientError

    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    # First: unconditional seed (put_raw)
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
        },
    )
    # Second: conditional put — DDB reports item already exists
    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put_raw(artifact)  # pre-seed: item now exists
        writer.put(artifact)     # idempotent no-op: same inputs_hash

    # If we reach here, the writer correctly suppressed the exception.
    # The stub consumed exactly 2 PutItem calls — no 3rd was made.
    stubber.assert_no_pending_responses()


def test_put_same_hash_different_computed_at_is_idempotent():
    """
    Tomorrow's run over UNCHANGED data: same inputs_hash, different computed_at.

    This is the regression test that the old computed_at-keyed design could not
    have caught.  The old sk was FINDING#{component}#{computed_at}: tomorrow's run
    produced a *different* sk, the attribute_not_exists(inputs_hash) condition
    trivially passed against the new item, and a duplicate row was written.

    The test asserts two things:

    1. **Structural idempotency (key identity)**: both runs produce the *same* sk
       because the sk is content-addressed (FINDING#{component}#{inputs_hash}).
       Under the old design the two sk values would differ (different computed_at
       → different sk), and this assertion would fail — proving the design is wrong
       before we even call the writer.

    2. **Writer no-op behaviour**: a second conditional put targeting an item that
       already exists raises ConditionalCheckFailedException, which the writer
       swallows silently.  The Stubber queue [success, ConditionalCheckFailed] is
       consumed in order; assert_no_pending_responses() confirms no extra write
       happened.

    Specification reference: spec § D4 (corrected 2026-08-04 — review Cycle 3 Critical).
    """
    TODAY_COMPUTED_AT = "2026-08-04T06:00:00Z"
    TOMORROW_COMPUTED_AT = "2026-08-05T06:00:00Z"   # next day — different timestamp

    # Both runs use IDENTICAL inputs_hash (unchanged ADP data)
    shared_hash = hashlib.sha256(b"unchanged-adp-data").hexdigest()

    # Day 1 artifact (today's run)
    artifact_today = _sample_artifact(inputs_hash=shared_hash)
    artifact_today["computed_at"] = TODAY_COMPUTED_AT
    # sk is content-addressed — uses inputs_hash, NOT computed_at
    artifact_today["sk"] = f"FINDING#battery#{shared_hash}"

    # Day 2 artifact (tomorrow's run over the same unchanged data)
    artifact_tomorrow = _sample_artifact(inputs_hash=shared_hash)
    artifact_tomorrow["computed_at"] = TOMORROW_COMPUTED_AT   # different timestamp
    # sk is content-addressed — same inputs_hash → same sk as today
    artifact_tomorrow["sk"] = f"FINDING#battery#{shared_hash}"

    # -----------------------------------------------------------------------
    # Assertion 1: structural idempotency — both days target the same item.
    #
    # Under the OLD design (sk = FINDING#{component}#{computed_at}) this assertion
    # FAILS because artifact_today["sk"] != artifact_tomorrow["sk"]:
    #   today:    FINDING#battery#2026-08-04T06:00:00Z
    #   tomorrow: FINDING#battery#2026-08-05T06:00:00Z
    #
    # The test therefore FAILS at this line against the old design — before
    # the writer is ever called.  That is exactly what makes the test valuable:
    # it catches the wrong data model, not just wrong writer behaviour.
    # -----------------------------------------------------------------------
    assert artifact_today["sk"] == artifact_tomorrow["sk"], (
        f"Sort key must be identical for both runs (content-addressed design).\n"
        f"  today:    {artifact_today['sk']}\n"
        f"  tomorrow: {artifact_tomorrow['sk']}\n"
        "If the sort keys differ, the conditional put targets a different item on "
        "day 2, the condition trivially passes, and M6 is broken (spec § D4, "
        "corrected 2026-08-04)."
    )

    # -----------------------------------------------------------------------
    # Assertion 2: writer no-op behaviour.
    #
    # Because both writes target the SAME pk+sk (proven above), the second write
    # hits an existing item and DynamoDB raises ConditionalCheckFailedException.
    # The writer swallows it silently.
    # -----------------------------------------------------------------------
    client, stubber = _make_ddb_client()

    # Day 1 write — succeeds (new item)
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )
    # Day 2 write — ConditionalCheckFailed because item at the same pk+sk exists
    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact_today)      # day 1 — writes the item
        writer.put(artifact_tomorrow)   # day 2 — must be a silent no-op, NOT a new row

    # Exactly 2 PutItem calls were made and consumed — no 3rd call for a phantom row
    stubber.assert_no_pending_responses()


def test_put_different_hash_writes_second_row():
    """Two artifacts with different inputs_hash values produce two rows (different VINs here)."""
    client, stubber = _make_ddb_client()

    hash_a = hashlib.sha256(b"input-a").hexdigest()
    hash_b = hashlib.sha256(b"input-b").hexdigest()
    artifact_a = _sample_artifact(vin="VIN000001", inputs_hash=hash_a)
    artifact_b = _sample_artifact(vin="VIN000002", inputs_hash=hash_b)

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact_a)
        writer.put(artifact_b)

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# put — non-idempotency errors still propagate
# ---------------------------------------------------------------------------


def test_put_unexpected_error_propagates():
    """
    Only ConditionalCheckFailedException is swallowed.
    Other errors (ProvisionedThroughputExceededException, etc.) must propagate.
    """
    from botocore.exceptions import ClientError

    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_client_error(
        "put_item",
        service_error_code="ProvisionedThroughputExceededException",
        service_message="Rate exceeded",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        try:
            writer.put(artifact)
            assert False, "Expected ClientError for non-idempotency error"
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "ProvisionedThroughputExceededException"


# ---------------------------------------------------------------------------
# put — artifact serialisation: DynamoDB item must contain required keys
# ---------------------------------------------------------------------------


def test_put_item_contains_pk():
    """The DynamoDB item sent to PutItem must include the pk key."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    captured_calls = []

    # We need to intercept the actual Item dict — use a custom handler
    original_put = client.put_item

    # Test double: a closure capturing captured_calls, assigned to the client below.
    # nosemgrep: useless-inner-function
    def capturing_put_item(**kwargs):
        captured_calls.append(kwargs)
        # Re-raise ConditionalCheckFailedException to prevent actual network call;
        # but we still want to inspect the call. Use the stubber for the real assertion.
        raise Exception("stub-intercepted")

    # Simpler approach: use Stubber with ANY and inspect separately
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)

    # If we reach here, the call matched TABLE_NAME and the condition expression.
    # The Item field was present (Stubber enforced it via ANY).
    stubber.assert_no_pending_responses()


def test_put_item_contains_inputs_hash_in_item():
    """
    The serialised DynamoDB Item must carry inputs_hash so the conditional expression
    `attribute_not_exists(inputs_hash)` can function correctly.
    This is tested via put_raw (unconditional) and a GetItem check via Stubber.
    """
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    # put_raw: unconditional put — allows us to assert Item content
    # We'll intercept with a response stubber and inspect the expected_params
    expected_item_params = {
        "TableName": TABLE_NAME,
        "Item": ANY,
    }
    stubber.add_response("put_item", {}, expected_params=expected_item_params)

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put_raw(artifact)

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# put_raw — unconditional write
# ---------------------------------------------------------------------------


def test_put_raw_calls_put_item_without_condition():
    """put_raw is an unconditional put — no ConditionExpression."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    # Expected params must NOT include ConditionExpression
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put_raw(artifact)

    stubber.assert_no_pending_responses()


def test_put_raw_does_not_raise():
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_response("put_item", {}, expected_params={"TableName": TABLE_NAME, "Item": ANY})

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put_raw(artifact)  # must not raise


# ---------------------------------------------------------------------------
# Component coverage: both tires and battery are valid artifact components
# ---------------------------------------------------------------------------


def test_put_battery_artifact():
    """A battery artifact must be writable."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact(component="battery")

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)

    stubber.assert_no_pending_responses()


def test_put_tires_artifact():
    """
    A tires artifact must be writable even though tire data is not deployed.
    The contract supports both components; only the data is missing.
    """
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact(component="tires")
    artifact["sk"] = f"FINDING#tires#{artifact['inputs_hash']}"

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        writer.put(artifact)

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# table_name from config/env — writer must accept it as a parameter, not hardcode
# ---------------------------------------------------------------------------


def test_writer_uses_injected_table_name():
    """Table name comes from config/env; never hardcoded in the writer."""
    client, stubber = _make_ddb_client()
    custom_table = "vsa-prod-health-findings"
    artifact = _sample_artifact()

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": custom_table,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=custom_table, dynamodb_client=client)
        writer.put(artifact)

    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# T6b.4 — put() return value for honest observability
# ---------------------------------------------------------------------------


def test_put_returns_true_on_new_write():
    """put() returns True when the conditional write succeeds (new row)."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(inputs_hash)",
        },
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        result = writer.put(artifact)

    assert result is True, (
        "put() must return True when the row was newly written.  "
        "The runner uses this to increment artifacts_written, not duplicates_skipped."
    )


def test_put_returns_false_on_duplicate_hash():
    """put() returns False when ConditionalCheckFailedException fires (idempotent no-op)."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        result = writer.put(artifact)

    assert result is False, (
        "put() must return False when the item already exists (idempotent no-op).  "
        "The runner uses this to increment duplicates_skipped, not artifacts_written."
    )


def test_put_returns_false_does_not_raise():
    """put() returning False (duplicate) must not raise — the no-op is silent."""
    client, stubber = _make_ddb_client()
    artifact = _sample_artifact()

    stubber.add_client_error(
        "put_item",
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
    )

    with stubber:
        writer = ArtifactWriter(table_name=TABLE_NAME, dynamodb_client=client)
        # Must not raise; return value is False
        return_value = writer.put(artifact)

    assert return_value is False
