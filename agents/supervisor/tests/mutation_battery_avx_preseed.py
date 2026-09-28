#!/usr/bin/env python3
"""Mutation battery for the AVX pre-seed reader (spec Fix Group 18).

Each entry breaks one property the reader is supposed to guarantee and asserts a
**named** test fails as a result.  Scoring requires an actual ``FAILED`` line
naming the expected test — a non-zero exit code alone is NOT accepted, because a
collection error, a ``SyntaxError`` from a bad anchor, or an unrelated failure
all produce one, and this spec has already shipped three vacuity defects of
exactly that shape (see the Group 5 handoff, § "Why seven cycles").

Restores the source on exit, including on SIGINT/SIGTERM.  If it is ever killed
harder than that, ``git status -- agents/supervisor/avx_preseed.py`` says so
immediately, and the next run refuses to score against a dirty baseline.

Run:  .venv/bin/python agents/supervisor/tests/mutation_battery_avx_preseed.py
"""

from __future__ import annotations

import atexit
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "agents" / "supervisor" / "avx_preseed.py"
BIDI = REPO / "agents" / "supervisor" / "bidi_app.py"
TESTS = REPO / "agents" / "supervisor" / "tests" / "test_avx_preseed.py"
BIDI_TESTS = REPO / "agents" / "supervisor" / "tests" / "test_bidi_app_avx_preseed.py"
D5_TESTS = REPO / "agents" / "supervisor" / "tests" / "test_d5_memory_id_getattr_census.py"
PYTHON = REPO / ".venv" / "bin" / "python"

