"""Cross-repo contract test: iOS client wire keys vs this Lambda's contract.

WHY THIS EXISTS
---------------
On 2026-07-28 the Discover iOS flow shipped against a backend that had been
verified end-to-end exclusively with `curl`. Both sides were individually
correct and individually tested; the *boundary* between them was not, and it
was wrong in two places at once:

1. The Swift request struct declared `runtimeSessionId`, which is what went on
   the wire. This handler requires `sessionId` and hard-rejects anything else
   with ``400 {"error": "sessionId is required"}``. Every Discover turn failed.

2. The Swift response struct required a `message` key. This handler returns
   `result`. The decode failed silently and the caller's fallback pushed the
   entire raw JSON body into the chat bubble as if it were the assistant's
   reply — a wrong-but-plausible render, which is worse than an error.

Unit tests on either side could not catch this. A contract test can: it reads
the ACTUAL key names out of both source files and asserts they agree.

APPROACH
--------
Deliberately source-parsing rather than live-calling, so the test needs no AWS
credentials, no network, and no simulator — it runs in CI and on a plane.

The CMS repo is expected as a sibling directory (same assumption
`scripts/update-staging-xcconfig.py` already makes). When it is absent the
test SKIPS rather than fails, so CVX-only checkouts stay green.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CVX_ROOT = Path(__file__).resolve().parents[3]
_CMS_ROOT = _CVX_ROOT.parent / "connected-mobility-guidance-on-aws"
_HANDLER = Path(__file__).resolve().parents[1] / "handler.py"
_SWIFT_MODEL = (
    _CMS_ROOT
    / "clients/ios/VSACompanion/Views/Acquire/DiscoverFlowModel.swift"
)

# Keys this handler reads from the request body but tolerates being absent.
# `vin` is optional; `personaId` is an optional narrow override.
_OPTIONAL_REQUEST_KEYS = {
    "vin",
    "personaId",
    # Optional: absent falls back to "fleet", the broadest segment. The
    # client sends it so OEM customers reach the authorised-dealer branch
    # of find_service_center, but an older client still works.
    "tenantSegment",
    # Optional: the visitor's unwrapped utterance, for the kiosk guardrail to
    # judge instead of the client-assembled prompt. Only clients that WRAP
    # their prompts need it — the CMS web ChatAgent sends `prompt` verbatim and
    # has nothing to separate out. Absent means the runtime guards the whole
    # prompt, which over-blocks a wrapping client but never leaves one
    # unguarded. `TestGuardedSpanContract` below pins that the iOS client,
    # which DOES wrap, sends it.
    "userText",
}


def _require_files() -> None:
    if not _SWIFT_MODEL.exists():
        pytest.skip(
            f"CMS repo not present at {_CMS_ROOT} — cross-repo contract test skipped"
        )


def _handler_src() -> str:
    return _HANDLER.read_text()


def _swift_src() -> str:
    return _SWIFT_MODEL.read_text()


def _handler_request_keys() -> set[str]:
    """Keys the handler reads out of the JSON request body."""
    return set(re.findall(r'body\.get\(\s*"([A-Za-z_][A-Za-z0-9_]*)"', _handler_src()))


def _handler_required_request_keys() -> set[str]:
    return _handler_request_keys() - _OPTIONAL_REQUEST_KEYS


def _swift_request_wire_keys() -> set[str]:
    """Keys the Swift `AssistantChatRequest` actually puts on the wire.

    Swift's `Encodable` emits the property name unless a `CodingKeys` case
    remaps it, so the wire key is the right-hand side of `case x = "y"` when
    present and the bare case name otherwise.
    """
    src = _swift_src()
    struct = re.search(
        r"struct\s+AssistantChatRequest\s*:\s*Encodable\s*\{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert struct, "AssistantChatRequest not found in DiscoverFlowModel.swift"
    body = struct.group(1)

    enum = re.search(r"enum\s+CodingKeys[^{]*\{(.*?)\}", body, re.DOTALL)
    if not enum:
        # No remapping: wire keys are the declared property names.
        return set(re.findall(r"^\s*let\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.MULTILINE))

    keys: set[str] = set()
    for line in enum.group(1).splitlines():
        line = line.split("//", 1)[0].strip()
        if not line.startswith("case "):
            continue
        for item in line[len("case "):].split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                _, wire = item.split("=", 1)
                keys.add(wire.strip().strip('"'))
            else:
                keys.add(item)
    return keys


def _swift_response_accepted_keys() -> set[str]:
    """Keys the Swift `AssistantChatResponse` will accept for the reply text."""
    src = _swift_src()
    struct = re.search(
        r"struct\s+AssistantChatResponse\s*:\s*Decodable\s*\{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert struct, "AssistantChatResponse not found in DiscoverFlowModel.swift"
    enum = re.search(r"enum\s+CodingKeys[^{]*\{(.*?)\}", struct.group(1), re.DOTALL)
    assert enum, "AssistantChatResponse has no CodingKeys — cannot verify contract"
    keys: set[str] = set()
    for line in enum.group(1).splitlines():
        line = line.split("//", 1)[0].strip()
        if not line.startswith("case "):
            continue
        for item in line[len("case "):].split(","):
            item = item.strip().strip('"')
            if item:
                keys.add(item.split("=")[-1].strip().strip('"'))
    return keys


class TestRequestContract:
    def test_swift_sends_every_key_the_handler_requires(self):
        """The exact regression that produced `400 sessionId is required`."""
        _require_files()
        required = _handler_required_request_keys()
        sent = _swift_request_wire_keys()
        missing = required - sent
        assert not missing, (
            f"iOS does not send required request key(s) {sorted(missing)}. "
            f"Handler requires {sorted(required)}; Swift sends {sorted(sent)}. "
            f"Remap via AssistantChatRequest.CodingKeys."
        )

    def test_session_id_is_the_wire_key(self):
        """Pin the specific name, not just set membership."""
        _require_files()
        assert "sessionId" in _swift_request_wire_keys(), (
            "iOS must send `sessionId`. It previously sent `runtimeSessionId`, "
            "which this handler rejects with HTTP 400."
        )
        assert "runtimeSessionId" not in _swift_request_wire_keys(), (
            "`runtimeSessionId` must not reach the wire — the handler does not "
            "read it, so the request would be rejected as missing `sessionId`."
        )

    def test_handler_still_requires_session_id(self):
        """Guard the other direction: handler contract changing under the client."""
        assert 'body.get("sessionId")' in _handler_src(), (
            "Handler no longer reads `sessionId` — update the iOS CodingKeys "
            "mapping in DiscoverFlowModel.swift in the same change."
        )


class TestResponseContract:
    def test_swift_accepts_the_key_the_handler_returns(self):
        """The regression that dumped raw JSON into the chat bubble."""
        _require_files()
        accepted = _swift_response_accepted_keys()
        assert "result" in accepted, (
            "iOS must accept `result` — this handler returns "
            '{"result": ..., "sessionId": ...}. Accepting only `message` makes '
            "the decode fail silently and renders the raw JSON body as the reply."
        )

    def test_handler_still_returns_result(self):
        assert '"result"' in _handler_src(), (
            "Handler no longer returns `result` — update "
            "AssistantChatResponse.init(from:) in the same change."
        )


class TestGuardedSpanContract:
    """The iOS client wraps its prompts, so it must send `userText`.

    Three parties have to agree for the kiosk guardrail to judge the visitor's
    words rather than our own scaffolding, and they live in two repositories:

      1. Swift puts `userText` on the wire,
      2. this handler forwards it into the runtime payload,
      3. the runtime guards it, falling back to `prompt` when absent.

    Any one of them silently reverting restores the defect in
    `issues/2026-08-18-kiosk-guardrail-blocks-own-prompt-scaffolding/`, where
    PROMPT_ATTACK fired on our own vehicle-facts block and blocked every
    Discover turn carrying vehicle context. The failure mode is a refusal
    message, not an error, so nothing else would notice.
    """

    def test_swift_sends_user_text(self):
        _require_files()
        assert "userText" in _swift_request_wire_keys(), (
            "iOS must send `userText`. Without it the runtime guards the "
            "assembled prompt, whose vehicle-facts block alone is enough to "
            "trip the PROMPT_ATTACK content filter."
        )

    def test_swift_does_not_send_the_wrapper_as_user_text(self):
        """`userText` must be the raw utterance, not `promptWithHistory(...)`."""
        _require_files()
        src = _swift_src()
        init = re.search(r"AssistantChatRequest\(\s*(.*?)\n\s*\)", src, re.DOTALL)
        assert init, "AssistantChatRequest construction not found"
        args = init.group(1)
        user_text_arg = re.search(r"userText:\s*([^,\n]+)", args)
        assert user_text_arg, "userText is declared but never populated"
        value = user_text_arg.group(1).strip()
        assert "promptWithHistory" not in value, (
            f"userText is populated with {value!r} — passing the wrapped prompt "
            "here reintroduces the defect while appearing to fix it."
        )

    def test_handler_forwards_user_text_to_the_runtime(self):
        src = _handler_src()
        assert 'body.get("userText")' in src, "handler no longer reads userText"
        assert 'runtime_payload["userText"]' in src, (
            "handler reads userText but no longer forwards it — the runtime "
            "would silently fall back to guarding the whole prompt."
        )

    def test_runtime_guards_user_text_with_a_prompt_fallback(self):
        """Reach into the runtime source: it is the third party to this contract."""
        app = _CVX_ROOT / "agents/supervisor/agentcore_app.py"
        assert app.exists(), f"runtime entrypoint missing at {app}"
        src = app.read_text()
        assert 'payload.get("userText")' in src, (
            "runtime no longer reads userText — the guarded span silently "
            "reverts to the client-assembled prompt."
        )
        assert "_guarded_input" in src and "check_input(_guarded_input)" in src, (
            "runtime must guard the resolved span, not `prompt` directly."
        )
        assert "or prompt" in src, (
            "the fallback to `prompt` must remain: a client that sends no "
            "userText has to stay guarded, not become unguarded."
        )
