"""Ledger and cap management for the simulated fleet operator (spec D10c/D10d).

Uses DynamoDB table ``vsa-{stage}-avx-sim-operator`` (env var
``AVX_SIM_OPERATOR_TABLE``):

  SEEN items   pk = ``SEEN#<fleet_id>``  sk = ``<finding_id>``
               attrs: ``first_seen_at`` (ISO UTC str), ``ttl`` (epoch-s, 10 days)
  CAP items    pk = ``CAP#<fleet_id>``   sk = ``<YYYY-MM-DD>``
               attrs: ``count`` (Number)

Design constraints (spec D10c/D10d):
- ``record_first_seen``: conditional put — the first write wins (idempotent re-runs).
- ``reserve_cap``: conditional add — ``count + n <= cap``; lost condition = fleet is
  at cap for today.
- The cap is reserved BEFORE any Action is written. A slot whose Action then fails
  to write stays spent (spec D10d: "never above the cap").  Mutation guard L1.
- A pair's two slots are reserved together before either Action is written.

Public API
----------
record_first_seen(ddb_client, table_name, fleet_id, finding_id, first_seen_at)
    -> bool (True = new insert, False = already existed)
reserve_cap(ddb_client, table_name, fleet_id, today, n, cap)
    -> bool (True = reserved, False = cap exceeded)
get_first_seen_times(ddb_client, table_name, fleet_id, finding_ids)
    -> dict[finding_id, first_seen_at]
get_today_cap_used(ddb_client, table_name, fleet_id, today)
    -> int
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

#: TTL in seconds for SEEN items: 10 days.
_SEEN_TTL_S: int = 10 * 24 * 3600


def _seen_pk(fleet_id: str) -> str:
    return f"SEEN#{fleet_id}"


def _cap_pk(fleet_id: str) -> str:
    return f"CAP#{fleet_id}"


def record_first_seen(
    ddb_client: Any,
    table_name: str,
    fleet_id: str,
    finding_id: str,
    first_seen_at: str,
) -> bool:
    """Conditionally write the first-seen record for a Finding.

    Returns ``True`` when the item was newly written, ``False`` when it already
    existed (another run beat us to it — the original timestamp stands).

    The conditional put ensures that if two runs overlap, the earlier run's
    ``first_seen_at`` wins.  A stale item being recreated after TTL expiry is also
    fine: 10 days after the Finding first appeared it will be seen as new.

    Mutation guard L1 (reserve after writing): if reserve_cap were called after
    ``record_first_seen`` and the Action write, a race between two runs on the same
    finding_id could produce two decisions on the same Finding.  The cap is reserved
    first (reserve_cap) and the SEEN record is written here only once the cap slot
    is held.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    ttl = int(time.time()) + _SEEN_TTL_S
    try:
        ddb_client.put_item(
            TableName=table_name,
            Item={
                "pk": {"S": _seen_pk(fleet_id)},
                "sk": {"S": finding_id},
                "first_seen_at": {"S": first_seen_at},
                "ttl": {"N": str(ttl)},
            },
            ConditionExpression="attribute_not_exists(sk)",
        )
        return True
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return False
        raise


def reserve_cap(
    ddb_client: Any,
    table_name: str,
    fleet_id: str,
    today: str,
    n: int,
    cap: int,
) -> bool:
    """Atomically reserve ``n`` cap slots for ``fleet_id`` on ``today``.

    Uses a conditional update ``count + n <= cap`` on the ``CAP`` item.  If the
    item does not exist it is created with ``count = n`` (atomically).

    Returns ``True`` when the slots were reserved, ``False`` when the cap is
    already at or above ``cap - n`` (fleet is done for the day).

    Mutation guard L1: this MUST be called BEFORE writing any Action.  An Action
    that writes after a failed reserve_cap would exceed the cap silently.
    Mutation guard L4: this does NOT check whether a person's decision already
    exists — that check is done by the conditional put on the Action row itself.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    if n < 1:
        raise ValueError(f"reserve_cap: n must be >= 1, got {n}")
    if n > cap:
        # No matter what the current count is, reserving n > cap can never succeed.
        return False

    # ConditionExpression: attribute_not_exists(#c) OR #c <= :max
    # where :max = cap - n.  This is a valid DDB condition expression (unlike
    # if_not_exists(...) + :n <= :cap which uses an UpdateExpression function in
    # a condition — that form raises ValidationException on the real service and
    # moto 5.2.3).  Semantics: allow the update iff the pre-update count is 0 or
    # absent (attribute_not_exists), or the pre-update count leaves room for n
    # more slots (count <= cap - n → post-update count = count + n <= cap).
    try:
        ddb_client.update_item(
            TableName=table_name,
            Key={
                "pk": {"S": _cap_pk(fleet_id)},
                "sk": {"S": today},
            },
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :n",
            ConditionExpression="attribute_not_exists(#c) OR #c <= :max",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={
                ":n": {"N": str(n)},
                ":zero": {"N": "0"},
                ":max": {"N": str(cap - n)},
            },
        )
        return True
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return False
        raise


def get_first_seen_times(
    ddb_client: Any,
    table_name: str,
    fleet_id: str,
    finding_ids: list[str],
) -> dict[str, str]:
    """Return a mapping ``{finding_id: first_seen_at}`` for all known findings.

    Uses ``Query`` on the SEEN partition for the fleet; findings not in the result
    are absent from the returned dict (caller treats them as too_new).

    Reads at most 100 pages of 100 items each for safety; a fleet with more than
    10,000 candidates in the ledger would be batched (practically unreachable at
    20/day/fleet with 10-day TTL).
    """
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415

    pk = _seen_pk(fleet_id)
    result: dict[str, str] = {}
    finding_set = set(finding_ids)

    last_key = None
    for _ in range(100):
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ProjectionExpression": "sk, first_seen_at",
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = ddb_client.query(**kwargs)
        for item in resp.get("Items", []):
            fid = item.get("sk", {}).get("S")
            fs_at = item.get("first_seen_at", {}).get("S")
            if fid and fs_at and fid in finding_set:
                result[fid] = fs_at
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    return result


def get_today_cap_used(
    ddb_client: Any,
    table_name: str,
    fleet_id: str,
    today: str,
) -> int:
    """Return the number of cap slots already used for ``fleet_id`` today.

    Returns 0 when no CAP item exists yet (first run of the day).
    """
    resp = ddb_client.get_item(
        TableName=table_name,
        Key={
            "pk": {"S": _cap_pk(fleet_id)},
            "sk": {"S": today},
        },
        ConsistentRead=True,
    )
    item = resp.get("Item", {})
    raw = item.get("count", {}).get("N")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
