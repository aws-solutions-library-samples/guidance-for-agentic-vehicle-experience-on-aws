"""Tests for `agents/supervisor/jwt_verify.py` (Fix Group 18 T18.6).

These tests use **real RSA keys and real signatures**. Only the JWKS *fetch* is
stubbed — by pre-populating the module's issuer→client cache — so `jwt.decode`
performs genuine cryptographic verification.

That distinction is the whole point, and this session earned it the hard way: a
stubbed client cannot reject what the service rejects, and a writer and reader
sharing a constant agree with each other while both disagree with reality. A test
that mocked `decode` would pass against a module that verified nothing.

The load-bearing test is
:func:`test_a_token_signed_by_an_attacker_key_naming_another_users_sub_is_rejected`
— that is the exact attack this module exists to stop.
"""

from __future__ import annotations

import time

import pytest

from agents.supervisor import jwt_verify as V

pytest.importorskip("cryptography", reason="RS256 verification requires cryptography")
pyjwt = pytest.importorskip("jwt", reason="PyJWT required")

REGION = "us-west-2"
# Deliberately NOT real-pool-shaped. `verify_id_token` treats a pool id as an opaque
# string — it only interpolates it into the issuer URL — so these need no realistic
# shape, and a realistic one would ship a publish-scanner critical
# (`cognito_user_pool_id`). That regex matches *placeholders* too: a pool-shaped value
# with the id replaced by X's is still flagged, which is why the fix is a different
# SHAPE rather than a fake value.
#
# This comment previously made that point by writing such a placeholder out in full, and
# so shipped the 7th scanner critical of its own accord — a comment explaining the trap,
# demonstrating it. That is the rule from ~/.kiro/steering/public-mirror-publish.md in
# miniature: naming a forbidden value to guard against it embeds the forbidden value.
# Describe the shape; do not spell it.
#
# Real pool ids live only in the deploy path and the live-verify script, resolved at
# runtime from CloudFormation and the identity pool.
POOL = "test-pool-trusted-primary"      # stands in for the CVX tenant pool
OTHER_POOL = "test-pool-trusted-extra"  # stands in for the CMS pool (also trusted)
EVIL_POOL = "test-pool-attacker"
VICTIM_SUB = "c8811380-c061-7085-0078-156412f0f674"
ATTACKER_SUB = "aaaa1111-bbbb-2222-cccc-333344445555"


def _keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


# Two independent keypairs: the pool's, and an attacker's. Generated once —
# 2048-bit keygen is slow enough to matter across a few dozen tests.
_POOL_PRIV, _POOL_PUB = _keypair()
_EVIL_PRIV, _EVIL_PUB = _keypair()


def _issuer(pool: str = POOL) -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{pool}"


def _mint(
    *,
    priv=None,
    sub: str = VICTIM_SUB,
    iss: str | None = None,
    token_use: str = "id",
    exp_offset: int = 3600,
    alg: str = "RS256",
    extra: dict | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": iss if iss is not None else _issuer(),
        "aud": "58s5if7gpcpembdbu1jrm85onr",
        "token_use": token_use,
        "iat": now - 10,
        "exp": now + exp_offset,
    }
    if extra:
        claims.update(extra)
    # `drop` removes a claim ENTIRELY, which is distinct from setting it to None.
    # "absent" and "present but null" are different inputs and a verifier can treat
    # them differently, so both need to be expressible.
    for key_name in drop:
        claims.pop(key_name, None)
    key = _POOL_PRIV if priv is None else priv
    return pyjwt.encode(claims, key, algorithm=alg)


