"""T5.1 — registry integrity + the Tier 2 / executor boundary.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction"
Doctrine: `~/.kiro/steering/agentic-tiers.md` § "What stays deterministic, and why"
          — "a boundary that exists only in a prompt is a boundary that will be
          crossed", hence an executable scan rather than a docstring.

The boundary: **no module under `agents/tier2/` may import `agents.executors`.**
Tier 2 agents write Findings; acting on a Finding is the owner's decision,
surfaced as a card and dispatched only after an explicit approval. A Tier 2
agent that could reach an executor could book a repair order or send a remote
vehicle command on its own initiative, with nobody waiting and nobody asked.

Two design notes on how this is tested, both deliberate:

1.  **The scanner is root-parameterized.** `modules_importing_executors(root)`
    takes the tree to scan. That lets the positive control below run the *real*
    scanner against a synthesized violating tree, proving the detector actually
    detects — without editing a file under `agents/tier2/`, which a concurrent
    session owns. A one-off manual mutation would also be unrepeatable; a
    positive control runs on every CI invocation forever.

2.  **Anti-vacuity floors.** The nearest precedent in this repo
    (`agents/tier2/tests/test_boundaries.py`) documents itself as passing
    "trivially" because the directories it scanned were empty at the time. A
    guard that cannot distinguish "scanned everything, found no violation" from
    "scanned nothing" is not a guard. So the tests below assert the scan
    actually visited a floor of real modules and parsed a floor of real imports,
    and fail if the corpus disappears.
"""

from __future__ import annotations

import ast
import logging
import textwrap
from pathlib import Path

from unittest.mock import MagicMock

import pytest

