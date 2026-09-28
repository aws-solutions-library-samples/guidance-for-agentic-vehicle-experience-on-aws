"""
DynamoDB artifact writer for the Tier 2 platform.

**Addendum D2 (2026-09-18)** — the cross-run idempotency contract changed:

Old contract (superseded):
    ``put()`` used a conditional ``PutItem`` with
    ``attribute_not_exists(inputs_hash)``.  A re-run over unchanged ADP data
    (same ``inputs_hash``) was a silent no-op.  This was spec § D4 / M6.

New contract (D2):
    Cross-run idempotency is now handled by ``write_finding()`` — a shared
    helper in ``agents/tier2/writer.py``.  ``write_finding()`` queries the
    ``finding_key`` GSI to find any live row before deciding whether to call
    ``put()`` (new condition → PutItem) or ``UpdateItem`` (persistent condition
    → in-place update).  Retry-safety WITHIN a single execution attempt is also
    handled there: if ``inputs_hash`` is unchanged the call returns early without
    touching ``put()``.

    This class exposes ``write_finding()`` as an instance method that delegates
    to the module-level function so callers (including ``runner.py``) can route
    through a single writer interface.

    Consequence for ``ArtifactWriter.put()``:
    ``put()`` is now unconditional.  By the time ``write_finding()`` calls it,
    the decision to write a new row has already been made.  No
    ``ConditionExpression`` is set.

    The old ``put_raw()`` method (unconditional write) is retained for cursor-state
    writes and back-fill operations; it is semantically identical to ``put()``.

Region awareness
----------------
The table name and DynamoDB client are both injected at construction time — the
writer is region-agnostic.  Satisfies ``~/.kiro/steering/cross-region-namespace.md``.

Usage
-----
::

    import boto3
    from agents.tier2.artifact_writer import ArtifactWriter

    client = boto3.client("dynamodb", region_name="us-west-2")
    writer = ArtifactWriter(table_name="vsa-staging-health-findings", dynamodb_client=client)

    # D2 path (normal operation — runner calls this):
    result = writer.write_finding(finding)   # D2 algorithm: GSI query → UpdateItem or PutItem

    # Raw unconditional write (cursor state, back-fill):
    writer.put_raw(item)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

_serializer = TypeSerializer()


def _coerce_floats(obj: Any) -> Any:
    """Recursively convert Python floats to Decimal.

    boto3's ``TypeSerializer`` rejects Python ``float`` values and requires
    ``Decimal`` instead (DynamoDB's Number type has arbitrary precision).
    This helper walks the artifact dict tree and converts every ``float`` it
    encounters.  All other types are returned unchanged.

    The conversion uses ``str(v)`` as the Decimal source to avoid the
    floating-point representation noise that ``Decimal(v)`` would introduce
    (e.g. ``Decimal(92.0)`` → ``Decimal('92.0000...0001')``).
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _coerce_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_floats(item) for item in obj]
    return obj


def _to_ddb_item(artifact: dict[str, Any]) -> dict[str, Any]:
    """Convert a plain-Python artifact dict to a DynamoDB AttributeValue map.

    boto3's low-level DynamoDB client requires ``{"field": {"S": "value"}}``
    format rather than plain ``{"field": "value"}``.  ``TypeSerializer.serialize``
    handles every Python type the artifact contract can carry (str, int, Decimal,
    list, dict, bool, None → NULL).

    Float values are pre-coerced to Decimal to satisfy TypeSerializer's type
    constraint.
    """
    coerced = _coerce_floats(artifact)
    return {k: _serializer.serialize(v) for k, v in coerced.items()}


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


#: Cross-run idempotency condition for the LEGACY Agent 2 artifact path
#: (spec § D4 / M6).  Restored 2026-09-18 after a live deploy showed Addendum
#: D2 supersedes it only on the AVX Findings table, not on this one --
#: see issues/2026-09-18-d2-writer-wired-before-its-table-exists/.
_CONDITION_NOT_EXISTS = "attribute_not_exists(inputs_hash)"