# (label, target, find, replace, test that MUST fail)
MUTATIONS: list[tuple[str, Path, str, str, str]] = [
    (
        "expiry: comparison removed entirely",
        TARGET,
        "if expires_at <= current:",
        "if False:",
        "test_expired_marker_is_rejected",
    ),
    (
        "expiry: '<=' relaxed to '<' (boundary only)",
        TARGET,
        "if expires_at <= current:",
        "if expires_at < current:",
        "test_marker_expiring_exactly_now_is_rejected",
    ),
    (
        "expiry: unparseable expires_at accepted",
        TARGET,
        "    if expires_at is None:",
        "    if False:",
        "test_marker_with_missing_or_unparseable_expires_at_is_rejected",
    ),
    (
        "expiry: compared as ISO strings instead of datetimes",
        TARGET,
        "if expires_at <= current:",
        'if str(body.get("expires_at")) <= current.isoformat():',
        "test_marker_expiring_exactly_now_is_rejected",
    ),
    (
        "selection: marker_kind filter dropped",
        TARGET,
        'if body.get("marker_kind") != MARKER_KIND:',
        "if False:",
        "test_non_marker_tool_message_is_not_treated_as_a_marker",
    ),
    (
        "selection: role filter dropped",
        TARGET,
        "if not isinstance(role, str) or role.upper() != MARKER_ROLE:",
        "if False:",
        "test_valid_marker_body_under_a_non_tool_role_is_ignored",
    ),
    (
        "selection: header/body finding_id agreement dropped",
        TARGET,
        "if body_finding_id != finding_id:",
        "if False:",
        "test_marker_whose_finding_id_disagrees_with_the_header_is_rejected",
    ),
    (
        "selection: oldest marker wins instead of newest",
        TARGET,
        "for turn in reversed(turns):",
        "for turn in turns:",
        "test_newest_marker_wins_when_a_finding_was_drilled_into_twice",
    ),
    (
        "cold-open: memory exception propagates",
        TARGET,
        "    except Exception:  # noqa: BLE001 — cold-open policy; see above.",
        "    except ZeroDivisionError:",
        "test_memory_read_raising_yields_none_rather_than_propagating",
    ),
    (
        "scope: actor_id taken from the marker body instead of the caller",
        TARGET,
        "            actor_id,\n            session_id,\n            k=_MARKER_LOOKBACK_TURNS,",
        "            'sub-aaaaaaaa-1111',\n            session_id,\n            k=_MARKER_LOOKBACK_TURNS,",
        "test_another_owners_marker_is_not_readable",
    ),
    (
        "bounding: evidence line cap removed",
        TARGET,
        "for entry in evidence[:_MAX_EVIDENCE_LINES]:",
        "for entry in evidence:",
        "test_evidence_lines_are_capped",
    ),
    (
        "bounding: per-line truncation removed",
        TARGET,
        "        if len(text) > _MAX_EVIDENCE_CHARS:",
        "        if False:",
        "test_long_evidence_line_is_truncated",
    ),
    (
        "jwt: sub falls back to another claim",
        TARGET,
        '    sub = claims.get("sub")',
        '    sub = claims.get("sub") or claims.get("session_id")',
        "test_actor_id_from_jwt_never_falls_back_to_another_identifier",
    ),
    # --- prompt composition -------------------------------------------------
    (
        "append: fragment never appended",
        TARGET,
        "    return prompt + fragment",
        "    return prompt",
        "test_valid_marker_appends_fragment_and_preserves_the_original_prompt",
    ),
    (
        "append: persona prompt replaced instead of extended",
        TARGET,
        "    return prompt + fragment",
        "    return fragment",
        "test_valid_marker_appends_fragment_and_preserves_the_original_prompt",
    ),
    (
        "append: cold-open guard removed (falsy fragment concatenated)",
        TARGET,
        "    if not fragment:\n        return prompt",
        "    if False:\n        return prompt",
        "test_prompt_returned_unchanged_for_every_cold_open_case",
    ),
    # --- bidi header threading ---------------------------------------------
    (
        "header: casing tuple reduced to the collapsed variant only",
        BIDI,
        '    "x-amzn-bedrock-agentcore-runtime-custom-avx-finding-id",\n'
        '    "x-amzn-bedrock-agentcore-runtime-custom-avx_finding_id",\n',
        "",
        "test_avx_finding_id_accepted_as_hyphenated",
    ),
    (
        "header: extraction dropped (avx_finding_id never populated)",
        BIDI,
        "    avx_finding_id = next(\n"
        "        (headers[k] for k in AVX_FINDING_ID_QUERY_PARAMS if k in headers), None\n"
        "    )",
        "    avx_finding_id = None",
        "test_avx_finding_id_accepted_as_camelcase_collapsed",
    ),
    (
        "header: not threaded into SessionContext",
        BIDI,
        "        avx_finding_id=avx_finding_id,",
        "        avx_finding_id=None,",
        "test_avx_finding_id_accepted_as_camelcase_collapsed",
    ),
    (
        "header: session.start fallback dropped",
        BIDI,
        '        avx_finding_id = avx_finding_id or start_msg.get("avxFindingId")',
        "        pass",
        "test_session_start_fallback_accepts_avx_finding_id",
    ),
    (
        "constraint 1: messages= passed to BidiAgent",
        BIDI,
        "        system_prompt=_persona_system_prompt,\n        name=\"vsa-supervisor-bidi\",",
        "        system_prompt=_persona_system_prompt,\n        messages=[],\n        name=\"vsa-supervisor-bidi\",",
        "test_bidi_app_does_not_pass_messages_to_bidi_agent",
    ),
    # The three below cover the AST guard's other assertions. Cycle 2 noted the
    # single entry above exercises only the `messages` keyword check, leaving 3
    # of the guard's 4 assertions with no re-running coverage — and the count
    # pin's whole purpose is to fire on a FUTURE edit, which is exactly what a
    # battery entry simulates.
    (
        "constraint 1: a SECOND BidiAgent construction added (count pin)",
        BIDI,
        "    agent = BidiAgent(",
        "    _unaudited = BidiAgent(model=model, tools=[], name=\"x\")\n    agent = BidiAgent(",
        "test_bidi_app_does_not_pass_messages_to_bidi_agent",
    ),
    (
        "constraint 1: BidiAgent built via **kwargs splat (contents invisible)",
        BIDI,
        "        system_prompt=_persona_system_prompt,\n        name=\"vsa-supervisor-bidi\",",
        "        system_prompt=_persona_system_prompt,\n        **{},\n        name=\"vsa-supervisor-bidi\",",
        "test_bidi_app_does_not_pass_messages_to_bidi_agent",
    ),
    (
        "constraint 1: history attached post-hoc instead of in the constructor",
        BIDI,
        "    log.info(\n        \"bidi: start session=%s",
        "    agent.messages = []\n    log.info(\n        \"bidi: start session=%s",
        "test_bidi_app_does_not_pass_messages_to_bidi_agent",
    ),
    # D5 census ratchet (review Cycle 2, Warning 1).
    (
        "D5: a new getattr(config, \"memory_id\", ...) site introduced",
        BIDI,
        "        memory_id=config.memory_id,",
        "        memory_id=getattr(config, \"memory_id\", None),",
        "test_no_new_getattr_memory_id_sites",
    ),
    # Security review Cycle 1 — W2 and S2.
    (
        "security W2: exc_info=True restored on the memory-read except",
        TARGET,
        '        log.warning(\n'
        '            "avx_preseed: memory read raised for finding=%s exc_type=%s — opening cold",\n'
        "            finding_id,\n"
        "            type(sys.exc_info()[1]).__name__,\n"
        "        )",
        '        log.warning(\n'
        '            "avx_preseed: memory read raised for finding=%s — opening cold",\n'
        "            finding_id,\n"
        "            exc_info=True,\n"
        "        )",
        "test_memory_read_failure_logs_exception_type_only_never_the_exception",
    ),
    (
        "security S2: finding_id length cap removed",
        TARGET,
        "    if len(finding_id) > _MAX_FINDING_ID_CHARS:",
        "    if False:",
        "test_absurdly_long_finding_id_is_rejected_before_any_memory_call",
    ),
]


