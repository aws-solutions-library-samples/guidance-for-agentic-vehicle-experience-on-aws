"""Live integration test for ledger.py — requires ``LIVE_TESTS=1``.

Run manually after deploy (the architect runs this before closing Group 11-1):

    LIVE_TESTS=1 .venv/bin/python -m pytest -p no:cacheprovider -v \
        agents/fi_sim_operator/tests/test_ledger_live.py

The test uses table ``vsa-staging-avx-sim-operator`` by default; override with
``AVX_SIM_OPERATOR_TABLE_LIVE``.  It writes only on the scratch key
``CAP#__live-test__`` / <today> and ``SEEN#__live-test__`` / ``__live-fid__``,
and DELETES them in the ``finally`` block — production fleet keys are never touched.
"""
from __future__ import annotations

import os
import datetime
import pytest
import boto3

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("LIVE_TESTS") != "1",
        reason="Set LIVE_TESTS=1 to run live integration tests",
    ),
    pytest.mark.live_aws,
]

_LIVE_TABLE = os.environ.get(
    "AVX_SIM_OPERATOR_TABLE_LIVE", "vsa-staging-avx-sim-operator"
)
_LIVE_FLEET = "__live-test__"
_LIVE_FID = "__live-fid__"
_TODAY = datetime.date.today().isoformat()
_CAP = 3


@pytest.fixture(scope="module")
def live_ddb():
    """Real boto3 client against the staging table.

    Region defaults to the environment; set AWS_DEFAULT_REGION if needed.
    """
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    return boto3.client("dynamodb", region_name=region)


def _delete_scratch_items(client):
    """Remove the scratch CAP and SEEN items written by this test run."""
    cap_pk = f"CAP#{_LIVE_FLEET}"
    seen_pk = f"SEEN#{_LIVE_FLEET}"
    for pk, sk in [(cap_pk, _TODAY), (seen_pk, _LIVE_FID)]:
        try:
            client.delete_item(
                TableName=_LIVE_TABLE,
                Key={"pk": {"S": pk}, "sk": {"S": sk}},
            )
        except Exception:  # noqa: BLE001
            pass  # Best-effort cleanup; item might not exist


class TestLiveLedger:
    """Live smoke-test against the real DynamoDB table.

    These tests verify the reserve_cap condition expression is accepted by the
    real DynamoDB service (not just by moto).  They also verify record_first_seen
    creates and preserves items correctly.
    """

    def test_reserve_and_refuse_and_read_back(self, live_ddb):
        """Full reserve/refuse/read-back cycle on the real table."""
        from agents.fi_sim_operator.ledger import (
            reserve_cap,
            get_today_cap_used,
            record_first_seen,
            get_first_seen_times,
        )
        try:
            # 1. Reserve 2 of 3 — must succeed
            ok1 = reserve_cap(live_ddb, _LIVE_TABLE, _LIVE_FLEET, _TODAY, n=2, cap=_CAP)
            assert ok1 is True, "First reserve (2 of 3) must succeed on real DynamoDB"

            # 2. Refuse the next 2 (only 1 slot left)
            refused = reserve_cap(live_ddb, _LIVE_TABLE, _LIVE_FLEET, _TODAY, n=2, cap=_CAP)
            assert refused is False, "Second n=2 reserve must be refused (only 1 slot left)"

            # 3. Count is still 2 (refused call did not increment)
            count = get_today_cap_used(live_ddb, _LIVE_TABLE, _LIVE_FLEET, _TODAY)
            assert count == 2, f"Count must still be 2 after refused reserve; got {count}"

            # 4. record_first_seen: first write returns True
            run_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            new = record_first_seen(live_ddb, _LIVE_TABLE, _LIVE_FLEET, _LIVE_FID, run_at)
            assert new is True, "record_first_seen must return True for a new item"

            # 5. Second write returns False (original wins)
            dup = record_first_seen(
                live_ddb, _LIVE_TABLE, _LIVE_FLEET, _LIVE_FID, "different-timestamp"
            )
            assert dup is False, "record_first_seen must return False for duplicate key"

            # 6. Read back: original timestamp preserved
            times = get_first_seen_times(
                live_ddb, _LIVE_TABLE, _LIVE_FLEET, [_LIVE_FID]
            )
            assert times.get(_LIVE_FID) == run_at, (
                f"first_seen_at must be the original timestamp; got {times.get(_LIVE_FID)}"
            )
        finally:
            _delete_scratch_items(live_ddb)
