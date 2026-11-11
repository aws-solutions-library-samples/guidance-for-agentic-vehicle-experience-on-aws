#!/usr/bin/env python3
"""Live-verify the AVX drill-down pre-seed round-trip against real AgentCore Memory.

Fix Group 18 T18.5. Every test in T18.1-T18.4 stubs the memory client, and per
`~/.kiro/steering/spec-workflow.md` § "Live-Verification Gate" a stub cannot fail
the way a service fails. This is the gate the group does not close without.

What it exercises, and what it does not
---------------------------------------
Exercises, against the REAL service: `ChatDrillDownExecutor.dispatch()` writing a
marker via `create_event`, and `resolve_pre_seed_fragment()` reading it back via
`get_last_k_turns` — the real event shape, the real role casing, the real
ordering, the real nesting. Those are precisely what a stub cannot get wrong.

Does NOT exercise: the `AvxFindingId` custom header actually arriving at the
runtime. That needs the deployed container plus an iOS client sending it, and it
is additionally gated by the `requestHeaderAllowlist` in the gitignored
`.bedrock_agentcore.yaml`. That half is AVX iOS Group 5's live-verification.

On cleanup — a corrected task premise
-------------------------------------
T18.5's Accept criterion said "cleans up every row it writes, and treats an
orphan as a failure of the run". **That is not achievable and the task was wrong
to require it.** `MemoryClient` exposes no delete-event operation:

    delete-ish methods: delete_memory, delete_memory_and_wait, delete_strategy

`delete_memory` destroys the entire Memory resource — which here is the live
staging resource `vsa_staging_cvx_foundation_memory-*` that the whole supervisor
depends on. Using it as test cleanup would be an outage.

So cleanup is replaced by CONTAINMENT plus HONEST REPORTING:

* every run writes under a **run-scoped synthetic actor id**
  (`livetest-avx-preseed-<uuid>`), so no real owner's memory scope is touched and
  the residue is trivially identifiable;
* the marker's own 72h `expires_at` means the reader rejects it thereafter, so
  the residue is inert to production code well before it ages out;
* the run asserts it created **exactly** the events it intended and no more, and
  prints the actor/session ids so an operator can audit them.

Usage
-----
    LIVE_TESTS=1 AWS_REGION=us-west-2 VSA_STAGE=staging \\
      .venv/bin/python scripts/live-verify-avx-preseed.py

Exit codes: 0 all checks passed · 1 a check failed · 2 skipped or cannot run.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

#: The staging Memory resource. Resolved from the environment when present so the
#: script follows the same coordinate the runtime uses, with the known staging
#: value as the fallback rather than a silent default of "".
_STAGING_MEMORY_FALLBACK = "vsa_staging_cvx_foundation_memory-XHAM1yG5mr"

FINDING_ID = f"FIND-livetest-{uuid.uuid4()}"
EVIDENCE_CANARY = f"livetest-evidence-{uuid.uuid4().hex[:12]}"


def fail(msg: str) -> None:
    print(f"\n  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def preflight() -> tuple[str, str]:
    """Return (memory_id, region), or exit.

    Fails closed on the region/stage trap this machine actually has: the shell
    exports `AWS_REGION=us-east-1` and `VSA_STAGE=prod` by default, and both
    silently retarget scripts in this repo. Checking rather than assuming is the
    difference between verifying staging and quietly writing into prod.
    """
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run this against real staging.")
        sys.exit(2)

    region = os.environ.get("VSA_MEMORY_REGION") or os.environ.get("AWS_REGION") or ""
    if region != "us-west-2":
        print(
            f"CANNOT RUN — region resolves to {region!r}, expected 'us-west-2'.\n"
            "  This machine exports us-east-1; pass AWS_REGION=us-west-2 explicitly.\n"
            "  Refusing to guess, because the wrong region writes to the wrong Memory."
        )
        sys.exit(2)

    stage = os.environ.get("VSA_STAGE", "")
    if stage and stage != "staging":
        print(
            f"CANNOT RUN — VSA_STAGE={stage!r}. This script writes events; it runs\n"
            "  against staging only. Pass VSA_STAGE=staging explicitly."
        )
        sys.exit(2)

    memory_id = os.environ.get("VSA_FOUNDATION_MEMORY_ID") or _STAGING_MEMORY_FALLBACK
    if "staging" not in memory_id:
        print(
            f"CANNOT RUN — memory id {memory_id!r} does not look like a staging\n"
            "  resource. Refusing to write events into a non-staging Memory."
        )
        sys.exit(2)

    # The executor reads these from the environment; set them so the REAL code
    # path resolves the same coordinates this pre-flight just validated.
    os.environ["VSA_FOUNDATION_MEMORY_ID"] = memory_id
    os.environ["VSA_MEMORY_REGION"] = region
    return memory_id, region


def main() -> int:
    memory_id, region = preflight()

    from bedrock_agentcore.memory import MemoryClient  # noqa: PLC0415

    from agents.executors.chat import ChatDrillDownExecutor  # noqa: PLC0415
    from agents.supervisor.avx_preseed import (  # noqa: PLC0415
        SESSION_ID_SEPARATOR,
        SESSION_PENDING_PREFIX,
        resolve_pre_seed_fragment,
    )

    owner = f"livetest-avx-preseed-{uuid.uuid4()}"
    other_owner = f"livetest-avx-preseed-{uuid.uuid4()}"
    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"

    print("AVX pre-seed live verification")
    print(f"  memory    : {memory_id}")
    print(f"  region    : {region}")
    print(f"  actor     : {owner}   (synthetic, run-scoped)")
    print(f"  session   : {session_id}")
    print(f"  finding   : {FINDING_ID}")
    print()

    client = MemoryClient(region_name=region)
    failures: list[str] = []

    # ---- 1. write, through the REAL executor ---------------------------------
    action = {
        "action_id": f"ACT-livetest-{uuid.uuid4()}",
        # The executor reads the finding id from action["finding"]["id"] (falling
        # back to action["finding_id"]). Supplying it is not optional: an empty
        # value now fails closed with MissingFindingId.
        "finding": {"id": FINDING_ID, "evidence": [EVIDENCE_CANARY, "second evidence line"]},
    }
    claims = {"sub": owner}

    result = ChatDrillDownExecutor(memory_client=client).dispatch(action, claims)
    status = getattr(result, "status", None)
    if getattr(result, "last_error_class", None):
        print(f"  executor returned error: {result.last_error_class} — {getattr(result, 'note', '')}")
    if status is None or "COMPLET" not in str(status).upper():
        fail(f"executor did not complete: status={status!r} note={getattr(result, 'note', '')!r}")
        # Hard stop: every negative check below would pass vacuously if nothing
        # was ever written. This is the anti-vacuity gate for the whole run.
        print("\nABORTING — nothing was written, so the remaining checks would")
        print("pass for the wrong reason (absent marker reads as 'correctly rejected').")
        return 1
    ok(f"marker written through the real executor (status={status}, target_ref={getattr(result, 'target_ref', None)})")

    # ---- 2. read it back, through the REAL reader ----------------------------
    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID, actor_id=owner, memory_id=memory_id, client=client
    )
    if not fragment:
        fail("reader returned None for a marker that was just written — the round-trip is broken")
        print("\nABORTING — same reason as above: negatives would be vacuous.")
        return 1
    ok("reader resolved a fragment from the real event")

    if FINDING_ID not in fragment:
        failures.append("fragment does not name the finding id")
    else:
        ok("fragment names the finding id")

    if EVIDENCE_CANARY not in fragment:
        failures.append(f"fragment omits the evidence canary {EVIDENCE_CANARY}")
    else:
        ok("fragment carries the evidence written by the executor")

    # ---- 3. expiry, against a real event ------------------------------------
    # The stub tests pin this; only a real event proves the timestamp the SERVICE
    # round-trips is still parseable and still compares correctly.
    far_future = datetime.now(timezone.utc) + timedelta(hours=80)  # > MARKER_TTL_HOURS
    expired = resolve_pre_seed_fragment(
        finding_id=FINDING_ID, actor_id=owner, memory_id=memory_id,
        client=client, now=far_future,
    )
    if expired is not None:
        failures.append("an expired marker was honoured against the real service")
    else:
        ok("expiry gate rejects the real marker once past expires_at")

    # ---- 4. cross-actor isolation, against the real service ------------------
    leaked = resolve_pre_seed_fragment(
        finding_id=FINDING_ID, actor_id=other_owner, memory_id=memory_id, client=client
    )
    if leaked is not None:
        failures.append("another actor read this owner's marker — scope isolation is broken")
    else:
        ok("a different actor cannot read the marker (real memory-scope isolation)")

    # ---- 5. residue: assert exactly what we wrote, and report it -------------
    try:
        events = client.list_events(
            memory_id=memory_id, actor_id=owner, session_id=session_id, max_results=100
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"could not list events for the residue report: {type(exc).__name__}")
        events = []

    if len(events) != 1:
        failures.append(
            f"expected exactly 1 event under the synthetic actor, found {len(events)} — "
            "the run wrote more than it intended"
        )
    else:
        ok("exactly 1 event written, as intended")

    print()
    print("RESIDUE (cannot be deleted — MemoryClient has no delete-event API;")
    print("         delete_memory would destroy the live staging Memory resource):")
    print(f"  actor_id   : {owner}")
    print(f"  session_id : {session_id}")
    print(f"  events     : {len(events)}")
    for ev in events:
        print(f"    - eventId={ev.get('eventId')} at={ev.get('eventTimestamp')}")
    print("  Inert to production: the synthetic actor is read by nothing, and the")
    print("  marker's 72h expires_at makes the reader reject it regardless.")

    print()
    if failures:
        print(f"VERDICT: FAIL ({len(failures)} check(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("VERDICT: PASS — the pre-seed round-trip works against real AgentCore Memory.")
    print()
    print("Still NOT proven by this script: that the AvxFindingId custom header")
    print("arrives at the deployed runtime. That needs the iOS client plus the")
    print("gitignored requestHeaderAllowlist, and is AVX iOS Group 5's gate.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(2)