def _restore(backups: dict[Path, Path]) -> None:
    for target, backup in backups.items():
        if backup.exists():
            shutil.copy2(backup, target)


def main() -> int:
    if not PYTHON.exists():
        print(f"FATAL: {PYTHON} not found", file=sys.stderr)
        return 2

    targets = sorted({m[1] for m in MUTATIONS}, key=str)
    suite = [str(TESTS), str(BIDI_TESTS), str(D5_TESTS)]

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--"] + [str(t) for t in targets],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()

    baseline = subprocess.run(
        [str(PYTHON), "-m", "pytest", *suite, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    if baseline.returncode != 0:
        print("FATAL: baseline suite is RED — refusing to score mutations.")
        print(baseline.stdout[-2000:])
        return 2
    print(f"baseline: GREEN{'  (note: targets have uncommitted changes)' if dirty else ''}")

    tmp = Path(tempfile.mkdtemp())
    backups = {t: tmp / (t.name + ".bak") for t in targets}
    for t, b in backups.items():
        shutil.copy2(t, b)
    atexit.register(_restore, backups)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_restore(backups), sys.exit(130)))

    originals = {t: t.read_text() for t in targets}
    caught = 0
    failures: list[str] = []

    for label, target, find, replace, expected_test in MUTATIONS:
        original = originals[target]
        if find not in original:
            failures.append(f"ANCHOR MISS  {label}\n    pattern absent in {target.name}")
            print(f"  ANCHOR MISS  {label}")
            continue

        target.write_text(original.replace(find, replace, 1))
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", *suite, "-q", "--no-header"],
            cwd=REPO, capture_output=True, text=True,
        )
        out = proc.stdout + proc.stderr
        target.write_text(original)

        # Scoring requires the EXPECTED test to appear on a FAILED line.
        # A bare non-zero exit is not accepted: a collection error, a
        # SyntaxError from a bad anchor, or an unrelated failure would
        # otherwise all score as CAUGHT. This spec has shipped three vacuity
        # defects of exactly that shape.
        hit = any(
            line.startswith("FAILED") and expected_test in line
            for line in out.splitlines()
        )
        if hit:
            caught += 1
            print(f"  CAUGHT  {label}\n            by {expected_test}")
        else:
            reason = "no FAILED line named it"
            if "ERROR" in out and "error" in out.lower() and "passed" not in out:
                reason = "COLLECTION ERROR — likely a broken anchor"
            failures.append(
                f"NOT CAUGHT  {label}\n    expected FAILED: {expected_test}\n"
                f"    exit={proc.returncode}  ({reason})"
            )
            print(f"  NOT CAUGHT  {label}  (expected {expected_test}; {reason})")

    _restore(backups)
    final = subprocess.run(
        [str(PYTHON), "-m", "pytest", *suite, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    print(f"\nrestored baseline: {'GREEN' if final.returncode == 0 else 'RED — INVESTIGATE'}")
    print(f"score: CAUGHT {caught}/{len(MUTATIONS)}")
    if failures:
        print("\n" + "\n".join(failures))
        return 1
    return 0 if final.returncode == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
