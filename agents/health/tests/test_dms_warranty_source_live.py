"""Live verification for the DMS warranty verdict read path — real DynamoDB, no mocks.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md` § Fix Group 21 task 4.
Design record: `decisions.md` § "Fix Group 21 tasks 1–2" (findings F1–F6) and the Cycle 1–3
review records.

Per `~/.kiro/steering/spec-workflow.md` § "Live-Verification Gate": a stub cannot fail the way
a service fails. Every one of the 80 unit tests for `dms_warranty_source` injects a `FakeDdb`,
which means the load-bearing claims of this module are asserted against a fixture rather than
observed. Specifically, these four are *unfalsifiable* against a stub and are exactly the class
of defect this repo's Tier 2 agent has already shipped four times (unqualified table name,
`CAST` type mismatch, missing dependency edge, unfollowed `NextToken`):

1. **`vin-index` exists on `dms-{stage}-repair-orders` and is queryable by `vehicle_vin`.**
   A wrong index name is a `ValidationException` here and a silent pass against a double — the
   `finding_key-index` / `by_finding_key` defect class (this spec's own Fix Group 10, F6).
2. **`ro-index` exists on `dms-{stage}-warranty-claims` and is queryable by `ro_id`.**
3. **A `verdict_status` stored as a DynamoDB `S` reads back as a `str`** that satisfies
   `DMS_VERDICT_ENUM` — i.e. the enum control actually engages against real stored bytes, not
   against a Python literal the test author chose.
4. **GSI propagation delay is real.** A GSI is eventually consistent, so a just-written item is
   not immediately visible to an index `Query`. No stub models this; see `_await_gsi` below.

## What this test does NOT prove, stated plainly

It seeds its own claim rather than driving DMS's `validate_warranty_claim` endpoint, because
**zero of 300 live `dms-staging-warranty-claims` rows carry `verdict_status`** — including all
87 whose lifecycle `status` is already `Validated` (`decisions.md` F5). There is nothing in
staging to read.

So this verifies **the read path against real DynamoDB**. It does *not* verify that DMS writes
the verdict in the shape assumed. That mitigation is structural rather than aspirational:
:data:`_PRODUCER_VERDICT_ATTRS` pins the seeded attribute set to the exact attributes
`validate_warranty_claim` writes in its single `UpdateExpression`
(`~/guidance-for-dealer-management-system-on-aws/source/handlers/warranty_claims.py:436-452`),
and `test_seeded_shape_matches_the_dms_producer` asserts the fixture has not drifted from it.
Closing the gap properly needs a claim driven through
`POST /dms/dealers/{dealerId}/warranty-claims/{claimId}/validate`, which is tracked as the
follow-on in `decisions.md`.

## Isolation

Writes **new** items only — never modifies any of the 300 existing claims — under a
`LIVETEST-` prefix on both `ro_id` and `claim_id`, against a synthetic VIN
(:data:`_TEST_VIN`) that cannot collide with a real Meridian (`MRDN…`) fleet VIN. Both items
are deleted in fixture teardown. Two DMS staging tables are touched, both additively.

## Running it

    LIVE_TESTS=1 \\
      DMS_WARRANTY_CLAIMS_TABLE=dms-staging-warranty-claims \\
      DMS_REPAIR_ORDERS_TABLE=dms-staging-repair-orders \\
      AWS_REGION=us-west-2 \\
      .venv/bin/python -m pytest agents/health/tests/test_dms_warranty_source_live.py -v

Note this runs under **operator credentials**, not the Tier 2 Lambda execution role. Fix Group
21 task 3 (the IAM grant) is what makes the *deployed* path work and is still blocked on Fix
Group 15; this test is therefore able to verify the read path before that grant exists, and
passing it says nothing about whether the deployed Lambda is authorised. Those are two
different claims — see `~/.kiro/steering/deploy-validation.md` § "Committed, deployed, and
live-verified are three different claims".

## ⚠ Running as an administrator masks a dependency this test cannot see

Both DMS tables are encrypted with a **customer-managed** CMK
(`alias/dms-staging-storage`, `KeyManager: CUSTOMER`), whose key policy
is a single `Principal: <account>:root` / `kms:*` statement with **no condition**. That is IAM
*delegation*, not the `Principal: "*"` + `kms:ViaService` blanket an AWS-managed key carries —
so DynamoDB evaluates the table-key decrypt **per caller**.

An operator principal holding `AdministratorAccess` therefore also holds `kms:Decrypt`, which is
why this suite passes. The Tier 2 Lambda role does not: `infrastructure/lib/vsa-tier2-stack.ts`
has **zero** references to either table or that key. Task 3's Accept originally granted
`dynamodb:GetItem` + `Query` only and forbade widening, so as specified it would have shipped
"complete" and mutation-verified while the deployed read still failed — on a KMS `AccessDenied`
that `fetch_warranty_verdict_phrase`'s deliberately-broad `except` degrades into a single log
line. That Accept was amended in the same commit as this file to require `kms:Decrypt` +
`kms:DescribeKey` and both table-name env vars, each with a presence mutation.
The same stack already carries the pattern for the ADP lake bucket's CMK.

Recorded here because it is the sharpest limitation of this file: **a green run under
superset permissions is evidence about the schema and the code, not about the deployment.**
Found in review Cycle 1 of this task (Warning 1); tracked on task 3's Accept and task 4's
"still owed" bullet in `tasks.md`.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_TESTS") != "1",
    reason=(
        "live test — set LIVE_TESTS=1, DMS_WARRANTY_CLAIMS_TABLE and "
        "DMS_REPAIR_ORDERS_TABLE to run"
    ),
)

from agents.health.dms_warranty_source import (  # noqa: E402  (after the skip guard)
    CLAIMS_RO_INDEX,
    CLAIMS_TABLE_ENV_VAR,
    REPAIR_ORDERS_TABLE_ENV_VAR,
    REPAIR_ORDERS_VIN_INDEX,
    fetch_verdict,
)
from agents.health.dms_verdicts import DMS_VERDICT_DENIED, DMS_VERDICT_ENUM  # noqa: E402

#: Synthetic VIN. Allowed characters only (``sanitise_vin`` keeps ``[A-Z0-9-]``), and
#: deliberately not a Meridian ``MRDN`` prefix so it cannot collide with a seeded fleet VIN.
_TEST_VIN = "LIVETEST0000000001"

_PREFIX = "LIVETEST-"

#: Synthetic dealer for seeded rows. Named so a human scanning the table knows not to use it.
_TEST_DEALER = "livetest-dealer-do-not-use"

#: The attributes DMS's ``validate_warranty_claim`` writes in its single ``UpdateExpression``
#: (``warranty_claims.py:436-452``). Pinned so the seeded fixture cannot drift from the
#: producer without a test failing — the read path is only meaningful against the real shape.
_PRODUCER_VERDICT_ATTRS = frozenset({
    "status",           # lifecycle → "Validated"
    "updated_at",
    "verdict_status",
    "verdict_reason",
    "matched_rule_id",
})

#: Bound on GSI propagation. Generous: the assertion is about *correctness once visible*, and a
#: tight bound would convert an eventual-consistency delay into a spurious failure.
_GSI_TIMEOUT_S = 30.0
_GSI_POLL_S = 0.5


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required — refusing to guess a table name")
    return value


@pytest.fixture(scope="module")
def ddb_client():
    """A real low-level DynamoDB client, or a hard failure.

    Anti-vacuity: `describe_table` is called on **both** tables before yielding, so the suite
    cannot pass by silently never reaching DynamoDB.

    It deliberately does **not** inspect the caller identity. A sibling spec's version of that
    guard tried a denylist (``"admin" in arn``), which admitted a machine whose user carried
    `AdministratorAccess`, and its allowlist replacement rejected the real role and made the
    honest path look broken. Reaching the tables is the property that matters.
    """
    boto3 = pytest.importorskip("boto3")
    claims = _require_env(CLAIMS_TABLE_ENV_VAR)
    repair_orders = _require_env(REPAIR_ORDERS_TABLE_ENV_VAR)

    client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    for table in (claims, repair_orders):
        client.describe_table(TableName=table)  # raises if absent or denied
    return client


@pytest.fixture(scope="module")
def table_names():
    return {
        "claims": _require_env(CLAIMS_TABLE_ENV_VAR),
        "repair_orders": _require_env(REPAIR_ORDERS_TABLE_ENV_VAR),
    }


def _await_gsi(client, *, table: str, index: str, key_attr: str, key_value: str) -> list[dict]:
    """Poll an index Query until it returns at least one item, or fail.

    **This function exists because of a live-only behaviour.** A GSI is eventually consistent:
    a `PutItem` is immediately visible to `GetItem` on the base table but not necessarily to a
    `Query` on an index. No stub in the unit suite models that, so an implementation that reads
    an index straight after a write can pass 80 stubbed tests and be flaky in production.

    The production code path does not write, so it is not exposed to this — but the *test* is,
    and a test that fails 1-in-N on propagation timing gets muted rather than fixed.
    """
    deadline = time.monotonic() + _GSI_TIMEOUT_S
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        resp = client.query(
            TableName=table,
            IndexName=index,
            KeyConditionExpression=f"{key_attr} = :k",
            ExpressionAttributeValues={":k": {"S": key_value}},
        )
        items = resp.get("Items", [])
        if items:
            return items
        time.sleep(_GSI_POLL_S)
    pytest.fail(
        f"{index} on {table} did not surface {key_attr}={key_value!r} within "
        f"{_GSI_TIMEOUT_S}s ({attempts} attempts)"
    )


@pytest.fixture
def seeded_claim(ddb_client, table_names):
    """Seed a repair order and a verdict-carrying claim; delete both on teardown.

    Yields ``(ro_id, claim_id, verdict_reason, updated_at)``.

    ``updated_at`` is written with ``datetime.now(timezone.utc).isoformat()`` — byte-for-byte
    the producer's own expression — so the recency bound is exercised against the real
    ``+00:00`` offset format rather than the ``Z``-suffixed form the unit fixtures also cover.

    **Both writes are inside the ``try``** (review Cycle 1, Warning 2). They were originally
    above it, which meant a failure on the *second* `put_item` left the repair-order row in a
    shared staging table permanently — the cleanup guarantee covered the test body and not the
    setup that created the rows.

    Delete failures are **surfaced, not swallowed**. A bare ``except Exception: pass`` makes a
    failed teardown invisible, so the suite reports green while leaking rows: exactly the
    "green with residue" state that makes a residue check meaningless.
    """
    suffix = uuid.uuid4().hex[:12]
    ro_id = f"{_PREFIX}RO-{suffix}"
    claim_id = f"{_PREFIX}WC-{suffix}"
    reason = f"live-verification {suffix}"
    updated_at = datetime.now(timezone.utc).isoformat()

    try:
        ddb_client.put_item(
            TableName=table_names["repair_orders"],
            Item={
                "ro_id": {"S": ro_id},
                "vehicle_vin": {"S": _TEST_VIN},
                "status": {"S": "Invoiced"},
                "dealer_id": {"S": _TEST_DEALER},
            },
        )
        ddb_client.put_item(
            TableName=table_names["claims"],
            Item={
                "claim_id": {"S": claim_id},
                "ro_id": {"S": ro_id},
                "vehicle_vin": {"S": _TEST_VIN},
                "dealer_id": {"S": _TEST_DEALER},
                # The five producer-written attributes, per _PRODUCER_VERDICT_ATTRS.
                "status": {"S": "Validated"},
                "updated_at": {"S": updated_at},
                "verdict_status": {"S": DMS_VERDICT_DENIED},
                "verdict_reason": {"S": reason},
                "matched_rule_id": {"S": "LIVETEST-RULE-001"},
            },
        )
        yield ro_id, claim_id, reason, updated_at
    finally:
        failures: list[str] = []
        for table, key in (
            (table_names["claims"], {"claim_id": {"S": claim_id}}),
            (table_names["repair_orders"], {"ro_id": {"S": ro_id}}),
        ):
            try:
                ddb_client.delete_item(TableName=table, Key=key)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{table} {key}: {type(exc).__name__}")
        if failures:
            # Loud, not silent: a leaked row in a shared staging table is a finding, and a
            # swallowed delete makes the residue check that follows a run meaningless.
            raise AssertionError(
                "live-test teardown failed to delete seeded row(s) — residue left in shared "
                "staging tables, clean up manually:\n  " + "\n  ".join(failures)
            )


# ---------------------------------------------------------------------------
# The four claims a stub cannot falsify
# ---------------------------------------------------------------------------


def test_repair_orders_vin_index_is_queryable_by_vehicle_vin(
    ddb_client, table_names, seeded_claim
):
    """Claim 1: hop 1's index and key attribute are real.

    A wrong index name or a `vin` / `vehicle_vin` mix-up is a `ValidationException` here and
    an invisible pass against a double. Both were live findings F1/F2 of this group.
    """
    ro_id, _, _, _ = seeded_claim
    items = _await_gsi(
        ddb_client,
        table=table_names["repair_orders"],
        index=REPAIR_ORDERS_VIN_INDEX,
        key_attr="vehicle_vin",
        key_value=_TEST_VIN,
    )
    assert any(i.get("ro_id", {}).get("S") == ro_id for i in items)


def test_claims_ro_index_is_queryable_by_ro_id(ddb_client, table_names, seeded_claim):
    """Claim 2: hop 2's index and key attribute are real."""
    ro_id, claim_id, _, _ = seeded_claim
    items = _await_gsi(
        ddb_client,
        table=table_names["claims"],
        index=CLAIMS_RO_INDEX,
        key_attr="ro_id",
        key_value=ro_id,
    )
    assert any(i.get("claim_id", {}).get("S") == claim_id for i in items)


