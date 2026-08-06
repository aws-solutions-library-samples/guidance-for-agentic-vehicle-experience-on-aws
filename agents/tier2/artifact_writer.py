"""
Idempotent DynamoDB artifact writer for the Tier 2 platform health agent.

Implements spec § D4 / M6: a re-run over unchanged ADP data (same ``inputs_hash``)
must not create a second row and must not raise.  The writer performs a conditional
``PutItem`` keyed on ``attribute_not_exists(inputs_hash)``; if the item already exists
(``ConditionalCheckFailedException``), the call is silently treated as a no-op.  All
other errors propagate to the caller.

Region awareness
----------------
The table name is injected from config/env at construction time — never hardcoded here.
The DynamoDB client is also injected, which means the caller controls both the region
and the endpoint.  This satisfies ``~/.kiro/steering/cross-region-namespace.md``:
the writer itself is region-agnostic; the deployment wires the correct regional client.

Usage
-----
::

    import boto3
    from agents.tier2.artifact_writer import ArtifactWriter

    client = boto3.client("dynamodb", region_name="us-west-2")
    writer = ArtifactWriter(table_name="vsa-staging-health-findings", dynamodb_client=client)

    writer.put(artifact)       # conditional — idempotent
    writer.put_raw(artifact)   # unconditional — for back-fill and tests only
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

    boto3's low-level DynamoDB client (used by the writer and the tests) requires
    ``{"field": {"S": "value"}}`` format rather than plain ``{"field": "value"}``.
    ``TypeSerializer.serialize`` handles every Python type the artifact contract
    can carry (str, int, Decimal, list, dict, bool, None → NULL).

    Float values are pre-coerced to Decimal to satisfy TypeSerializer's type
    constraint — DynamoDB's Number type has arbitrary precision and does not
    accept IEEE-754 floats natively.
    """
    coerced = _coerce_floats(artifact)
    return {k: _serializer.serialize(v) for k, v in coerced.items()}


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_CONDITION_NOT_EXISTS = "attribute_not_exists(inputs_hash)"


class ArtifactWriter:
    """Writes health-finding artifacts to DynamoDB with M6 idempotency.

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

    def put(self, artifact: dict[str, Any]) -> bool:
        """Conditionally write *artifact* to DynamoDB.

        The condition ``attribute_not_exists(inputs_hash)`` ensures that a
        re-run over unchanged ADP data (same ``inputs_hash``) does not produce
        a second row.  If the item already exists, ``ConditionalCheckFailedException``
        is swallowed silently — the existing row is the canonical record.

        All other ``ClientError`` subclasses (e.g. throttling, capacity errors,
        missing permissions) propagate unchanged so the scheduler can apply its
        retry policy.

        Parameters
        ----------
        artifact:
            Plain-Python dict conforming to the spec § D4 contract.  Must carry
            an ``inputs_hash`` key (SHA-256 hex string) for the condition to be
            meaningful.

        Returns
        -------
        bool
            ``True`` if a new row was written; ``False`` if the write was a
            silent idempotent no-op (item with this ``inputs_hash`` already
            existed).  The caller uses this to distinguish new findings from
            duplicate-skips in the observability run record.
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
                # Idempotent no-op: the item with this inputs_hash already
                # exists.  A re-run over unchanged data is not an error.
                return False
            # All other errors (throttling, capacity, auth, …) propagate.
            raise

    def put_raw(self, item: dict[str, Any]) -> None:
        """Unconditionally write *item* to DynamoDB, with no condition expression.

        Intended for back-fill operations and tests only.  In normal operation
        use :meth:`put` so idempotency is preserved.

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
