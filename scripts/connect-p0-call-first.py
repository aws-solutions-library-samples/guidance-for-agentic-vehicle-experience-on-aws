#!/usr/bin/env python3
"""Connect provisioning script: P0 call-first queue setup.

Creates the resources described in
issues/2026-10-05-p0-callback-waits-behind-chat/report.md § Fix (scope contract):

  1. Create queue `meridian-fleet-support-p0-chat` (same hours-of-operation and
     outbound config as `meridian-fleet-support`).
  2. Associate it with routing profile `meridian-fleet-support-agents` on CHAT,
     same priority as the existing CHAT entry, Delay = --delay-seconds (default 60).
  3. Create flow `meridian-vsa-escalation-flow-after-callback` from the content of
     the existing chat flow, with the working-queue ARN swapped to the new queue
     (refuse unless the old ARN occurs exactly where expected).
  4. Print (not write) the tenant-config change needed
     (new escalation.callback.chatContactFlowId pointing at the new flow).

Dry run by default: only Describe/List/Get/Search calls are allowed, enforced by a
before-call hook registered on the botocore session BEFORE any client is created.
Pass --apply to write.

Usage:
    python3 scripts/connect-p0-call-first.py --account <id> --instance <id> [--delay-seconds 60]
    python3 scripts/connect-p0-call-first.py --account <id> --instance <id> --apply
    python3 scripts/connect-p0-call-first.py --account <id> --instance <id> --rollback ...
        --new-queue-id <id> --new-flow-id <id>

Identity guard: refuses unless STS reports the account given with --account, and
the instance given with --instance exists in it. There is no bypass flag.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import boto3
import botocore.session as _botocore_session

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    stream=sys.stderr,
)

# ---------------------------------------------------------------------------
# Stated identity pair — refuse unless the caller matches exactly.
# The operator states the account (--account) and the instance (--instance) on
# the command line; the script refuses unless STS reports that account and the
# instance resolves in it. No account id is kept in this file: it ships in the
# public mirror.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Connect resource names
# ---------------------------------------------------------------------------
SOURCE_QUEUE_NAME = "meridian-fleet-support"
NEW_QUEUE_NAME = "meridian-fleet-support-p0-chat"
SOURCE_FLOW_NAME = "meridian-vsa-escalation-flow"
NEW_FLOW_NAME = "meridian-vsa-escalation-flow-after-callback"
ROUTING_PROFILE_NAME = "meridian-fleet-support-agents"

# ---------------------------------------------------------------------------
# Dry-run guard
# ---------------------------------------------------------------------------
# Installed on the botocore session BEFORE any client is created.
# Refuses any mutating Connect call when _is_dry_run is True.
# Tests prove the guard by using a real client with dummy creds and
# AWS_ENDPOINT_URL=http://127.0.0.1:1; a blocked write raises DryRunBlocked
# (not a ConnectionError), proving the guard fires before the network.

# Dry run admits only read operations: an allowlist by verb, so a write the
# script did not anticipate is refused too (a denylist of known writes would let
# it through).
_READ_PREFIXES: tuple[str, ...] = ("Describe", "List", "Get", "Search")

_is_dry_run: bool = True  # module-level flag; set False only by run_apply()


class DryRunBlocked(Exception):
    """Raised by the before-call guard when a write is attempted in dry-run mode."""


class IdentityMismatch(Exception):
    """Raised by check_identity when account or instance does not match."""


class FlowSwapError(Exception):
    """Raised by swap_queue_arn when the old ARN appears 0 or >1 times."""


def _dry_run_event_handler(event_name: str, **kwargs: Any) -> None:
    """Botocore before-call event handler: refuse mutating operations in dry-run mode.

    The guard is installed on the botocore session before any client is created
    from it, so no client created from this session can bypass it.

    For testing: registered on the session object returned by make_connect_client(),
    proving the guard fires before any network call is attempted.
    """
    if not _is_dry_run:
        return  # apply mode — allow through
    # event_name is "before-call.<service>.<Operation>"
    operation_name = event_name.split(".")[-1] if event_name else ""
    if not operation_name.startswith(_READ_PREFIXES):
        raise DryRunBlocked(
            f"dry-run: refusing non-read call '{operation_name}'. "
            "Pass --apply to write."
        )


# Module-level botocore session — guard registered here, before any client is created.
_SESSION = _botocore_session.Session()
# register_first on every service and operation: a narrower handler can run
# after a Stubber's and be bypassed.
_SESSION.get_component("event_emitter").register_first("before-call.*.*", _dry_run_event_handler)


def make_connect_client(region: str) -> Any:
    """Return a Connect client created from the guarded module-level session.

    The guard is already registered on _SESSION; any client created from it
    inherits the before-call event hook. Clients created via boto3.client()
    directly bypass the guard — callers must use this factory.
    """
    return boto3.Session(botocore_session=_SESSION).client(
        "connect", region_name=region
    )


# ---------------------------------------------------------------------------
# Identity guard
# ---------------------------------------------------------------------------


def check_identity(*, actual_account: str, expected_account: str) -> None:
    """Refuse unless the caller's account is the one the operator stated."""
    if not actual_account or not expected_account or actual_account != expected_account:
        raise IdentityMismatch(
            "Identity mismatch: the credentials are not for the account given with "
            "--account. Nothing was read or written."
        )


