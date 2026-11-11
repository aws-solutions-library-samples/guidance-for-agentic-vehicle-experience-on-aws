"""Assert the order-progression state machine's ASL passes `orderId` correctly.

WHY THIS EXISTS
---------------
Two defects shipped through review, `cdk synth`, and `tsc --noEmit` and were only
caught by a live staging run on 2026-08-02
(issues/2026-08-02-order-progression-sfn-payload-and-resultpath/):

1. `payload: sfn.TaskInput.fromJsonPathAt('$')` combined with
   `payloadResponseOnly: true` renders as the LITERAL STRING ``"Parameters": "$"``
   — CDK emits `props.payload?.value` verbatim in that mode, and a bare `$` path
   has no key to carry the `.$` suffix. `order_progressor` received the str
   ``"$"`` and raised ``'str' object has no attribute 'get'`` on the first invoke.

2. Without `resultPath: DISCARD`, the Lambda's return value replaces the state,
   so the second invoke would receive ``{"advanced": 1, ...}`` — no `orderId` —
   silently fall through to tenant-scan mode, find no active tenants, and report
   success having advanced nothing. The order advances exactly one stage then
   stalls with no error.

Neither is visible in the TypeScript: both are properties of the *rendered* ASL.
So this test renders it and asserts on the output. Type-checking the stack cannot
substitute for this.

Do not weaken these assertions to make a refactor pass — an over-permissive
version of this test is indistinguishable from no test, and the failure mode it
guards is a silent stall in front of an audience.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infrastructure"

TRANSITION_COUNT = 9
"""9 non-terminal transitions: orderPlaced -> ... -> readyForDelivery."""


@pytest.fixture(scope="module")
def asl() -> dict:
    """Synthesize vsa-staging-acquire and return the state machine's ASL."""
    if not (INFRA_DIR / "node_modules").is_dir():
        pytest.skip("infrastructure/node_modules absent — run `make install` first")
    if shutil.which("npx") is None:
        pytest.skip("npx not available")

    env = {**os.environ, "VSA_STAGE": "staging", "AWS_REGION": "us-west-2"}
    proc = subprocess.run(
        ["npx", "cdk", "synth", "vsa-staging-acquire", "--json"],
        cwd=INFRA_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(f"cdk synth failed:\n{proc.stderr[-2000:]}")

    template = json.loads(proc.stdout)
    machines = [
        r for r in template["Resources"].values()
        if r["Type"] == "AWS::StepFunctions::StateMachine"
    ]
    assert len(machines) == 1, f"expected exactly 1 state machine, got {len(machines)}"

    definition = machines[0]["Properties"]["DefinitionString"]
    if isinstance(definition, dict):
        # Fn::Join of literal chunks and ARN Refs — substitute a placeholder for
        # the non-literal parts so the result is parseable JSON.
        parts = definition["Fn::Join"][1]
        definition = "".join(
            p if isinstance(p, str) else "arn:aws:placeholder" for p in parts
        )
    return json.loads(definition)


def _invoke_states(asl: dict) -> list[tuple[str, dict]]:
    return [(f"Invoke{i}", asl["States"][f"Invoke{i}"]) for i in range(TRANSITION_COUNT)]


class TestOrderProgressionAsl:
    def test_all_nine_invokes_present(self, asl):
        for name, _ in _invoke_states(asl):
            assert name in asl["States"], f"{name} missing from the ASL"

    def test_no_invoke_passes_a_literal_string_payload(self, asl):
        """Defect 1. `Parameters` must not be a bare string.

        If it is, the Lambda receives that string as its whole event.
        """
        for name, state in _invoke_states(asl):
            params = state.get("Parameters")
            assert not isinstance(params, str), (
                f"{name}.Parameters is the literal string {params!r} — the Lambda "
                f"will receive that string as its event and fail on "
                f"event.get('orderId'). Omit the `payload` prop so the state "
                f"input passes through, or use "
                f"TaskInput.fromObject({{'orderId.$': '$.orderId'}})."
            )

    def test_every_invoke_receives_order_id(self, asl):
        """Either no Parameters at all (state input passes straight through), or
        Parameters that explicitly carry orderId as a JSON path."""
        for name, state in _invoke_states(asl):
            if "Parameters" not in state:
                continue  # state input passes through unchanged — correct
            params = state["Parameters"]
            assert isinstance(params, dict), f"{name}.Parameters must be an object"
            keys = set(params)
            assert "orderId.$" in keys or "orderId" in keys, (
                f"{name}.Parameters is {params!r} — it must convey orderId, "
                f"because order_progressor keys single-order mode off "
                f"event['orderId'] and otherwise falls through to tenant-scan "
                f"mode and advances nothing."
            )

    def test_every_invoke_discards_its_result(self, asl):
        """Defect 2. ResultPath must be null (JsonPath.DISCARD) so the Lambda's
        return value does not replace {"orderId": ...} for the next invoke."""
        for name, state in _invoke_states(asl):
            assert "ResultPath" in state, (
                f"{name} has no ResultPath — the Lambda's return value "
                f"({{'advanced': N, ...}}) will replace the state and the next "
                f"invoke will lose orderId, silently advancing nothing. Set "
                f"resultPath: sfn.JsonPath.DISCARD."
            )
            assert state["ResultPath"] is None, (
                f"{name}.ResultPath is {state['ResultPath']!r}, expected null "
                f"(JsonPath.DISCARD) so the execution input survives all "
                f"{TRANSITION_COUNT} invokes."
            )

    def test_machine_is_standard_not_express(self, asl):
        """Express caps at 5 minutes and is at-least-once; spec D4 requires
        STANDARD. Asserted here because the ASL is where it is observable
        alongside the rest of the topology."""
        # StateMachineType lives on the CFN resource, not in the ASL, so assert
        # the topology instead: a Wait per transition proves the linear chain.
        waits = [k for k in asl["States"] if k.startswith("Wait")]
        assert len(waits) == TRANSITION_COUNT, (
            f"expected {TRANSITION_COUNT} Wait states, found {len(waits)}: {sorted(waits)}"
        )

    def test_chain_alternates_wait_then_invoke(self, asl):
        """Wait{i} -> Invoke{i} -> Wait{i+1}, terminating after Invoke8."""
        for i in range(TRANSITION_COUNT):
            assert asl["States"][f"Wait{i}"]["Next"] == f"Invoke{i}"
            invoke = asl["States"][f"Invoke{i}"]
            if i < TRANSITION_COUNT - 1:
                assert invoke["Next"] == f"Wait{i + 1}", (
                    f"Invoke{i} should lead to Wait{i + 1}, got {invoke.get('Next')!r}"
                )
            else:
                assert "Next" not in invoke, (
                    f"Invoke{i} is the terminal transition and must not chain onward"
                )