class ArtifactWriter:
    """Writes artifacts to DynamoDB using the D2 in-place-update algorithm.

    The primary entry point for ``runner.py`` is :meth:`write_finding`, which
    delegates to the shared module-level helper in ``agents.tier2.writer``.
    That helper implements the four-step D2 algorithm:

    1. Compute ``finding_key`` (already on the Finding dict).
    2. Query the ``finding_key`` GSI for a live row.
    3. If live row exists → ``UpdateItem`` (preserve ``finding_id``).
    4. If no live row → ``PutItem`` (new ``finding_id``).

    Under D2, ``put()`` is unconditional (no ``ConditionExpression``).
    All idempotency decisions are made upstream in :meth:`write_finding`.

    Parameters
    ----------
    table_name:
        The DynamoDB table to write to.  Must come from config/env; never
        hardcoded at the call site.
    dynamodb_client:
        A boto3 low-level DynamoDB client (``boto3.client("dynamodb", ...)``)
        with appropriate permissions.  Injected so the caller controls the
        region and the tests can use botocore Stubber without network calls.
    """

    def __init__(self, table_name: str, dynamodb_client: Any) -> None:
        self._table_name = table_name
        self._client = dynamodb_client

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def table_name(self) -> str:
        """The table name this writer targets."""
        return self._table_name

    @property
    def dynamodb_client(self) -> Any:
        """The injected DynamoDB client."""
        return self._client

    def write_finding(
        self,
        finding: dict[str, Any],
        *,
        utc_now_fn: Any = None,
        standing: Any = None,
        refresh_on_noop: bool = False,
    ) -> dict[str, Any]:
        """Write a Finding using the D2 in-place-update algorithm.

        Delegates to ``agents.tier2.writer.write_finding()``.  This is the
        **primary write path** for ``runner.py`` — both the deterministic
        fallback (``run_health_agent``) and the Strands-backed production path
        (``make_strands_agent_fn``) route artifact writes here.

        Parameters
        ----------
        finding:
            Plain-Python dict conforming to the AVX Finding schema (with
            ``finding_key``, ``finding_id``, ``scope``, ``agent_id``, etc.).
        utc_now_fn:
            Optional callable for deterministic timestamps in tests.
        standing:
            Resolved ``StandingFacts``, forwarded verbatim to
            ``writer.write_finding``, which projects its fields as top-level
            ``owner_id`` / ``fleet_id`` / ``vehicle_id`` attributes on the
            persisted DDB item for VIN-scoped Findings only.  ``None`` means no
            standing is provable (fail-closed).

        Returns
        -------
        dict
            ``{"action": "put" | "update" | "noop", "finding_id": str}``
        """
        from agents.tier2.writer import write_finding as _write_finding  # lazy import

        return _write_finding(
            finding,
            standing=standing,
            table_name=self._table_name,
            dynamodb_client=self._client,
            utc_now_fn=utc_now_fn,
            refresh_on_noop=refresh_on_noop,
        )

    def put(self, artifact: dict[str, Any]) -> bool:
        """Conditionally write *artifact* to DynamoDB; the LEGACY Agent 2 path.

        Uses ``ConditionExpression = attribute_not_exists(inputs_hash)`` (spec
        § D4 / M6) so a re-run over unchanged ADP data does not create a second
        row. On ``ConditionalCheckFailedException`` the call is a silent no-op
        returning ``False``; all other ``ClientError`` subclasses propagate so
        the scheduler can apply its retry policy.

        **Why this is still conditional after Addendum D2.** An earlier revision
        of this method made it unconditional, on the reasoning that D2's
        :meth:`write_finding` decides put-vs-update before reaching it. That is
        true *for an AVX Finding*, and false for this method's actual caller. A
        live staging deploy on 2026-09-18 established the difference — see
        ``issues/2026-09-18-d2-writer-wired-before-its-table-exists/``:

        * ``runner.py`` writes Agent 2 artifacts to ``HEALTH_FINDINGS_TABLE``
          (``vsa-{stage}-health-findings``), keyed on ``pk``/``sk``, with no GSIs.
        * :meth:`write_finding` Queries a ``by_finding_key`` GSI and PutItems
          an AVX Finding keyed on ``finding_id``.

        Those are two different artifacts, so D2 does not supersede this
        method's idempotency — it supersedes it only on the AVX Findings table
        that Group 3 creates. Removing the condition here dropped the legacy
        table's only cross-run idempotency guarantee, which is exactly the M6
        property ``~/.kiro/steering/agentic-tiers.md`` records as having been
        untrue once before, and caught then only by querying a live service.

        :meth:`write_finding` remains available and unchanged for Group 3 to wire
        against the AVX table. It has no caller in the deployed path today, and
        that is deliberate rather than an oversight.
        """
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_to_ddb_item(artifact),
                ConditionExpression=_CONDITION_NOT_EXISTS,
            )
            return True
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code == "ConditionalCheckFailedException":
                # Idempotent no-op: a row with this inputs_hash already exists.
                return False
            raise

    def put_raw(self, item: dict[str, Any]) -> None:
        """Unconditionally write *item* to DynamoDB.

        Semantically identical to ``put()`` under D2.  Retained as a separate
        method for cursor-state writes and back-fill operations where the
        caller intent differs conceptually from a new-Finding write.

        Parameters
        ----------
        item:
            Plain-Python dict.  Any existing item with the same primary key will
            be silently overwritten.
        """
        self._client.put_item(
            TableName=self._table_name,
            Item=_to_ddb_item(item),
        )