# ---------------------------------------------------------------------------
# Flow content swap
# ---------------------------------------------------------------------------


def swap_queue_arn(content: str, old_arn: str, new_arn: str) -> str:
    """Replace old_arn with new_arn in Connect flow content JSON.

    Raises FlowSwapError if old_arn does not appear exactly once.
    This prevents silently writing a broken flow when the content structure
    has changed since the script was written.
    """
    count = content.count(old_arn)
    if count == 0:
        raise FlowSwapError(
            f"Old queue ARN not found in flow content. "
            f"Expected exactly 1 occurrence of {old_arn!r}. "
            "Verify the source flow name and that it routes to the expected queue."
        )
    if count > 1:
        raise FlowSwapError(
            f"Old queue ARN appears {count} times in flow content. "
            f"Expected exactly 1 occurrence of {old_arn!r}. "
            "The flow may have multiple queue branches — edit this script to handle "
            "the ambiguity explicitly."
        )
    return content.replace(old_arn, new_arn, 1)


# ---------------------------------------------------------------------------
# Provisioning helpers
# ---------------------------------------------------------------------------


def _get_caller_account() -> str:
    """Return the AWS account ID of the current caller via STS."""
    sts = boto3.client("sts")
    return sts.get_caller_identity()["Account"]


def _find_instance_id(connect_client: Any, instance_name: str) -> str:
    """Resolve a Connect instance by name to its ID."""
    paginator = connect_client.get_paginator("list_instances")
    for page in paginator.paginate():
        for inst in page.get("InstanceSummaryList", []):
            if inst.get("InstanceAlias") == instance_name or inst.get("Id") == instance_name:
                return inst["Id"]
    raise ValueError(f"Connect instance {instance_name!r} not found")


def _find_queue_by_name(connect_client: Any, instance_id: str, name: str) -> dict:
    """Return the queue summary dict for `name`, or raise ValueError."""
    paginator = connect_client.get_paginator("list_queues")
    for page in paginator.paginate(InstanceId=instance_id, QueueTypes=["STANDARD"]):
        for q in page.get("QueueSummaryList", []):
            if q.get("Name") == name:
                return q
    raise ValueError(f"Queue {name!r} not found in instance {instance_id!r}")


def _find_contact_flow_by_name(connect_client: Any, instance_id: str, name: str) -> dict:
    """Return the contact flow summary dict for `name`, or raise ValueError."""
    paginator = connect_client.get_paginator("list_contact_flows")
    for page in paginator.paginate(InstanceId=instance_id):
        for cf in page.get("ContactFlowSummaryList", []):
            if cf.get("Name") == name:
                return cf
    raise ValueError(f"Contact flow {name!r} not found in instance {instance_id!r}")


def _find_routing_profile_by_name(connect_client: Any, instance_id: str, name: str) -> dict:
    """Return the routing profile summary dict for `name`, or raise ValueError."""
    paginator = connect_client.get_paginator("list_routing_profiles")
    for page in paginator.paginate(InstanceId=instance_id):
        for rp in page.get("RoutingProfileSummaryList", []):
            if rp.get("Name") == name:
                return rp
    raise ValueError(f"Routing profile {name!r} not found in instance {instance_id!r}")


def _get_chat_priority(connect_client: Any, instance_id: str, routing_profile_id: str) -> int:
    """Return the Priority of the existing CHAT queue entry in the routing profile."""
    paginator = connect_client.get_paginator("list_routing_profile_queues")
    for page in paginator.paginate(InstanceId=instance_id, RoutingProfileId=routing_profile_id):
        for entry in page.get("RoutingProfileQueueConfigSummaryList", []):
            if entry.get("Channel") == "CHAT":
                return entry["Priority"]
    return 1  # default if no chat entry found


# ---------------------------------------------------------------------------
# Plan / Apply / Rollback
# ---------------------------------------------------------------------------


