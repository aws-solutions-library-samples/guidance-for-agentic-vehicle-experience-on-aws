#!/usr/bin/env python3
"""Compare the triage classifier's grades with the live CMS event catalog.

Read-only. For every catalog entry that carries a `dtc_code`, the CMS grade is the more severe
of its `severity_hint` (P0-P3, any case) and its severity (4 or CRITICAL -> P0,
3 or HIGH -> P1, 2 or MEDIUM -> P2, 1 or LOW -> P3). The script classifies each code on its own through the
real classifier (`lambdas/triage-classifier`, reading `lambdas/shared/dtc_severity.yaml`)
and reports every code the classifier grades LESS severely than CMS. More severe is
allowed.

Why: the classifier gives any code missing from its table the default P2 ("safe to
drive"). The fire and crash codes were missing for months while CMS graded them P0
(issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/). The offline check
`scripts/check-dtc-severity-parity.py` pins a snapshot of the catalog; this script checks
the live one.

Usage:
    .venv/bin/python3 scripts/check-dtc-severity-drift.py [--stage staging] [--region us-west-2]

Exit codes: 0 no drift · 1 drift found, a coded entry that could not be graded, or
nothing compared.
Never prints anything but event ids, codes and grades.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
BY_SEVERITY = {
    "4": "P0", "3": "P1", "2": "P2", "1": "P3",
    "CRITICAL": "P0", "HIGH": "P1", "MEDIUM": "P2", "LOW": "P3",
}


def _parity():
    """The parity check's loader and `level_for`, so both scripts classify the same way."""
    spec = importlib.util.spec_from_file_location("check_dtc_severity_parity", ROOT / "scripts" / "check-dtc-severity-parity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cms_grade(item: dict) -> str | None:
    """The more severe of the entry's hint and severity.

    None when neither is present, or when either is present but unreadable: a field the
    script cannot read might be the more severe one, so it is not ignored.
    """
    grades = []
    hint = item.get("severity_hint")
    if hint is not None:
        if not (isinstance(hint, str) and hint.strip().upper() in ORDER):
            return None
        grades.append(hint.strip().upper())
    sev = item.get("severity")
    if sev is not None:
        key = str(sev).strip().upper()
        if key.endswith(".0"):  # DynamoDB numbers can come back as Decimal("4.0")
            key = key[:-2]
        if key not in BY_SEVERITY:
            return None
        grades.append(BY_SEVERITY[key])
    return min(grades, key=ORDER.__getitem__) if grades else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()

    table = f"cms-{args.stage}-event-catalog"
    ddb = boto3.resource("dynamodb", region_name=args.region).Table(table)
    items: list[dict] = []
    kwargs: dict = {}
    while True:  # follow LastEvaluatedKey to the end
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    coded = [i for i in items if isinstance(i.get("dtc_code"), str) and i["dtc_code"]]
    if not coded:
        print(f"FAIL: {table} returned {len(items)} items and none carries a dtc_code")
        return 1

    parity = _parity()
    drift, ungradable, compared = [], [], 0
    for item in sorted(coded, key=lambda i: (i["dtc_code"], str(i.get("event_id", "")))):
        grade = cms_grade(item)
        if grade is None:
            ungradable.append((item["dtc_code"], item.get("event_id")))
            continue
        compared += 1
        got = parity.level_for(item["dtc_code"])
        if ORDER[got] > ORDER[grade]:
            drift.append((item["dtc_code"], item.get("event_id"), grade, got))

    print(f"{table}: {compared} of {len(coded)} coded entries compared")
    for code, event_id, grade, got in drift:
        print(f"  DRIFT {code!r:14} {event_id!r}: CMS {grade}, classifier {got}")
    for code, event_id in ungradable:
        print(f"  UNGRADABLE {code!r:14} {event_id!r}: no usable severity or hint")
    # With coded entries present, "none compared" means every one was ungradable, so the
    # ungradable check covers it; the empty case returned above.
    if drift or ungradable:
        print(f"FAIL: {len(drift)} drift, {len(ungradable)} ungradable, {compared} compared; "
              "see lambdas/shared/dtc_severity.yaml")
        return 1
    print("OK: no code graded less severely than CMS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
