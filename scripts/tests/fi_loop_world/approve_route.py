"""Approve-route piece — end-to-end helpers for the AVX approve route in FiLoopWorld.

Covers the two engineering-decision Action kinds the loop script exercises:

  POST /findings/{finding_id}/approve
      body: {"action_kind": "record_engineering_decision",
              "target_system": "adp",
              "parameters": {"option_id": "<id>"}}
      Group: product-engineer.
      The executor calls the ADP digital-thread writer Lambda (registered in
      moto by FiLoopWorld); on success the on_decision_recorded hook writes the
      quality.issue Finding.

  POST /findings/{finding_id}/approve  (second call, on the quality.issue Finding)
      body: {"action_kind": "issue_recall",
              "target_system": "adp",
              "parameters": {}}
      Group: product-engineer.
      The executor calls the writer with the issue payload.  On success the
      DDB recall-campaigns-issued row transitions from 'draft' to 'issued'.

The route goes through the FiLoopWorld's AVX REST API (FakeRestApi), which
dispatches to the real api-avx-findings handler.  The handler lazily imports
``agents.executors.registry`` and calls its EXECUTORS["adp"]._execute(), which
invokes the moto Lambda registered as ADP_WRITER_ARN.  The writer is the real
ADP digital_thread_writer/handler.py loaded from ADP 9bb82e96.

This module provides:
  - ``post_approve(world, finding_id, action_kind, target_system, parameters, sub)``
      POST /findings/{finding_id}/approve through the world's AVX API.
  - ``assert_adp_writer_wrote_draft(world, thread_id, option_id)``
      Verify that the ADP writer created the thread v1 + draft row.
  - ``assert_adp_writer_issued(world, campaign_id)``
      Verify that the ADP writer transitioned the draft to 'issued'.
  - ``assert_quality_issue_finding(world, investigation_finding_id)``
      Verify that the on_decision_recorded hook wrote a quality.issue Finding.

Facts from code (handler.py lines, ADP commit 9bb82e96):
  - approve path: handler.py:1198 ``_approve_finding``
  - engineering kinds gate: handler.py:1275–1283 (product-engineer only)
  - Action.status pending_execution → executed / declined: base.py:72–100
  - on_decision_recorded hook → quality.issue Finding: adp.py:503–508,
    agents/fi_quality/issue.py:56 (ISSUE_FINDING_KIND = 'quality.issue')
  - ADP writer issue path: handler.py:1098 ``_handle_issue_recall``,
    mark_recall_issued DDB conditional update handler.py:1175–1190
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def post_approve(
    world: Any,
    *,
    finding_id: str,
    action_kind: str,
    target_system: str,
    parameters: dict[str, Any],
    sub: str = "eng-001",
) -> dict[str, Any]:
    """POST /findings/{finding_id}/approve through *world*'s AVX REST API.

    Mints a product-engineer token for *sub* and sends the approve body.
    Returns the parsed response body dict.  Raises ``AssertionError`` on
    non-200/202 status codes (includes the handler's 200 path for approve).
    """
    token = world._mint_engineer_token(sub)
    body = {"action_kind": action_kind, "target_system": target_system,
            "parameters": parameters}
    body_bytes = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{world.avx_url}findings/{finding_id}/approve",
        data=body_bytes,
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_bytes = e.read()
        err_body = err_bytes.decode()
        # Try to get more info from the world's request log
        api_error = ""
        if hasattr(world, "avx_api") and world.avx_api.requests:
            last_reqs = [
                r for r in world.avx_api.requests
                if r.get("route", "").endswith("/approve")
            ]
            if last_reqs and last_reqs[-1].get("error"):
                api_error = f"\nHandler traceback:\n{last_reqs[-1]['error'][:2000]}"
        raise AssertionError(
            f"POST /findings/{finding_id}/approve ({action_kind}) returned "
            f"{e.code}: {err_body}{api_error}"
        ) from e


def assert_adp_writer_wrote_draft(
    world: Any, *, thread_id: str, option_id: str
) -> str:
    """Assert the ADP writer created thread v1 + draft row.

    Returns the ``campaign_id`` (draft_id) for the caller.
    """
    import boto3

    r = boto3.resource("dynamodb", region_name="us-east-1")

    # Thread row (version 1, status 'decided')
    rows = r.Table("adp-staging-digital-thread").query(
        KeyConditionExpression="thread_id = :t",
        ExpressionAttributeValues={":t": thread_id},
    ).get("Items", [])
    assert len(rows) >= 1, (
        f"Expected at least 1 thread row for thread_id={thread_id!r}, got {rows}"
    )
    v1 = min(rows, key=lambda x: x.get("version", 9999))
    assert str(v1.get("status")) == "decided", (
        f"Thread v1 status should be 'decided', got {v1!r}"
    )
    assert v1.get("option_id") == option_id, (
        f"Thread v1 option_id should be {option_id!r}, got {v1.get('option_id')!r}"
    )
    campaign_id = str(v1.get("recall_campaign_id", ""))
    assert campaign_id.startswith("MRD-"), (
        f"Thread v1 recall_campaign_id should start with 'MRD-', got {campaign_id!r}"
    )

    # Draft row in recall-campaigns-issued
    draft = r.Table("adp-staging-recall-campaigns-issued").get_item(
        Key={"campaign_id": campaign_id}
    ).get("Item")
    assert draft is not None, (
        f"No draft row in recall-campaigns-issued for campaign_id={campaign_id!r}"
    )
    assert draft.get("status") == "draft", (
        f"Draft row status should be 'draft', got {draft!r}"
    )
    assert draft.get("thread_id") == thread_id, (
        f"Draft row thread_id should be {thread_id!r}, got {draft.get('thread_id')!r}"
    )
    return campaign_id


def assert_quality_issue_finding(
    world: Any, *, investigation_finding_id: str, findings_table: str = "vsa-staging-avx-findings"
) -> str:
    """Assert the on_decision_recorded hook wrote a quality.issue Finding.

    Returns the quality.issue Finding id.
    """
    import boto3

    r = boto3.resource("dynamodb", region_name="us-west-2")
    # The quality.issue Finding's subject_key is "{thread_id}/issue" (issue.py:248).
    # We scan for kind == 'quality.issue' and population_id == 'meridian-motors'.
    resp = r.Table(findings_table).scan(
        FilterExpression="finding_kind = :k",
        ExpressionAttributeValues={":k": "quality.issue"},
    )
    items = resp.get("Items", [])
    assert len(items) >= 1, (
        f"Expected at least 1 quality.issue Finding after on_decision_recorded, "
        f"got {items}"
    )
    # Find the one linked to the investigation Finding via evidence
    linked = [
        it for it in items
        if any(e.get("kind") == "quality.thread" for e in (it.get("evidence") or []))
    ]
    if linked:
        issue_finding = linked[0]
    else:
        issue_finding = items[0]
    issue_finding_id = issue_finding.get("finding_id", "")
    assert issue_finding_id, f"quality.issue Finding has no finding_id: {issue_finding}"
    rec = issue_finding.get("recommended_action") or {}
    assert rec.get("action_kind") == "issue_recall", (
        f"quality.issue recommended_action.action_kind should be 'issue_recall', "
        f"got {rec!r}"
    )
    return issue_finding_id


def assert_adp_writer_issued(
    world: Any, *, campaign_id: str
) -> None:
    """Assert the ADP writer transitioned the draft to 'issued'."""
    import boto3

    r = boto3.resource("dynamodb", region_name="us-east-1")
    row = r.Table("adp-staging-recall-campaigns-issued").get_item(
        Key={"campaign_id": campaign_id}
    ).get("Item")
    assert row is not None, (
        f"No row in recall-campaigns-issued for campaign_id={campaign_id!r}"
    )
    assert row.get("status") == "issued", (
        f"Expected status 'issued', got {row.get('status')!r} for {campaign_id!r}"
    )
    assert row.get("received_date") is not None, (
        f"issued row should carry received_date, got {row!r}"
    )