def plan(connect: Any, instance_id: str, delay_seconds: int) -> dict:
    """Describe what will be created. Reads only — does not write anything."""
    log.info("--- PLAN (dry run) ---")

    # Describe source queue
    src_queue_summary = _find_queue_by_name(connect, instance_id, SOURCE_QUEUE_NAME)
    src_queue = connect.describe_queue(
        InstanceId=instance_id, QueueId=src_queue_summary["Id"]
    )["Queue"]
    log.info("Source queue: %s (%s)", src_queue["Name"], src_queue["QueueId"])
    log.info("  HoursOfOperationId: %s", src_queue["HoursOfOperationId"])
    log.info("  OutboundCallerConfig: %s", src_queue.get("OutboundCallerConfig"))

    # Describe source flow
    src_flow_summary = _find_contact_flow_by_name(connect, instance_id, SOURCE_FLOW_NAME)
    src_flow = connect.describe_contact_flow(
        InstanceId=instance_id, ContactFlowId=src_flow_summary["Id"]
    )["ContactFlow"]
    log.info("Source flow: %s (%s)", src_flow["Name"], src_flow["Id"])

    # Routing profile
    rp = _find_routing_profile_by_name(connect, instance_id, ROUTING_PROFILE_NAME)
    priority = _get_chat_priority(connect, instance_id, rp["Id"])
    log.info("Routing profile: %s (%s)", rp["Name"], rp["Id"])
    log.info("  Existing CHAT priority: %d", priority)

    # What would be created
    log.info("--- Would create: ---")
    log.info(
        "  Queue %r with HoursOfOperationId=%s, Delay=%ds on routing profile CHAT",
        NEW_QUEUE_NAME, src_queue["HoursOfOperationId"], delay_seconds,
    )
    log.info("  Contact flow %r (copy of %r with queue ARN swapped)", NEW_FLOW_NAME, SOURCE_FLOW_NAME)

    # Tenant config change to print
    print("\n--- Tenant config change (print only — apply manually) ---")
    print(json.dumps({
        "escalation": {
            "callback": {
                "chatContactFlowId": "<id of the new flow, printed by --apply>",
                "_comment": (
                    f"Set this to the id of {NEW_FLOW_NAME!r} after running --apply. "
                    "This routes the P0 chat to the delayed queue so the call arrives first."
                ),
            }
        }
    }, indent=2))
    print()

    return {
        "source_queue_id": src_queue["QueueId"],
        "source_queue_arn": src_queue["QueueArn"],
        "source_flow_id": src_flow["Id"],
        "routing_profile_id": rp["Id"],
        "chat_priority": priority,
    }


def apply_changes(connect: Any, instance_id: str, delay_seconds: int) -> dict:
    """Create the queue, associate it, create the flow. Writes to Connect."""
    global _is_dry_run
    _is_dry_run = False  # enable write calls
    created: dict[str, str] = {}

    try:
        context = plan(connect, instance_id, delay_seconds)

        # 0. Validate the flow swap BEFORE any write: if the source flow no longer
        # holds the queue ARN exactly once, refuse with nothing created
        # (review cycle 1 W5: the refusal used to come after two writes).
        src_flow_full = connect.describe_contact_flow(
            InstanceId=instance_id, ContactFlowId=context["source_flow_id"]
        )["ContactFlow"]
        swap_queue_arn(src_flow_full["Content"], context["source_queue_arn"], "<new-queue-arn>")

        # 1. Describe source queue for copy
        src_queue = connect.describe_queue(
            InstanceId=instance_id, QueueId=context["source_queue_id"]
        )["Queue"]

        # 2. Create new queue
        log.info("Creating queue %r ...", NEW_QUEUE_NAME)
        create_q_kwargs: dict[str, Any] = {
            "InstanceId": instance_id,
            "Name": NEW_QUEUE_NAME,
            "HoursOfOperationId": src_queue["HoursOfOperationId"],
            "Description": (
                f"P0 callback delayed queue — chats route here when a P0 outbound "
                f"call has been placed. Delay={delay_seconds}s lets the call be "
                f"offered first. Copy of {SOURCE_QUEUE_NAME!r}."
            ),
        }
        if src_queue.get("OutboundCallerConfig"):
            create_q_kwargs["OutboundCallerConfig"] = src_queue["OutboundCallerConfig"]
        new_queue = connect.create_queue(**create_q_kwargs)
        new_queue_id = new_queue["QueueId"]
        new_queue_arn = new_queue["QueueArn"]
        log.info("Created queue: %s (%s)", NEW_QUEUE_NAME, new_queue_id)
        created["queue_id"] = new_queue_id

        # 3. Associate new queue with routing profile
        log.info("Associating queue with routing profile %r ...", ROUTING_PROFILE_NAME)
        connect.associate_routing_profile_queues(
            InstanceId=instance_id,
            RoutingProfileId=context["routing_profile_id"],
            QueueConfigs=[{
                "QueueReference": {"QueueId": new_queue_id, "Channel": "CHAT"},
                "Priority": context["chat_priority"],
                "Delay": delay_seconds,
            }],
        )
        log.info("Associated queue to routing profile at priority=%d delay=%ds",
                 context["chat_priority"], delay_seconds)

        # 4. Create new flow from source with queue ARN swapped (validated in step 0)
        new_content = swap_queue_arn(
            src_flow_full["Content"], context["source_queue_arn"], new_queue_arn
        )
        log.info("Creating flow %r ...", NEW_FLOW_NAME)
        new_flow = connect.create_contact_flow(
            InstanceId=instance_id,
            Name=NEW_FLOW_NAME,
            Type="CONTACT_FLOW",
            Content=new_content,
            Description=(
                f"Escalation flow for P0 chats placed after an outbound callback. "
                f"Routes to {NEW_QUEUE_NAME!r} (delayed queue). "
                f"Copy of {SOURCE_FLOW_NAME!r}."
            ),
        )
        new_flow_id = new_flow["ContactFlowId"]
        new_flow_arn = new_flow["ContactFlowArn"]
        log.info("Created flow: %s (%s)", NEW_FLOW_NAME, new_flow_id)

        # 5. Print the tenant-config change
        print("\n--- Tenant config change (set chatContactFlowId in tenant DDB) ---")
        print(json.dumps({
            "escalation": {
                "callback": {
                    # StartChatContact's ContactFlowId takes the flow id, the
                    # same form as escalation.callback.contactFlowId.
                    "chatContactFlowId": new_flow_id,
                }
            }
        }, indent=2))
        print()

        return {
            "new_queue_id": new_queue_id,
            "new_queue_arn": new_queue_arn,
            "new_flow_id": new_flow_id,
            "new_flow_arn": new_flow_arn,
        }
    except Exception:
        if created:
            log.error(
                "apply failed part-way; undo with: --rollback --new-queue-id %s",
                created.get("queue_id"),
            )
        raise
    finally:
        _is_dry_run = True  # always restore to safe default


