"""Set a tenant's `(tenantId, "current")` pointer without undoing an operator's pin.

Shared by `scripts/seed-personas.py` and `scripts/seed-meridian-fleet.py`, which run on every
`make deploy-staging` (via `seed-staging`). Each used to `put_item` the pointer unconditionally
with the seed's own version, so every deploy moved every tenant off whatever version an
operator had activated by hand. In staging on 2026-10-01 that was all six seeded tenants:
meridian on `1.1.1` (`autoOpenHumanChatOnP0` off, a user decision), the rest on `1.1.0`. See
issues/2026-10-01-tenant-seed-repoints-current/.

Rule: the pointer is the operator's once it targets a version row that exists. The seed only
writes it when it is absent (fresh account) or dangling (targets a row that is not there, which
no reader can use). Both writes are conditional, so a concurrent operator flip wins.

Deliberately NOT "repoint when the pointer is older than the seeded version": the next seed
bump would then move meridian off `1.2.1` and drop the P0 callback, the same revert deferred.
Activating a new seeded version on an existing account is a deliberate step; the notice
printed below gives the command.
"""
from __future__ import annotations

import json
import shlex
from typing import Callable

from botocore.exceptions import ClientError

CURRENT = "current"

# Outcomes, returned so callers and tests can tell the paths apart.
CREATED = "created"          # no pointer existed; created at the seeded version
UNCHANGED = "unchanged"      # pointer already targets the seeded version
PRESERVED = "preserved"      # pointer targets another existing version; left alone
REPAIRED = "repaired"        # pointer was dangling; repointed to the seeded version
LOST_RACE = "lost-race"      # a concurrent writer changed it between read and write


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def set_current_pointer(
    table,
    tenant_id: str,
    seeded_version: str,
    *,
    table_name: str = "<tenant-config table>",
    region: str = "<region>",
    log: Callable[[str], None] = print,
) -> str:
    """Point `current` at `seeded_version` only if nothing usable is there.

    Call AFTER writing the `seeded_version` row, so the dangling check below sees it.
    Returns one of the outcome constants above. Any non-conditional DynamoDB error propagates:
    a seeder that cannot read the pointer must not guess.
    """
    key = {"tenantId": tenant_id, "version": CURRENT}
    pointer = table.get_item(Key=key, ConsistentRead=True).get("Item")

    if not pointer or not pointer.get("activeVersion"):
        # A pointer row with no usable activeVersion (absent, or an empty string) is as
        # unusable as no row; readers KeyError on it. The condition pins the state we read,
        # so a concurrent operator write still wins.
        if not pointer:
            put_kwargs = {"ConditionExpression": "attribute_not_exists(tenantId)"}
        elif "activeVersion" not in pointer:
            put_kwargs = {"ConditionExpression": "attribute_not_exists(activeVersion)"}
        else:
            put_kwargs = {
                "ConditionExpression": "activeVersion = :observed",
                "ExpressionAttributeValues": {":observed": pointer["activeVersion"]},
            }
        try:
            table.put_item(Item={**key, "activeVersion": seeded_version}, **put_kwargs)
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            log(f"  current: {tenant_id} pointer was written concurrently — left as is")
            return LOST_RACE
        log(f"  current: {tenant_id} -> {seeded_version} (no usable pointer existed)")
        return CREATED

    active = pointer["activeVersion"]
    if active == seeded_version:
        return UNCHANGED

    target = table.get_item(
        Key={"tenantId": tenant_id, "version": active}, ConsistentRead=True
    ).get("Item")
    if target:
        # The printed command is conditional on the pin still being `active`, so pasting it
        # later cannot overwrite a different version someone has activated since.
        # shlex.quote + json.dumps: `active` comes from the table and is operator-written, so
        # it is quoted for the shell and escaped for JSON rather than spliced in raw.
        item = json.dumps({"tenantId": {"S": tenant_id}, "version": {"S": CURRENT},
                           "activeVersion": {"S": seeded_version}}, separators=(",", ":"))
        values = json.dumps({":a": {"S": active}}, separators=(",", ":"))
        log(
            f"  current: {tenant_id} stays on {active!r} (set outside this seed); "
            f"seeded {seeded_version} was written but NOT activated.\n"
            f"    Only if you mean to replace {active!r} with {seeded_version}:\n"
            f"    aws dynamodb put-item --table-name {shlex.quote(table_name)} "
            f"--region {shlex.quote(region)} --item {shlex.quote(item)} "
            f"--condition-expression 'activeVersion = :a' "
            f"--expression-attribute-values {shlex.quote(values)}"
        )
        return PRESERVED

    try:
        table.put_item(
            Item={**key, "activeVersion": seeded_version},
            ConditionExpression="activeVersion = :observed",
            ExpressionAttributeValues={":observed": active},
        )
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise
        log(f"  current: {tenant_id} pointer changed while repairing — left as is")
        return LOST_RACE
    log(
        f"  current: {tenant_id} pointed at missing version {active}; "
        f"repointed to {seeded_version}"
    )
    return REPAIRED
