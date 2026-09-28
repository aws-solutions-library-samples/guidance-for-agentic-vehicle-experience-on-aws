#!/usr/bin/env python3
"""Consolidated mutation battery for the AVX executor package.

Not a pytest module — run it directly:

    .venv/bin/python agents/executors/tests/mutation_battery.py

For each entry: break the property in the source, run the guarding test(s),
assert the run FAILS, then restore. A test that still passes against the broken
source asserts the construct's presence, not the property — which is the defect
class this package kept producing (see `~/.kiro/steering/testing.md` § "Mutation
testing at the green boundary").

Committed rather than left in /tmp for three reasons: the ad-hoc scripts were
lost to /tmp cleanup mid-session; the evidence behind "N mutations verified" is
otherwise unreproducible by the next reader; and two operational lessons are
encoded here that cost real time to learn:

1.  **__pycache__ must be cleared between mutations.** Consecutive write/restore
    cycles within the same second defeat .pyc invalidation, which produced false
    NOT-CAUGHT results twice.
2.  **A mutation must change BEHAVIOUR, not just text.** Early anchors hit a
    docstring, a constant definition, and 1-of-3 call sites; each reported a
    false gap. Anchors here are behavioural and asserted present before use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EX = ROOT / "agents" / "executors"
ALL = "tests/"

# (label, relative path, find, replace, pytest selector)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -- Addendum D5: the declined / failed distinction ----------------------
    ("D5: terminal 4xx routed to failed instead of declined",
     "dms.py", "return ExecuteResult.refused(", "return ExecuteResult.terminal_error(",
     "tests/test_dms.py"),
    ("D5: client-error 4xx (401/403/404) recorded as a dealer refusal",
     "base.py", '    if status_code in CLIENT_ERROR_4XX:\n        return "client_error"',
     '    if False:\n        return "client_error"', ALL),
    ("D5: 408/425 no longer transient (a timeout read as a refusal)",
     "base.py", "_NON_REFUSAL_4XX = frozenset({408, 425, 429})",
     "_NON_REFUSAL_4XX = frozenset({429})", ALL),
    ("D5: failed allowed to carry escalate=False",
     "base.py", "        if self.status == ActionStatus.FAILED and not self.escalate:",
     "        if False and self.status == ActionStatus.FAILED and not self.escalate:",
     "tests/test_base.py"),
    ("D5: a 2xx accepted as an error status on a failure result",
     "base.py", "        if 200 <= code < 300:", "        if False and 200 <= code < 300:",
     "tests/test_base.py"),
    ("D5: target_refused accepted with a 5xx",
     "base.py", "            if not (400 <= code < 500):",
     "            if False and not (400 <= code < 500):", "tests/test_base.py"),
    # Closes the audit gap found 2026-09-20: this property (review Cycle 3 Warning 3,
    # tracked on the portfolio backlog as W3 of `Group 5 test-pinning debt`) WAS pinned
    # by three named tests, but had no battery entry — so anyone measuring coverage by
    # running the battery alone read it as unpinned. Same shape as the finding the
    # battery exists to catch, one level up: the pin was real, the inventory was wrong.
    # Verified by hand before being added here — deleting the isinstance check makes
    # dms report 'AttributeError' instead of 'InvalidJsonResponse', because a 2xx then
    # reaches a failure-status construction.
    ("D5: dms 2xx non-object body no longer labelled InvalidJsonResponse",
     "dms.py", "                    if not isinstance(body, dict):",
     "                    if False:",
     "tests/test_dms.py::test_2xx_with_non_object_body_reports_invalid_json_response"),

    # -- Escalation: fired once, for every target, honestly reported ---------
    ("escalation: registry stops firing (chat/ios_owner/never_raises reach nobody)",
     "registry.py", '    if fire_escalation and getattr(result, "escalate", False):',
     "    if False:", "tests/test_registry.py"),
    ("escalation: fired twice (double owner card, doubled failure metric)",
     "registry.py", "            escalation.fire(",
     "            escalation.fire(\n                action=action, claims=claims, target_system=target,\n                result=result,\n                **(dict(escalation_clients) if escalation_clients else {}),\n            )\n            escalation.fire(",
     "tests/test_registry.py"),
    # Behavioural, NOT textual: narrowing the except clause leaves the module
    # importable while removing the guard. The first version of this entry
    # replaced `try:` with `if True:`, dangling the `except` — the module failed
    # to import, pytest errored, and the run was scored CAUGHT. It proved only
    # that the file parses, and it hid a real Critical for a whole cycle. Verify
    # that a mutation changes BEHAVIOUR before believing a CAUGHT result.
    ("escalation: dispatch's guard narrowed, so it destroys the ExecuteResult",
     "registry.py", "        except Exception as exc:  # noqa: BLE001",
     "        except KeyboardInterrupt as exc:  # noqa: BLE001",
     "tests/test_registry.py::TestEscalationFailureDoesNotDestroyTheResult"),
    # The regression path here is a tidy-imports pass: the import carries
    # `# noqa: PLC0415`, a marker whose meaning is "a linter wants this hoisted".
    ("escalation: lazy import hoisted above the guard (ImportError destroys the result)",
     "registry.py",
     "        try:\n            # Imported INSIDE the try, not above it",
     "        from agents.executors import escalation  # noqa: PLC0415\n\n        try:\n            # HOISTED",
     "tests/test_registry.py::TestEscalationFailureDoesNotDestroyTheResult"),
    ("escalation: dispatch's guard drops the module logger (NameError in the handler)",
     "registry.py", "log = logging.getLogger(__name__)", "_unused_log = None",
     "tests/test_registry.py::TestEscalationFailureDoesNotDestroyTheResult"),
    ("escalation: ios-owner recursion guard removed (escalates through itself)",
     "escalation.py", "    if target_system == _OWNER_FEED_TARGET:", "    if False:",
     "tests/test_escalation.py"),
    ("escalation: owner card reuses the Action's key (put_item replaces the Action)",
     "escalation.py",
     '        "action_id": f"{source_action_id}#escalation" if source_action_id else None,',
     '        "action_id": source_action_id,', ALL),
    ("escalation: stored row's source_action_id points at itself",
     "ios_owner.py", '            "source_action_id": action.get("source_action_id") or action_id,',
     '            "source_action_id": action_id,', "tests/test_escalation.py"),
    ("escalation: owner_visible reported True when the card write failed",
     "escalation.py", "                if status is None or status not in _FAILED_STATUSES:",
     "                if True:", ALL),
    ("escalation: operator half suppressed when the owner half fails",
     "escalation.py", '    op = dict(DEFAULT_ESCALATION["operator_visible"])',
     '    if not outcome["owner_visible"]:\n        return outcome\n    op = dict(DEFAULT_ESCALATION["operator_visible"])',
     "tests/test_escalation.py"),
    ("escalation: SSM can rename the operator alarm keys (silences the alarm)",
     "escalation.py", '    op = dict(DEFAULT_ESCALATION["operator_visible"])',
     '    op = dict(cfg.get("operator_visible") or {})', "tests/test_escalation.py"),
    ("escalation: owner-facing category/severity frozen (pinning over-applied)",
     "escalation.py", '    owner_cfg = dict(cfg.get("owner_visible") or {})',
     '    owner_cfg = dict(DEFAULT_ESCALATION["owner_visible"])', "tests/test_escalation.py"),

    # -- Authorization and credential handling ------------------------------
    ("authz: retail sources tenantId from the action instead of claims",
     "retail.py", "_extract_tenant_id(claims)",
     '((action.get("parameters") or {}).get("tenantId"))', "tests/test_retail.py"),
    ("authz: retail sends no Authorization header (confused deputy)",
     "retail.py",
     '                        "Authorization": f"Bearer {jwt}",\n', "",
     "tests/test_retail.py"),
    ("authz: retail accepts a missing JWT and sends anonymously",
     "retail.py", '        if not jwt:\n            missing_jwt = ExecuteResult.terminal_error(',
     '        if False:\n            missing_jwt = ExecuteResult.terminal_error(',
     "tests/test_retail.py"),
    ("authz: vehicle_command drops the forwarded JWT header",
     "vehicle_command.py", '"Authorization"', '"X-Not-Authorization"',
     "tests/test_vehicle_command.py"),
    ("authz: chat prefers action.owner_id, enabling a cross-owner marker write",
     "chat.py", "        if caller_sub and claimed_owner and claimed_owner != caller_sub:",
     "        if False:", "tests/test_chat.py"),
    ("authz: ios_owner writes a card with an empty owner_id and reports success",
     "ios_owner.py", "if not owner_id:", "if False:", "tests/test_ios_owner.py"),

    # -- Dispatch-target boundary -------------------------------------------
    ("boundary: unknown target falls back to AdvisoryExecutor (silent success)",
     "registry.py", "    try:\n        return EXECUTORS[target_system]\n    except KeyError:",
     '    try:\n        return EXECUTORS[target_system]\n    except KeyError:\n        return EXECUTORS["none"]\n    if False:',
     "tests/test_registry.py"),
    ("boundary: registry key/executor mismatch validator removed",
     "registry.py", "        if declared != key:", "        if False and declared != key:",
     "tests/test_registry.py"),
    ("boundary: Tier 2 import scanner stops matching agents.executors",
     "tests/test_registry.py",
     'return len(parts) >= 2 and parts[0] == "agents" and parts[1] == "executors"',
     "return False", "tests/test_registry.py"),
    ("boundary: Tier 2 scan corpus emptied (anti-vacuity floor)",
     "tests/test_registry.py", 'TIER2_ROOT = REPO_ROOT / "agents" / "tier2"',
     'TIER2_ROOT = REPO_ROOT / "agents" / "tier2" / "schemas"', "tests/test_registry.py"),
    ("boundary: vehicle_id path-injection validation removed",
     "vehicle_command.py", '        if not _VEHICLE_ID_RE.fullmatch(vehicle_id) or ".." in vehicle_id:',
     "        if False:", "tests/test_vehicle_command.py"),
    ("boundary: vehicle_id validator drops the '..' traversal check",
     "vehicle_command.py", 'or ".." in vehicle_id:', "or False:",
     "tests/test_vehicle_command.py"),
    ("boundary: vehicle_id validator over-strict (rejects legitimate ids)",
     "vehicle_command.py", '_VEHICLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")',
     '_VEHICLE_ID_RE = re.compile(r"[A-Za-z0-9]{1,8}")', "tests/test_vehicle_command.py"),

    # -- Privacy ------------------------------------------------------------
    ("privacy: @never_raises logs the exception message and traceback",
     "base.py", '            log.error(\n                "executor %s: unhandled %s escaped dispatch; mapping to failed/"',
     '            log.exception(\n                "executor %s: unhandled %s escaped dispatch; mapping to failed/"',
     "tests/test_registry.py"),
    ("privacy: malformed-policy warning echoes the parameter value",
     "base.py", "            target,\n            type(exc).__name__,", "            target,\n            exc,",
     "tests/test_base.py"),
    ("privacy: chat marker drops its explicit expiry",
     "chat.py", '        "expires_at": expires_at,\n', "", "tests/test_chat.py"),

    # -- Wall clock ---------------------------------------------------------
    ("wall clock: fit-to-budget removed (combinations exceed the gateway limit)",
     "base.py", "        self._fit_to_budget()", "        pass", "tests/test_registry.py"),
    ("wall clock: SSM retry_count clamp removed",
     "base.py", "        retries = max(1, min(_MAX_RETRY_COUNT, requested_retries))",
     "        retries = max(1, requested_retries)", "tests/test_registry.py"),
    ("wall clock: retail deadline check removed",
     "retail.py", "            if _monotonic() >= deadline:", "            if False:",
     "tests/test_retail.py"),
    ("wall clock: vehicle_command deadline check removed",
     "vehicle_command.py", "            if time.monotonic() - loop_start >= deadline:",
     "            if False:", "tests/test_vehicle_command.py"),

    # -- Test-infrastructure guards (guards on the guards) ------------------
    ("guard: conftest AWS-factory scan narrowed to miss the DynamoDB resource",
     "tests/conftest.py",
     'if "boto3.client" in src or "boto3.resource" in src:\n                monkeypatch.setattr(module, attr_name, lambda *a, **k: MagicMock(), raising=False)',
     'if "boto3.client" in src:\n                monkeypatch.setattr(module, attr_name, lambda *a, **k: MagicMock(), raising=False)',
     "tests/test_aws_client_guard.py::test_guard_covers_every_aws_factory"),

    # -- Retail: the configurator path must not submit a configuration-less lead --
    # Added 2026-09-20 with spec 2026-09-20-avx-acquire-configurator-target. Dropping
    # this guard makes configurator_recommend POST a lead indistinguishable from a
    # bare lead_capture: the owner's approved configuration is silently lost while
    # the Action reports success and a real leadId. Same class as R4 — an executor
    # reporting a durable reference for work it did not actually do.
    ("retail: configurator dispatches without the savedConfig it approves",
     "retail.py", "            if not saved_config:", "            if False:",
     "tests/test_retail.py::test_configurator_recommend_without_saved_config_fails_closed"),
]


_SNAPSHOT: dict[Path, str] = {}


def _snapshot_package() -> None:
    """Record every source file before any mutation is applied.

    `finally` handles exceptions but not a SIGKILL or a harness timeout, and this
    battery mutates files in place. A reviewer's run was interrupted mid-mutation
    and left `vehicle_command.py` reading `if False:`; the next run self-detected
    via the red baseline, but the tree was dirty in between. Restoring from a
    snapshot makes recovery possible without git.
    """
    for path in sorted(EX.rglob("*.py")):
        _SNAPSHOT[path] = path.read_text(encoding="utf-8")


def _restore_package() -> None:
    for path, text in _SNAPSHOT.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.relative_to(EX)}")


def _install_signal_handlers() -> None:
    import signal

    def _bail(signum: int, _frame: object) -> None:
        print(f"\n!! interrupted (signal {signum}) — restoring sources")
        _restore_package()
        raise SystemExit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _bail)
        except (ValueError, OSError):
            pass  # not on the main thread


def _clear_pycache() -> None:
    for d in ROOT.rglob("__pycache__"):
        if "/.venv/" not in str(d):
            shutil.rmtree(d, ignore_errors=True)


def _run(selector: str) -> tuple[int, int]:
    """Run `selector`. Returns (exit_code, number_of_FAILED_tests).

    The failure COUNT is what the scoring uses, not the exit code. A non-zero exit
    with zero failures means pytest never got as far as running the property's
    test — a collection error, an import error, a syntax error, or a nodeid that
    matched nothing. Both vacuous entries found in this battery were exactly that
    shape:

    * Cycle 4: the anchor turned `try:` into `if True:`, dangling the `except`.
      The module failed to import, pytest errored, rc != 0, scored CAUGHT. It
      proved only that the file parses.
    * Cycle 5: a moved test left a stale nodeid. pytest exits 4 on "no tests
      matched", rc != 0, scored CAUGHT on an UNMUTATED tree.

    A selector baseline caught the second but not the first, because it validated
    the selector rather than the measurement. Counting failures catches both, and
    anything else of the same shape.
    """
    _clear_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    args = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "--no-header"]
    args += [f"agents/executors/{s}" for s in selector.split()]
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, env=env)
    out = proc.stdout + proc.stderr
    nfail = sum(1 for line in out.splitlines() if line.startswith("FAILED "))
    return proc.returncode, nfail


def _selector_is_green(selector: str) -> tuple[bool, int]:
    """Run `selector` against the UNMUTATED tree.

    Returns (green, exit_code). A selector must be green before it can be used to
    measure anything:

    * exit 4 means "no tests matched" — a stale or misspelled nodeid. The old
      scoring counted that as CAUGHT, so a moved test silently made its entry
      unfalsifiable. That is exactly what happened when
      `test_guard_covers_every_aws_factory` moved out of `conftest.py`.
    * a non-zero exit from a genuinely failing test means the entry would score
      CAUGHT no matter what the mutation did.
    """
    rc, _nfail = _run(selector)
    return rc == 0, rc


def main() -> int:
    _snapshot_package()
    _install_signal_handlers()

    # Scope to this package: the repo-wide suite carries unrelated pre-existing
    # failures (see active-projects.md), and a red baseline from those would make
    # every mutation look "caught".
    baseline, _ = _run(".")
    if baseline != 0:
        print("BASELINE IS RED — fix the suite before running the battery")
        return 2

    caught: list[str] = []
    missed: list[str] = []
    skipped: list[str] = []
    invalid: list[str] = []

    verified_selectors: dict[str, bool] = {}

    for label, rel, find, repl, selector in MUTATIONS:
        path = EX / rel
        original = path.read_text(encoding="utf-8")
        if find not in original:
            skipped.append(f"{label}  [anchor not found in {rel}]")
            continue

        # Baseline the selector once per distinct selector, before mutating.
        if selector not in verified_selectors:
            green, rc = _selector_is_green(selector)
            verified_selectors[selector] = green
            if not green:
                reason = "no tests matched (stale nodeid?)" if rc == 4 else f"already failing (rc={rc})"
                invalid.append(f"{label}  [selector {selector!r}: {reason}]")
                continue
        elif not verified_selectors[selector]:
            invalid.append(f"{label}  [selector {selector!r}: unusable, see above]")
            continue

        try:
            path.write_text(original.replace(find, repl, 1), encoding="utf-8")
            rc, nfail = _run(selector)
            if nfail >= 1:
                caught.append(label)
            elif rc != 0:
                # Non-zero exit with no failing test: pytest never ran the
                # property's guard. The mutation measured nothing.
                invalid.append(
                    f"{label}  [rc={rc} with 0 failing tests — collection/import "
                    "error, not a caught property]"
                )
            else:
                missed.append(label)
        finally:
            path.write_text(original, encoding="utf-8")

    _clear_pycache()
    _restore_package()  # belt-and-braces: verify nothing was left mutated

    print("=" * 78)
    print(f"CAUGHT {len(caught)}/{len(MUTATIONS)}")
    for c in caught:
        print(f"  ok  {c}")
    if skipped:
        print(f"\nSKIPPED — anchor drift, needs re-anchoring: {len(skipped)}")
        for s in skipped:
            print(f"  !!  {s}")
    if invalid:
        print(f"\nINVALID — selector unusable, mutation NOT measured: {len(invalid)}")
        for i in invalid:
            print(f"  ??  {i}")
    if missed:
        print(f"\nNOT CAUGHT — the test asserts presence, not the property: {len(missed)}")
        for m in missed:
            print(f"  XX  {m}")
    print("=" * 78)
    if invalid:
        print("An INVALID entry measured NOTHING: either its selector was unusable, or")
        print("the run exited non-zero with no failing test (a collection/import error).")
        print("Both shapes were previously scored CAUGHT, which is how two vacuous")
        print("entries survived. Fix the entry; do not ignore an INVALID.")
    return 1 if (missed or skipped or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
