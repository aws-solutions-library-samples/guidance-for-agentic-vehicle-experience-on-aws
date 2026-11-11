"""Pin the BrokerSnsPublish IAM statement to exactly one endpoint ARN (spec T1.3b).

The broker's ``sns:Publish`` grant was previously scoped to ``endpoint/*/*/*``,
allowing publication to ANY SNS endpoint in the account. T1.3b narrows it to the
single AVX APNs platform application ``vsa-{stage}-avx-apns-sandbox``
(``APNS_SANDBOX`` token-auth type), so the broker can only reach device tokens
registered under that one application.

Python, because jest runs in no CI job and the jest test in
``infrastructure/test/vsa-tier2-stack-avx-broker.test.ts`` is not gated.

Mutants caught here (verified, see docstrings):
  M1 — resource reverted to ``endpoint/*/*/*``       → test_broker_sns_publish_resource_is_pinned fails
  M2 — second resource entry added to the array      → test_exactly_one_resource_in_broker_sns_publish fails
"""
from __future__ import annotations

import os
import re

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_STACK = os.path.join(_REPO_ROOT, "infrastructure", "lib", "vsa-tier2-stack.ts")

# The expected literal fragment inside a backtick template-literal; ${...} tokens are
# dollar-brace, NOT regex groups, so they are matched literally here via re.escape on
# the surrounding text and a minimal lookahead for the inner token names.
_EXPECTED_RE = re.compile(
    r"endpoint/APNS_SANDBOX/vsa-\$\{stage\}-avx-apns-sandbox/\*"
)


def _stack_src() -> str:
    with open(_STACK) as fh:
        return fh.read()


def _publish_statement_block() -> str:
    """Return the source text of the BrokerSnsPublish addToPolicy call."""
    src = _stack_src()
    start = src.index("sid: 'BrokerSnsPublish'")
    stmt_start = src.rindex("brokerRole.addToPolicy(", 0, start)
    stmt_end = src.index("));", start) + 3
    return src[stmt_start:stmt_end]


def test_sid_appears_exactly_once():
    """Guard against a duplicate statement that could silently restore a broad grant."""
    src = _stack_src()
    assert src.count("sid: 'BrokerSnsPublish'") == 1, (
        "sid 'BrokerSnsPublish' must appear exactly once in the stack source; "
        "a duplicate could reopen the grant"
    )


def test_exactly_one_resource_in_broker_sns_publish():
    """There must be exactly ONE resource ARN.

    Mutant M2: adding a second resource (e.g. a second platform application or
    a broad ``*``) causes this test to fail because len(non-comment lines) > 1.
    """
    block = _publish_statement_block()
    resources_match = re.search(r"resources:\s*\[(.*?)\]", block, re.S)
    assert resources_match, "BrokerSnsPublish statement has no resources array"
    resources_body = resources_match.group(1)
    non_comment_lines = [
        line.strip()
        for line in resources_body.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    assert len(non_comment_lines) == 1, (
        f"BrokerSnsPublish must have exactly one resource entry; "
        f"found {len(non_comment_lines)}: {non_comment_lines}"
    )


def _strip_line_comments(text: str) -> str:
    """Remove // … end-of-line comments so pattern checks are on code only."""
    return re.sub(r"//[^\n]*", "", text)


def test_broker_sns_publish_resource_is_pinned_to_apns_sandbox_application():
    """The resource is exactly ``endpoint/APNS_SANDBOX/vsa-{stage}-avx-apns-sandbox/*``.

    Mutant M1: reverting the resource to ``endpoint/*/*/*`` causes this to fail
    because:
      - _EXPECTED_RE does not match ``endpoint/*/*/*`` (pattern mismatch), AND
      - the stripped block still contains ``endpoint/*/*/*`` so the negative
        assertion fires.
    Both assertions would independently catch the mutant.
    """
    block = _publish_statement_block()
    assert _EXPECTED_RE.search(block), (
        "BrokerSnsPublish resource must be "
        "`arn:aws:sns:${region}:${account}:endpoint/APNS_SANDBOX/vsa-${stage}-avx-apns-sandbox/*`; "
        f"pattern not found in block:\n{block}"
    )
    # Negative assertion on code-only text (comments may reference the old pattern).
    code_only = _strip_line_comments(block)
    assert "endpoint/*/*/*" not in code_only, (
        "BrokerSnsPublish must NOT use the broad endpoint/*/*/*; "
        "T1.3b narrows it to the APNS_SANDBOX application only"
    )


def test_non_empty():
    """Anti-vacuity: the block is substantial and contains the key literals."""
    block = _publish_statement_block()
    assert "iam.PolicyStatement" in block, "block must contain iam.PolicyStatement"
    assert "APNS_SANDBOX" in block, "block must name APNS_SANDBOX"
    assert "avx-apns-sandbox" in block, "block must name avx-apns-sandbox"
    assert "brokerRole.addToPolicy(" in block, "block must start from addToPolicy"
