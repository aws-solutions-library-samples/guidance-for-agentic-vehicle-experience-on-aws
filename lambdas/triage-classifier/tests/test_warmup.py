"""Tests for the triage classifier's warmup path.

Invoke:  pytest -q lambdas/triage-classifier/tests/test_warmup.py

WHY THIS EXISTS
---------------
`vsa-<stage>-triage-classifier-warmer` (EventBridge Scheduler, every 4 minutes)
invokes this handler with `{"warmup": true}` to keep one execution environment hot.
Measured 2026-08-19: cold invocations took 2510-2600 ms against Amazon Nova Sonic's
~1 s bidi tool budget, so a cold classifier made the voice assistant stop speaking
mid-turn — which presents to a user as a broken app, and was misdiagnosed twice as a
prompt-wording problem before anyone measured the Lambda.

The warmup contract has two halves, and only one of them is obvious:

1. It must return without raising, so scheduler failure metrics mean real failures.
2. It must do **no work** — no tenant config read, no CMS read, no DynamoDB write.
   A ping that took the normal path would need to carry plausible business
   identifiers, and would deposit a fake triage decision every 4 minutes forever.

Half 2 was enforced only by a source comment when the warmer shipped. That is the
kind of guarantee that breaks silently: someone moves the early-return below a
"harmless" lookup and nothing fails until the decisions table is full of synthetic
rows. Hence an executable assertion.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]

# Import the BUILT bundle, not the source tree, and deliberately so.
#
# `handler.py` imports `from triage_classifier.classifier import classify` — the
# packaged name. The source directory is `lambdas/triage-classifier` (hyphen), and
# `scripts/build-triage-classifier.sh` renames it to `triage_classifier` at build
# time, so the source is not importable under the name its own imports use.
#
# Testing the bundle turns that awkwardness into an asset: it also asserts the
# warmup path is present in what actually ships. On 2026-08-19 the warmer's schedule
# deployed while the rebuilt bundle did not, and every ping failed with
# `KeyError: 'tenantId'` — a green `UPDATE_COMPLETE` with a stale asset. A test that
# read only source would have passed throughout that outage.
BUNDLE = ROOT / "infrastructure" / "build" / "triage-classifier"

import pytest

if not (BUNDLE / "triage_classifier" / "handler.py").exists():
    pytest.skip(
        f"bundle not built at {BUNDLE} — run scripts/build-triage-classifier.sh",
        allow_module_level=True,
    )

sys.path.insert(0, str(BUNDLE))

from triage_classifier import handler as handler_mod


WARM_RESPONSE = {"status": "warm"}


def test_warmup_returns_warm_status():
    assert handler_mod.handler({"warmup": True}, None) == WARM_RESPONSE


def test_warmup_needs_no_business_identifiers():
    """No tenantId, no vin, no sessionId — and it must still succeed.

    The real path reads `event["tenantId"]` and `event["vin"]` unguarded, so if the
    early-return were ever moved below them this raises `KeyError` rather than
    silently degrading. That is exactly what happened on the first deploy of the
    warmer, when the schedule shipped but the rebuilt handler bundle did not: every
    ping failed with `KeyError: 'tenantId'` and the environment stayed warm only by
    accident.
    """
    assert handler_mod.handler({"warmup": True}, None) == WARM_RESPONSE


def test_warmup_touches_no_aws_resource(monkeypatch):
    """The load-bearing assertion: a ping must not read or write anything.

    Every outbound seam is replaced with a fail-fast stub. If the warmup path is ever
    moved below one of them, that stub raises and this test names which seam was
    crossed — a more useful failure than a timeout or a mystery row in DynamoDB.
    """
    def boom(name):
        def _fail(*_args, **_kwargs):
            raise AssertionError(
                f"warmup path must not reach {name}; the early return has moved "
                "below an outbound call"
            )
        return _fail

    monkeypatch.setattr(handler_mod, "_decisions_table", boom("_decisions_table"))
    monkeypatch.setattr(handler_mod.tenant_config, "load_raw", boom("tenant_config.load_raw"))
    monkeypatch.setattr(handler_mod.cms_client, "get_client", boom("cms_client.get_client"))

    assert handler_mod.handler({"warmup": True}, None) == WARM_RESPONSE


@pytest.mark.parametrize(
    "event",
    [
        {},                      # scheduler misconfigured to send nothing
        {"warmup": False},       # explicit opt-out
        {"warmup": "true"},      # string, not bool — must NOT count as warmup
        {"warmup": 1},           # truthy int — must NOT count as warmup
    ],
)
def test_non_warmup_events_take_the_real_path(event):
    """`is True` is deliberate, not incidental.

    A loose `if event.get("warmup"):` would treat any truthy value as a ping, so a
    real triage request carrying an unrelated `warmup` key would silently return
    "warm" and the driver would get no classification at all. These events must fall
    through to the real path — which, with no `tenantId`, raises `KeyError`. That
    exception IS the assertion: it proves the early return was not taken.
    """
    with pytest.raises(KeyError):
        handler_mod.handler(event, None)