def test_fetch_verdict_reads_a_real_verdict_end_to_end(
    ddb_client, table_names, seeded_claim
):
    """Claims 3 and the whole two-hop path, through the production function.

    This is the assertion task 4 exists for: `fetch_verdict` against a real boto3 client and
    two real index Queries, returning a verdict that satisfies the enum control.
    """
    _, _, reason, updated_at = seeded_claim

    # Wait for both indexes before calling the production path, so a propagation delay is not
    # misread as a logic failure.
    _await_gsi(
        ddb_client, table=table_names["repair_orders"], index=REPAIR_ORDERS_VIN_INDEX,
        key_attr="vehicle_vin", key_value=_TEST_VIN,
    )

    verdict = None
    deadline = time.monotonic() + _GSI_TIMEOUT_S
    while time.monotonic() < deadline:
        verdict = fetch_verdict(_TEST_VIN, ddb_client=ddb_client)
        if verdict is not None:
            break
        time.sleep(_GSI_POLL_S)

    assert verdict is not None, (
        "fetch_verdict returned None against a real seeded verdict — the two-hop path does "
        "not work against live DynamoDB"
    )
    assert verdict["verdict_status"] == DMS_VERDICT_DENIED
    assert verdict["verdict_status"] in DMS_VERDICT_ENUM, (
        "a verdict read from a real DynamoDB `S` attribute did not satisfy the enum control"
    )
    assert isinstance(verdict["verdict_status"], str)
    assert verdict["verdict_reason"] == reason
    assert verdict["provenance"] == "live"
    # The recency bound parsed the producer's own `+00:00` format rather than rejecting it.
    assert verdict["verdict_updated_at"] == updated_at