def rollback(connect: Any, instance_id: str, new_queue_id: str, new_flow_id: str | None) -> None:
    """Delete the resources created by apply_changes."""
    global _is_dry_run
    _is_dry_run = False

    try:
        rp = _find_routing_profile_by_name(connect, instance_id, ROUTING_PROFILE_NAME)

        log.info("Disassociating queue %s from routing profile ...", new_queue_id)
        try:
            connect.disassociate_routing_profile_queues(
                InstanceId=instance_id,
                RoutingProfileId=rp["Id"],
                QueueReferences=[{"QueueId": new_queue_id, "Channel": "CHAT"}],
            )
        except Exception as exc:  # noqa: BLE001 — a partial apply may not have associated it
            log.warning("Disassociate skipped (%s); continuing the rollback.", type(exc).__name__)

        if new_flow_id:
            log.info("Deleting flow %s ...", new_flow_id)
            connect.delete_contact_flow(
                InstanceId=instance_id, ContactFlowId=new_flow_id
            )

        log.info("Deleting queue %s ...", new_queue_id)
        connect.delete_queue(InstanceId=instance_id, QueueId=new_queue_id)
        log.info("Rollback complete.")
    finally:
        _is_dry_run = True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True,
                   help="AWS account id the credentials must belong to")
    p.add_argument("--instance", required=True,
                   help="Connect instance id or alias, which must exist in that account")
    p.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    p.add_argument("--delay-seconds", type=int, default=60,
                   help="Chat queue delay in seconds (default: 60)")
    p.add_argument("--apply", action="store_true", help="Write to Connect (default: dry-run)")
    p.add_argument("--rollback", action="store_true",
                   help="Delete resources created by --apply")
    p.add_argument("--new-queue-id", help="Queue ID to delete (required with --rollback)")
    p.add_argument("--new-flow-id", help="Flow ID to delete with --rollback, if the flow was created")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    check_identity(actual_account=_get_caller_account(), expected_account=args.account)
    connect = make_connect_client(region=args.region)
    instance_id = _find_instance_id(connect, args.instance)

    if args.rollback:
        if not args.new_queue_id:
            sys.exit("--rollback requires --new-queue-id (and --new-flow-id if the flow was created)")
        rollback(connect, instance_id, args.new_queue_id, args.new_flow_id)
    elif args.apply:
        result = apply_changes(connect, instance_id, args.delay_seconds)
        log.info("Apply complete: %s", result)
    else:
        plan(connect, instance_id, args.delay_seconds)


if __name__ == "__main__":
    main()
