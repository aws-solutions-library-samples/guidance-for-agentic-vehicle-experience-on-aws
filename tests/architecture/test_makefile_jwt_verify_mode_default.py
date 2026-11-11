"""The Makefile's deploy-time default for ``VSA_JWT_VERIFY_MODE`` must be ``enforce``.

Follow-on to issue ``2026-09-23-bidi-jwt-aud-not-verified``. On 2026-10-03 the deployed
bidi staging runtime (``vsa_supervisor_bidi_staging-*``) was flipped from ``observe``
to ``enforce`` via an env-only ``update-agent-runtime`` call. That flip is only durable
if the Makefile does not silently revert it on the next ``make deploy-staging-bidi-runtime``.

The failure mode this test prevents
-----------------------------------
Before 2026-10-03 the Makefile shipped::

    --env VSA_JWT_VERIFY_MODE=${VSA_JWT_VERIFY_MODE:-observe}

A shell parameter default of ``observe`` means a redeploy with the env var unset
would push ``observe`` to the deployed runtime, undoing a security-posture change
that is otherwise invisible in the diff. The runtime would start logging
``jwt verification FAILED`` for real tokens again instead of rejecting bad ones,
and nobody would notice until the next audit.

This test asserts the deploy-time default is ``enforce``. Keeping the shell override
(``${VAR:-...}``) is a feature — an operator with a specific reason can still deploy
``off`` or ``observe`` from a one-shot env — but the default when nobody overrides
must be the stricter mode. If a future change intentionally lowers the default, this
test must be edited in the same commit, which is the point.

Scope boundary
--------------
This test is only about the Makefile's deploy-time default. ``jwt_verify.py``'s own
module default (``observe``, applied when the env var is unset at runtime import) is
a separate decision with a different audience: it keeps local tests and local dev
from unexpectedly fail-closing when nothing has set the env var. The two defaults
are intentionally decoupled — the module default protects local work, the Makefile
default protects deploys. This test guards the Makefile half.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


def _makefile_text() -> str:
    assert MAKEFILE.is_file(), f"Makefile not found at {MAKEFILE}"
    return MAKEFILE.read_text(encoding="utf-8")


def test_deploy_bidi_passes_default_enforce() -> None:
    """``agentcore deploy`` MUST pass ``--env VSA_JWT_VERIFY_MODE`` with a shell
    default of ``enforce``.

    The exact wire form the Makefile recipe emits — Make doubles ``$`` to ``$$``, so
    the Makefile literal is ``$${VSA_JWT_VERIFY_MODE:-enforce}``.
    """
    text = _makefile_text()
    # One canonical shape: --env <space> VAR=<default-expression>
    pattern = re.compile(
        r"--env\s+VSA_JWT_VERIFY_MODE=\$\$\{VSA_JWT_VERIFY_MODE:-([a-z]+)\}"
    )
    hits = pattern.findall(text)
    assert hits, (
        "No '--env VSA_JWT_VERIFY_MODE=$${VSA_JWT_VERIFY_MODE:-<mode>}' line found "
        "in Makefile. Did deploy-staging-bidi-runtime stop passing the env entirely? "
        "If so, the runtime falls back to jwt_verify.py's module default (observe), "
        "which silently reverts the 2026-10-03 enforce flip."
    )
    assert all(mode == "enforce" for mode in hits), (
        f"Deploy-time default must be 'enforce' (see issue "
        f"2026-09-23-bidi-jwt-aud-not-verified summary.md). Found: {hits}. "
        f"If this change is deliberate, update this test in the same commit."
    )


def test_jwt_verify_mode_echo_matches_deploy_default() -> None:
    """The pre-deploy info line's default MUST match what the deploy actually ships.

    The echo was observed drifting from the deploy line's default in a prior
    revision of this file (echo said one mode, deploy passed another), so the test
    pins them to the same value rather than checking each independently."""
    text = _makefile_text()
    pattern = re.compile(
        r"JWT verify mode:\s*\$\$\{VSA_JWT_VERIFY_MODE:-([a-z]+)\}"
    )
    hits = pattern.findall(text)
    assert hits, (
        "No 'JWT verify mode: $${VSA_JWT_VERIFY_MODE:-<mode>}' echo found. The "
        "pre-deploy info echo documents what the next deploy will ship; losing it "
        "makes a silent default-revert easier, not harder."
    )
    assert all(mode == "enforce" for mode in hits), (
        f"Pre-deploy echo must report 'enforce' to match the deploy line. "
        f"Found: {hits}."
    )


def test_no_stray_observe_or_off_default_anywhere() -> None:
    """A loose grep: no ``${VSA_JWT_VERIFY_MODE:-observe}`` or ``:-off`` form may
    appear anywhere in the Makefile.

    This catches the specific regression this test exists for — someone changes one
    of the two canonical lines but not the other, or adds a third occurrence in a
    new target with the pre-flip default copied in. Enforce is the only acceptable
    shell default; anything else must be an explicit operator-supplied override, not
    a baked-in fallback."""
    text = _makefile_text()
    forbidden = re.compile(
        r"\$\$\{VSA_JWT_VERIFY_MODE:-(observe|off)\}"
    )
    hits = forbidden.findall(text)
    assert not hits, (
        f"Found disallowed Makefile defaults for VSA_JWT_VERIFY_MODE: {hits}. "
        f"The deploy-time default is 'enforce' per the 2026-10-03 flip. If you "
        f"need a one-off deploy at a lower mode, export the env var inline:\n"
        f"  VSA_JWT_VERIFY_MODE=observe make deploy-staging-bidi-runtime\n"
        f"The baked-in fallback must stay 'enforce'."
    )
