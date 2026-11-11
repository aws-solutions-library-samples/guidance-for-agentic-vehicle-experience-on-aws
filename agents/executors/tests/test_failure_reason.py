"""Red tests: operator-facing failure reasons — spec D14, contract § 10a.

Expected interface (Group 2, T2.1):

    agents/executors/failure_reason.py
        failure_reason(*, target_system: str, failure_kind: str | None = None,
                       error_status: int | None = None, retry_count: int = 0,
                       guardrail: str | None = None, guardrail_case: str | None = None,
                       ro_status: str | None = None) -> str

    guardrail_case (G4 only): "not_operator" | "no_ref" | "lookup_failed" | "not_found"
                              | "other_vehicle" | "not_completed"

The sentences are pinned literally here, copied from contract § 10a, never imported.

What these tests guard (Fix Group 5, review FG4-contract Critical 1): **no failure_reason
ever contains text from an exception, a response body or the caller.** A signature check
alone does not prove that. A builder can read `sys.exc_info()`, echo an unmapped input,
or admit any alphanumeric RO status, and still have the right parameter names. So:
  - every row is pinned by exact equality, and the whole table is also run inside an
    `except` block whose exception carries a sentinel (catches an ambient read);
  - a sentinel is fed into every `str` parameter, and must never appear in the output
    (catches an echo);
  - every output, valid input or not, must match the schema's `failure_reason` pattern
    (the vocabulary is closed);
  - the builder never raises; anything outside the table gets the fallback sentence.
The call-site half (a writer that bypasses the builder, e.g. `failure_reason=str(exc)`)
is guarded in `lambdas/api-avx-findings/tests/test_handler.py`
`TestD14FailureReasonPersisted`.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures()  # noqa: placeholder — xfail removed by Group 2

_REPO = Path(__file__).resolve().parents[3]
_SENTINEL = "SENTINEL-Bearer eyJhbGci.internal-dms.corp"
_FALLBACK = "AVX could not complete this action."

#: Every `target_system` in action.schema.json, with its § 10a names. `chat` and `none`
#: have no name: they take the target-free sentence (row 10).
_NAMES = {
    "dms": ("DMS", "DMS"),
    "cms": ("CMS", "CMS"),
    "retail": ("The retail system", "the retail system"),
    "vehicle-command": ("The vehicle", "the vehicle"),
    "ios-owner": ("The owner's app", "the owner's app"),
}
_ALL_TARGETS = (*_NAMES, "chat", "none")

#: DMS's seven RO status words (DMS `source/handlers/repair_orders.py`, the vocabulary
#: above `RO_ACTIVE_STATUSES`).
_DMS_RO_STATUSES = ("Draft", "InProgress", "AwaitingParts", "Ready", "Delivered", "Closed",
                    "Invoiced")


def _fn():
    from agents.executors.failure_reason import failure_reason  # noqa: PLC0415
    return failure_reason


def _schema_pattern() -> re.Pattern:
    schema = json.loads((_REPO / "agents/tier2/schemas/action.schema.json").read_text())
    return re.compile(schema["$defs"]["audit_entry"]["properties"]["failure_reason"]["pattern"])


# ---------------------------------------------------------------------------
# Rows 10-18: target outcomes, pinned by literal
# ---------------------------------------------------------------------------

_TARGET_ROWS = [
    # row 10: chat and none are target-free, whatever the kind
    (dict(target_system="chat", failure_kind="executor_terminal_error"), _FALLBACK),
    (dict(target_system="none", failure_kind="executor_terminal_error"), _FALLBACK),
    (dict(target_system="chat", failure_kind="target_refused"), _FALLBACK),
    # row 11: Stage A's CmsExecutor
    (dict(target_system="cms", failure_kind="target_refused"),
     "CMS cannot carry out this action yet."),
    # rows 12-13: refusals (reachable: vehicle 409; DMS 422 via the shared classifier)
    (dict(target_system="vehicle-command", failure_kind="target_refused", error_status=409),
     "The vehicle refused the request (HTTP 409)."),
    (dict(target_system="dms", failure_kind="target_refused", error_status=422),
     "DMS refused the request (HTTP 422)."),
    (dict(target_system="retail", failure_kind="target_refused", error_status=400),
     "The retail system refused the request (HTTP 400)."),
    (dict(target_system="cms", failure_kind="target_refused", error_status=422),
     "CMS refused the request (HTTP 422)."),
    (dict(target_system="dms", failure_kind="target_refused"),
     "DMS refused the request."),
    (dict(target_system="ios-owner", failure_kind="target_refused"),
     "The owner's app refused the request."),
    # row 14: exhausted, the target answered (5xx / 408 / 425 / 429)
    (dict(target_system="dms", failure_kind="executor_exhausted_retries", error_status=503,
          retry_count=3),
     "DMS was unavailable (HTTP 503) after 3 attempts."),
    (dict(target_system="retail", failure_kind="executor_exhausted_retries", error_status=429,
          retry_count=1),
     "The retail system was unavailable (HTTP 429) after 1 attempt."),
    # row 15: exhausted, no answer (connection error, timeout)
    (dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=3),
     "DMS did not respond after 3 attempts."),
    (dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=1),
     "DMS did not respond after 1 attempt."),
    (dict(target_system="vehicle-command", failure_kind="executor_exhausted_retries",
          retry_count=12),
     "The vehicle did not respond after 12 attempts."),
    # row 16: exhausted before any attempt (deadline)
    (dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=0),
     "AVX ran out of time before it could contact DMS."),
    (dict(target_system="retail", failure_kind="executor_exhausted_retries", retry_count=0),
     "AVX ran out of time before it could contact the retail system."),
    # row 17: terminal with a status (DMS 400 is what an unknown dealer_id gets)
    (dict(target_system="dms", failure_kind="executor_terminal_error", error_status=400),
     "DMS did not accept the request (HTTP 400)."),
    (dict(target_system="vehicle-command", failure_kind="executor_terminal_error",
          error_status=403, retry_count=2),
     "The vehicle did not accept the request (HTTP 403)."),
    # row 18: terminal, no status (malformed response, missing token, non-transient error)
    (dict(target_system="vehicle-command", failure_kind="executor_terminal_error"),
     "AVX could not complete the request to the vehicle."),
    (dict(target_system="dms", failure_kind="executor_terminal_error", retry_count=2),
     "AVX could not complete the request to DMS."),
    (dict(target_system="ios-owner", failure_kind="executor_terminal_error"),
     "AVX could not complete the request to the owner's app."),
]


@pytest.mark.parametrize("kwargs,expected", _TARGET_ROWS,
                         ids=[f"row-{i}" for i in range(len(_TARGET_ROWS))])
def test_target_rows(kwargs, expected):
    assert _fn()(**kwargs) == expected


def test_a_status_sentence_names_the_status_it_was_given():
    """W1: the sentence never contradicts `error_status`. For every status-bearing row
    and a spread of statuses, the sentence names exactly that status."""
    fn = _fn()
    for status in (400, 403, 404, 409, 422, 429, 500, 503, 504):
        for kind in ("target_refused", "executor_exhausted_retries", "executor_terminal_error"):
            reason = fn(target_system="dms", failure_kind=kind, error_status=status,
                        retry_count=2)
            assert f"(HTTP {status})" in reason, (kind, status, reason)
            assert "did not respond" not in reason, (kind, status, reason)


# ---------------------------------------------------------------------------
# Rows 1-9: guardrails, on every target (review FG4-contract Warning 3)
# ---------------------------------------------------------------------------

_GUARDRAIL_ROWS = [
    ("G1", None, None, "Blocked: exempting a safety recall needs a fleet operator."),
    ("G2", None, None, "Blocked: grounding a vehicle needs a fleet operator."),
    ("G3", None, None, "Blocked: sending a warranty claim needs a fleet operator."),
    ("G4", "not_operator", None, "Blocked: completing a recall needs a fleet operator."),
    ("G4", "no_ref", None, "Blocked: completing a recall needs a service record."),
    ("G4", "lookup_failed", None, "Blocked: the service record could not be checked."),
    ("G4", "not_found", None, "Blocked: the service record could not be found."),
    ("G4", "other_vehicle", None, "Blocked: the service record is for a different vehicle."),
    ("G4", "not_completed", "Closed",
     "Blocked: the service record is not complete (status Closed)."),
]


@pytest.mark.parametrize("target", _ALL_TARGETS)
@pytest.mark.parametrize("guardrail,case,ro_status,expected", _GUARDRAIL_ROWS,
                         ids=[f"{g}-{c}" for g, c, _, _ in _GUARDRAIL_ROWS])
def test_guardrail_reasons_on_every_target(guardrail, case, ro_status, expected, target):
    """A guardrail sentence wins over the target rows, on every target. G3's kind
    (`submit_warranty_claim`) targets DMS, so a builder that checks the DMS refusal first
    would tell the operator "DMS refused the request." for a G3 block."""
    assert _fn()(target_system=target, failure_kind="target_refused", guardrail=guardrail,
                 guardrail_case=case, ro_status=ro_status) == expected


@pytest.mark.parametrize("word", _DMS_RO_STATUSES)
def test_each_dms_status_word_is_shown(word):
    assert _fn()(target_system="cms", failure_kind="target_refused", guardrail="G4",
                 guardrail_case="not_completed", ro_status=word) == (
        f"Blocked: the service record is not complete (status {word}).")


@pytest.mark.parametrize("ro_status", [
    "Secret123",          # alphanumeric, not a DMS word: an isalnum() allowlist admits it
    "JaneSmith",
    "closed",             # case matters
    "Closed ",            # so does whitespace
    "Scheduled",          # named only in a DMS event-consumer docstring, not the vocabulary
    "<script>",
    "",
    _SENTINEL,
    None,
])
def test_any_other_ro_status_is_written_as_unknown(ro_status):
    assert _fn()(target_system="cms", failure_kind="target_refused", guardrail="G4",
                 guardrail_case="not_completed", ro_status=ro_status) == (
        "Blocked: the service record is not complete (status unknown).")


# ---------------------------------------------------------------------------
# Outside the table: the fallback, never an exception, never an echo
# ---------------------------------------------------------------------------

_OUTSIDE_THE_TABLE = [
    dict(target_system=_SENTINEL, failure_kind="target_refused"),
    dict(target_system=_SENTINEL, failure_kind="target_refused", error_status=409),
    dict(target_system="dms", failure_kind=_SENTINEL),
    dict(target_system="dms", failure_kind=None),
    dict(target_system="dms"),
    dict(target_system="dms", failure_kind="executor_terminal_error", guardrail="G2"),
    dict(target_system="dms", failure_kind="target_refused", guardrail=_SENTINEL),
    dict(target_system="dms", failure_kind="target_refused", guardrail="G4"),
    dict(target_system="dms", failure_kind="target_refused", guardrail="G4",
         guardrail_case=_SENTINEL),
    dict(target_system="dms", failure_kind="target_refused", guardrail="G1",
         guardrail_case="no_ref"),
    dict(target_system="dms", failure_kind="target_refused", guardrail="G2", error_status=403),
    dict(target_system="dms", failure_kind="target_refused", guardrail="G4",
         guardrail_case="no_ref", ro_status="Closed"),
    dict(target_system="dms", failure_kind="target_refused", guardrail_case="no_ref"),
    dict(target_system="dms", failure_kind="target_refused", ro_status="Closed"),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", error_status=503,
         retry_count=0),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=-1),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=2.0),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count="3"),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=_SENTINEL),
    dict(target_system="dms", failure_kind="executor_exhausted_retries", retry_count=True),
    dict(target_system="dms", failure_kind=None, guardrail="G2"),
    dict(target_system="dms", failure_kind="target_refused", error_status=600),
    dict(target_system="dms", failure_kind="target_refused", error_status=99),
    dict(target_system="dms", failure_kind="target_refused", error_status="409"),
    dict(target_system="dms", failure_kind="target_refused", error_status=True),
    dict(target_system="dms", failure_kind="target_refused", error_status=409.0),
    dict(target_system="chat", failure_kind="executor_terminal_error", error_status=500),
    dict(target_system=None, failure_kind="target_refused"),
    dict(target_system=7, failure_kind="target_refused"),
]


@pytest.mark.parametrize("kwargs", _OUTSIDE_THE_TABLE,
                         ids=[f"outside-{i}" for i in range(len(_OUTSIDE_THE_TABLE))])
def test_inputs_outside_the_table_get_the_fallback(kwargs):
    """Contract § 10a: the builder never raises, and never echoes an input."""
    assert _fn()(**kwargs) == _FALLBACK


# ---------------------------------------------------------------------------
# No-leak, by behaviour (review FG4-contract Critical 1(a))
# ---------------------------------------------------------------------------

_STR_PARAMS = ("target_system", "failure_kind", "guardrail", "guardrail_case", "ro_status")

#: One valid call per row family, used as the base the sentinel is injected into.
_BASES = [kw for kw, _ in _TARGET_ROWS] + [
    dict(target_system="cms", failure_kind="target_refused", guardrail=g,
         guardrail_case=c, ro_status=r)
    for g, c, r, _ in _GUARDRAIL_ROWS
]


@pytest.mark.parametrize("param", _STR_PARAMS)
@pytest.mark.parametrize("base", _BASES, ids=[f"base-{i}" for i in range(len(_BASES))])
def test_sentinel_in_any_string_parameter_never_reaches_the_reason(base, param):
    reason = _fn()(**{**base, param: _SENTINEL})
    for fragment in ("SENTINEL", "Bearer", "eyJ", "internal-dms"):
        assert fragment not in reason, (param, reason)
    assert _schema_pattern().fullmatch(reason), (param, reason)


def test_called_inside_an_except_it_reads_nothing_from_the_exception():
    """A builder that appends `sys.exc_info()[1]` keeps the right signature and passes
    every test that calls it outside an `except`. Every row, called while an exception
    carrying the sentinel is being handled, must still return its exact sentence, and so
    must every out-of-table input: the fallback branch is where a never-raising builder
    is most tempted to add "debugging context" (review Cycle 6b W1, mutant X1)."""
    fn = _fn()
    rows = _TARGET_ROWS + [
        (dict(target_system="dms", failure_kind="target_refused", guardrail=g,
              guardrail_case=c, ro_status=r), expected)
        for g, c, r, expected in _GUARDRAIL_ROWS
    ] + [(kwargs, _FALLBACK) for kwargs in _OUTSIDE_THE_TABLE]
    try:
        raise ConnectionError(
            f"HTTPSConnectionPool(host='internal-dms.corp', port=443) {_SENTINEL}")
    except ConnectionError:
        got = [(kwargs, fn(**kwargs), expected) for kwargs, expected in rows]
    for kwargs, reason, expected in got:
        assert reason == expected, (kwargs, reason)


def test_every_output_is_in_the_schema_vocabulary():
    """The vocabulary is closed: across every target, kind, status and attempt count,
    plus every out-of-table input, the output matches the schema's `pattern` and fits
    its length bounds."""
    fn = _fn()
    pattern = _schema_pattern()
    outputs = []
    for target in (*_ALL_TARGETS, _SENTINEL, "unknown-target"):
        for kind in ("target_refused", "executor_exhausted_retries",
                     "executor_terminal_error", None, _SENTINEL):
            for status in (None, 400, 409, 422, 429, 503, 99, 600):
                for n in (0, 1, 2, 3, 2.0, "3", True):
                    outputs.append(fn(target_system=target, failure_kind=kind,
                                      error_status=status, retry_count=n))
    outputs += [fn(**kw) for kw in _OUTSIDE_THE_TABLE]
    for reason in outputs:
        assert isinstance(reason, str)
        assert 1 <= len(reason) <= 300
        assert pattern.fullmatch(reason), reason


def test_no_free_text_parameter():
    """No parameter could carry an exception message, a response body or caller text.
    Necessary, not sufficient: the behavioural tests above carry the property."""
    params = set(inspect.signature(_fn()).parameters)
    assert params == {"target_system", "failure_kind", "error_status", "retry_count",
                      "guardrail", "guardrail_case", "ro_status"}
