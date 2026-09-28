"""The deployed guardrail policy must match `KIOSK_GUARDRAIL_TOPICS`.

WHY THIS EXISTS
---------------
`agents/supervisor/guardrails/kiosk_guardrail.py` declares itself the single
source of truth for the kiosk topic policy, but TypeScript cannot import a Python
constant, so `infrastructure/lib/vsa-core-stack.ts` hand-mirrors it. Nothing
enforced the mirror and the two had already drifted on `examples` before
2026-08-02 (Python carried 7 and 8 for two topics; the stack carried 5 and 5;
Bedrock caps examples at 5, so the Python constant was itself un-submittable).

This test compares the Python constant against the **synthesized CloudFormation
template** — what will actually be deployed — rather than against the TypeScript
source text. That distinction matters:

  - An earlier version of this check regex-parsed `kioskTopicsConfig` out of the
    `.ts` file. It worked, but it was coupled to source *formatting*: a
    reformat, a helper extraction, or a switch to a loop would break the parser,
    and a parser that silently stops matching is worse than no test because it
    reports success forever. (Review Cycle 7 raised exactly this fragility.)
  - Synthesizing is slower (~6 s) but asserts the property we actually care
    about: the policy Bedrock receives is the policy that was reviewed in Python.
    It is immune to how the TypeScript happens to be written.

Do not weaken these assertions to make a refactor pass — the guardrail topic
definitions ARE the content-safety policy for an unattended public kiosk.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infrastructure"

# Bedrock service limits, per
# docs.aws.amazon.com/bedrock/latest/APIReference/API_GuardrailTopicConfig.html
# NOTE the CloudFormation TemplateReference page for TopicConfig claims the
# definition cap is 1000. It is stale; the service enforces 200. Trusting the
# CFN page cost a deploy on 2026-08-02.
MAX_DEFINITION = 200
MAX_TOPIC_NAME = 100
MAX_EXAMPLES = 5
MAX_EXAMPLE = 100


@pytest.fixture(scope="module")
def python_topics() -> list[dict]:
    sys.path.insert(0, str(REPO_ROOT))
    from agents.supervisor.guardrails.kiosk_guardrail import KIOSK_GUARDRAIL_TOPICS
    return KIOSK_GUARDRAIL_TOPICS


@pytest.fixture(scope="module")
def template_topics() -> list[dict]:
    """Synthesize vsa-staging-core and return the guardrail's TopicsConfig."""
    if not (INFRA_DIR / "node_modules").is_dir():
        pytest.skip("infrastructure/node_modules absent — run `make install` first")
    if shutil.which("npx") is None:
        pytest.skip("npx not available")

    env = {**os.environ, "VSA_STAGE": "staging", "AWS_REGION": "us-west-2"}
    proc = subprocess.run(
        ["npx", "cdk", "synth", "vsa-staging-core", "--json"],
        cwd=INFRA_DIR, env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(f"cdk synth failed:\n{proc.stderr[-2000:]}")

    resources = json.loads(proc.stdout)["Resources"]
    guardrails = [r for r in resources.values() if r["Type"] == "AWS::Bedrock::Guardrail"]
    assert len(guardrails) == 1, f"expected 1 guardrail, got {len(guardrails)}"
    return guardrails[0]["Properties"]["TopicPolicyConfig"]["TopicsConfig"]


class TestDeployedGuardrailMatchesPython:
    def test_same_topic_names_in_the_same_order(self, template_topics, python_topics):
        assert [t["Name"] for t in template_topics] == [
            t["name"] for t in python_topics
        ], "the deployed topic set has drifted from KIOSK_GUARDRAIL_TOPICS"

    def test_definitions_are_identical(self, template_topics, python_topics):
        py = {t["name"]: t["definition"] for t in python_topics}
        for topic in template_topics:
            assert topic["Definition"] == py[topic["Name"]], (
                f"Topic {topic['Name']!r} definition differs between the "
                f"synthesized template and kiosk_guardrail.py.\n"
                f"  template: {topic['Definition']!r}\n"
                f"  python  : {py[topic['Name']]!r}"
            )

    def test_examples_are_identical(self, template_topics, python_topics):
        py = {t["name"]: t.get("examples", []) for t in python_topics}
        for topic in template_topics:
            assert topic.get("Examples", []) == py[topic["Name"]], (
                f"Topic {topic['Name']!r} examples differ.\n"
                f"  template: {topic.get('Examples', [])}\n"
                f"  python  : {py[topic['Name']]}"
            )

    def test_all_topics_are_deny(self, template_topics):
        for topic in template_topics:
            assert topic["Type"] == "DENY", (
                f"Topic {topic['Name']!r} is {topic['Type']!r}; the kiosk policy is "
                f"block-by-default and every topic must be DENY."
            )


class TestDeployedGuardrailWithinServiceLimits:
    """Belt-and-braces against the synth-time guard in vsa-core-stack.ts. If the
    guard is ever removed or bypassed, these still fail before a deploy does."""

    def test_definitions_within_limit(self, template_topics):
        for topic in template_topics:
            n = len(topic["Definition"])
            assert n <= MAX_DEFINITION, (
                f"Topic {topic['Name']!r} definition is {n} chars — Bedrock caps it "
                f"at {MAX_DEFINITION} and returns CREATE_FAILED, rolling the stack "
                f"back mid-deploy."
            )

    def test_topic_names_within_limit(self, template_topics):
        for topic in template_topics:
            assert len(topic["Name"]) <= MAX_TOPIC_NAME

    def test_example_counts_and_lengths_within_limits(self, template_topics):
        for topic in template_topics:
            examples = topic.get("Examples", [])
            assert len(examples) <= MAX_EXAMPLES, (
                f"Topic {topic['Name']!r} has {len(examples)} examples — max "
                f"{MAX_EXAMPLES}. A sixth makes the whole guardrail un-creatable."
            )
            for ex in examples:
                assert len(ex) <= MAX_EXAMPLE, (
                    f"Topic {topic['Name']!r} example is {len(ex)} chars — max "
                    f"{MAX_EXAMPLE}: {ex!r}"
                )
