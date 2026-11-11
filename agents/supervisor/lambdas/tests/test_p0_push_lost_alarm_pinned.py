"""The lost-p0-push alarm reads the metric the broker writes, and pages the operator topic.

Issue ``issues/2026-10-23-lost-p0-push-not-alarmed`` (design § L1). jest runs in no CI
job, so this Python pin over ``infrastructure/lib/vsa-tier2-stack.ts`` is the CI guard
(same pattern as ``test_escalation_alarm_targets_pinned.py``); the template-level check is
``infrastructure/test/vsa-tier2-stack-avx-broker.test.ts`` › "lost p0 push alarm".

The names are cross-checked against the broker's own constants, so renaming the metric
on either side alone fails here instead of leaving an alarm that watches nothing.
"""
from __future__ import annotations

import os
import re
import sys

_LAMBDAS_DIR = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

import avx_broker as broker  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_STACK = os.path.join(_REPO_ROOT, "infrastructure", "lib", "vsa-tier2-stack.ts")


def _alarm_block() -> str:
    src = open(_STACK).read()
    start = src.index("new cloudwatch.Alarm(this, 'AvxBrokerP0PushLostAlarm'")
    end = src.index("});", start)
    block = src[start:end]
    # The action line follows the constructor.
    action_end = src.index("\n", src.index("brokerP0PushLostAlarm.addAlarmAction", end))
    return block + src[end:action_end]


def test_alarm_watches_the_broker_metric_for_p0():
    block = _alarm_block()
    assert f"namespace:     '{broker._PUSH_METRIC_NAMESPACE}'" in block
    assert f"metricName:    '{broker._PUSH_METRIC_NAME}'" in block
    assert re.search(r"dimensionsMap:\s*\{\s*Stage:\s*stage,\s*Severity:\s*'p0'\s*\}", block), block
    assert broker._P0 == "p0"


def test_alarm_fires_on_one_lost_push_and_treats_silence_as_healthy():
    block = _alarm_block()
    assert re.search(r"threshold:\s*1,", block)
    assert re.search(r"evaluationPeriods:\s*1,", block)
    assert "GREATER_THAN_OR_EQUAL_TO_THRESHOLD" in block
    assert "TreatMissingData.NOT_BREACHING" in block
    assert re.search(r"statistic:\s*'Sum'", block)
    assert re.search(r"period:\s*cdk\.Duration\.minutes\(5\)", block)
    assert "actionsEnabled" not in block, "an alarm for a safety event must not be gated off"


def test_alarm_pages_the_operator_topic():
    block = _alarm_block()
    assert re.search(
        r"brokerP0PushLostAlarm\.addAlarmAction\(\s*new cloudwatchActions\.SnsAction\(\s*operatorAlertTopic\s*\)\s*\)",
        block,
    ), block
    src = open(_STACK).read()
    assert re.search(
        r"const operatorAlertTopic = new sns\.Topic\(this, 'Tier2OperatorAlertTopic', \{\s*"
        r"topicName:\s*`vsa-\$\{stage\}-tier2-operator-alerts`",
        src,
    ), "operatorAlertTopic must be the vsa-{stage}-tier2-operator-alerts topic"


def test_broker_emf_dimension_names_match_the_alarm():
    """The EMF line declares exactly the dimension set the alarm selects on."""
    import inspect
    body = inspect.getsource(broker._emit_push_not_delivered)
    assert '"Dimensions": [["Stage", "Severity"]]' in body
    assert '"Stage": _STAGE' in body
    assert '"Severity": dim_severity' in body


def test_broker_can_read_the_p0_ledger():
    """Review cycle 1 S1: without this grant every carded p0 hits AccessDenied, raises
    BrokerTransientError and lands in the DLQ, so no escalation is ever pushed. jest pins
    the synthesized policy; this is the CI pin over the source."""
    src = open(_STACK).read()
    m = re.search(
        r"brokerRole\.addToPolicy\(new iam\.PolicyStatement\(\{\s*"
        r"sid:\s*'BrokerAvxP0LedgerGetItem',\s*"
        r"effect:\s*iam\.Effect\.ALLOW,\s*"
        r"actions:\s*\[([^\]]*)\],\s*"
        r"resources:\s*\[([^\]]*)\],\s*\}\)\);",
        src,
    )
    assert m, "BrokerAvxP0LedgerGetItem statement not found in its expected shape"
    assert [a.strip() for a in m.group(1).split(",") if a.strip()] == ["'dynamodb:GetItem'"]
    assert [r.strip() for r in m.group(2).split(",") if r.strip()] == [
        "this.avxRateLimitsTable.tableArn"]
    assert broker._RATE_LIMITS_TABLE.endswith("avx-rate-limits")
    assert src.count("sid: 'BrokerAvxP0LedgerGetItem'") == 1


def _broker_event_source_block() -> str:
    src = open(_STACK).read()
    start = src.index("avxBrokerFn.addEventSource(new lambdaEventSources.DynamoEventSource(")
    end = src.index("));", start)
    return src[start:end]


def test_one_broker_mapping_reads_the_findings_stream_keyed_on_finding_id():
    """Review cycle 2 W1/S1, corrected in cycle 3 S1: the p0 ledger reclaims any
    ``claimed`` row at once.

    That is safe while no two deliveries of one Finding run at the same time. Lambda
    keeps one partition key's stream records in order, one batch at a time, for any
    ParallelizationFactor (AWS "Using AWS Lambda with Amazon DynamoDB", Polling and
    batching streams), so the factor is not the threat. What the rule needs is (1)
    exactly one event source mapping invoking the broker, and (2) the Findings table
    keyed on ``finding_id``, the key the ledger row is per. A second mapping (another
    ``addEventSource`` or an ``EventSourceMapping`` targeting the broker) would let two
    deliveries of one p0 push twice.
    """
    src = open(_STACK).read()
    assert src.count("avxBrokerFn.addEventSource(") == 1
    block = _broker_event_source_block()
    assert block.startswith(
        "avxBrokerFn.addEventSource(new lambdaEventSources.DynamoEventSource(\n"
        "      this.avxFindingsTable,")
    assert "addEventSourceMapping" not in src
    assert not re.search(r"new lambda\.EventSourceMapping\(", src)
    assert not re.search(r"(target|functionName|FunctionName):\s*avxBrokerFn", src)
    table = src[src.index("this.avxFindingsTable = new dynamodb.Table(this, 'AvxFindings', {"):]
    table = table[:table.index("});")]
    assert "partitionKey: { name: 'finding_id', type: dynamodb.AttributeType.STRING }," in table
    assert "sortKey" not in table


def test_alarm_text_states_the_push_does_not_retry_by_itself():
    """Review cycle 1 W1 / cycle 2 S9: the operator must not wait for a retry."""
    block = re.sub(r"`\s*\+\s*`", "", _alarm_block())  # join the template-literal pieces
    assert "The push does not retry by itself" in block, block
