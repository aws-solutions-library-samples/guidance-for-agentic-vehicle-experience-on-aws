"""One fake clock for the script and every real module the world runs.

The script compares its own clock with timestamps the real code writes (run records'
``started_at`` against the invoke time, alarm history against the invoke time,
Finding ``expires_at`` against now). So the script and the real code must read the
same clock. ``WorldClock.install`` swaps ``datetime.datetime`` / ``datetime.date`` on
the real ``datetime`` module (so function-local ``import datetime`` sees it) and every
already-bound reference in modules loaded from the AVX worktree or the DMS repo, the
way freezegun does. The script's ``time`` module is replaced by ``ScriptTime``: its
``sleep`` advances the clock and runs the world's due background work (deferred
async invokes, alarm evaluations) instead of waiting.
"""
from __future__ import annotations

import datetime as _dtmod
import sys
import time as _real_time
from typing import Any, Callable

_RealDateTime = _dtmod.datetime
_RealDate = _dtmod.date

_CLOCK: "WorldClock | None" = None


class _DateTimeMeta(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _RealDateTime)


class _DateMeta(type):
    def __instancecheck__(cls, obj: Any) -> bool:
        return isinstance(obj, _RealDate)


class FakeDateTime(_RealDateTime, metaclass=_DateTimeMeta):
    @classmethod
    def now(cls, tz: Any = None) -> _RealDateTime:  # type: ignore[override]
        t = _CLOCK.now()  # aware UTC
        return t.astimezone(tz) if tz is not None else t.astimezone().replace(tzinfo=None)

    @classmethod
    def utcnow(cls) -> _RealDateTime:  # type: ignore[override]
        return _CLOCK.now().replace(tzinfo=None)

    @classmethod
    def today(cls) -> _RealDateTime:  # type: ignore[override]
        return cls.now()


class FakeDate(_RealDate, metaclass=_DateMeta):
    @classmethod
    def today(cls) -> _RealDate:  # type: ignore[override]
        return _CLOCK.now().astimezone().date()


class WorldClock:
    """Aware-UTC 'now' that moves only when advanced."""

    def __init__(self, start: _RealDateTime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._t = start.astimezone(_dtmod.timezone.utc)
        self._pumps: list[Callable[[], None]] = []
        self._patched: list[tuple[Any, str, Any]] = []

    # -- reading ---------------------------------------------------------
    def now(self) -> _RealDateTime:
        return _RealDateTime.fromtimestamp(self._t.timestamp(), _dtmod.timezone.utc)

    def epoch(self) -> float:
        return self._t.timestamp()

    def iso(self) -> str:
        return self.now().isoformat(timespec="seconds").replace("+00:00", "Z")

    # -- moving ----------------------------------------------------------
    def advance(self, seconds: float) -> None:
        self._t = self._t + _dtmod.timedelta(seconds=float(seconds))
        for pump in list(self._pumps):
            pump()

    def set(self, t: _RealDateTime) -> None:
        self._t = t.astimezone(_dtmod.timezone.utc)

    def on_advance(self, fn: Callable[[], None]) -> None:
        self._pumps.append(fn)

    # -- installing ------------------------------------------------------
    def install(self, roots: list[str]) -> None:
        global _CLOCK
        _CLOCK = self
        self._swap(_dtmod, "datetime", FakeDateTime)
        self._swap(_dtmod, "date", FakeDate)
        self.patch_loaded_modules(roots)

    def patch_loaded_modules(self, roots: list[str]) -> None:
        """Rebind real datetime classes in every loaded module under ``roots``
        (never in this package: its own references must stay the real classes)."""
        own = str(__import__("pathlib").Path(__file__).resolve().parent)
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None) or ""
            if not any(f.startswith(r) for r in roots) or f.startswith(own):
                continue
            for name, val in list(vars(mod).items()):
                if val is _RealDateTime:
                    self._swap(mod, name, FakeDateTime)
                elif val is _RealDate:
                    self._swap(mod, name, FakeDate)

    def _swap(self, obj: Any, name: str, new: Any) -> None:
        self._patched.append((obj, name, getattr(obj, name)))
        setattr(obj, name, new)

    def uninstall(self) -> None:
        global _CLOCK
        for obj, name, old in reversed(self._patched):
            setattr(obj, name, old)
        # A module first imported while the clock was installed bound the fake classes
        # (``from datetime import datetime`` read the swapped attribute), so it is not in
        # ``_patched``.  Rebind it too, or it calls ``_CLOCK.now()`` after ``_CLOCK`` is None
        # in a later test (6.4 fix round 2: the after6_c2 world broke three after6_s1
        # tests in full-suite order with a 502 from ``agents.fi_quality.write``).
        own = str(__import__("pathlib").Path(__file__).resolve().parent)
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None) or ""
            if f.startswith(own):
                continue
            d = getattr(mod, "__dict__", None)
            if not isinstance(d, dict):
                continue
            for name, val in list(d.items()):
                if val is FakeDateTime:
                    setattr(mod, name, _RealDateTime)
                elif val is FakeDate:
                    setattr(mod, name, _RealDate)
        self._patched.clear()
        _CLOCK = None


class ScriptTime:
    """Stands in for the script module's ``time``: sleeping advances the world clock."""

    def __init__(self, clock: WorldClock) -> None:
        self._clock = clock
        self.slept: list[float] = []

    def time(self) -> float:
        return self._clock.epoch()

    def monotonic(self) -> float:
        return self._clock.epoch()

    def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self._clock.advance(max(float(seconds), 0.0))

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)
