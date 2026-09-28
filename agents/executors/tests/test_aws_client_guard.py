"""The AWS-factory guard, in a module pytest actually collects.

Review Cycle 4: this test lived in `conftest.py`, and **pytest does not collect
tests from conftest.py** — `--collect-only | grep` found 0 occurrences. So the
anti-staleness promise in its own docstring was inert for a whole cycle: the
guard it protects could have gone stale and nothing would have said so.

That is the second time a guard in this package was written and never executed
(the first was the phantom `IosOwnerExecutor._get_ddb` patch, which `raising=False`
made a no-op). Both had the same tell: nothing ever failed because of them.
`test_this_module_is_collected` below is the cheap protection against a third.

The fixture and the discovery helpers stay in `conftest.py`, which is where an
autouse fixture belongs; only the assertions moved.
"""

from __future__ import annotations

import inspect

from agents.executors.tests.conftest import (
    _PACKAGE_MODULES,
    _resolve_class_member,
    discover_aws_factories,
)


def test_this_module_is_collected() -> None:
    """Trivially true when run, and the point is that it IS run.

    A guard whose own absence is undetectable is not a guard. If these assertions
    ever stop being collected again, this is the test whose disappearance from the
    count is the signal.
    """
    assert True


def test_guard_covers_every_aws_factory() -> None:
    """Every boto3 factory in the package source must be neutralised in tests.

    This is the anti-staleness check. The previous hand-written guard listed
    three factories and missed the DynamoDB one, which is the only factory that
    WRITES — so the gap was invisible and the blast radius was the largest.
    Discovering the inventory from source and asserting coverage against it means
    a newly added factory fails this test instead of quietly reaching AWS.
    """
    inventory = discover_aws_factories()

    # Sanity floor: if the scan finds nothing, it is broken, not the package clean.
    assert inventory, (
        "the AST scan found no boto3 factories in agents/executors/ — the scanner "
        "is broken, since base/dms/escalation/ios_owner all construct clients"
    )
    # An explicit expected inventory, not a `>= N` floor.
    #
    # A floor only catches REMOVALS below the threshold, and it silently couples
    # unrelated work to itself: review Cycle 4 noted that deleting dms's two dead
    # escalation factories (correct cleanup, after escalation moved to the
    # registry) drops the count from 6 to 4 and breaks a `>= 5` floor, so the
    # cleanup and the floor had to move together or the cleanup looked like a
    # regression.
    #
    # Pinning the exact set makes both directions deliberate: adding a factory
    # fails here until it is acknowledged, and removing one fails until the
    # expectation is updated in the same edit. That is the difference between an
    # inventory and a threshold.
    expected_inventory = {
        "base": ["_get_ssm"],
        "escalation": ["_get_cloudwatch", "_get_events"],
        "ios_owner": ["_make_ddb_resource"],
    }
    assert inventory == expected_inventory, (
        "the set of boto3 factories in agents/executors/ has changed.\n"
        f"  found:    {inventory}\n"
        f"  expected: {expected_inventory}\n"
        "If a factory was ADDED, confirm the autouse fixture in conftest.py "
        "neutralises it and then add it here. If one was REMOVED, update this "
        "expectation in the same commit. Do not relax this into a count: "
        "ios_owner._make_ddb_resource is the only factory that WRITES, and a "
        "count cannot tell you which one went missing."
    )

    unguarded: list[str] = []
    for mod_name, fn_names in inventory.items():
        module = next((m for m in _PACKAGE_MODULES if m.__name__.endswith(mod_name)), None)
        if module is None:
            continue
        for fn_name in fn_names:
            live = getattr(module, fn_name, None)
            if live is None:
                # A class member, not a module-level function. Resolve it through
                # the classes in this module rather than skipping it: skipping is
                # what made this assertion unable to cover the only factory that
                # WRITES (ios_owner's DynamoDB resource, reached via a property).
                live = _resolve_class_member(module, fn_name)
            if live is None:
                unguarded.append(f"{mod_name}.{fn_name} (unresolvable — cannot verify)")
                continue
            try:
                src = inspect.getsource(live)
            except (OSError, TypeError):
                continue  # patched
            if "boto3.client" in src or "boto3.resource" in src:
                unguarded.append(f"{mod_name}.{fn_name}")

    assert not unguarded, (
        f"AWS client factories reachable un-stubbed during tests: {unguarded}. "
        "Add them to the autouse fixture in conftest.py. An un-stubbed factory "
        "is not merely slow: ios_owner writes to vsa-{VSA_STAGE}-avx-actions, so "
        "with VSA_STAGE=prod a unit test targets a production-named table."
    )



def test_package_modules_list_is_complete() -> None:
    """`_PACKAGE_MODULES` must name every module in the package.

    `vehicle_command` was absent until review Cycle 5. It has no boto3 factory
    today, so the guard failed closed — but by accident, not by assertion: a
    factory added to that module would have escaped both the autouse fixture and
    the inventory check above, and nothing would have said so.

    Derived from the directory listing rather than compared against a second hand-
    written list, so adding a module fails here until it is acknowledged.
    """
    from agents.executors.tests.conftest import _EXECUTORS_DIR, _PACKAGE_MODULES

    on_disk = {
        p.stem
        for p in _EXECUTORS_DIR.glob("*.py")
        if p.stem != "__init__"
    }
    listed = {m.__name__.rsplit(".", 1)[-1] for m in _PACKAGE_MODULES}

    missing = on_disk - listed
    assert not missing, (
        f"module(s) {sorted(missing)} exist in agents/executors/ but are absent "
        "from _PACKAGE_MODULES in conftest.py, so the AWS-client guard does not "
        "inspect them. A boto3 factory added there would reach real AWS from a "
        "unit test — and ios_owner's writes to vsa-{VSA_STAGE}-avx-actions are why "
        "that matters."
    )
    stale = listed - on_disk
    assert not stale, f"_PACKAGE_MODULES names non-existent module(s): {sorted(stale)}"
