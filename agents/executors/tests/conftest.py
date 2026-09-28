"""Package-level test guards for `agents/executors/`.

**No unit test in this package may construct a real AWS client.**

Why this needs to be structural rather than per-test diligence: several paths
swallow exceptions by design. `escalation.fire` must never raise (it runs on an
already-failing path), and `@never_raises` catches everything by contract. So a
test that forgets to stub an AWS factory does not fail — it silently reaches out.
Two consequences, and the second is not hypothetical:

* **Latency.** One un-stubbed escalation call took the suite from ~2.9s to ~4.3s
  in client construction and endpoint resolution alone.
* **Real writes to real tables.** `ios_owner` resolves its table from
  `AVX_ACTIONS_TABLE`, defaulting to `vsa-{VSA_STAGE}-avx-actions`. Review
  Cycle 2 found 16 tests attempting a real `put_item`, and because this shell
  carries `VSA_STAGE=prod` the target was `vsa-prod-avx-actions`. A unit test
  writing to a production-named table is the failure mode, whether or not
  credentials happened to be present at the time.

The first version of this file guarded three factories by name and missed
`ios_owner._make_ddb_resource` — the DynamoDB one, i.e. the only one that
writes. A hand-maintained list of things to guard is a list that goes stale, so
the factories are now DISCOVERED from module source: any function in the package
whose body constructs a boto3 client or resource is patched, and
`test_guard_covers_every_aws_factory` fails if a new one appears unguarded.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from agents.executors import (
    advisory,
    base,
    chat,
    dms,
    escalation,
    ios_owner,
    registry,
    retail,
    vehicle_command,
)

#: Every module in the package that may hold an AWS client factory.
#: Every module in the package. `vehicle_command` was missing until review
#: Cycle 5: it has no boto3 factory today, so the guard failed closed — but by
#: accident rather than by assertion, and a factory added there would have escaped
#: both the autouse fixture and the inventory check. Derived-vs-listed matters
#: less here than completeness, so the list is asserted complete below.
_PACKAGE_MODULES = (
    advisory,
    base,
    chat,
    dms,
    escalation,
    ios_owner,
    registry,
    retail,
    vehicle_command,
)

_EXECUTORS_DIR = Path(base.__file__).resolve().parent


def discover_aws_factories() -> dict[str, list[str]]:
    """Return {module_name: [function names that construct a boto3 client]}.

    Static AST scan of the package source: finds every module-level function or
    method whose body contains `boto3.client(...)` or `boto3.resource(...)`.
    Derived from source rather than enumerated by hand so that adding a factory
    cannot silently escape the guard.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(_EXECUTORS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in ("client", "resource")
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "boto3"
                ):
                    names.append(node.name)
                    break
        if names:
            found[path.stem] = sorted(set(names))
    return found


@pytest.fixture(autouse=True)
def _no_real_aws_clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Patch every discovered AWS factory in the package with a mock."""
    for module in _PACKAGE_MODULES:
        for attr_name in dir(module):
            if attr_name.startswith("__"):
                continue
            attr = getattr(module, attr_name, None)
            if not inspect.isfunction(attr):
                continue
            try:
                src = inspect.getsource(attr)
            except (OSError, TypeError):
                continue
            if "boto3.client" in src or "boto3.resource" in src:
                monkeypatch.setattr(module, attr_name, lambda *a, **k: MagicMock(), raising=False)

    # Class-level factories (methods and properties) are not module-level
    # functions, so the loop above cannot see them. Walk classes too.
    #
    # An earlier version patched `IosOwnerExecutor._get_ddb` with raising=False —
    # an attribute that DOES NOT EXIST (the class exposes a `_ddb` property), so
    # it silently created a phantom nothing reads, and deleting the whole block
    # changed no test outcome. Discovering class members by the same
    # source-inspection rule removes the chance to name the wrong one.
    for module in _PACKAGE_MODULES:
        for cls_name in dir(module):
            cls = getattr(module, cls_name, None)
            if not inspect.isclass(cls) or cls.__module__ != module.__name__:
                continue
            for member_name, member in list(vars(cls).items()):
                fn = member.fget if isinstance(member, property) else member
                if not (inspect.isfunction(fn) or inspect.ismethod(fn)):
                    continue
                try:
                    src = inspect.getsource(fn)
                except (OSError, TypeError):
                    continue
                if "boto3.client" not in src and "boto3.resource" not in src:
                    continue
                if isinstance(member, property):
                    monkeypatch.setattr(
                        cls, member_name, property(lambda self: MagicMock()), raising=True
                    )
                else:
                    monkeypatch.setattr(
                        cls, member_name, lambda self, *a, **k: MagicMock(), raising=True
                    )

    # Process-global caches must not carry a client or a parameter across tests.
    escalation.reset_clients()
    base.reset_policy_cache()
    yield
    escalation.reset_clients()
    base.reset_policy_cache()


def _resolve_class_member(module: Any, fn_name: str) -> Any:
    """Find `fn_name` on any class defined in `module`, unwrapping properties."""
    for cls_name in dir(module):
        cls = getattr(module, cls_name, None)
        if not inspect.isclass(cls) or cls.__module__ != module.__name__:
            continue
        member = vars(cls).get(fn_name)
        if member is None:
            continue
        return member.fget if isinstance(member, property) else member
    return None


