"""W3 investigation-to-issue flow test.

Exercises the full maker track end-to-end through the real CMS FI Lambda:
  product-engineer → POST population/patterns/{id}/investigations (202)
  → pump tier2 runner (fi_quality rules path, FI_INVESTIGATION_MODEL_ENABLED=false)
  → GET population/investigations/{id} status=completed, options include software_update
  → POST population/investigations/{id}/decision (software_update option)
  → GET population/threads/{threadId} shows draft thread record
  → POST population/threads/{threadId}/issue
  → refresh_population(); GET population/campaigns/software shows issued campaign

Also verifies: fleet-operator gets 403 on GET population/patterns.

Sources: AVX 49ff3719:scripts/tests/fi_loop_world/world.py (pump_investigation pattern),
         CMS 1ce93a75b:services/fleet_intelligence/maker.py (_READ_ROUTES, FORBIDDEN_MSG).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_cms_world import maker as _maker
from scripts.tests.fi_rw_world import seed

FLEET_A = seed.FLEETS[0]
VIN_0 = seed.vin(0)
PATTERN_ID = "pat-sw-001"
FAILURE_MODE_ID = "fm-sw-001"
VINS = [seed.vin(i) for i in range(3)]


def _cms_get(url: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _cms_post(url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestMakerFlow:

    def test_fleet_operator_forbidden_on_patterns(self) -> None:
        """A fleet-operator cannot access GET population/patterns (maker scope check)."""
        with CmsWorld(run_scheduled=False) as w:
            tok = w.cms_fi_token(["fleet-operator"], [FLEET_A])
            url = w.cms_fi_url + "api/v1/fleet-intelligence/population/patterns"
            status, body = _cms_get(url, tok)
        assert status == 403, f"expected 403, got {status}: {body}"
        assert "population scope" in body.get("error", "").lower() or \
               "authorized" in body.get("error", "").lower(), body

    def test_investigation_to_issue_flow(self) -> None:
        """Full maker flow: start → pump → decision → issue → campaign in cache."""
        with CmsWorld(run_scheduled=False) as w:
            # Seed a pattern with a software_update remedy option
            _maker.seed_pattern(w, pattern_id=PATTERN_ID,
                                failure_mode_id=FAILURE_MODE_ID, vin_list=VINS)
            # Also seed the population cache so patterns are available
            w.refresh_population()

            base = w.cms_fi_url + "api/v1/fleet-intelligence/population"
            eng = w.cms_fi_token(["product-engineer"], [])

            # 1. POST investigation → 202
            status, body = _cms_post(
                f"{base}/patterns/{PATTERN_ID}/investigations", eng)
            assert status == 202, f"expected 202, got {status}: {body}"
            inv_id = body.get("investigationId")
            assert inv_id, f"missing investigationId: {body}"

            # 2. Pump tier2 runner (fi_quality rules path)
            pump_result = _maker.pump_investigation(w, inv_id)
            inv_row = pump_result["investigation"]
            assert inv_row.get("status") == "completed", (
                f"investigation not completed: {inv_row.get('status')!r}\n"
                f"error: {inv_row.get('error_message', '')}")

            # 3. GET investigation through maker → status=completed, options present
            status, body = _cms_get(f"{base}/investigations/{inv_id}", eng)
            assert status == 200, f"GET /investigations/{inv_id} → {status}: {body}"
            options = (body.get("result") or {}).get("options") or []
            sw_options = [o for o in options if o.get("remedyType") == "software_update"]
            assert sw_options, f"no software_update option in: {options}"
            sw_option_id = sw_options[0]["remedyOptionId"]

            # 4. POST decision with the software_update option
            status, body = _cms_post(
                f"{base}/investigations/{inv_id}/decision",
                eng, {"optionId": sw_option_id})
            assert status == 200, f"POST decision → {status}: {body}"
            thread_id = (body.get("action") or {}).get("threadId") or \
                        (body.get("action") or {}).get("thread_id") or \
                        body.get("threadId")

            # thread_id may come from the Action response; fall back to DDB scan
            if not thread_id:
                import boto3
                items = boto3.resource("dynamodb", region_name="us-east-1").Table(
                    f"adp-staging-digital-thread").scan().get("Items", [])
                assert items, "no thread record after decision"
                thread_id = items[0]["thread_id"]

            assert thread_id, f"could not determine thread_id from: {body}"

            # 5. GET /population/threads/{threadId} (reads DDB directly)
            status, body = _cms_get(f"{base}/threads/{thread_id}", eng)
            assert status == 200, f"GET /threads/{thread_id} → {status}: {body}"

            # 6. POST issue
            status, body = _cms_post(f"{base}/threads/{thread_id}/issue", eng)
            assert status == 200, f"POST issue → {status}: {body}"

            # 7. Refresh population cache; GET campaigns/software should show the campaign
            w.refresh_population()
            status, body = _cms_get(f"{base}/campaigns/software", eng)
            assert status == 200, f"GET campaigns/software → {status}: {body}"
            campaigns = body.get("data") or []
            assert len(campaigns) >= 1, f"expected at least one campaign, got: {campaigns}"


# ---------------------------------------------------------------------------
# C-W10: POST puts message on the real queue; pump receives, processes, deletes
# ---------------------------------------------------------------------------

def test_post_puts_message_on_real_queue_and_pump_drains_it() -> None:
    """POST investigation → message on vsa-{stage}-avx-investigations (not the -queue variant).
    pump_investigation receives the real message, processes it, and deletes it.

    Before fix: the world created vsa-staging-avx-investigations-queue (wrong name) and
    pump built a synthetic SQS event (never ReceiveMessage/DeleteMessage). This test would
    fail with QueueDoesNotExist or ApproximateNumberOfMessages==0 after POST.
    """
    with CmsWorld(run_scheduled=False) as w:
        _maker.seed_pattern(w, pattern_id=PATTERN_ID,
                            failure_mode_id=FAILURE_MODE_ID, vin_list=VINS)
        w.refresh_population()

        import boto3 as _boto3  # noqa: PLC0415
        from scripts.tests.fi_cms_world.maker import INVESTIGATIONS_QUEUE  # noqa: PLC0415
        sqs = _boto3.client("sqs", region_name="us-west-2")

        eng = w.cms_fi_token(["product-engineer"], [])
        base = w.cms_fi_url + "api/v1/fleet-intelligence/population"

        # POST investigation
        post_status, post_body = _cms_post(
            f"{base}/patterns/{PATTERN_ID}/investigations", eng)
        assert post_status == 202, f"POST investigation → {post_status}: {post_body}"
        investigation_id = post_body["investigationId"]        # The real queue should have 1 message after POST.
        q_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        q_attrs = sqs.get_queue_attributes(
            QueueUrl=q_url,
            AttributeNames=["ApproximateNumberOfMessages",
                             "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        assert int(q_attrs["ApproximateNumberOfMessages"]) == 1, (
            f"expected 1 message on {INVESTIGATIONS_QUEUE} after POST; got {q_attrs}"
        )

        # Pump: receive, process, delete.
        _maker.pump_investigation(w, investigation_id)

        # After pump: queue is drained (0 visible, 0 in-flight).
        q_attrs_after = sqs.get_queue_attributes(
            QueueUrl=q_url,
            AttributeNames=["ApproximateNumberOfMessages",
                             "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        assert int(q_attrs_after["ApproximateNumberOfMessages"]) == 0, (
            f"expected 0 messages after pump; got {q_attrs_after}"
        )
        assert int(q_attrs_after.get("ApproximateNumberOfMessagesNotVisible", "0")) == 0, (
            f"expected 0 in-flight after pump; got {q_attrs_after}"
        )

        # DLQ must be empty.
        dlq_name = f"{INVESTIGATIONS_QUEUE}-dlq"
        dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]
        dlq_attrs = sqs.get_queue_attributes(
            QueueUrl=dlq_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )["Attributes"]
        assert int(dlq_attrs["ApproximateNumberOfMessages"]) == 0, (
            f"DLQ {dlq_name} has messages: {dlq_attrs}"
        )


# ---------------------------------------------------------------------------
# C-W-F: pump_investigation must NOT delete a message the runner reported failed
# ---------------------------------------------------------------------------

def test_pump_investigation_failure_moves_to_dlq(monkeypatch) -> None:
    """Runner failure: ChangeMessageVisibility(0) instead of DeleteMessage.

    Forces failure by removing ATHENA_RESULTS_BUCKET from the Lambda env,
    which makes _run_investigation_sqs return batchItemFailures for every
    record without calling run_investigation.  After two failing pumps moto's
    redrive policy (maxReceiveCount=2) moves the message to the DLQ.

    Before fix (WF1 mutant: always delete): the first pump deletes the message;
    the second pump raises AssertionError inside pump_investigation ("no message
    in queue").  The test fails at that assertion, not on an import/type error.

    After fix: two pumps each call ChangeMessageVisibility(0); the third
    receive_message triggers moto's redrive and routes the message to the DLQ.
    """
    import boto3 as _boto3
    from scripts.tests.fi_cms_world.maker import (  # noqa: PLC0415
        INVESTIGATIONS_QUEUE,
        INVESTIGATIONS_DLQ,
    )

    with CmsWorld(run_scheduled=False) as w:
        _maker.seed_pattern(w, pattern_id=PATTERN_ID,
                            failure_mode_id=FAILURE_MODE_ID, vin_list=VINS)
        w.refresh_population()

        eng = w.cms_fi_token(["product-engineer"], [])
        base = w.cms_fi_url + "api/v1/fleet-intelligence/population"

        # POST investigation → enqueues the message.
        post_status, post_body = _cms_post(
            f"{base}/patterns/{PATTERN_ID}/investigations", eng)
        assert post_status == 202, f"POST investigation → {post_status}: {post_body}"
        inv_id = post_body["investigationId"]

        # Strip ATHENA_RESULTS_BUCKET from the Lambda env so every SQS record
        # is returned as a batchItemFailure without touching Athena.
        _orig_env = w.lam._env

        def _env_no_bucket(name: str) -> dict:
            e = dict(_orig_env(name))
            e.pop("ATHENA_RESULTS_BUCKET", None)
            return e

        monkeypatch.setattr(w.lam, "_env", _env_no_bucket)

        sqs = _boto3.client("sqs", region_name="us-west-2")
        q_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        dlq_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_DLQ)["QueueUrl"]

        # First pump: runner reports failure (no bucket) → ChangeMessageVisibility(0).
        r1 = _maker.pump_investigation(w, inv_id)
        assert r1["result"].get("batchItemFailures"), (
            f"expected batchItemFailures from first pump; got: {r1['result']}")

        # Second pump: same failure → ChangeMessageVisibility(0); receive count=2.
        r2 = _maker.pump_investigation(w, inv_id)
        assert r2["result"].get("batchItemFailures"), (
            f"expected batchItemFailures from second pump; got: {r2['result']}")

        # Trigger moto's DLQ redrive: a third receive_message call sees
        # receive_count=2 >= maxReceiveCount=2 and routes the message to the DLQ.
        sqs.receive_message(QueueUrl=q_url, MaxNumberOfMessages=1, WaitTimeSeconds=0)

        # DLQ must have exactly one message.
        dlq_attrs = sqs.get_queue_attributes(
            QueueUrl=dlq_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )["Attributes"]
        assert int(dlq_attrs["ApproximateNumberOfMessages"]) == 1, (
            f"expected 1 message in DLQ after two failed pumps; "
            f"got {dlq_attrs['ApproximateNumberOfMessages']}"
        )

        # Main queue must be empty.
        q_attrs = sqs.get_queue_attributes(
            QueueUrl=q_url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        assert int(q_attrs["ApproximateNumberOfMessages"]) == 0, (
            f"main queue not empty after DLQ redrive; got {q_attrs}")
