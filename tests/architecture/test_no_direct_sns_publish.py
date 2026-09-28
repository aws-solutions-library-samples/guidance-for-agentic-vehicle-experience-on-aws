"""Guard: the broker is the ONLY code path that may call ``sns:Publish``.

Encodes the parent PRD § M12 channel-policy monopoly structurally, per this spec's
Group 7 second task.

WHY A MONOPOLY
==============

Every outbound push to a customer must pass one chokepoint that applies rate limiting,
the per-owner cardinality cap, per-category consent, and feedback demotion. An agent that
calls ``sns.publish`` directly bypasses all four at once -- consent most seriously. That is
not a style violation; it is the difference between a notification policy and a suggestion.

`~/.kiro/steering/agentic-tiers.md` makes the same point generally: the delivery decision
(channel, consent, quiet hours, frequency) belongs to a broker, never to the agent itself.
A boundary that exists only in a prompt or a docstring will be crossed, so it is asserted
here instead.

WHY THIS IS WRITTEN BEFORE THE BROKER EXISTS
============================================

`agents/supervisor/lambdas/avx_broker.py` is not built yet (Group 7 task 1). This guard is
still live and still meaningful: it currently asserts that *nothing anywhere* under
``agents/`` publishes to SNS, which is the correct state today and the state that must hold
right up until the broker lands. Writing it now means the monopoly is enforced from the
moment the first publish call is added, rather than being retrofitted after a second one
has already appeared somewhere else.

It deliberately does NOT require the broker to exist. Requiring that would make this guard
fail for a reason unrelated to what it protects, and a guard that is red for an unrelated
reason stops being read -- the failure mode `~/.kiro/steering/agentic-tiers.md` records for
CVX's permanently-red ``test_agent_iam_has_no_connect_action``.
"""
from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The single file permitted to publish. Relative to the repo root, POSIX form.
_BROKER_REL = "agents/supervisor/lambdas/avx_broker.py"

#: Every boto3 SNS method that reaches a subscriber. Matching only ``publish`` left
#: a documented escape past the broker chokepoint (security review Group 16 Cycle 1 W2):
#:
#:   publish_batch — same ``sns:Publish`` IAM action, same topic, same subscribers;
#:                   just batched. REAL: present on the boto3 SNS client.
#:
#:   publish_platform_endpoint — **does NOT exist on the boto3 SNS client** (verified:
#:                   ``hasattr(boto3.client("sns"), "publish_platform_endpoint")`` is
#:                   False). Kept deliberately as defence-in-depth against a future
#:                   wrapper or resource class exposing that name, but it is NOT the
#:                   APNs-direct path and nothing today can call it. The real
#:                   APNs-direct path is ``publish(TargetArn=<endpoint-arn>)``, which
#:                   bypasses the topic-subscription layer and is already covered by
#:                   ``publish`` above. Recorded so a later reader does not mistake
#:                   this entry for live coverage of that hazard — security review
#:                   Cycle 2 S1 caught it as a phantom in the Cycle 1 fix.
#:
#: The monopoly is about reaching a customer, so the set is defined by "does it deliver
#: to a subscriber", not by the method's name resembling "publish".
_FORBIDDEN_METHODS = frozenset(
    {"publish", "publish_batch", "publish_platform_endpoint"}
)

#: Production roots searched. Matches the sibling guard
#: ``test_d2_writer_wired_once_table_exists.py`` (``_PRODUCTION_ROOTS``).
#: T14.2 in this spec recorded "lambdas/ added as a fifth root — the same blind
#: spot that hid Critical C1 from the sibling guard." This was the same blind spot
#: recurring (W7, Fix Group 16 Cycle 1): a publish(...) in lambdas/ bypassed the
#: rate-limiting, cardinality-cap, consent and demotion chokepoint while the guard
#: stayed green. Root sets must stay in sync.
_SEARCH_ROOTS = ("agents", "lambdas")

_SKIP_FRAGMENTS = ("/tests/", "test_", "/__pycache__/")


def _candidate_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root_name in _SEARCH_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            posix = path.as_posix()
            if any(frag in posix for frag in _SKIP_FRAGMENTS):
                continue
            out.append(path)
    return out


