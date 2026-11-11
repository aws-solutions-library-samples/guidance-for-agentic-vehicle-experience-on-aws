"""Verify Cognito ID-token signatures at the bidi session boundary.

Fix Group 18 T18.6, from security review Cycle 1 W1.

The gap
-------
The bidi runtime does not verify JWT signatures. `agentcore_app.py::_extract_actor_id`
documents the trust boundary at length and states its reasoning *"holds for the TEXT
runtime only"*, because that runtime's `InvokeAgentRuntime` is IAM-scoped to a single
route Lambda. The bidi runtime's Cognito Identity Pool authenticated role holds
`InvokeAgentRuntimeWithWebSocketStream` and accepts external pools via
`additionalVoiceAuthUserPools`, so its forgeable set is **every signed-in app user**: a
caller can present a JWT naming any `sub` and the runtime believes it.

Why this verifies at the BOUNDARY, not in one reader
----------------------------------------------------
`ctx.jwt` is consumed at 13 call sites across 6 tool modules — `escalate`,
`fleet_actions`, `triage`, `decision_journal`, `fleet_health`, `daily_briefing`. Those
**act**. The pre-seed reader that surfaced this only **reads**, and at most five capped
evidence strings.

So hardening the reader alone would have changed the security posture very little while
reading as a fix. Verifying once in `_establish_session` covers all 13 consumers at once,
because they all read the `SessionContext` that function builds. That is the whole design
decision.

Rollout: observe, then enforce
------------------------------
This is an auth change on a live voice surface. A bug here rejects every session, so
`VSA_JWT_VERIFY_MODE` gates it:

* ``observe`` (default) — verify, log the outcome, and **proceed regardless**. Produces
  the false-reject signal needed before enforcing, at zero risk.
* ``enforce`` — a failed verification rejects the session.
* ``off`` — skip entirely. For local development only.

What is verified, and what deliberately is not
----------------------------------------------
Verified: RS256 signature against the issuing pool's JWKS; `iss` against an
**allowlist** of trusted pool ids; `exp`/`iat`; `token_use == "id"`.

**Not** verified: `aud` (the app-client id). That is deliberate and worth stating
plainly rather than leaving as a silent gap. The threat this module exists to close is
one signed-in user forging *another user's* `sub` — and signature verification defeats
that completely, because minting a token with someone else's `sub` requires the pool's
private key. `aud` addresses a different threat (token confusion between app clients
*within* the same pool, all of which are ours) and would require threading a client-id
allowlist as well. Named as a follow-on rather than pretended away.

The `iss` allowlist direction matters
-------------------------------------
Expected issuers are **constructed from the configured pool ids**, then the token's
`iss` is matched against that set. The inverse — reading `iss` from the token and
fetching that issuer's JWKS — is the classic confused-deputy bug: an attacker points
`iss` at a pool they control, signs with their own key, and verification passes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

#: Comma-separated Cognito user-pool ids whose tokens this runtime accepts.
#: There is more than one: CVX's own pool plus any pool wired in through
#: `additionalVoiceAuthUserPools` (the iOS client signs in against the CMS pool).
ENV_TRUSTED_POOL_IDS = "VSA_TRUSTED_USER_POOL_IDS"

#: ``off`` | ``observe`` | ``enforce``. Defaults to ``observe``.
ENV_VERIFY_MODE = "VSA_JWT_VERIFY_MODE"

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)

#: JWKS fetch timeout. PyJWKClient defaults to 30s, which is unusable on a
#: session-establish path — a slow endpoint would stall the handshake far past any
#: plausible user patience. Fail fast and let the mode decide what that means.
_JWKS_TIMEOUT_SECONDS = 3.0

#: How long a fetched JWKS stays fresh. Cognito rotates signing keys rarely, and the
#: client falls back to its cached set when a refresh fails, so a long lifespan trades
#: a negligible staleness window for far fewer fetches on the hot path.
_JWKS_LIFESPAN_SECONDS = 3600.0

#: Clock-skew tolerance on `exp`/`iat`.
_LEEWAY_SECONDS = 30.0

#: Cache of PyJWKClient per issuer, at module scope so the JWKS survives across
#: sessions in a warm container. Keyed by issuer URL, never by anything caller-supplied.
_JWKS_CLIENTS: dict[str, Any] = {}


@dataclass(frozen=True)
class VerifyOutcome:
    """Result of a verification attempt.

    ``reason`` is a short machine-readable token, safe to log: it never contains the
    token, the claims, a `sub`, or a service error message. Those can carry a JWT or a
    VIN, which is the discipline `session_memory.py` already states explicitly.
    """

    ok: bool
    reason: str
    sub: Optional[str] = None
    claims: Optional[Mapping[str, Any]] = None


def verify_mode() -> str:
    """Return the configured mode, defaulting to ``observe``.

    An unrecognised value falls back to ``observe`` with a warning rather than to
    ``off``: a typo in an env var must not silently disable a security control.
    """
    raw = (os.environ.get(ENV_VERIFY_MODE) or MODE_OBSERVE).strip().lower()
    if raw not in _VALID_MODES:
        log.warning(
            "jwt_verify: %s=%r is not one of %s — falling back to %r",
            ENV_VERIFY_MODE, raw, _VALID_MODES, MODE_OBSERVE,
        )
        return MODE_OBSERVE
    return raw


def trusted_pool_ids() -> list[str]:
    """Configured pool ids, in order, de-duplicated and stripped."""
    raw = os.environ.get(ENV_TRUSTED_POOL_IDS) or ""
    seen: list[str] = []
    for part in raw.split(","):
        pool = part.strip()
        if pool and pool not in seen:
            seen.append(pool)
    return seen


def _issuer_for(pool_id: str, region: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


def pyjwt_available() -> bool:
    """Whether ``jwt`` (PyJWT) can be imported at all.

    Distinct from :func:`crypto_available`: PyJWT missing and PyJWT-without-RS256 are
    different deployment defects with different fixes, and collapsing them produces a
    log line that sends an operator to the wrong file.
    """
    try:
        import jwt  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def crypto_available() -> bool:
    """Whether PyJWT can actually do RS256.

    Both PyJWT and ``cryptography`` *do* reach the runtime image today, but only down a
    three-link chain of ranges that nothing in this repo controls::

        strands-agents==1.39.0  ->  mcp>=1.23.0,<2.0.0  ->  pyjwt[crypto]>=2.10.1
                                                                  ->  cryptography>=3.4.0

    Verified 2026-09-23 by reading installed distribution metadata, not by assuming: an
    earlier version of this docstring claimed ``cryptography`` was absent from the image
    and present locally only via ``moto``. That was wrong — ``mcp`` requires
    ``pyjwt[crypto]`` unconditionally, and the ``[crypto]`` extra pulls
    ``cryptography`` in.

    The hazard is therefore not absence but *drift*, and it is the same shape as the
    ``aws_sdk_bedrock_runtime`` incident three times over: every container rebuild may
    float ``mcp``, ``pyjwt`` and ``cryptography`` independently. The load-bearing link —
    ``mcp`` asking for the ``[crypto]`` extra — is a third-party decision that a minor
    ``mcp`` release inside ``<2.0.0`` could drop, which would remove RS256 from the
    image without changing one line of this repo.

    Both packages are consequently pinned explicitly in the ROOT ``requirements.txt``
    rather than inherited. This function stays because a pin is a claim about the image
    and this is a check against the image actually loaded.
    """
    try:
        from jwt import algorithms  # noqa: PLC0415
    except ImportError:
        return False
    return bool(getattr(algorithms, "has_crypto", False))


def assert_enforceable() -> None:
    """Raise if ``enforce`` is configured but verification cannot actually run.

    Called at entrypoint import. Deliberately fails the container start rather than
    degrading, because the two silent alternatives are both worse:

    * silently fall back to ``observe`` — the operator believes enforcement is on when
      it is not, which is the worst possible state for a security control;
    * enforce with no crypto — every RS256 decode raises, so every session is rejected
      and voice is down with a misleading cause.

    A container that refuses to start names its own reason, and
    ``tests/architecture/test_bidi_runtime_deps_pinned.py`` is the pre-deploy guard.
    """
    mode = verify_mode()
    if mode == MODE_OFF:
        # Security review Cycle 1 Suggestion 2: `off` disabled verification with NO
        # signal anywhere, so an operator who set it in a deployed environment — or a
        # stale env var carried forward by a redeploy — would see a healthy runtime
        # silently accepting forged tokens. This is the one log line that makes that
        # state visible, and it is emitted once at import rather than per session.
        log.warning(
            "jwt_verify: %s=off — JWT signature verification is DISABLED. Every `sub` is "
            "trusted unverified. This is intended for local development ONLY; if this "
            "appears in a deployed runtime's logs, the runtime is accepting forged "
            "identities. Set %s=observe or =enforce and redeploy.",
            ENV_VERIFY_MODE, ENV_VERIFY_MODE,
        )
        return
    if mode != MODE_ENFORCE:
        return
    if not pyjwt_available():
        raise RuntimeError(
            f"{ENV_VERIFY_MODE}=enforce but PyJWT cannot be imported at all. Add "
            "`PyJWT[crypto]` to the ROOT requirements.txt (the only file the AgentCore "
            "toolkit installs) and redeploy. Refusing to start rather than reject every "
            "session or silently downgrade to observe."
        )
    if not crypto_available():
        raise RuntimeError(
            f"{ENV_VERIFY_MODE}=enforce but PyJWT cannot perform RS256: PyJWT imports "
            "but the `cryptography` package is missing from this image. Add it to the "
            "ROOT requirements.txt and redeploy. Refusing to start rather than reject "
            "every session or silently downgrade to observe."
        )
    if not trusted_pool_ids():
        raise RuntimeError(
            f"{ENV_VERIFY_MODE}=enforce but {ENV_TRUSTED_POOL_IDS} is empty. With no "
            "trusted pool there is no issuer to verify against, so every session would "
            "be rejected. Set the pool id(s) and redeploy."
        )


def _client_for(issuer: str) -> Any:
    """Return a cached PyJWKClient for *issuer*, creating one on first use."""
    client = _JWKS_CLIENTS.get(issuer)
    if client is None:
        from jwt import PyJWKClient  # noqa: PLC0415

        client = PyJWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=_JWKS_LIFESPAN_SECONDS,
            timeout=_JWKS_TIMEOUT_SECONDS,
        )
        _JWKS_CLIENTS[issuer] = client
    return client


def _unverified_issuer(token: str) -> Optional[str]:
    """Read `iss` WITHOUT verifying, solely to pick which allowlisted issuer to match.

    The value is never trusted: the caller checks it against issuers built from the
    configured pool ids, and a token whose `iss` is not in that set is rejected before
    any key is fetched. This function exists so we do not fetch JWKS for an
    attacker-named issuer.

    ``ImportError`` deliberately PROPAGATES. It is a deployment defect, not a malformed
    token, and :func:`verify_id_token` maps it to its own reason — an earlier version
    caught it here and returned ``None``, which reported a missing dependency as
    ``unparseable_token`` and would have sent an operator hunting a client bug.
    """
    import jwt as pyjwt  # noqa: PLC0415

    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 — a malformed token is simply not verifiable.
        return None
    iss = claims.get("iss") if isinstance(claims, Mapping) else None
    return iss if isinstance(iss, str) and iss else None


def verify_id_token(
    token: Optional[str],
    *,
    pool_ids: Optional[Sequence[str]] = None,
    region: Optional[str] = None,
) -> VerifyOutcome:
    """Verify a Cognito ID token. Never raises.

    Returns :class:`VerifyOutcome`. ``ok=False`` carries a short ``reason`` describing
    the failure class, never the token or the claims.
    """
    if not token or not isinstance(token, str):
        return VerifyOutcome(False, "missing_token")

    pools = list(pool_ids) if pool_ids is not None else trusted_pool_ids()
    if not pools:
        return VerifyOutcome(False, "no_trusted_pools_configured")

    resolved_region = region or os.environ.get("AWS_REGION") or "us-west-2"
    expected = {_issuer_for(p, resolved_region): p for p in pools}

    issuer: Optional[str]
    try:
        issuer = _unverified_issuer(token)
    except ImportError:
        # PyJWT absent from the image. A deployment defect, and it must not be
        # indistinguishable from a client sending garbage.
        return VerifyOutcome(False, "pyjwt_unavailable")
    if issuer is None:
        return VerifyOutcome(False, "unparseable_token")
    if issuer not in expected:
        # Do NOT fetch this issuer's JWKS. Trusting the token's own `iss` is the
        # confused-deputy bug: the attacker would supply a pool they control.
        return VerifyOutcome(False, "untrusted_issuer")

    if not crypto_available():
        # Distinct reason so an operator can tell a deployment defect from an
        # authentication failure. These must never look alike in logs.
        return VerifyOutcome(False, "crypto_unavailable")

    try:
        import jwt as pyjwt  # noqa: PLC0415

        signing_key = _client_for(issuer).get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            leeway=_LEEWAY_SECONDS,
            # `aud` intentionally unverified — see the module docstring. Every app
            # client in an allowlisted pool is ours, and signature verification
            # already defeats the cross-user forgery this module exists to stop.
            options={"verify_aud": False, "verify_exp": True, "verify_iat": True},
        )
    except Exception as exc:  # noqa: BLE001 — classify, never propagate.
        name = type(exc).__name__
        if name in ("ExpiredSignatureError",):
            return VerifyOutcome(False, "expired")
        if name in ("InvalidSignatureError", "InvalidAlgorithmError", "DecodeError"):
            return VerifyOutcome(False, "bad_signature")
        if name in ("PyJWKClientError", "PyJWKClientConnectionError", "URLError", "TimeoutError"):
            # JWKS unreachable. Fail closed in the outcome; the CALLER's mode decides
            # whether that ends the session. Never fall through to "verified".
            return VerifyOutcome(False, "jwks_unavailable")
        return VerifyOutcome(False, f"invalid_{name}")

    if not isinstance(claims, Mapping):
        return VerifyOutcome(False, "claims_not_an_object")

    token_use = claims.get("token_use")
    if token_use != "id":
        # An access token carries different claims and a different audience meaning;
        # accepting either interchangeably is how token-type confusion starts.
        return VerifyOutcome(False, "wrong_token_use")

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return VerifyOutcome(False, "no_sub")

    return VerifyOutcome(True, "verified", sub=sub.strip(), claims=claims)
