"""``AVX_FLEET_IDS_CLAIM_TRUSTED`` is ``true`` on the staging text runtime and on no other runtime.

Spec ``2026-10-02-fi-cost-stage-b`` task 5.0 step 3 (user decision 2026-10-06, G3F1.4): the
fleet_manager ``fleet_recommendation`` tool trusts the caller's ``custom:fleetIds`` claim only on
the text supervisor runtime. That runtime is reached only through ``/assistant/chat``, behind the
``vsa-{stage}-api`` Cognito authorizer, and before turning it on every app client of every pool
that authorizer accepts was checked live to exclude ``custom:fleetIds`` from ``WriteAttributes``
(``decisions.md`` 2026-10-07). The bidi runtime keeps refusing: its JWT hardening is separate work.

Why the Makefile: every Makefile runtime deploy re-applies its own ``--env`` list, so a flag set
only by an env-only ``update-agent-runtime`` is silently dropped by the next deploy, and a flag
added to the wrong deploy block turns trust on for a runtime nobody checked.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
FLAG = "AVX_FLEET_IDS_CLAIM_TRUSTED"
TEXT_STAGING = "vsa_supervisor_text_staging"


def _deploy_commands() -> dict[str, str]:
    """Each ``agentcore deploy -a <runtime>`` command with its ``\\``-continued lines."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = re.search(r"agentcore deploy -a (\S+)", line)
        if not m:
            continue
        cmd = [line]
        j = i
        while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            cmd.append(lines[j])
        assert m.group(1) not in out, f"two deploy commands for {m.group(1)}"
        out[m.group(1)] = "\n".join(cmd)
    return out


def test_the_makefile_has_the_four_runtime_deploys():
    """Guards the parser: if a deploy block moves or is renamed, the tests below must not pass
    vacuously."""
    assert set(_deploy_commands()) == {
        "vsa_supervisor_bidi", "vsa_supervisor_text_prod",
        "vsa_supervisor_bidi_staging", TEXT_STAGING,
    }


def test_text_staging_deploy_sets_the_flag_to_true():
    cmd = _deploy_commands()[TEXT_STAGING]
    assert re.findall(rf"--env\s+{FLAG}=(\S+)", cmd) == ["true"], cmd


def test_no_other_runtime_deploy_mentions_the_flag():
    for name, cmd in _deploy_commands().items():
        if name != TEXT_STAGING:
            assert FLAG not in cmd, f"{name} must not set {FLAG}"


def test_the_flag_appears_nowhere_else_in_the_makefile():
    """A second mention (an export, a default) could reach a runtime this test can't see."""
    assert MAKEFILE.read_text(encoding="utf-8").count(FLAG) == 1


def test_fleet_manager_prompt_offers_fleet_recommendation():
    from agents.supervisor.persona_definitions import FLEET_MANAGER_PROMPT

    assert "`fleet_recommendation`" in FLEET_MANAGER_PROMPT
    assert "available: false" in FLEET_MANAGER_PROMPT
    assert "decision history are not" not in FLEET_MANAGER_PROMPT
    assert "pending action queues" not in FLEET_MANAGER_PROMPT