class _FakeJwksClient:
    """Stubs ONLY the network fetch. The key it returns is real."""

    def __init__(self, public_key, *, raise_exc: Exception | None = None) -> None:
        self._public_key = public_key
        self._raise = raise_exc
        self.calls = 0

    def get_signing_key_from_jwt(self, token):  # noqa: ANN001
        self.calls += 1
        if self._raise is not None:
            raise self._raise

        class _Key:
            key = self._public_key

        return _Key()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate module state and env between tests."""
    monkeypatch.delenv(V.ENV_VERIFY_MODE, raising=False)
    monkeypatch.delenv(V.ENV_TRUSTED_POOL_IDS, raising=False)
    V._JWKS_CLIENTS.clear()
    yield
    V._JWKS_CLIENTS.clear()


def _install_jwks(public_key=_POOL_PUB, *, pool: str = POOL, raise_exc=None):
    client = _FakeJwksClient(public_key, raise_exc=raise_exc)
    V._JWKS_CLIENTS[_issuer(pool)] = client
    return client


# ---------------------------------------------------------------------------
# The core threat
# ---------------------------------------------------------------------------


def test_a_valid_token_from_a_trusted_pool_verifies():
    _install_jwks()
    out = V.verify_id_token(_mint(), pool_ids=[POOL], region=REGION)
    assert out.ok, out.reason
    assert out.reason == "verified"
    assert out.sub == VICTIM_SUB


def test_a_token_signed_by_an_attacker_key_naming_another_users_sub_is_rejected():
    """THE test. This is the attack the module exists to stop.

    A signed-in attacker mints a token claiming the victim's `sub`, signed with
    their own key. Before T18.6 the runtime believed it outright. Real crypto
    here: the pool's public key genuinely cannot validate that signature.
    """
    _install_jwks(_POOL_PUB)
    forged = _mint(priv=_EVIL_PRIV, sub=VICTIM_SUB)

    out = V.verify_id_token(forged, pool_ids=[POOL], region=REGION)

    assert not out.ok
    assert out.reason == "bad_signature"
    assert out.sub is None, "a rejected token must not surface a sub to the caller"


def test_an_attackers_own_valid_token_cannot_claim_a_different_sub():
    """Signature validity does not launder a swapped `sub`.

    The attacker holds a genuine token for themselves. Editing the payload to name
    the victim invalidates the signature, which is exactly the property wanted.
    """
    _install_jwks(_POOL_PUB)
    genuine = _mint(sub=ATTACKER_SUB)
    header, payload, sig = genuine.split(".")
    tampered_payload = _mint(sub=VICTIM_SUB).split(".")[1]
    tampered = f"{header}.{tampered_payload}.{sig}"

    out = V.verify_id_token(tampered, pool_ids=[POOL], region=REGION)
    assert not out.ok
    assert out.reason == "bad_signature"


# ---------------------------------------------------------------------------
# Issuer allowlist — direction matters
# ---------------------------------------------------------------------------


def test_an_untrusted_issuer_is_rejected_without_fetching_its_jwks():
    """The confused-deputy guard.

    Reading `iss` from the token and fetching THAT issuer's JWKS would let an
    attacker point at a pool they control and sign with their own key. Asserting
    the fetch never happens is what pins the direction of the check — a version
    that fetched first and compared later would still return False here, so a
    reason-only assertion would not catch it.
    """
    evil_client = _install_jwks(_EVIL_PUB, pool=EVIL_POOL)
    token = _mint(priv=_EVIL_PRIV, iss=_issuer(EVIL_POOL))

    out = V.verify_id_token(token, pool_ids=[POOL], region=REGION)

    assert not out.ok
    assert out.reason == "untrusted_issuer"
    assert evil_client.calls == 0, "JWKS was fetched for an issuer the token chose"


def test_multiple_trusted_pools_are_all_accepted():
    """Two pools are legitimately trusted: CVX's own and the CMS pool iOS signs into."""
    _install_jwks(_POOL_PUB, pool=OTHER_POOL)
    token = _mint(iss=_issuer(OTHER_POOL))

    out = V.verify_id_token(token, pool_ids=[POOL, OTHER_POOL], region=REGION)
    assert out.ok, out.reason


def test_no_configured_pools_fails_closed():
    """Empty config must not mean 'accept anything'."""
    _install_jwks()
    out = V.verify_id_token(_mint(), pool_ids=[], region=REGION)
    assert not out.ok
    assert out.reason == "no_trusted_pools_configured"


# ---------------------------------------------------------------------------
# Algorithm confusion
# ---------------------------------------------------------------------------


