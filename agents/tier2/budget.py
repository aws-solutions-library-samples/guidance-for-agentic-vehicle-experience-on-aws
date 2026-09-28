"""
Per-run budget cap enforcement — spec § D6.

A Tier 2 agent has no human waiting and therefore no natural stopping signal.
The failure mode of an uncapped run is silent and financial: the agent loop
continues consuming tokens and incurring cost with nobody present to observe it.

This module provides a BudgetCap that:
  - Tracks accumulated token usage and elapsed wall-clock time.
  - Raises BudgetExceeded when either dimension is breached.
  - Must be checked *in the agent loop* (before each unit of work), not merely
    logged after the fact.
  - Carries enough state on BudgetExceeded to allow the caller (runner.py) to
    write a well-formed partial artifact rather than discarding all work done so
    far.

Usage pattern in runner.py:

    cap = BudgetCap(max_tokens=50_000, max_wall_seconds=300.0)
    for chunk in batches:
        cap.check()                      # raises BudgetExceeded if over limit
        result = agent(chunk)
        cap.record_tokens(result.usage.total_tokens)
        cap.check()                      # catches breach after the call too

The cap is deliberately a simple synchronous counter: the Tier 2 agent runs
in a Lambda under EventBridge Scheduler, where asyncio complexity is not warranted
and synchronous enforcement is clear and auditable.

Privacy: VINs are customer-identifying. This module never logs, records, or
surfaces VINs — it works on counts and timestamps only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


@dataclass
class BudgetExceeded(Exception):
    """
    Raised by BudgetCap when a run exceeds its token or wall-clock ceiling.

    Attributes
    ----------
    dimension:
        Which limit was breached — ``"tokens"`` or ``"wall_clock"``.
    used:
        The amount used at the time of breach (token count or elapsed seconds).
    limit:
        The configured ceiling for this dimension.
    tokens_used:
        Accumulated token count at the time of breach (always present,
        regardless of which dimension triggered the exception).
    elapsed_seconds:
        Elapsed wall-clock seconds at the time of breach.
    partial_finding:
        Optional finding text accumulated before the breach.  The runner
        uses this to write a well-formed partial artifact.  May be None if
        no partial work exists yet.  Excluded from ``__repr__`` — see the
        field declaration below.
    """

    dimension: Literal["tokens", "wall_clock"]
    used: float
    limit: float
    tokens_used: int
    elapsed_seconds: float
    # repr=False closes a leak the custom __str__ did not: the auto-generated
    # dataclass __repr__ exposes finding text — and therefore VINs — via
    # repr(exc), "%r" % exc, pprint(exc), and repr([exc]). `%r` is a routine
    # logging idiom and Tier 2 runs unattended, so the log is the widest
    # exposure. Security review Cycle 2 Warning.
    partial_finding: Optional[str] = field(default=None, repr=False)

    def __str__(self) -> str:
        return (
            f"BudgetExceeded(dimension={self.dimension!r}, "
            f"used={self.used}, limit={self.limit}, "
            f"tokens_used={self.tokens_used}, "
            f"elapsed_seconds={self.elapsed_seconds:.2f})"
        )

    # Make BudgetExceeded properly raisable as an Exception.
    def __post_init__(self) -> None:
        super().__init__(str(self))


# ---------------------------------------------------------------------------
# BudgetCap
# ---------------------------------------------------------------------------


class BudgetCap:
    """
    Hard token and wall-clock ceiling for one Tier 2 agent run.

    Parameters
    ----------
    max_tokens:
        Maximum total tokens (input + output) the run may consume.
        Must be a positive integer.
    max_wall_seconds:
        Maximum elapsed wall-clock time in seconds.
        Must be a positive float.
    _start_time:
        Overrides the start timestamp; used only in unit tests to inject a
        known start time without patching ``time.monotonic``.

    Raises
    ------
    ValueError
        If ``max_tokens`` or ``max_wall_seconds`` are not positive.
    """

    def __init__(
        self,
        max_tokens: int,
        max_wall_seconds: float,
        _start_time: Optional[float] = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        if max_wall_seconds <= 0:
            raise ValueError(
                f"max_wall_seconds must be positive, got {max_wall_seconds}"
            )

        self.max_tokens: int = max_tokens
        self.max_wall_seconds: float = max_wall_seconds
        self._tokens_used: int = 0
        self._start_time: float = (
            _start_time if _start_time is not None else time.monotonic()
        )
        self._partial_finding: Optional[str] = None

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def tokens_used(self) -> int:
        """Total tokens accumulated so far."""
        return self._tokens_used

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed wall-clock seconds since the cap was created."""
        return time.monotonic() - self._start_time

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record_tokens(self, n: int) -> None:
        """
        Add ``n`` tokens to the running total.

        Does **not** raise on its own — call ``check()`` immediately after
        to enforce the ceiling.  This separation allows the caller to record
        usage from a completed LLM call before deciding whether to abort.

        Parameters
        ----------
        n:
            Number of tokens to add.  Must be non-negative.

        Raises
        ------
        ValueError
            If ``n`` is negative.
        """
        if n < 0:
            raise ValueError(f"Token count must be non-negative, got {n}")
        self._tokens_used += n

    def set_partial_finding(self, finding: str) -> None:
        """
        Record the best partial finding text accumulated so far.

        The runner calls this after each completed unit of work so that, on
        budget breach, the exception carries whatever reasoning was finished
        before the cap fired.  Never logs or stores VINs — the finding is
        owner-facing text only.
        """
        self._partial_finding = finding

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def check(self) -> None:
        """
        Check both dimensions and raise BudgetExceeded if either is breached.

        Tokens are checked first; wall-clock second.  The order is
        deterministic for predictable test behaviour.

        This method is the enforcement point.  It must be called in the
        agent loop — before each unit of work, and optionally after — so
        that a breach is detected as soon as it occurs rather than after
        an unbounded further run.

        Raises
        ------
        BudgetExceeded
            If accumulated tokens exceed ``max_tokens`` OR elapsed wall-clock
            time exceeds ``max_wall_seconds``.
        """
        elapsed = self.elapsed_seconds

        if self._tokens_used > self.max_tokens:
            raise BudgetExceeded(
                dimension="tokens",
                used=float(self._tokens_used),
                limit=float(self.max_tokens),
                tokens_used=self._tokens_used,
                elapsed_seconds=elapsed,
                partial_finding=self._partial_finding,
            )

        if elapsed > self.max_wall_seconds:
            raise BudgetExceeded(
                dimension="wall_clock",
                used=elapsed,
                limit=self.max_wall_seconds,
                tokens_used=self._tokens_used,
                elapsed_seconds=elapsed,
                partial_finding=self._partial_finding,
            )

    def check_tokens(self) -> None:
        """
        Check only the token dimension.

        Provided for callers that want to enforce token budget at a specific
        point without re-evaluating wall clock (e.g. immediately after
        ``record_tokens``).
        """
        if self._tokens_used > self.max_tokens:
            raise BudgetExceeded(
                dimension="tokens",
                used=float(self._tokens_used),
                limit=float(self.max_tokens),
                tokens_used=self._tokens_used,
                elapsed_seconds=self.elapsed_seconds,
                partial_finding=self._partial_finding,
            )

    def check_wall_clock(self) -> None:
        """
        Check only the wall-clock dimension.

        Provided for callers that want an explicit wall-clock gate at a known
        point (e.g. before starting a new batch chunk after a long query).
        """
        elapsed = self.elapsed_seconds
        if elapsed > self.max_wall_seconds:
            raise BudgetExceeded(
                dimension="wall_clock",
                used=elapsed,
                limit=self.max_wall_seconds,
                tokens_used=self._tokens_used,
                elapsed_seconds=elapsed,
                partial_finding=self._partial_finding,
            )

    # ------------------------------------------------------------------
    # Summary (for observability — no customer data)
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """
        Return a dict summarising current usage against limits.

        Safe to log: contains counts and fractions only, no VINs.
        """
        elapsed = self.elapsed_seconds
        return {
            "tokens_used": self._tokens_used,
            "max_tokens": self.max_tokens,
            "token_fraction": self._tokens_used / self.max_tokens,
            "elapsed_seconds": round(elapsed, 3),
            "max_wall_seconds": self.max_wall_seconds,
            "wall_fraction": elapsed / self.max_wall_seconds,
        }