from agents.executors import base, registry
from agents.executors.base import ActionStatus, Executor, ExecuteResult
from agents.executors.registry import (
    EXECUTORS,
    TARGET_SYSTEMS,
    UnknownTargetSystemError,
    get_executor,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TIER2_ROOT = REPO_ROOT / "agents" / "tier2"

#: Floors for the anti-vacuity assertions. Deliberately well below the real
#: counts so ordinary refactors don't trip them, but above zero so an empty or
#: unreadable corpus does.
_MIN_TIER2_MODULES_SCANNED = 5
_MIN_TIER2_IMPORTS_PARSED = 20


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


class ScanResult:
    """What a scan saw, so callers can assert on coverage as well as findings."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.modules_scanned: list[Path] = []
        self.imports_parsed: int = 0
        self.unparseable: list[str] = []


def _is_executor_import(node: ast.AST) -> bool:
    """True if `node` imports the `agents.executors` package in any form.

    Catches `import agents.executors.registry`, `from agents.executors import X`,
    `from agents.executors.dms import Y`, and the aliased variants. Prefix-matched
    on dotted segments so a future `agents.executors_helpers` is not a false
    positive while `agents.executors.anything` is still caught.
    """

    def _matches(dotted: str | None) -> bool:
        if not dotted:
            return False
        parts = dotted.split(".")
        return len(parts) >= 2 and parts[0] == "agents" and parts[1] == "executors"

    if isinstance(node, ast.Import):
        return any(_matches(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        # level > 0 is a relative import; from inside agents/tier2 a relative
        # import can never reach agents.executors, so only absolute matters.
        if node.level and node.level > 0:
            return False
        return _matches(node.module)
    return False


def modules_importing_executors(root: Path) -> ScanResult:
    """Scan `root` recursively for modules importing `agents.executors`.

    Parses with `ast` — nothing is imported or executed, so the scan is safe to
    run against arbitrary source and cannot be defeated by runtime patching.
    """
    result = ScanResult()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            result.unparseable.append(f"{path}: {type(exc).__name__}")
            continue
        result.modules_scanned.append(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                result.imports_parsed += 1
                if _is_executor_import(node):
                    result.violations.append(f"{path.relative_to(root)}:{node.lineno}")
    return result


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


class TestTier2CannotImportExecutors:
    def test_no_tier2_module_imports_executors(self) -> None:
        assert TIER2_ROOT.is_dir(), f"expected Tier 2 source tree at {TIER2_ROOT}"
        result = modules_importing_executors(TIER2_ROOT)

        assert not result.violations, (
            "module(s) under agents/tier2/ import agents.executors: "
            f"{result.violations}. Tier 2 agents write Findings; they must not be "
            "able to dispatch Actions. A Tier 2 agent holding an executor could "
            "book a repair order or send a remote vehicle command with nobody "
            "waiting and nobody asked."
        )

    def test_scan_was_not_vacuous(self) -> None:
        """The scan must have actually read a corpus.

        Without this, `test_no_tier2_module_imports_executors` would pass just as
        happily against a deleted, renamed, or unreadable `agents/tier2/` — which
        is precisely how the precedent guard in this repo came to describe itself
        as passing "trivially".
        """
        result = modules_importing_executors(TIER2_ROOT)

        assert len(result.modules_scanned) >= _MIN_TIER2_MODULES_SCANNED, (
            f"boundary scan visited only {len(result.modules_scanned)} module(s) "
            f"under {TIER2_ROOT} (floor {_MIN_TIER2_MODULES_SCANNED}). The "
            "no-violation result above is therefore meaningless — it found "
            "nothing because it looked at (almost) nothing."
        )
        assert result.imports_parsed >= _MIN_TIER2_IMPORTS_PARSED, (
            f"boundary scan parsed only {result.imports_parsed} import "
            f"statement(s) (floor {_MIN_TIER2_IMPORTS_PARSED}); the AST walk is "
            "probably not reaching import nodes."
        )
        assert not result.unparseable, (
            f"boundary scan could not parse: {result.unparseable}. An unparseable "
            "module is an unscanned module, so the clean verdict does not cover it."
        )

    def test_scanner_detects_a_real_violation(self, tmp_path: Path) -> None:
        """Positive control — the permanent form of the mutation.

        `tasks.md` specified proving this guard by adding an
        `import agents.executors.registry` to `agents/tier2/runner.py`. That file
        is owned by a concurrent session and is off-limits, and a hand-edit would
        be unrepeatable anyway. Running the same scanner against a synthesized
        violating tree proves the same property — the detector detects — on every
        run instead of once.
        """
        tier2_like = tmp_path / "tier2"
        tier2_like.mkdir()
        (tier2_like / "innocent.py").write_text(
            "import json\nfrom agents.tier2.contract import validate_artifact\n",
            encoding="utf-8",
        )
        (tier2_like / "violating.py").write_text(
            "import agents.executors.registry\n", encoding="utf-8"
        )

        result = modules_importing_executors(tier2_like)

        assert any("violating.py" in v for v in result.violations), (
            "the scanner failed to flag a module that plainly imports "
            f"agents.executors.registry; violations={result.violations}. The "
            "clean verdict on the real tree cannot be trusted."
        )
        assert not any("innocent.py" in v for v in result.violations), (
            "the scanner flagged a module that imports agents.tier2, not "
            "agents.executors — it is over-matching and will produce false alarms."
        )

    @pytest.mark.parametrize(
        "source",
        [
            "import agents.executors.registry",
            "import agents.executors",
            "import agents.executors.dms as d",
            "from agents.executors import registry",
            "from agents.executors.dms import DmsExecutor",
            "from agents.executors.base import ExecuteResult",
        ],
    )
    def test_scanner_catches_every_import_form(self, tmp_path: Path, source: str) -> None:
        """Each import spelling must be caught, not just the one form tested above."""
        mod = tmp_path / "m.py"
        mod.write_text(source + "\n", encoding="utf-8")
        result = modules_importing_executors(tmp_path)
        assert result.violations, f"scanner missed import form: {source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            "import agents.tier2.contract",
            "from agents.health import agent",
            "import executors",
            "from agents import tier2",
        ],
    )
    def test_scanner_does_not_over_match(self, tmp_path: Path, source: str) -> None:
        """A guard that flags innocent imports gets disabled by whoever it annoys."""
        mod = tmp_path / "m.py"
        mod.write_text(source + "\n", encoding="utf-8")
        result = modules_importing_executors(tmp_path)
        assert not result.violations, f"scanner false-positived on: {source!r}"


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    def test_all_six_targets_registered(self) -> None:
        assert set(EXECUTORS) == set(TARGET_SYSTEMS)
        assert len(EXECUTORS) == 6, f"expected six executors, got {sorted(EXECUTORS)}"

    def test_target_systems_match_spec_enum(self) -> None:
        """Pinned against the spec's Action.target_system enum verbatim."""
        assert set(TARGET_SYSTEMS) == {
            "dms",
            "vehicle-command",
            "retail",
            "none",
            "chat",
            "ios-owner",
        }

    def test_key_agrees_with_executor_target_system(self) -> None:
        """A key/executor mismatch routes one target's Actions to another's API."""
        for key, executor in EXECUTORS.items():
            assert executor.target_system == key, (
                f"EXECUTORS[{key!r}] is {type(executor).__name__} with "
                f"target_system={executor.target_system!r}"
            )

    def test_every_executor_satisfies_the_protocol(self) -> None:
        for key, executor in EXECUTORS.items():
            assert isinstance(executor, Executor), f"{key} fails the Executor protocol"
            assert callable(getattr(executor, "dispatch", None)), f"{key}.dispatch missing"

    def test_every_dispatch_is_never_raises_wrapped(self) -> None:
        """The Protocol's "Never raises" clause must be enforced, not just stated.

        An unwrapped `dispatch` that throws would leave the Action row written but
        statusless and the owner's card stuck on `pending_execution` forever.
        """
        unwrapped = [
            key
            for key, ex in EXECUTORS.items()
            if not hasattr(type(ex).dispatch, "__wrapped__")
        ]
        assert not unwrapped, (
            f"executor(s) {unwrapped} have a dispatch that is not @never_raises-"
            "wrapped; an exception there strands the Action on pending_execution."
        )

    def test_no_executor_module_imports_sns(self) -> None:
        """Channel policy belongs to the broker — no executor may push directly.

        Consent, quiet hours, severity threshold and rate limits are all enforced
        at the broker. An executor calling `sns:Publish` bypasses every one of
        them and can notify an owner who opted out.
        """
        executors_dir = Path(base.__file__).resolve().parent
        offenders: list[str] = []
        scanned = 0
        for path in sorted(executors_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            scanned += 1
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif node.module:
                        names = [node.module]
                    for n in names:
                        segs = {s for part in n.lower().split(".") for s in part.split("_")}
                        if "sns" in segs:
                            offenders.append(f"{path.name}:{node.lineno} imports {n}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("client", "resource")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).lower() == "sns"
                ):
                    offenders.append(f"{path.name}:{node.lineno} constructs an SNS client")

        assert scanned >= 7, (
            f"only scanned {scanned} executor module(s); the corpus is too small "
            "for this verdict to mean anything (expected base + six executors + "
            "registry)."
        )
        assert not offenders, f"executor module(s) reach SNS directly: {offenders}"


# ---------------------------------------------------------------------------
# Dispatch-target boundary — fail closed on an unknown target
# ---------------------------------------------------------------------------


class TestUnknownTargetFailsClosed:
    @pytest.mark.parametrize("bad", ["", "DMS", "dms ", "sms", "unknown-target", "None"])
    def test_get_executor_raises_on_unknown_target(self, bad: str) -> None:
        with pytest.raises(UnknownTargetSystemError):
            get_executor(bad)

    def test_unknown_target_does_not_fall_back_to_advisory(self) -> None:
        """The specific wrong answer this guard exists to prevent.

        `AdvisoryExecutor` returns a *success* status. If an unroutable target
        fell back to it, the Action would be recorded completed and the owner
        told their request went through, when nothing was ever dispatched. Silent
        success on an undeliverable action is worse than a loud error.
        """
        advisory = EXECUTORS["none"]
        try:
            resolved = get_executor("definitely-not-a-target")
        except UnknownTargetSystemError:
            return  # correct: failed closed
        assert resolved is not advisory, (
            "get_executor fell back to AdvisoryExecutor for an unknown target. "
            "AdvisoryExecutor reports success, so the owner would be told work "
            "was dispatched that never left the building."
        )
        pytest.fail(f"unknown target resolved to {type(resolved).__name__} instead of raising")

    @pytest.mark.parametrize("target", [None, 123, "", {}])
    def test_dispatch_rejects_unusable_target_system(self, target: object) -> None:
        with pytest.raises(UnknownTargetSystemError):
            registry.dispatch({"action_id": "AVX-ACT-x", "target_system": target}, {})

    def test_advisory_would_have_reported_success(self) -> None:
        """Substantiates the premise of the fall-back test above.

        If `AdvisoryExecutor` ever stopped returning a success status, the
        fall-back test would still pass but would no longer be guarding anything
        — so pin the premise itself.
        """
        result = EXECUTORS["none"].dispatch(
            {"action_id": "AVX-ACT-premise", "target_system": "none", "action_kind": "acknowledge_only"},
            {"sub": "owner-1"},
        )
        assert isinstance(result, ExecuteResult)
        assert result.status not in (ActionStatus.FAILED, ActionStatus.DECLINED), (
            "AdvisoryExecutor no longer reports success; the unknown-target "
            "fall-back test is no longer protecting against silent success."
        )


class TestRegistryImportIsSideEffectFree:
    """Importing the registry must not touch the network.

    `EXECUTORS` instantiates all six executors at module import. Two of them
    (dms, vehicle-command) read per-target policy from SSM. When that read ran in
    the constructor, `import agents.executors.registry` performed several live
    `get_parameter` calls before any Action existed — reading whichever
    `VSA_STAGE` the process carried (observed resolving to `/vsa/prod/...` in a
    developer shell), and blocking on boto3 retries wherever SSM was unreachable.

    Policy is therefore loaded lazily on first dispatch, behind base.py's
    process-lifetime cache, so the cold-start-read intent survives without the
    import-time I/O. These tests hold that line.
    """

    def test_constructing_every_executor_makes_no_ssm_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _boom(*args: object, **kwargs: object) -> object:
            calls.append("ssm")
            raise AssertionError(
                "an executor constructor called SSM. Policy must load lazily on "
                "first dispatch, not at construction — registry.py builds all six "
                "at import, so a constructor read makes importing the registry "
                "perform network I/O against an ambient VSA_STAGE."
            )

        monkeypatch.setattr(base, "_get_ssm", _boom)
        base.reset_policy_cache()

        # Rebuild the table exactly as registry.py does at import.
        for cls in (
            type(EXECUTORS["dms"]),
            type(EXECUTORS["vehicle-command"]),
            type(EXECUTORS["retail"]),
            type(EXECUTORS["none"]),
            type(EXECUTORS["chat"]),
            type(EXECUTORS["ios-owner"]),
        ):
            cls()

        assert not calls, f"executor construction performed {len(calls)} SSM call(s)"

    def test_policy_still_loads_on_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The premise of the test above: laziness must not mean never.

        Without this, deleting the policy read entirely would leave
        `test_constructing_every_executor_makes_no_ssm_call` passing while the
        operator's configured retry policy was silently ignored in favour of the
        hardcoded default — which is the failure the config indirection exists to
        prevent.

        The asserted value is deliberately *inside* the clamp bound
        (`_MAX_RETRY_COUNT`); an earlier version of this test asserted 7, which
        the clamp then reduced to 5, so the test failed for a reason unrelated to
        the property it is about. Clamping is covered separately below.
        """
        reads: list[str] = []
        configured = base._MAX_RETRY_COUNT - 1
        assert configured >= 1, "clamp bound leaves no in-range value to test with"

        class _Stub:
            def get_parameter(self, Name: str) -> dict:  # noqa: N803 — boto3 kwarg name
                reads.append(Name)
                return {"Parameter": {"Value": '{"retry_count": %d}' % configured}}

        base.reset_policy_cache()
        policy = base.load_retry_policy("dms", ssm_client=_Stub())

        assert reads, "load_retry_policy made no SSM read at all"
        assert policy.retry_count == configured, (
            f"the SSM-provided retry_count ({configured}) did not reach the policy "
            "object; a hardcoded default would outrank the operator's configuration."
        )
        base.reset_policy_cache()

    def test_ssm_supplied_retry_count_is_clamped_to_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator must not be able to configure past the gateway limit.

        The approve route is synchronous behind API Gateway's 29s timeout, so
        `retry_count` and `backoff_cap_ms` are bounded at load time. Without the
        clamp, `retry_count: 50` in Parameter Store would be accepted and the
        route would die at the gateway with the Action still `pending_execution`.
        """
        class _Stub:
            def get_parameter(self, Name: str) -> dict:  # noqa: N803
                return {
                    "Parameter": {
                        "Value": '{"retry_count": 50, "backoff_cap_ms": 600000}'
                    }
                }

        base.reset_policy_cache()
        policy = base.load_retry_policy("dms", ssm_client=_Stub())
        base.reset_policy_cache()

        assert policy.retry_count <= base._MAX_RETRY_COUNT, (
            f"retry_count={policy.retry_count} exceeds the clamp "
            f"({base._MAX_RETRY_COUNT}); SSM configuration can push the "
            "synchronous approve route past API Gateway's limit."
        )
        assert policy.backoff_cap_ms <= base._MAX_BACKOFF_CAP_MS

    def test_worst_case_wall_clock_stays_inside_the_gateway_budget(self) -> None:
        """The sub-29s property the spec calls load-bearing, asserted numerically.

        Walks all THREE axes — retry_count, backoff_base_ms, backoff_cap_ms —
        including values above the clamps, because review Cycle 2 found the
        earlier version of this test walked only `retry_count` while its docstring
        claimed the clamp's whole range. Five reachable combinations violated the
        budget, the worst at 42s against a 29s gateway timeout, and the test was
        green throughout.
        """
        violations: list[str] = []
        combinations = 0
        for retries in range(1, base._MAX_RETRY_COUNT + 1):
            for base_ms in (0, 500, 1000, 4000, 8000, 99999):
                for cap_ms in (0, 1000, 4000, 8000, 99999):
                    if cap_ms < base_ms:
                        continue  # rejected by __post_init__
                    combinations += 1
                    policy = base.RetryPolicy(
                        retry_count=retries,
                        backoff_base_ms=base_ms,
                        backoff_cap_ms=cap_ms,
                    )
                    worst = policy.worst_case_wall_clock_s
                    if worst > base.SYNCHRONOUS_BUDGET_S + 0.01:
                        violations.append(
                            f"retry_count={retries} base={base_ms} cap={cap_ms} "
                            f"-> {worst:.1f}s"
                        )
                    assert policy.per_attempt_timeout_s > 0

        assert combinations >= 60, (
            f"only {combinations} combinations exercised; the sweep is too narrow "
            "to substantiate the claim in this docstring"
        )
        assert not violations, (
            f"{len(violations)} policy combination(s) exceed the "
            f"{base.SYNCHRONOUS_BUDGET_S}s synchronous budget, so the approve "
            f"route can be killed by API Gateway mid-dispatch leaving the Action "
            f"on pending_execution:\n  " + "\n  ".join(violations)
        )

    def test_budget_fit_does_not_silently_change_the_staging_default(self) -> None:
        """The fit-to-budget logic must leave the confirmed defaults alone.

        The clamping exists for pathological configurations. If it also altered
        the user-confirmed staging values (retry_count=3, base=1000, cap=4000),
        it would be changing settled behaviour rather than bounding abuse.
        """
        p = base.RetryPolicy()
        assert (p.retry_count, p.backoff_base_ms, p.backoff_cap_ms) == (3, 1000, 4000)
        assert p.worst_case_wall_clock_s <= base.SYNCHRONOUS_BUDGET_S + 0.01

    def test_attempts_are_preferred_over_backoff_when_trimming(self) -> None:
        """A pathological backoff loses its window before attempts are dropped.

        Attempts are what the policy is for; the wait between them is the
        negotiable part. Asserted so a future refactor cannot quietly invert the
        priority and start dropping retries instead.
        """
        p = base.RetryPolicy(retry_count=3, backoff_base_ms=99999, backoff_cap_ms=99999)
        assert p.retry_count == 3, "attempts were dropped before backoff was trimmed"
        assert p.backoff_cap_ms < 99999, "backoff was not trimmed"
        assert p.worst_case_wall_clock_s <= base.SYNCHRONOUS_BUDGET_S + 0.01

    def test_never_raises_does_not_log_secrets(self, caplog: pytest.LogCaptureFixture) -> None:
        """@never_raises must log the exception CLASS only — no message, no traceback.

        It is the catch-all for all six executors, so it sees arbitrary requests /
        botocore exceptions whose messages routinely echo request and response
        content. An earlier version used `log.exception` (= `exc_info=True`) and
        therefore emitted the whole traceback, ending in
        `ExceptionClass: <message>` — leaking a forwarded JWT and a VIN while its
        own docstring promised it did not.

        caplog is deliberately left UNSCOPED: the previous VIN canary in
        test_chat.py scoped itself to logger `agents.executors.chat`, which cannot
        observe a leak emitted from `agents.executors.base`, which is why this
        path went unnoticed.
        """
        vin_canary = "1FA6P8TH5L5100001"
        jwt_canary = "eyJhbGciOiJSUzI1NiJ9.CANARYPAYLOAD.sig"

        class _Exploding:
            target_system = "canary"

            @base.never_raises
            def dispatch(self, action, claims):  # type: ignore[no-untyped-def]
                raise RuntimeError(
                    f"upstream 401 body={{'vin':'{vin_canary}'}} "
                    f"Authorization=Bearer {jwt_canary}"
                )

        with caplog.at_level(logging.DEBUG):
            result = _Exploding().dispatch({}, {})

        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == base.FailureKind.EXECUTOR_TERMINAL_ERROR
        assert result.last_error_class == "RuntimeError", (
            "the exception class is the one thing that SHOULD be recorded"
        )

        blob = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.args) for r in caplog.records]
            + [r.exc_text or "" for r in caplog.records]
        )
        assert vin_canary not in blob, (
            f"VIN leaked into a log record via @never_raises:\n{blob[:600]}"
        )
        assert jwt_canary not in blob, (
            f"JWT leaked into a log record via @never_raises:\n{blob[:600]}"
        )
        assert not any(r.exc_info for r in caplog.records), (
            "a log record carries exc_info, so the traceback (ending in the "
            "exception message) is emitted — do not reintroduce log.exception here."
        )


