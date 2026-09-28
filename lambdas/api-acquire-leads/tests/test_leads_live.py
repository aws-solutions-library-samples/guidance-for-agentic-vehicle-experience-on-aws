"""Live verification for the leadId idempotency fix — real DynamoDB, no mocks.

Per `~/.kiro/steering/spec-workflow.md` § "Live-Verification Gate": a stub cannot
fail the way a service fails. Every other test for this handler patches `_get_ddb`
with a `MagicMock`, which means two load-bearing claims of
`issues/2026-09-20-acquire-lead-id-not-idempotent/` are asserted against a fixture
rather than observed:

1. `attribute_not_exists(leadId)` on a `PutItem` against a partition-key table
   **rejects** a second write rather than overwriting it.
2. DynamoDB raises `ConditionalCheckFailedException` in the response shape the
   handler reads at `exc.response["Error"]["Code"]`.

If either is untrue, the fix is inert and `idempotentReplay: true` is a lie.

Opt-in, so it never runs in CI by default:

    LIVE_TESTS=1 VSA_ACQUIRE_LEADS_TABLE=vsa-staging-acquire-leads \\
      AWS_REGION=us-west-2 .venv/bin/python -m pytest \\
      lambdas/api-acquire-leads/tests/test_leads_live.py -v

Writes and deletes its own items under a `LEAD-LIVETEST-` prefix and a synthetic
tenant, so it cannot collide with real leads.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_TESTS") != "1",
    reason="live test — set LIVE_TESTS=1 and VSA_ACQUIRE_LEADS_TABLE to run",
)

_TABLE_ENV = "VSA_ACQUIRE_LEADS_TABLE"
_PREFIX = "LEAD-LIVETEST-"
_TENANT = "livetest-tenant-do-not-use"


@pytest.fixture(scope="module")
def table():
    """A real DynamoDB Table handle, or a hard failure.

    Anti-vacuity: this fixture calls `describe_table` before yielding, so the suite
    cannot pass by silently not reaching DynamoDB. It deliberately does NOT inspect
    the caller identity — an earlier version of this guard in a sibling spec tried a
    denylist ("admin" in the ARN), which admitted a machine whose user carried
    AdministratorAccess, and its allowlist replacement rejected the real role and
    made the honest path look broken. Reaching the table is the property that
    matters; who you are is the deployment's business.
    """
    boto3 = pytest.importorskip("boto3")
    name = os.environ.get(_TABLE_ENV)
    if not name:
        pytest.fail(f"{_TABLE_ENV} is required — refusing to guess a table name")

    ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    handle = ddb.Table(name)
    handle.meta.client.describe_table(TableName=name)  # raises if absent/denied
    return handle


@pytest.fixture
def lead_id(table):
    lid = f"{_PREFIX}{uuid.uuid4().hex[:12]}"
    yield lid
    try:
        table.delete_item(Key={"leadId": lid})
    except Exception:  # noqa: BLE001
        pass


def test_conditional_put_rejects_a_second_write(table, lead_id):
    """Claim 1: the condition rejects rather than overwrites.

    Asserted by reading the row back: if DynamoDB had overwritten it, capturedAt
    would be the second value. This is the claim the whole fix rests on — without
    it, a duplicate id silently replaces a stored lead.
    """
    from botocore.exceptions import ClientError

    table.put_item(
        Item={"leadId": lead_id, "tenantId": _TENANT, "capturedAt": "first",
              "idempotencyDigest": "digest-one"},
        ConditionExpression="attribute_not_exists(leadId)",
    )

    with pytest.raises(ClientError) as caught:
        table.put_item(
            Item={"leadId": lead_id, "tenantId": _TENANT, "capturedAt": "second",
                  "idempotencyDigest": "digest-two"},
            ConditionExpression="attribute_not_exists(leadId)",
        )

    # Claim 2: the error code is where the handler looks for it.
    code = caught.value.response.get("Error", {}).get("Code")
    assert code == "ConditionalCheckFailedException", (
        f"DynamoDB reported {code!r}; the handler branches on "
        "exc.response['Error']['Code'] == 'ConditionalCheckFailedException' and "
        "would fall through to a 500 for every replay."
    )

    stored = table.get_item(Key={"leadId": lead_id}).get("Item") or {}
    assert stored.get("capturedAt") == "first", (
        "the second write was applied — the condition did not protect the row, so "
        "a colliding id would destroy an existing lead."
    )
    assert stored.get("idempotencyDigest") == "digest-one"


def test_unconditional_put_does_overwrite(table, lead_id):
    """Anti-vacuity partner: proves the test above measures the ConditionExpression
    and not some incidental immutability of the table."""
    table.put_item(Item={"leadId": lead_id, "tenantId": _TENANT, "capturedAt": "first"})
    table.put_item(Item={"leadId": lead_id, "tenantId": _TENANT, "capturedAt": "second"})
    stored = table.get_item(Key={"leadId": lead_id}).get("Item") or {}
    assert stored.get("capturedAt") == "second", (
        "an unconditional put did NOT overwrite — this table does not behave like a "
        "normal partition-key table and the sibling test proves nothing."
    )
