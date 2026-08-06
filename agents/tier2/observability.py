"""
Structured run observability — spec § D5.

Records the lifecycle of one Tier 2 agent run: start time, rows read, artifacts
written, token/cost consumption, and abort reason if the run was cut short by
the budget cap.

The run record is emitted as a single JSON line to stdout, which CloudWatch Logs
picks up automatically from Lambda.  Every field is safe to log:

  - No VINs (customer-identifying in this domain).
  - No finding text (owner-facing content; not needed for operational insight).
  - No ADP column values.

The module is deliberately simple: one class, one emit call, no external
dependencies beyond the standard library and ``budget.BudgetCap``.

Usage pattern in runner.py:

    obs = RunObserver(run_id="sched-2026-08-04T00:00:00Z", stage="staging",
                      component="battery")
    try:
        cap = BudgetCap(max_tokens=50_000, max_wall_seconds=300.0)
        for chunk in batches:
            cap.check()
            result = agent(chunk)
            cap.record_tokens(result.usage.total_tokens)
            obs.record_rows_read(len(chunk))
            obs.record_tokens(result.usage.total_tokens, cost_usd=...)
            obs.record_artifact_written()
            cap.check()
    except BudgetExceeded as exc:
        obs.finish(abort_reason=str(exc))
        # then write partial artifact from exc.partial_finding
    else:
        obs.finish()
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import IO, Optional


@dataclass
class RunRecord:
    """
    Mutable record for one Tier 2 agent run.

    Fields are populated incrementally as the run progresses and emitted in
    ``finish()``.

    Attributes
    ----------
    run_id:
        Unique identifier for this invocation (e.g. ISO-8601 scheduled time).
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.
    component:
        Which health component was processed — ``"battery"`` or ``"tires"``.
    started_at:
        ISO-8601 UTC string captured at construction time.
    finished_at:
        Populated in ``finish()``; ``None`` until then.
    rows_read:
        Total ADP rows read by the run.
    artifacts_written:
        Number of new artifacts successfully written to DynamoDB (rows that
        did not previously exist).  A re-run over unchanged data returns 0
        here and non-zero in ``duplicates_skipped``.
    duplicates_skipped:
        Number of ``put()`` calls that were idempotent no-ops because the
        item with that ``inputs_hash`` already existed.  Non-zero on a re-run
        over unchanged data; 0 on a first run.  Together with
        ``artifacts_written`` gives a complete account of every ``put()`` call.
    tokens_used:
        Accumulated token count.
    estimated_cost_usd:
        Estimated Bedrock cost for this run (caller provides per-call cost).
    abort_reason:
        Populated when the run was cut short by the budget cap; ``None`` for
        a normal completion.  Safe to log: contains dimension/counts only,
        derived from ``BudgetExceeded.__str__()``, never VINs.
    """

    run_id: str
    stage: str
    component: str
    started_at: str
    finished_at: Optional[str] = None
    rows_read: int = 0
    artifacts_written: int = 0
    duplicates_skipped: int = 0
    tokens_used: int = 0
    estimated_cost_usd: float = 0.0
    abort_reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Return the record as a plain dict, safe to serialise to JSON."""
        return {
            "event": "tier2_run",
            "run_id": self.run_id,
            "stage": self.stage,
            "component": self.component,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "rows_read": self.rows_read,
            "artifacts_written": self.artifacts_written,
            "duplicates_skipped": self.duplicates_skipped,
            "tokens_used": self.tokens_used,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "abort_reason": self.abort_reason,
            "status": "aborted" if self.abort_reason else "completed",
        }


class RunObserver:
    """
    Manages the lifecycle of one Tier 2 agent run record.

    Parameters
    ----------
    run_id:
        Unique identifier for this invocation.
    stage:
        Deployment stage (``"staging"`` or ``"prod"``).
    component:
        Health component being processed.
    _output:
        Output stream for the JSON log line.  Defaults to ``sys.stdout``.
        Override in tests to capture output without touching stdout.
    _clock:
        Callable returning the current UTC time as an ISO-8601 string.
        Override in tests to produce deterministic timestamps.
    """

    def __init__(
        self,
        run_id: str,
        stage: str,
        component: str,
        _output: Optional[IO[str]] = None,
        _clock: Optional[callable] = None,
    ) -> None:
        self._output: IO[str] = _output if _output is not None else sys.stdout
        self._clock = _clock if _clock is not None else _utc_now

        self._record = RunRecord(
            run_id=run_id,
            stage=stage,
            component=component,
            started_at=self._clock(),
        )
        self._finished = False

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def record(self) -> RunRecord:
        """Return the current run record (mutable; snapshot before emitting)."""
        return self._record

    # ------------------------------------------------------------------
    # Incremental updates (called inside the agent loop)
    # ------------------------------------------------------------------

    def record_rows_read(self, n: int) -> None:
        """Add ``n`` to the total rows-read counter."""
        if n < 0:
            raise ValueError(f"Row count must be non-negative, got {n}")
        self._record.rows_read += n

    def record_tokens(self, n: int, cost_usd: float = 0.0) -> None:
        """
        Add ``n`` tokens and ``cost_usd`` to the running totals.

        Safe to call with ``n=0`` and ``cost_usd=0.0`` as a no-op.
        """
        if n < 0:
            raise ValueError(f"Token count must be non-negative, got {n}")
        if cost_usd < 0:
            raise ValueError(f"cost_usd must be non-negative, got {cost_usd}")
        self._record.tokens_used += n
        self._record.estimated_cost_usd += cost_usd

    def record_artifact_written(self) -> None:
        """Increment the artifact-written counter by one."""
        self._record.artifacts_written += 1

    def record_duplicate_skipped(self) -> None:
        """Increment the duplicates-skipped counter by one.

        Called when :meth:`ArtifactWriter.put` returns ``False`` — the item
        with this ``inputs_hash`` already existed and the write was a no-op.
        Together with :meth:`record_artifact_written`, this gives a complete
        account of every ``put()`` call in the run record.
        """
        self._record.duplicates_skipped += 1

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def finish(self, abort_reason: Optional[str] = None) -> RunRecord:
        """
        Mark the run complete and emit the JSON run record.

        Parameters
        ----------
        abort_reason:
            Pass the string representation of ``BudgetExceeded`` when the run
            was aborted by the budget cap.  Must not contain VINs or finding text.
            ``None`` for a normal completion.

        Returns
        -------
        RunRecord
            The final, immutable snapshot of this run's record.

        Raises
        ------
        RuntimeError
            If ``finish()`` is called more than once on the same observer.
        """
        if self._finished:
            raise RuntimeError("RunObserver.finish() called more than once")

        self._record.finished_at = self._clock()
        self._record.abort_reason = abort_reason
        self._finished = True

        self._emit()
        return self._record

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self) -> None:
        """Write the run record as a single JSON line to the output stream."""
        line = json.dumps(self._record.to_dict(), ensure_ascii=True)
        self._output.write(line + "\n")
        self._output.flush()


# ---------------------------------------------------------------------------
# Clock helper
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
