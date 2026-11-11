"""Count a health source's managed pool (stage D U7 fallback record).

Selection rotates a component's managed cursor when the managed pool is larger than the nightly
cap; U7 allows that only as a 7-day fallback that the run states in evidence. To state it, the run
needs the pool size and where its own slice starts. This module builds that count from the
source's OWN managed-pool SQL (``_build_select_managed_vins_sql(cursor=None, ...)``), so the count
and the selection can't use different predicates, and parses the one-row answer.

SQL stays inside ``agents/health`` (the selection layer sees only numbers).
"""

from __future__ import annotations

from typing import Any, Optional

from agents.health._vin import sanitise_vin

__all__ = ["COUNT_LIMIT", "COUNT_MAX_POLLS", "COUNT_MAX_QUEUE_SECONDS", "build_count_sql",
           "parse_count"]

#: LIMIT passed to the source's managed-pool SQL when it is wrapped for counting. Managed pools
#: are thousands of vehicles; a count that reaches this limit can't be told from a truncated one,
#: so :func:`parse_count` refuses it.
COUNT_LIMIT = 10_000_000

#: The count is optional evidence spending the run's 600 s wall budget, so it waits less than the
#: run's essential queries do: at most this long QUEUED, and at most COUNT_MAX_POLLS polls once
#: running. A count that times out records the run's coverage as unmeasured (review cycle 1 S1).
COUNT_MAX_QUEUE_SECONDS = 30.0
COUNT_MAX_POLLS = 30


def build_count_sql(managed_pool_sql: str, *, after: Optional[str]) -> str:
    """Wrap *managed_pool_sql* (one ``vin`` column) in a count of the pool and of VINs ``<= after``.

    Returns one row: ``managed_pool`` (pool size) and ``at_or_before`` (managed VINs that sort at or
    before *after*; 0 when *after* is None). The slice that starts after the cursor *after* begins
    at position ``at_or_before + 1`` in VIN order.

    Raises ``ValueError`` when *after* isn't a VIN: sanitisation must not drop a character, since a
    shortened cursor would put the slice in the wrong place.
    """
    if after is None:
        before_expr = "0"
    else:
        safe = sanitise_vin(after)
        if safe != after.upper():
            raise ValueError("managed cursor is not a VIN")
        before_expr = f"COALESCE(SUM(CASE WHEN vin <= '{safe}' THEN 1 ELSE 0 END), 0)"
    return (
        f"SELECT COUNT(*) AS managed_pool, {before_expr} AS at_or_before"
        f" FROM ({managed_pool_sql}) AS managed_pool_vins"
    )


def _as_count(value: Any) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("managed pool count is not a non-negative integer")
    return int(value)


def parse_count(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Return ``(managed_pool, at_or_before)`` from the count query's rows, or raise ``ValueError``."""
    if len(rows) != 1:
        raise ValueError(f"managed pool count returned {len(rows)} rows, expected 1")
    row = rows[0]
    pool = _as_count(row.get("managed_pool"))
    at_or_before = _as_count(row.get("at_or_before"))
    if pool >= COUNT_LIMIT:
        raise ValueError("managed pool count reached the count limit")
    return pool, at_or_before
