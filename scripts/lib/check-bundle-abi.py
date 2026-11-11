#!/usr/bin/env python3
"""Fail if a Lambda bundle contains a compiled extension the Lambda cannot load.

Every CVX Python Lambda runs CPython 3.12 on Linux x86_64 (lambda.Runtime.PYTHON_3_12,
default architecture). pip picks wheels for the interpreter that runs it unless told
otherwise, and this repo's machines run Python 3.14 on macOS. On 2026-09-17 that put
`_cffi_backend.cpython-314-x86_64-linux-gnu.so` into the staging WebSocket authorizer,
whose token verification then could not load its crypto backend
(issues/2026-10-01-ws-authorizer-bundle-built-for-python-3-14/).

Checks every `*.so` under the bundle:
  * a `cpython-XYZ` tag in the file name must be the target version (default 312);
  * the file must be an ELF shared object for x86-64 (catches macOS Mach-O builds and
    arm64 builds, whose names may carry no version tag, e.g. `_rust.abi3.so`).

Usage:
    python3 scripts/lib/check-bundle-abi.py <bundle-dir> [--python 3.12]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_CPYTHON_TAG = re.compile(r"\.cpython-(\d+)")
_ELF_MAGIC = b"\x7fELF"
_EM_X86_64 = 0x3E


def _elf_machine(path: Path) -> int | None:
    """Return the ELF e_machine value, or None if the file is not ELF."""
    with path.open("rb") as fh:
        header = fh.read(20)
    if len(header) < 20 or header[:4] != _ELF_MAGIC:
        return None
    byteorder = "little" if header[5] == 1 else "big"
    return int.from_bytes(header[18:20], byteorder)


def problems(bundle: Path, python: str = "3.12") -> list[str]:
    """Every reason a compiled file in `bundle` would not load on the target."""
    want = python.replace(".", "")
    found: list[str] = []
    for so in sorted(bundle.rglob("*.so")):
        rel = so.relative_to(bundle)
        m = _CPYTHON_TAG.search(so.name)
        if m and m.group(1) != want:
            found.append(f"{rel}: built for CPython {m.group(1)}, Lambda runs {want}")
        machine = _elf_machine(so)
        if machine is None:
            found.append(f"{rel}: not a Linux (ELF) shared object")
        elif machine != _EM_X86_64:
            found.append(f"{rel}: ELF machine 0x{machine:x}, Lambda runs x86-64")
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--python", default="3.12")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        print(f"ERROR: {args.bundle} is not a directory", file=sys.stderr)
        return 2
    found = problems(args.bundle, args.python)
    if found:
        print(f"ERROR: {args.bundle} has {len(found)} extension(s) the Lambda cannot load:",
              file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    count = sum(1 for _ in args.bundle.rglob("*.so"))
    print(f"OK: {count} compiled extension(s) in {args.bundle.name}, all CPython "
          f"{args.python} / Linux x86-64")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
