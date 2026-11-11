#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Set, read back, or try the simulated fleet operator's switch (FI spec D10a, task 11.4).

The parameter ``/cvx/{stage}/fi/sim-operator`` (String, JSON) is the stand-in's only
switch. Without ``--apply`` this is a dry run: it reads the current value, validates the
value it would write with the stand-in's own parser, prints both, and writes nothing (a
botocore guard refuses every non-read call on the SSM client).

Examples::

    # what would be written (four demo fleets on, cap 20, VEH-MRDN-0015 held), and what is there now
    python3 scripts/set-fi-sim-operator.py --stage staging
    # write it, then read it back (needs the user's go: D10h)
    python3 scripts/set-fi-sim-operator.py --stage staging --apply
    # every fleet off (holds kept)
    python3 scripts/set-fi-sim-operator.py --stage staging --off --apply
    # run the deployed Lambda now in dry_run (reads only) or observe (first-seen records only)
    python3 scripts/set-fi-sim-operator.py --stage staging --invoke dry_run
    python3 scripts/set-fi-sim-operator.py --stage staging --invoke observe     # = --observe-now

``approve`` is never invoked by hand: approvals come from the 07:10Z schedule.

Exit codes: 0 done; 1 an AWS call failed; 2 a refused argument, an invalid value, or a
read-back that differs from what was written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.executors.simulated_operator import DEMO_FLEET_IDS  # noqa: E402
from agents.fi_sim_operator.config import ConfigError, parse_config  # noqa: E402

#: D10a's value (user, prompt 287: cap 20; only VEH-MRDN-0015 held, by VIN and by id).
#: The id is the VIN form, ``MRDN0000000000015``: the car's vehicleId after the vehicleId = VIN
#: cohort 3 re-key, and held as its VIN before it (the Lambda resolves a VIN-shaped id with no
#: registry row to itself). ``VEH-MRDN-0015`` is not listed: after the re-key it resolves to
#: nothing and would stop every fleet, and ``--hold-vehicle-id`` only adds to this default.
DEFAULT_CAP = 20
DEFAULT_HOLD_VINS = ("MRDN0000000000015",)
DEFAULT_HOLD_VEHICLE_IDS = ("MRDN0000000000015",)
INVOKE_MODES = ("dry_run", "observe")


def param_name(stage: str) -> str:
    return f"/cvx/{stage}/fi/sim-operator"


def function_name(stage: str, region: str) -> str:
    return f"vsa-{stage}-avx-sim-operator-{region}"


def build_value(*, fleets: list[str], off: bool, cap: int,
                hold_vins: list[str], hold_vehicle_ids: list[str]) -> dict:
    """The parameter value: every named fleet ``enabled`` (or all off with ``off``)."""
    return {
        "fleets": {f: {"enabled": not off} for f in sorted(fleets)},
        "daily_cap": cap,
        "hold_vins": sorted(set(hold_vins)),
        "hold_vehicle_ids": sorted(set(hold_vehicle_ids)),
    }


def _validate(value: dict) -> Optional[str]:
    """``None`` if the stand-in would run with ``value``, else why not."""
    unknown = sorted(set(value["fleets"]) - set(DEMO_FLEET_IDS))
    if unknown:
        return f"not demo fleets: {unknown}"
    if not 0 <= value["daily_cap"] <= 20:
        return "daily_cap must be 0-20"
    parsed = parse_config(value)
    if isinstance(parsed, ConfigError):
        return f"the stand-in's parser refuses it: {parsed.reason}"
    return None


def _read(ssm: Any, name: str) -> Optional[str]:
    from botocore.exceptions import ClientError
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


def _session(region: str, *, read_only: bool) -> Any:
    import boto3
    import readonly_guard
    session = boto3.Session(region_name=region)
    if read_only:
        readonly_guard.install(session)
    return session


def main(argv: Optional[list[str]] = None, *, session_factory=_session) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--stage", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--fleets", default=",".join(sorted(DEMO_FLEET_IDS)),
                    help="comma-separated demo fleet ids (default: all four)")
    ap.add_argument("--off", action="store_true", help="write every named fleet as enabled: false")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--hold-vin", action="append", default=list(DEFAULT_HOLD_VINS))
    ap.add_argument("--hold-vehicle-id", action="append", default=list(DEFAULT_HOLD_VEHICLE_IDS))
    ap.add_argument("--apply", action="store_true", help="write the parameter, then read it back")
    ap.add_argument("--invoke", choices=INVOKE_MODES,
                    help="invoke the deployed Lambda now in this mode (never approve)")
    ap.add_argument("--observe-now", action="store_true", help="same as --invoke observe")
    args = ap.parse_args(argv)
    if args.observe_now:
        if args.invoke and args.invoke != "observe":
            print("refused: --observe-now and --invoke differ", file=sys.stderr)
            return 2
        args.invoke = "observe"
    if args.apply and args.invoke:
        print("refused: --apply and --invoke are separate steps (write, read back, then invoke)",
              file=sys.stderr)
        return 2

    value = build_value(fleets=[f for f in args.fleets.split(",") if f], off=args.off,
                        cap=args.cap, hold_vins=args.hold_vin, hold_vehicle_ids=args.hold_vehicle_id)
    problem = _validate(value)
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    name = param_name(args.stage)

    try:
        ssm = session_factory(args.region, read_only=not args.apply).client("ssm")
        current = _read(ssm, name)
        print(f"{name} now:   {current if current is not None else '(absent: the stand-in is off)'}")
        print(f"{name} value: {text}")
        if args.apply:
            ssm.put_parameter(Name=name, Value=text, Type="String", Overwrite=True)
            back = _read(ssm, name)
            if back is None or json.loads(back) != value:
                print(f"read-back differs: {back}", file=sys.stderr)
                return 2
            print("written and read back")
        elif current is not None and json.loads(current) == value:
            print("dry run: already this value")
        else:
            print("dry run: nothing written (add --apply)")

        if args.invoke:
            lam = session_factory(args.region, read_only=False).client("lambda")
            resp = lam.invoke(FunctionName=function_name(args.stage, args.region),
                              InvocationType="RequestResponse",
                              Payload=json.dumps({"mode": args.invoke}).encode())
            body = resp["Payload"].read().decode()
            print(f"invoke {args.invoke}: status {resp.get('StatusCode')} "
                  f"{'FunctionError ' + resp['FunctionError'] if resp.get('FunctionError') else ''}")
            print(body)
            if resp.get("FunctionError"):
                return 1
    except Exception as exc:  # noqa: BLE001
        print(f"AWS call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