def _publish_call_sites(path: pathlib.Path) -> list[int]:
    """Line numbers of genuine ``.publish(...)`` / ``publish(...)`` CALL sites.

    AST-based rather than grep-based on purpose. The word "publish" appears in prose
    throughout this repo (including this module's own docstring), and a substring guard
    would both false-positive on comments and miss nothing useful -- the presence-vs-property
    trap. Only ``ast.Call`` nodes count.

    Matches any receiver: ``sns.publish(...)``, ``self._sns.publish(...)``,
    ``boto3.client("sns").publish(...)``. Narrowing to a receiver named exactly ``sns``
    would be trivially evaded by renaming the variable, which is how the sibling
    index-name guard was defeated by a chained receiver.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else None
        )
        if name in _FORBIDDEN_METHODS:
            lines.append(node.lineno)
    return lines


def test_only_the_broker_publishes_to_sns() -> None:
    """No file under ``agents/`` or ``lambdas/`` except the broker may call ``publish(...)``."""
    offenders: list[str] = []
    for path in _candidate_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _BROKER_REL:
            continue
        for lineno in _publish_call_sites(path):
            offenders.append(f"  {rel}:{lineno}")

    assert not offenders, (
        "`publish(...)` is called outside the broker, bypassing the channel-policy "
        "chokepoint:\n"
        + "\n".join(sorted(offenders))
        + "\n\nEvery customer-facing push must route through "
        f"{_BROKER_REL}, which is the only place rate limiting, the per-owner "
        "cardinality cap, per-category CONSENT, and feedback demotion are applied. A "
        "direct publish bypasses all four -- consent most seriously.\n\n"
        "If this is operator-facing alerting rather than a customer push, it still does "
        "not belong under agents/ or lambdas/: infrastructure notifying an operator is "
        "a CDK concern (SNS subscription / alarm action), not an agent call. See parent "
        "PRD \u00a7 M12 and ~/.kiro/steering/agentic-tiers.md.\n\n"
        f"Searched roots: {_SEARCH_ROOTS}."
    )


def test_guard_is_not_vacuous_it_can_see_a_publish_call() -> None:
    """Pin the discriminating property: the AST walk really does detect a publish call.

    Without this, `test_only_the_broker_publishes_to_sns` passes trivially today (there
    are no publish calls anywhere yet) and would keep passing even if `_publish_call_sites`
    were broken to always return []. That is the exact shape of a guard that reports
    green while protecting nothing -- and the reason this repo's sibling guards were
    defeated nine times. Parses a synthetic module instead of touching real source.
    """
    import tempfile

    sample = (
        "import boto3\n"
        "def f(topic, msg):\n"
        "    sns = boto3.client('sns')\n"
        "    sns.publish(TopicArn=topic, Message=msg)\n"          # attribute receiver
        "\n"
        "def g(client, topic):\n"
        "    boto3.client('sns').publish(TopicArn=topic)\n"        # chained receiver
        "\n"
        "def h(sns, topic):\n"
        "    sns.publish_batch(TopicArn=topic, PublishBatchRequestEntries=[])\n"
        "\n"
        "def i(sns, arn):\n"
        "    sns.publish_platform_endpoint(TargetArn=arn, Message='x')\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "synthetic.py"
        p.write_text(sample)
        found = _publish_call_sites(p)

    assert len(found) == 4, (
        "_publish_call_sites failed to detect known delivery call sites in a synthetic "
        f"module (expected 4, got {found}). The guard above is therefore vacuous: it "
        "would report green regardless of what the tree contains. Note the sample "
        "covers all three names in _FORBIDDEN_METHODS at two receiver shapes — if a "
        "name is dropped from that set this count falls and this test fails, which is "
        "the point: narrowing the matcher must not be silent."
    )


def test_prose_mentions_of_publish_are_not_counted() -> None:
    """A comment or docstring naming `publish` must not register as a call site.

    Complements the test above: together they pin both halves -- the walk detects real
    calls, and does not invent them from prose. This module's own docstring says
    "publish" repeatedly, so a substring implementation would flag this very file.
    """
    import tempfile

    sample = (
        '"""This module would publish to SNS but does not."""\n'
        "# publish(TopicArn=..., Message=...) -- intentionally commented out\n"
        "def f():\n"
        "    return None  # no publish call here\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "prose_only.py"
        p.write_text(sample)
        found = _publish_call_sites(p)

    assert found == [], (
        "Prose-only mentions of `publish` were counted as call sites "
        f"(lines {found}). The guard is substring-based and will false-positive on "
        "docstrings, including its own."
    )