def test_narration_carries_the_real_verdict_verbatim(ddb_client, table_names, seeded_claim):
    """The end of the path: a real verdict reaches the Finding text unaltered.

    This is what makes the wiring meaningful rather than merely present — and the verbatim
    property is the deterministic-seam boundary from
    `~/.kiro/steering/agentic-tiers.md` § "What stays deterministic, and why".
    """
    from agents.health import agent as agent_mod

    _await_gsi(
        ddb_client, table=table_names["repair_orders"], index=REPAIR_ORDERS_VIN_INDEX,
        key_attr="vehicle_vin", key_value=_TEST_VIN,
    )

    original = agent_mod._dms_ddb_singleton
    agent_mod._dms_ddb_singleton = ddb_client
    try:
        phrase = None
        deadline = time.monotonic() + _GSI_TIMEOUT_S
        while time.monotonic() < deadline:
            phrase = agent_mod.fetch_warranty_verdict_phrase(_TEST_VIN)
            if phrase is not None:
                break
            time.sleep(_GSI_POLL_S)

        assert phrase == DMS_VERDICT_DENIED, (
            f"the fetch helper returned {phrase!r} against a real seeded verdict"
        )

        result = agent_mod.run_health_agent(
            _TEST_VIN, [{"health_pct": 92.0, "age_days": 400, "vin": _TEST_VIN}]
        )
    finally:
        agent_mod._dms_ddb_singleton = original

    finding = result["finding"]
    assert DMS_VERDICT_DENIED in finding, (
        "the real DMS verdict did not reach the narration verbatim"
    )
    for softening in ("declined", "may require review", "under review"):
        assert softening not in DMS_VERDICT_ENUM  # anti-vacuity
        assert softening not in finding, f"verdict softened to {softening!r}"