def test_hs256_signed_with_the_public_key_is_rejected():
    """Classic algorithm-confusion attack, hand-crafted.

    The attacker takes the pool's PUBLIC key (published in the JWKS, so not a
    secret) and uses it as an HMAC secret with alg=HS256. A verifier that honours
    the token's own `alg` accepts it. Pinning `algorithms=["RS256"]` stops it.

    Built by hand with `hmac`/`hashlib` rather than `pyjwt.encode`, because PyJWT's
    ENCODER refuses an asymmetric key as an HMAC secret (`InvalidKeyError`). That
    guard protects whoever mints tokens; it does nothing for whoever verifies them,
    and the decoder is what is under test here. Using the encoder would have made
    this test unwritable and the protection unverified.
    """
    import base64
    import hashlib
    import hmac
    import json as _json

    from cryptography.hazmat.primitives import serialization

    pub_pem = _POOL_PUB.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    header = b64(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        _json.dumps(
            {
                "sub": VICTIM_SUB,
                "iss": _issuer(),
                "token_use": "id",
                "iat": int(time.time()) - 10,
                "exp": int(time.time()) + 3600,
            }
        ).encode()
    )
    signing_input = header + b"." + payload
    sig = b64(hmac.new(pub_pem, signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + sig).decode()

    _install_jwks(_POOL_PUB)
    out = V.verify_id_token(forged, pool_ids=[POOL], region=REGION)

    assert not out.ok, "an HS256-confusion token was ACCEPTED — algorithms= is not pinned"
    assert out.reason in (
        "bad_signature",
        "invalid_InvalidAlgorithmError",
    ), out.reason


def test_alg_none_is_rejected():
    _install_jwks(_POOL_PUB)
    unsigned = pyjwt.encode(
        {"sub": VICTIM_SUB, "iss": _issuer(), "token_use": "id",
         "exp": int(time.time()) + 3600},
        key="",
        algorithm="none",
    )
    out = V.verify_id_token(unsigned, pool_ids=[POOL], region=REGION)
    assert not out.ok


# ---------------------------------------------------------------------------
# Claim validation
# ---------------------------------------------------------------------------


def test_expired_token_is_rejected():
    _install_jwks()
    out = V.verify_id_token(
        _mint(exp_offset=-3600), pool_ids=[POOL], region=REGION
    )
    assert not out.ok
    assert out.reason == "expired"


def test_access_token_is_rejected_even_when_correctly_signed():
    """Token-type confusion: an access token carries different claims and audience
    semantics, so accepting either interchangeably is a real hazard."""
    _install_jwks()
    out = V.verify_id_token(
        _mint(token_use="access"), pool_ids=[POOL], region=REGION
    )
    assert not out.ok
    assert out.reason == "wrong_token_use"


def test_token_without_a_sub_is_rejected():
    _install_jwks()
    out = V.verify_id_token(
        _mint(extra={"sub": ""}), pool_ids=[POOL], region=REGION
    )
    assert not out.ok
    assert out.reason in ("no_sub", "bad_signature"), out.reason


@pytest.mark.parametrize("bad", [None, "", "not-a-jwt", "only.two", 12345])
def test_malformed_input_never_raises(bad):
    _install_jwks()
    out = V.verify_id_token(bad, pool_ids=[POOL], region=REGION)
    assert not out.ok


# ---------------------------------------------------------------------------
# JWKS unavailability must fail CLOSED
# ---------------------------------------------------------------------------


def test_jwks_unavailable_fails_closed_and_is_distinguishable():
    """A network failure must never read as 'verified'.

    It also gets its own reason: an operator has to be able to tell a JWKS outage
    from an authentication failure, because the responses differ completely.
    """
    from jwt.exceptions import PyJWKClientError

    _install_jwks(_POOL_PUB, raise_exc=PyJWKClientError("boom"))
    out = V.verify_id_token(_mint(), pool_ids=[POOL], region=REGION)

    assert not out.ok
    assert out.reason == "jwks_unavailable"


def test_an_unexpected_exception_still_fails_closed():
    _install_jwks(_POOL_PUB, raise_exc=ValueError("something odd"))
    out = V.verify_id_token(_mint(), pool_ids=[POOL], region=REGION)
    assert not out.ok
    assert out.ok is False


# ---------------------------------------------------------------------------
# Mode + configuration handling
# ---------------------------------------------------------------------------


def test_mode_defaults_to_observe(monkeypatch):
    assert V.verify_mode() == V.MODE_OBSERVE


@pytest.mark.parametrize("raw,expected", [
    ("off", V.MODE_OFF), ("observe", V.MODE_OBSERVE), ("enforce", V.MODE_ENFORCE),
    ("ENFORCE", V.MODE_ENFORCE), ("  observe  ", V.MODE_OBSERVE),
])
def test_mode_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(V.ENV_VERIFY_MODE, raw)
    assert V.verify_mode() == expected


def test_an_unrecognised_mode_falls_back_to_observe_not_off(monkeypatch):
    """A typo must not silently disable the control.

    Falling back to `off` would mean `VSA_JWT_VERIFY_MODE=enfroce` turns
    verification off entirely while looking configured.
    """
    monkeypatch.setenv(V.ENV_VERIFY_MODE, "enfroce")
    assert V.verify_mode() == V.MODE_OBSERVE


def test_pool_ids_are_parsed_deduped_and_stripped(monkeypatch):
    monkeypatch.setenv(V.ENV_TRUSTED_POOL_IDS, f" {POOL}, {OTHER_POOL} ,{POOL}, ")
    assert V.trusted_pool_ids() == [POOL, OTHER_POOL]


def test_assert_enforceable_is_a_noop_outside_enforce(monkeypatch):
    monkeypatch.setenv(V.ENV_VERIFY_MODE, "observe")
    V.assert_enforceable()  # must not raise


def test_assert_enforceable_raises_when_enforce_has_no_pools(monkeypatch):
    """Enforcing with no trusted pool would reject every session."""
    monkeypatch.setenv(V.ENV_VERIFY_MODE, "enforce")
    monkeypatch.delenv(V.ENV_TRUSTED_POOL_IDS, raising=False)
    with pytest.raises(RuntimeError, match="TRUSTED_USER_POOL_IDS"):
        V.assert_enforceable()


def test_assert_enforceable_raises_when_enforce_has_no_crypto(monkeypatch):
    """The `redis` failure shape: a dependency absent from the image.

    Refusing to start is deliberate — silently downgrading to observe would leave
    an operator believing enforcement is on, and enforcing without crypto would
    reject every session with a misleading cause.
    """
    monkeypatch.setenv(V.ENV_VERIFY_MODE, "enforce")
    monkeypatch.setenv(V.ENV_TRUSTED_POOL_IDS, POOL)
    monkeypatch.setattr(V, "crypto_available", lambda: False)
    with pytest.raises(RuntimeError, match="cryptography"):
        V.assert_enforceable()


def test_crypto_unavailable_is_its_own_reason(monkeypatch):
    """Distinguishable from an auth failure — they demand different responses."""
    _install_jwks()
    monkeypatch.setattr(V, "crypto_available", lambda: False)
    out = V.verify_id_token(_mint(), pool_ids=[POOL], region=REGION)
    assert not out.ok
    assert out.reason == "crypto_unavailable"


def test_pyjwt_available_is_true_in_this_venv():
    """Positive control. Without it the negative test below could pass vacuously."""
    assert V.pyjwt_available() is True
    assert V.crypto_available() is True


def test_pyjwt_missing_is_not_reported_as_a_malformed_token(monkeypatch):
    """PyJWT absent must NOT surface as `unparseable_token`.

    The regression this pins: `_unverified_issuer` used to catch `Exception`, which
    swallowed the `ImportError` and returned None, so a missing dependency was reported
    with the same reason as a client sending garbage. That sends an operator hunting a
    client bug while the real fault is the image — the exact deployment-defect-versus-
    auth-failure conflation `verify_id_token` is documented to avoid.

    `sys.modules["jwt"] = None` is what makes this test non-vacuous. An earlier version
    monkeypatched `_unverified_issuer` itself to raise, which asserted only that
    `verify_id_token` *handles* a propagated ImportError — it could not see whether the
    real function propagates one. Restoring the defective body left that version green
    (mutation M19). Blocking the import instead exercises the actual code path: Python
    raises ImportError when a `sys.modules` entry is None.
    """
    import sys

    monkeypatch.setitem(sys.modules, "jwt", None)

    # The callee must PROPAGATE, not swallow. This is the assertion M19 defeated.
    with pytest.raises(ImportError):
        V._unverified_issuer("a.b.c")

    # And the caller must map that to its own reason.
    out = V.verify_id_token("a.b.c", pool_ids=[POOL], region=REGION)
    assert not out.ok
    assert out.reason == "pyjwt_unavailable", (
        f"expected pyjwt_unavailable, got {out.reason!r} — a missing PyJWT must be "
        "distinguishable from a malformed token in logs."
    )


def test_assert_enforceable_names_pyjwt_when_pyjwt_itself_is_missing(monkeypatch):
    """PyJWT-missing and cryptography-missing need different remedies, so different messages.

    Both are fixed in requirements.txt, but one adds `PyJWT[crypto]` and the other adds
    `cryptography`; a single shared message would point at the wrong line half the time.
    """
    monkeypatch.setenv(V.ENV_VERIFY_MODE, "enforce")
    monkeypatch.setenv(V.ENV_TRUSTED_POOL_IDS, POOL)
    monkeypatch.setattr(V, "pyjwt_available", lambda: False)
    with pytest.raises(RuntimeError, match="PyJWT cannot be imported"):
        V.assert_enforceable()


#: Sentinel for "the claim is absent entirely", which is a different input from
#: "the claim is present and null". A bare None cannot express both.
_ABSENT = object()


@pytest.mark.parametrize(
    "token_use",
    ["access", "identity", "id_token", "ID", "Id", " id", "id ", "", None, 0, ["id"], _ABSENT],
)
def test_token_use_must_be_exactly_id(token_use):
    """Only the literal string `"id"` may pass. Not a substring, not a case variant.

    Added from security review Cycle 1 Suggestion 1, which found this invariant
    **not test-enforced**: two mutations SURVIVED the suite —
    `if "id" not in str(token_use)` (substring) and `if token_use == "access"`
    (denylist). Both are exploitable-shaped even though neither is exploitable today,
    because Cognito's IdP only ever emits `id` or `access`.

    The substring form is the worse of the two: it admits `id_token` and `identity`,
    and `str(None)` is `'None'` which contains no `id`, so it happens to reject the
    missing case for the wrong reason. The denylist form admits *everything* except
    the one value it names — the same failure shape as the live-verify anti-vacuity
    guard earlier in this spec, which was first written as a denylist and cleanly
    admitted an admin principal.

    Accepting an access token interchangeably with an ID token is how token-type
    confusion starts: it carries different claims and a different audience meaning.
    """
    _install_jwks()
    if token_use is _ABSENT:
        token = _mint(drop=("token_use",))
    else:
        token = _mint(token_use=token_use)
    out = V.verify_id_token(token, pool_ids=[POOL], region=REGION)
    assert not out.ok, f"token_use={token_use!r} was accepted; only the literal 'id' may pass"
    assert out.reason == "wrong_token_use"


def test_token_use_exactly_id_is_accepted():
    """Positive control. Without it the parametrized rejection test could pass vacuously."""
    _install_jwks()
    out = V.verify_id_token(_mint(), pool_ids=[POOL], region=REGION)
    assert out.ok and out.reason == "verified"


def test_outcome_never_carries_claims_on_failure():
    """A rejected token must not leak its payload to the caller."""
    _install_jwks(_POOL_PUB)
    out = V.verify_id_token(
        _mint(priv=_EVIL_PRIV), pool_ids=[POOL], region=REGION
    )
    assert out.claims is None
    assert out.sub is None


def test_reason_strings_never_contain_the_token_or_a_sub():
    """`reason` is logged; it must be safe to log."""
    _install_jwks(_POOL_PUB)
    token = _mint(priv=_EVIL_PRIV, sub=VICTIM_SUB)
    for out in (
        V.verify_id_token(token, pool_ids=[POOL], region=REGION),
        V.verify_id_token("garbage", pool_ids=[POOL], region=REGION),
        V.verify_id_token(_mint(exp_offset=-1), pool_ids=[POOL], region=REGION),
    ):
        assert VICTIM_SUB not in out.reason
        assert token[:20] not in out.reason
        assert len(out.reason) < 60, f"reason looks like a dump: {out.reason!r}"