class TestRegistryValidationBites:
    """The import-time validator must reject each way the table can be wrong.

    Exercised by re-running the validator against a patched `EXECUTORS`, so these
    assert the validator's behaviour rather than merely that it was called once
    at import.
    """

    def test_missing_target_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trimmed = {k: v for k, v in EXECUTORS.items() if k != "dms"}
        monkeypatch.setattr(registry, "EXECUTORS", trimmed)
        with pytest.raises(RuntimeError, match="missing target_system"):
            registry._validate_registry()

    def test_extra_target_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        padded = dict(EXECUTORS)
        padded["carrier-pigeon"] = EXECUTORS["none"]
        monkeypatch.setattr(registry, "EXECUTORS", padded)
        with pytest.raises(RuntimeError, match="unknown target_system"):
            registry._validate_registry()

    def test_key_executor_mismatch_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The dangerous one: everything imports, dispatch goes to the wrong API."""
        swapped = dict(EXECUTORS)
        swapped["dms"] = EXECUTORS["retail"]
        monkeypatch.setattr(registry, "EXECUTORS", swapped)
        with pytest.raises(RuntimeError, match="target_system"):
            registry._validate_registry()

    def test_validator_passes_on_the_real_table(self) -> None:
        registry._validate_registry()



# ---------------------------------------------------------------------------
# Review Cycle 2, Critical 3 — escalation is enforced at ONE point, for EVERY
# target.
#
# Cycle 1 lifted escalation out of dms into escalation.py, but left each executor
# responsible for calling it. Cycle 2 found that structurally unreliable: `chat`
# and `ios_owner` had zero call sites, and so did `@never_raises` — the catch-all
# in base.py that synthesises escalate=True for all six executors when an
# exception escapes. The paths most likely to fail unexpectedly were exactly the
# ones that escalated to nobody, and `AVXActionTerminalFailure` is dimensioned by
# target_system while only dms could emit it.
#
# Firing once in registry.dispatch() makes the property checkable in one place.
# These tests are that check, and they run across all six targets rather than the
# subset that happened to wire it up.
# ---------------------------------------------------------------------------


class TestEscalationFiresForEveryTarget:
    @staticmethod
    def _force_failure(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
        """Make `target`'s executor return a terminal failure without network."""
        failing = ExecuteResult.exhausted(
            retry_count=3, last_error_class="HTTPError", last_error_status=503
        )
        stub = type(
            "FailingExecutor",
            (),
            {
                "target_system": target,
                "dispatch": lambda self, action, claims: failing,
            },
        )()
        patched = dict(EXECUTORS)
        patched[target] = stub
        monkeypatch.setattr(registry, "EXECUTORS", patched)

    @pytest.mark.parametrize("target", list(TARGET_SYSTEMS))
    def test_every_target_escalates_on_terminal_failure(
        self, target: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All six. Not just the ones that used to call fire() themselves."""
        self._force_failure(monkeypatch, target)
        events, cw, enqueue = MagicMock(), MagicMock(), MagicMock()

        result = registry.dispatch(
            {
                "action_id": "AVX-ACT-reg-1",
                "owner_id": "owner-1",
                "action_kind": "book_combined_visit",
                "target_system": target,
            },
            {"sub": "owner-1", "token": "jwt"},
            escalation_clients={
                "events_client": events,
                "cloudwatch_client": cw,
                "enqueue_owner_card": enqueue,
                "ssm_client": MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            },
        )

        assert result.escalate is True
        events.put_events.assert_called_once(), (
            f"target={target!r} produced a terminal failure that emitted no "
            "EventBridge event — an operator alarm on AVXActionTerminalFailure is "
            "silent for this target"
        )
        cw.put_metric_data.assert_called_once()
        dims = {
            d["Name"]: d["Value"]
            for d in cw.put_metric_data.call_args.kwargs["MetricData"][0]["Dimensions"]
        }
        assert dims.get("target_system") == target

    @pytest.mark.parametrize("target", [t for t in TARGET_SYSTEMS if t != "ios-owner"])
    def test_owner_card_is_addressed_and_does_not_collide(
        self, target: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner card must reach the owner AND not overwrite the Action.

        Two Cycle findings in one assertion, because they interact:
        * C3 — the card must carry a non-empty `owner_id`, or it is written
          addressed to nobody and invisible to every owner-scoped query.
        * Cycle 2 Critical 1 — the card's `action_id` must NOT be the failing
          Action's own id. `IosOwnerExecutor` uses `action_id` as the DynamoDB row
          key and `avx-actions` is keyed on it alone, so reusing it makes the
          card's `put_item` a full replace of the Action that just failed —
          destroying its `audit_trail`, i.e. the D5 failure record, as a side
          effect of telling the owner about the failure.
        """
        self._force_failure(monkeypatch, target)
        enqueue = MagicMock()

        registry.dispatch(
            {
                "action_id": "AVX-ACT-original",
                "owner_id": "owner-42",
                "action_kind": "book_combined_visit",
                "target_system": target,
            },
            {"sub": "owner-42"},
            escalation_clients={
                "events_client": MagicMock(),
                "cloudwatch_client": MagicMock(),
                "enqueue_owner_card": enqueue,
                "ssm_client": MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            },
        )

        enqueue.assert_called_once()
        finding = enqueue.call_args.args[0]
        assert finding.get("owner_id") == "owner-42", (
            f"target={target!r}: escalation card carries owner_id="
            f"{finding.get('owner_id')!r}; an unaddressed card is invisible to the owner"
        )
        assert finding.get("action_id") != "AVX-ACT-original", (
            f"target={target!r}: the escalation card reuses the failing Action's "
            "action_id. ios_owner keys the DDB row on action_id, so this write "
            "REPLACES the Action row and destroys its audit_trail — the D5 failure "
            "record is erased by the act of reporting the failure."
        )
        assert finding.get("source_action_id") == "AVX-ACT-original", (
            "the card must still reference the Action it is about"
        )

    def test_never_raises_failures_also_escalate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The catch-all path escalates too — it previously never did.

        `@never_raises` synthesises escalate=True for any unexpected exception in
        any of the six executors. Before centralisation nothing fired for it, so
        the least-expected failures were the least-reported ones.
        """
        class _Exploding:
            target_system = "dms"

            @base.never_raises
            def dispatch(self, action, claims):  # type: ignore[no-untyped-def]
                raise RuntimeError("unexpected")

        patched = dict(EXECUTORS)
        patched["dms"] = _Exploding()
        monkeypatch.setattr(registry, "EXECUTORS", patched)
        events, cw = MagicMock(), MagicMock()

        result = registry.dispatch(
            {"action_id": "A", "owner_id": "o", "action_kind": "k", "target_system": "dms"},
            {"sub": "o"},
            escalation_clients={
                "events_client": events,
                "cloudwatch_client": cw,
                "enqueue_owner_card": MagicMock(),
                "ssm_client": MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            },
        )

        assert result.failure_kind == base.FailureKind.EXECUTOR_TERMINAL_ERROR
        events.put_events.assert_called_once()
        cw.put_metric_data.assert_called_once()

    def test_escalation_fires_exactly_once_not_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against the double-fire that centralisation could reintroduce.

        While migrating, both the executors AND the registry fired, producing two
        owner cards and two AVXActionTerminalFailure datapoints per failure —
        which would have read as a doubling of the failure rate on the operator's
        own alarm.
        """
        self._force_failure(monkeypatch, "dms")
        events, cw, enqueue = MagicMock(), MagicMock(), MagicMock()

        registry.dispatch(
            {"action_id": "A", "owner_id": "o", "action_kind": "k", "target_system": "dms"},
            {"sub": "o"},
            escalation_clients={
                "events_client": events,
                "cloudwatch_client": cw,
                "enqueue_owner_card": enqueue,
                "ssm_client": MagicMock(get_parameter=MagicMock(side_effect=Exception())),
            },
        )

        assert events.put_events.call_count == 1, (
            f"put_events fired {events.put_events.call_count} times for one failure"
        )
        assert cw.put_metric_data.call_count == 1
        assert enqueue.call_count == 1

    def test_success_and_declined_do_not_escalate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Addendum D5: `declined` does not escalate; nor does success."""
        for result in (
            ExecuteResult.success(target_ref="RO-1"),
            ExecuteResult.refused(last_error_status=400),
        ):
            stub = type(
                "R", (), {"target_system": "dms", "dispatch": lambda s, a, c, _r=result: _r}
            )()
            patched = dict(EXECUTORS)
            patched["dms"] = stub
            monkeypatch.setattr(registry, "EXECUTORS", patched)
            events, cw = MagicMock(), MagicMock()

            registry.dispatch(
                {"action_id": "A", "owner_id": "o", "action_kind": "k", "target_system": "dms"},
                {"sub": "o"},
                escalation_clients={"events_client": events, "cloudwatch_client": cw},
            )
            events.put_events.assert_not_called()
            cw.put_metric_data.assert_not_called()



# ---------------------------------------------------------------------------
# Review Cycle 3 Warning 6 — the three HTTP executors must agree on the audit
# trail for the same underlying failure.
#
# They did not. For one malformed 2xx, dms and vehicle_command recorded
# `InvalidJsonResponse` while retail recorded `TypeError` or `JSONDecodeError`;
# for a terminal 4xx and for status exhaustion, retail recorded `None` where the
# siblings recorded `HTTPError`. An operator comparing two audit trails for the
# same downstream problem saw two different stories, and the Cycle 2 C2 fix had
# been applied one executor too narrowly as well as one branch too narrowly.
#
# Pinned as a cross-executor invariant rather than three per-file assertions,
# because the defect is the DISAGREEMENT — a per-file test cannot see it, which
# is why it survived two cycles.
# ---------------------------------------------------------------------------


class TestHttpExecutorsAgreeOnTheAuditTrail:
    _SCENARIOS = {
        "2xx_non_object_body": ("InvalidJsonResponse", 200, []),
        "2xx_unparseable_body": ("InvalidJsonResponse", 200, "<html>gateway</html>"),
        "terminal_4xx_refusal": ("HTTPError", 400, {"error": "no"}),
        # 403 takes the client_error branch, which 400 does not. Without it,
        # retail's client_error class could revert to "ClientError" and diverge
        # from its siblings again with a green suite (review Cycle 4, W6).
        "client_error_403": ("HTTPError", 403, {"error": "forbidden"}),
        "client_error_404": ("HTTPError", 404, {"error": "missing"}),
        "status_exhaustion_5xx": ("HTTPError", 503, {"error": "down"}),
    }

    @staticmethod
    def _dispatch_all(status: int, body: object) -> dict[str, str | None]:
        import os

        import responses as _responses

        from agents.executors.dms import DmsExecutor
        from agents.executors.retail import RetailExecutor
        from agents.executors.vehicle_command import VehicleCommandExecutor

        os.environ.update(
            {
                "VSA_DMS_API_ENDPOINT": "http://dms.test",
                "VSA_CMS_COMMANDS_ENDPOINT": "http://cms.test",
                "VSA_API_BASE": "http://api.test",
            }
        )
        fast = base.RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0)
        ssm = MagicMock(get_parameter=MagicMock(side_effect=Exception()))
        claims = {"token": "t", "id_token": "t", "custom:tenantId": "ten"}

        targets = {
            "dms": (
                "http://dms.test/api/dms/fleet/repair-orders",
                lambda: DmsExecutor(
                    retry_policy=fast, sleep_fn=lambda _s: None, ssm_client=ssm
                ).dispatch(
                    # F35.2: DMS requires vehicle_vin + dealer_id; without them the
                    # executor now stops locally and never reaches the stubbed URL.
                    {
                        "action_id": "A",
                        "owner_id": "o",
                        "action_kind": "k",
                        "parameters": {"vehicle_vin": "VIN1", "dealer_id": "dealer-seattle"},
                    },
                    claims,
                ),
            ),
            "vehicle-command": (
                "http://cms.test/api/commands/VEH-1",
                lambda: VehicleCommandExecutor(
                    retry_policy=fast, _sleep=lambda _s: None
                ).dispatch(
                    {
                        "action_id": "A",
                        "owner_id": "o",
                        "action_kind": "k",
                        "vehicle_id": "VEH-1",
                        "params": {"commandName": "lock"},
                    },
                    claims,
                ),
            ),
            "retail": (
                "http://api.test/acquire/leads",
                lambda: RetailExecutor(sleep=lambda _s: None, ssm_client=ssm).dispatch(
                    {
                        "action_id": "A",
                        "owner_id": "o",
                        "action_kind": "lead_capture",
                        "parameters": {},
                    },
                    claims,
                ),
            ),
        }

        observed: dict[str, str | None] = {}
        for name, (url, run) in targets.items():
            with _responses.RequestsMock(assert_all_requests_are_fired=False) as rm:
                if isinstance(body, str):
                    rm.add(_responses.POST, url, body=body, status=status)
                else:
                    rm.add(_responses.POST, url, json=body, status=status)
                observed[name] = run().last_error_class
        return observed

    @pytest.mark.parametrize("scenario", list(_SCENARIOS))
    def test_all_three_report_the_same_failure_class(self, scenario: str) -> None:
        expected, status, body = self._SCENARIOS[scenario]
        observed = self._dispatch_all(status, body)

        assert len(set(observed.values())) == 1, (
            f"{scenario}: the three HTTP executors disagree on last_error_class — "
            f"{observed}. One underlying failure must produce one audit record, or "
            "an operator comparing trails sees two different stories."
        )
        assert next(iter(observed.values())) == expected, (
            f"{scenario}: expected {expected!r}, got {observed}. A bare Python "
            "exception name means an ExecuteResult construction raised inside a "
            "retry loop and @never_raises degraded the specific class."
        )

    def test_no_executor_records_a_2xx_as_an_error_status(self) -> None:
        """The construction rule, verified through the real executors.

        base.py rejects a 2xx on a failure kind; this confirms no executor is
        reaching that rejection (which would surface as a degraded class rather
        than as an error, because @never_raises catches it).
        """
        for scenario, (_expected, status, body) in self._SCENARIOS.items():
            if not (200 <= status < 300):
                continue
            observed = self._dispatch_all(status, body)
            for name, cls in observed.items():
                assert cls not in ("ValueError", "TypeError", "AttributeError"), (
                    f"{name} recorded {cls!r} for {scenario} — the hallmark of a "
                    "2xx passed into a failure-status construction"
                )



# ---------------------------------------------------------------------------
# Review Cycle 4 Critical — a test that ENTERS the escalation-failure handler.
#
# The Cycle 3 W1 fix wrapped `escalation.fire` in try/except, and the handler
# called `log.error` in a module that defined no logger. Every invocation raised
# `NameError`, so `dispatch` still destroyed the `ExecuteResult` — byte-for-byte
# the outcome W1 was filed for, and worse: it read as protected, it failed for
# every cause rather than some, and it discarded the original exception so the
# escalation failure became undiagnosable.
#
# Nothing caught it because no test entered the branch, and the mutation battery's
# entry for this property was a SyntaxError false positive (turning `try:` into
# `if True:` dangles the `except`, so the module fails to import and the run gets
# scored CAUGHT — proving only that the file parses).
#
# The general lesson, and the reviewer's: every new `except` carries a test that
# enters it. An unexecuted handler is not error handling.
# ---------------------------------------------------------------------------


class TestEscalationFailureDoesNotDestroyTheResult:
    @staticmethod
    def _failing_executor(monkeypatch: pytest.MonkeyPatch) -> ExecuteResult:
        expected = ExecuteResult.exhausted(
            retry_count=3, last_error_class="HTTPError", last_error_status=503
        )
        stub = type(
            "S", (), {"target_system": "dms", "dispatch": lambda s, a, c: expected}
        )()
        patched = dict(EXECUTORS)
        patched["dms"] = stub
        monkeypatch.setattr(registry, "EXECUTORS", patched)
        return expected

    _ACTION = {
        "action_id": "AVX-ACT-esc-fail",
        "owner_id": "owner-1",
        "action_kind": "book_combined_visit",
        "target_system": "dms",
    }

    @pytest.mark.parametrize(
        "boom",
        [
            RuntimeError("escalation blew up"),
            ValueError("bad config shape"),
            KeyError("missing key"),
            TypeError("wrong type"),
        ],
    )
    def test_result_survives_any_escalation_exception(
        self, boom: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting a failure must never be able to lose the failure."""
        expected = self._failing_executor(monkeypatch)

        from agents.executors import escalation

        monkeypatch.setattr(escalation, "fire", MagicMock(side_effect=boom))

        result = registry.dispatch(dict(self._ACTION), {"sub": "owner-1"})

        # Assert escalation was actually attempted. Without this the test passes
        # when escalation never fires at all — it would then be asserting that a
        # non-event does not break anything, which is true of almost any change.
        # Review Cycle 5 flagged these four as vacuous in isolation for exactly
        # that reason.
        assert escalation.fire.called, (
            "escalation was never attempted, so this test proves nothing about "
            "surviving an escalation failure"
        )
        assert result is expected, (
            f"a {type(boom).__name__} from escalation destroyed the ExecuteResult; "
            "the approve route is the caller, so the Action row is left written but "
            "statusless and the owner's card stays on pending_execution forever"
        )
        assert result.failure_kind == base.FailureKind.EXECUTOR_EXHAUSTED_RETRIES, (
            "the precise failure kind must survive the escalation failure"
        )

    def test_the_handler_records_the_cause(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The handler must be reachable AND say what went wrong.

        The first version discarded the cause. It must name the exception class
        (not its message — that can carry a JWT or a VIN, per base.never_raises).
        """
        self._failing_executor(monkeypatch)
        from agents.executors import escalation

        secret = "eyJhbGciOiJSUzI1NiJ9.HANDLER_CANARY.sig"
        monkeypatch.setattr(
            escalation, "fire", MagicMock(side_effect=RuntimeError(f"leak {secret}"))
        )

        with caplog.at_level(logging.DEBUG):
            registry.dispatch(dict(self._ACTION), {"sub": "owner-1"})

        blob = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.args) for r in caplog.records]
            + [r.exc_text or "" for r in caplog.records]
        )
        assert "RuntimeError" in blob, (
            "the escalation failure was swallowed with no record of the cause, so "
            "an operator cannot tell escalation is broken"
        )
        assert secret not in blob, "the exception message leaked into a log record"

    def test_registry_defines_a_logger(self) -> None:
        """The specific defect, pinned directly.

        `registry.py` was the only module in the package using `log.` without
        defining one. A one-line absence that made a 20-line safety comment false.
        """
        assert isinstance(getattr(registry, "log", None), logging.Logger), (
            "registry.py has no module logger, so any log call in it raises "
            "NameError — including the one guarding the escalation call"
        )

    def test_result_survives_an_unimportable_escalation_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lazy import must be INSIDE the guard, and this is what pins it.

        Review Cycle 5 found the import sitting outside the `try`, so an
        ImportError destroyed the `ExecuteResult` while the adjacent comment
        asserted it could not. Cycle 6 found the fix correct but unpinned:
        reverting the import back above the `try` left all 350 tests passing.

        The regression path is concrete, not theoretical. The import carries
        `# noqa: PLC0415` — a marker whose whole meaning is "a linter wants this
        hoisted". Any tidy-imports pass performs exactly that revert, nothing
        fails, and the comment becomes false prose about a safety property. This
        test is the thing that objects.
        """
        import builtins

        expected = self._failing_executor(monkeypatch)
        real_import = builtins.__import__

        def _fail_escalation_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "agents.executors.escalation":
                raise ImportError("simulated: escalation module unavailable")
            fromlist = args[2] if len(args) > 2 else kwargs.get("fromlist")
            if fromlist and "escalation" in tuple(fromlist):
                raise ImportError("simulated: escalation attribute unavailable")
            return real_import(name, *args, **kwargs)

        import sys

        monkeypatch.delitem(sys.modules, "agents.executors.escalation", raising=False)
        monkeypatch.setattr(builtins, "__import__", _fail_escalation_import)

        result = registry.dispatch(dict(self._ACTION), {"sub": "owner-1"})

        assert result is expected, (
            "an ImportError on the lazy `escalation` import destroyed the "
            "ExecuteResult. The import must sit INSIDE the try/except, or the "
            "guard does not cover what its comment promises."
        )
        assert result.failure_kind == base.FailureKind.EXECUTOR_EXHAUSTED_RETRIES
