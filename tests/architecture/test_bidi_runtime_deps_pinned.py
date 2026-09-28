"""Guards the dependency pins that bidi (voice) runtime startup depends on.

Incident this exists for: `issues/2026-09-21-bidi-voice-import-error-unpinned-sdk/`.

`requirements.txt` carried `aws_sdk_bedrock_runtime>=0.0.2`. The 2026-09-11 redeploy
of `vsa_supervisor_bidi_staging` resolved that range to 0.11.0, which renamed
`BedrockRuntimeClient` -> `AsyncBedrockRuntimeClient`. `strands.experimental.bidi`'s
`nova_sonic` module imports the old name at module scope, so the container raised
ImportError before serving anything. iOS saw only `NSURLErrorDomain -1011` from the
failed WebSocket upgrade, and the control plane reported the runtime `READY` the
whole time. Voice was dead for 11 days; nobody exercised it, so nothing surfaced.

Two independent properties, because either alone is insufficient:

  A. The requirement is EXACT-pinned. A range is an unreviewed dependency upgrade on
     every container rebuild, and 0.x packages break between minors.
  B. The import the runtime actually performs still works against what is installed.
     A pin can be exact and still wrong, e.g. pinned to a version incompatible with
     the pinned `strands-agents`.

A alone would pass while pinned to a broken version. B alone would pass today and
regress the moment someone reintroduces a range and a rebuild floats the version.

Verified compatibility matrix (established 2026-09-21 by ACTUAL IMPORT in throwaway
venvs, not by reading source):

    aws_sdk_bedrock_runtime  0.3.0 .. 0.9.0   IMPORT OK   (against strands-agents==1.39.0)
    aws_sdk_bedrock_runtime  0.10.0           IMPORT FAIL  cannot import name 'Config'
    aws_sdk_bedrock_runtime  0.11.0           IMPORT FAIL  cannot import name 'BedrockRuntimeClient'

    0.6.0 is the version currently pinned and deployed. 0.9.0 is the newest compatible.

Two separate breakages, not one: 0.11.0 is the `BedrockRuntimeClient` rename that took
production down, and 0.10.0 fails earlier and differently on `Config`. Anything at or
above 0.10.0 breaks bidi.

Also verified: `agents/supervisor/requirements.txt` declares a DIFFERENT pair
(strands-agents==1.38.0 + aws_sdk_bedrock_runtime==0.3.0). That pair also imports OK, so
the divergence is untidy but not a latent trap — worth knowing before someone "fixes" it
on the assumption that it is broken. It is not what deploys the runtime either;
`agentcore deploy` reads the repo-root requirements.txt.

**A grep of the wheels' source said the rename landed in 0.8.0. That was wrong** --
0.8.0 and 0.9.0 still import fine, because the name remains reachable through a
re-export the grep could not see. The claim was corrected only because the boundary
was re-checked by importing rather than by reading. Do not infer importability from
source inspection here; install the version and import it.

0.6.0 is kept rather than bumped to the 0.9.0 ceiling: it is the version already
installed, verified, and deployed, and this repo's own precedent (see the boto3 pin in
the live spec's SDK-pin task) is to pin the known-good version and treat adopting a
newer one as a separate decision with its own quarantine check. Bumping would also
require a redeploy to take effect, for no functional gain.

Mutation results — executed 2026-09-21, not reasoned about. M2's documented outcome
was WRONG on first writing and is corrected here; the correction is the useful part.

  M1  `aws_sdk_bedrock_runtime==0.6.0` -> `>=0.0.2` (the original defect)
      Expected A to fail. Observed: 2 failed -- `test_bidi_sdk_is_exact_pinned`
      [aws_sdk_bedrock_runtime] plus `test_installed_bidi_sdk_matches_the_pin`
      (which short-circuits when the spec has no `==`). Restored.

  M2  pin -> `aws_sdk_bedrock_runtime==0.11.0` (the version that actually broke prod)
      Predicted: A passes and nothing catches it locally, since A cannot tell a
      wrong-but-exact pin from a right one.
      OBSERVED: 1 failed -- `test_installed_bidi_sdk_matches_the_pin` caught it,
      because the pin (0.11.0) no longer matches the installed version (0.6.0).
      The prediction was wrong, and the third test is load-bearing rather than
      belt-and-braces as first assumed. Restored.

  M3  delete the `aws_sdk_bedrock_runtime` line entirely
      Expected A to fail. Observed: 2 failed -- A's "is absent" branch plus the
      installed-match short-circuit. Restored.

What M2 establishes: the three tests close a loop locally that no one of them closes
alone. A wrong-but-exact pin either (a) disagrees with what is installed, caught by
`test_installed_bidi_sdk_matches_the_pin`, or (b) agrees with what is installed, in
which case the bad version is imported for real and
`test_nova_sonic_imports_against_installed_versions` fails. There is no third path
where a version that breaks the runtime passes all three.

Known gap, stated rather than papered over: the loop above is only as good as the
local venv being a faithful proxy for the container. This suite cannot observe what
`pip install` resolves inside an AgentCore container build -- it verifies the pin is
exact, matches what is installed, and that the installed version imports. The
deploy-time equivalent is the post-deploy smoke step in the issue's summary: tail the
runtime log group for `cannot import name` after any `agentcore deploy`. A stub cannot
fail the way a service fails, and neither can a local import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Packages whose exact version bidi runtime startup is sensitive to.
#
# `aws_sdk_bedrock_runtime` and `strands-agents` are imported at module scope by
# agents/supervisor/bidi_app.py's import chain, so a breaking change in either kills
# the container before it serves a request.
#
# `PyJWT` and `cryptography` (added 2026-09-23, Fix Group 18 T18.6) are sensitive for a
# related but distinct reason: bidi_app.py calls `assert_jwt_enforceable()` at import,
# which raises when `VSA_JWT_VERIFY_MODE=enforce` and RS256 is unavailable — so under
# enforce they too decide whether the container starts. Under `observe` their absence is
# WORSE than a crash: verification silently reports a deployment defect instead of an
# auth result, and the operator believes a security control is running when it is not.
#
# Both already arrive transitively via strands-agents -> mcp -> pyjwt[crypto], but down
# three chained ranges this repo does not control, which is the drift this guard exists
# to stop. See the rationale block in requirements.txt.
BIDI_CRITICAL_PACKAGES = (
    "aws_sdk_bedrock_runtime",
    "strands-agents",
    "PyJWT",
    "cryptography",
)

# Any specifier that is not a bare `==`. `~=` and `>=` both permit a rebuild to
# pick up a version nobody reviewed.
_NON_EXACT = re.compile(r"(>=|<=|>|<|~=|!=|\^)")


def _requirement_lines() -> list[str]:
    assert REQUIREMENTS.is_file(), f"requirements.txt not found at {REQUIREMENTS}"
    lines = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _spec_for(package: str) -> str | None:
    """Return the requirement line for `package`, matching name-normalised forms."""
    wanted = re.sub(r"[-_.]+", "-", package).lower()
    for line in _requirement_lines():
        name = re.split(r"[=<>~!\[;]", line, maxsplit=1)[0].strip()
        if re.sub(r"[-_.]+", "-", name).lower() == wanted:
            return line
    return None


# ─── A. the pin is exact ──────────────────────────────────────────────────────


@pytest.mark.parametrize("package", BIDI_CRITICAL_PACKAGES)
def test_bidi_sdk_is_exact_pinned(package: str) -> None:
    spec = _spec_for(package)

    assert spec is not None, (
        f"{package} is absent from requirements.txt. bidi_app.py imports it at module "
        f"scope via strands.experimental.bidi, so the runtime container cannot start "
        f"without it. Do not delete this line to make a test pass."
    )
    assert not _NON_EXACT.search(spec), (
        f"{package} must be pinned with a bare '==', found: {spec!r}. A range lets an "
        f"AgentCore container rebuild silently adopt a new version -- exactly how bidi "
        f"voice broke for 11 days on 2026-09-11 (see this file's docstring). 0.x "
        f"packages break between minor versions."
    )
    assert "==" in spec, f"{package} has no '==' pin at all: {spec!r}"


# ─── B. the import the runtime performs actually works ────────────────────────


def test_nova_sonic_imports_against_installed_versions() -> None:
    """The exact module-scope import chain from agents/supervisor/bidi_app.py.

    Reproduces the production failure locally. If the installed
    `aws_sdk_bedrock_runtime` no longer exports what `strands`'s nova_sonic module
    expects, this raises the same ImportError the container did.
    """
    try:
        from strands.experimental.bidi.models import nova_sonic  # noqa: F401
        from strands.experimental.bidi.models.nova_sonic import (  # noqa: F401
            NOVA_SONIC_V2_MODEL_ID,
            BidiNovaSonicModel,
        )
    except ImportError as exc:  # pragma: no cover - the regression path
        pytest.fail(
            "The bidi runtime's module-scope import chain is broken against the "
            f"installed packages: {exc}\n\n"
            "This is the 2026-09-21 failure mode. If the message mentions "
            "'cannot import name BedrockRuntimeClient', the installed "
            "aws_sdk_bedrock_runtime is too new for the pinned strands-agents -- "
            "check requirements.txt against the pin recorded in its comment."
        )


def test_rs256_is_available_against_installed_versions() -> None:
    """PyJWT can actually perform RS256 with what is installed.

    The B-property for the JWT pins, and not redundant with the exactness check: PyJWT
    imports fine without `cryptography`, and `jwt.algorithms.has_crypto` silently
    becomes False. An image in that state passes every pin check, then reports
    `crypto_unavailable` on every session — or, under `VSA_JWT_VERIFY_MODE=enforce`,
    refuses to start.

    Asserted through `jwt_verify`'s own accessors rather than by importing PyJWT
    directly, so this fails if those accessors regress too.
    """
    from agents.supervisor import jwt_verify

    assert jwt_verify.pyjwt_available(), (
        "PyJWT cannot be imported against the installed packages. "
        "agents/supervisor/jwt_verify.py needs it for session-boundary JWT "
        "verification; check the PyJWT pin in requirements.txt."
    )
    assert jwt_verify.crypto_available(), (
        "PyJWT imports but cannot perform RS256 — `cryptography` is missing or "
        "incompatible. Cognito ID tokens are RS256, so verification would fail for "
        "every session with reason `crypto_unavailable`. Check the cryptography pin "
        "in requirements.txt."
    )


def test_installed_jwt_packages_match_their_pins() -> None:
    """The venv matches the JWT pins, so the test above validates what deploys.

    Same reasoning as `test_installed_bidi_sdk_matches_the_pin`, and the M2 mutation
    recorded in this file's docstring is why it is not treated as belt-and-braces: it
    is the test that catches a wrong-but-exact pin.
    """
    import importlib.metadata as md

    for package in ("PyJWT", "cryptography"):
        spec = _spec_for(package)
        assert spec and "==" in spec, f"covered by test_bidi_sdk_is_exact_pinned[{package}]"
        pinned = spec.split("==", 1)[1].strip().split(None, 1)[0].rstrip(";,")

        installed = md.version(package)
        assert installed == pinned, (
            f"requirements.txt pins {package}=={pinned} but the active venv has "
            f"{installed}, so the RS256 availability check above is validating a "
            f"version that will not be deployed. "
            f"Run: pip install '{package}=={pinned}'"
        )


def test_installed_bidi_sdk_matches_the_pin() -> None:
    """The local venv matches requirements.txt, so test B is testing the pinned version.

    Without this, B could pass against a locally-installed version that the pin does
    not actually select -- a green suite proving nothing about what deploys.
    """
    import importlib.metadata as md

    spec = _spec_for("aws_sdk_bedrock_runtime")
    assert spec and "==" in spec, "covered by test_bidi_sdk_is_exact_pinned"
    pinned = spec.split("==", 1)[1].strip().split(None, 1)[0].rstrip(";,")

    installed = md.version("aws_sdk_bedrock_runtime")
    assert installed == pinned, (
        f"requirements.txt pins aws_sdk_bedrock_runtime=={pinned} but the active venv "
        f"has {installed}. Test B is therefore validating a version that will not be "
        f"deployed. Run: pip install 'aws_sdk_bedrock_runtime=={pinned}'"
    )
