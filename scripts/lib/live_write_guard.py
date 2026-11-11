"""Refuse a live-verify script's write flag unless real AWS calls are impossible or the live-run step gave its go.

Why: on 2026-10-29 a coder helper called ``lv.main([... '--part', 'item9', '--allow-writes'])`` from a plain
``python3 -c`` while debugging a test. Plain python never loads the repo ``conftest.py``, whose AWS block swaps in
fake credentials and refuses the transport, so the call ran the whole flow against staging with the user's real
credentials (issues/2026-10-30-unexplained-item9-shaped-run-2026-10-29/). ``LIVE_TESTS=1`` did not stop it: the
caller sets that itself.

Rule (the same for every script that has a write flag): the flag is refused, before any AWS client, unless
  * the conftest AWS block is installed and closed (pytest, or ``import conftest``), or
  * the script's own live-go variable equals ``--stage``. Only the live-run step, on the user's go, sets it.
Read-only runs are not guarded. Nothing else about a script's live behaviour changes.

Use, in a script's ``main`` after argument parsing, the ``LIVE_TESTS`` gate and the stage refusal, before any client::

    HERE = Path(__file__).resolve().parent
    if str(HERE / "lib") not in sys.path:
        sys.path.insert(0, str(HERE / "lib"))
    import live_write_guard as _lwg

    _LIVE_GO_ENV = "FI_COST_LIVE_GO"
    ...
    msg = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={"--allow-writes": args.allow_writes, "--allow-managed-writes": args.allow_managed_writes},
    )
    if msg:
        print(msg, file=sys.stderr)
        return 1

A command a script prints for the operator to paste (a cleanup or resume command) must start with
``{_LIVE_GO_ENV}={stage} `` so it passes this guard.
"""
from __future__ import annotations

import os


def aws_block_active() -> bool:
    """True iff the repo ``conftest.py`` AWS block is installed and closed in this process.

    That block (fake credentials, refused transport) is installed whenever pytest runs and by ``import conftest``;
    it marks the transport it installs with ``aws_guard``. With it in place nothing here can reach real AWS, except
    during a ``live_aws`` test pass, when the conftest restores the real environment and lets requests through
    (``_LIVE_PASS["active"]``); the marker stays set then, so that state does not count as a block.
    """
    try:
        import botocore.endpoint  # noqa: PLC0415
    except ImportError:
        return False
    send = botocore.endpoint.Endpoint._send
    live_pass_open = getattr(send, "__globals__", {}).get("_LIVE_PASS", {}).get("active", False)
    return bool(getattr(send, "aws_guard", False)) and not live_pass_open


def writes_sanctioned(stage: str, go_env: str) -> bool:
    """A write flag may proceed: real calls are impossible, or the live-run step gave its go for this stage."""
    return aws_block_active() or os.environ.get(go_env) == stage


def refusal_message(flags: str, stage: str, go_env: str) -> str:
    return (
        f"refused: {flags} would use real AWS credentials. In a test or debug session run under pytest or "
        "`import conftest` first (it installs the AWS block). A live run needs "
        f"{go_env}={stage}, which only the live-run step sets on the user's go "
        "(docs/DEPLOYMENT.md); a helper must not set it."
    )


def check_write_flags(*, stage: str, go_env: str, flags: dict[str, bool]) -> str | None:
    """The refusal message when any write flag is set and the call is not sanctioned, else None.

    ``flags`` maps each of the script's write-capable flags (``--allow-writes``, ``--allow-managed-writes``,
    ``--allow-paging``, ...) to whether it was given.
    """
    given = [name for name, on in flags.items() if on]
    if not given or writes_sanctioned(stage, go_env):
        return None
    return refusal_message(" / ".join(given), stage, go_env)