# ---------------------------------------------------------------------------
# Anti-vacuity partners
# ---------------------------------------------------------------------------


def test_unseeded_vin_returns_none_against_the_real_tables(ddb_client):
    """Proves the positive tests measure the seeded data and not some blanket behaviour.

    Without this, a `fetch_verdict` that returned a verdict for *any* input would pass every
    assertion above. Uses a VIN that has no repair order, so hop 1 legitimately finds nothing.
    """
    absent_vin = f"LIVETEST{uuid.uuid4().hex[:10].upper()}"
    assert fetch_verdict(absent_vin, ddb_client=ddb_client) is None


def test_the_300_existing_staging_claims_still_carry_no_verdict(ddb_client, table_names):
    """Pins finding F5, live.

    If this ever fails, staging has gained real verdict data and this whole file should be
    re-pointed at it rather than seeding — which would close the one gap this test documents
    (that it verifies the read path, not that DMS writes the shape). A failure here is good
    news and an instruction, not a defect.
    """
    paginator = ddb_client.get_paginator("scan")
    with_verdict = 0
    total = 0
    for page in paginator.paginate(
        TableName=table_names["claims"],
        ProjectionExpression="claim_id,verdict_status",
    ):
        for item in page.get("Items", []):
            # Skip this suite's own in-flight fixtures.
            if item.get("claim_id", {}).get("S", "").startswith(_PREFIX):
                continue
            total += 1
            if "verdict_status" in item:
                with_verdict += 1

    assert total > 0, "scanned zero non-test claims — the scan is not reaching real data"
    assert with_verdict == 0, (
        f"{with_verdict} of {total} staging claims now carry verdict_status. Finding F5 is "
        "stale: re-point this file at real data and drop the seeding, then update "
        "decisions.md and the task-4 note in tasks.md."
    )


def test_seeded_shape_matches_the_dms_producer(ddb_client, table_names, seeded_claim):
    """Guards the one thing this file cannot otherwise prove.

    The fixture stands in for DMS's `validate_warranty_claim`. If the fixture drifts from what
    that handler actually writes, this test verifies a shape nothing produces — which would be
    worse than not testing, because it would read as live confirmation.
    """
    _, claim_id, _, _ = seeded_claim
    item = ddb_client.get_item(
        TableName=table_names["claims"], Key={"claim_id": {"S": claim_id}}
    ).get("Item")

    assert item is not None, "the seeded claim is not readable on the base table"
    missing = _PRODUCER_VERDICT_ATTRS - set(item)
    assert not missing, (
        f"seeded claim is missing producer-written attribute(s) {sorted(missing)} — the "
        "fixture has drifted from warranty_claims.py:436-452"
    )
    # The lifecycle status is `Validated` and is NOT the verdict — the distinction this
    # module's docstring calls out, asserted against a real row.
    assert item["status"]["S"] == "Validated"
    assert item["verdict_status"]["S"] != item["status"]["S"]
