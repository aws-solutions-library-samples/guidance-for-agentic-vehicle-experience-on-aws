"""Grounding reader factory for the FI fleet-health agent (spec D9 / D8 step 3).

Wraps Stage C's ``check_grounded`` function from
``agents.fi_recall_warranty.selection`` into a callable suitable for injection
into the fleet-health loop.

Public API
----------
make_groundings_reader(ddb_client, groundings_table, vehicle_id=None) -> Callable[[], tuple[bool, int]]

The returned reader returns ``(is_grounded, error_count)`` matching Stage C's
``check_grounded`` signature exactly:
  - ``is_grounded``: True when an open grounding exists (vehicle already grounded).
  - ``error_count``: 1 on any read exception, 0 on success.

This surfaces the error count for the loop's ``grounding_read_errors`` stat
(defect fix: the previous version dropped it by returning only the bool).

Fail-closed: on any exception is_grounded is False (not grounded → grounding
item is written; CMS POST is idempotent, D8 step 3).

Mutation guard (TestRealGroundingsReader.test_real_groundings_reader_client_error_is_not_grounded):
    A reader that returns True on ClientError would suppress the grounding item;
    the test would fail on ``"grounding" not in result.items``.

Fixture contract (TestRealGroundingsReader — W15 fix):
    The test must mock ``ddb_client.query`` (not ``Table.get_item``), because
    ``check_grounded`` uses ``ddb_client.query()``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from agents.fi_recall_warranty.selection import check_grounded

log = logging.getLogger(__name__)


def make_groundings_reader(
    ddb_client: Any,
    groundings_table: str = "cms-staging-storage-vehicle-groundings",
    vehicle_id: Optional[str] = None,
) -> Callable[[], tuple[bool, int]]:
    """Return a callable that checks whether a vehicle is grounded.

    Parameters
    ----------
    ddb_client:
        boto3 DynamoDB **client** (low-level, not resource).  Tests inject a
        MagicMock; the real runner injects the live client.
    groundings_table:
        CMS groundings table name.
    vehicle_id:
        The vehicle's CMS vehicleId (pre-resolved from the VIN by the caller).
        When None, the reader returns (False, 0).

    Returns
    -------
    Callable[[], tuple[bool, int]]
        A zero-arg closure.  Returns ``(is_grounded, error_count)`` matching
        Stage C's ``check_grounded`` return shape so the loop can accumulate
        ``grounding_read_errors`` correctly.

    Mutation guard:
        Returning (True, 1) on exception → grounding item suppressed → test fails.
        Returning (is_grounded, 0) on exception → error_count silently dropped → stat test fails.
    """
    def _reader() -> tuple[bool, int]:
        if vehicle_id is None:
            # No vehicleId resolved: cannot check → treat as not grounded.
            return False, 0
        # Delegate entirely to Stage C's check_grounded which already handles
        # all exception cases and returns (bool, error_count).
        return check_grounded(
            vehicle_id=vehicle_id,
            ddb_client=ddb_client,
            groundings_table=groundings_table,
        )

    return _reader
