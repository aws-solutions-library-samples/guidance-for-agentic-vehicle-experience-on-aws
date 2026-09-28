"""Cross-repo regression guard: no seeded vehicle may carry a real-automaker VIN.

WHY THIS EXISTS
===============

`cms/issues/2026-09-20-unrebranded-vins-on-meridian-vehicles/` found two vehicles
whose every *attribute* said Meridian — `make`, `model`, `fleetId`,
`modelManifestName`, `licensePlate` — while their **VIN** named a different, real
automaker:

    VEH-FORD-001   1FTFW1ED5MFB12345   1FT = Ford Motor Company (truck)
    VEH-VO-001     1G1FY6S07N4100001   1G1 = General Motors

That issue fixed the live DynamoDB values and its own Prevention section says, in
full:

    Gap, stated plainly: this was a one-time data correction and nothing stops it
    recurring. There is no guard asserting that a `producer == meridian` row
    carries a Meridian-consistent VIN. The cheap version is a seed-time or
    test-time assertion that no vehicle's VIN begins with a known real-automaker
    WMI — mechanical, needs no live AWS, and would have caught all three rows the
    day the rebrand landed. Recommended rather than built here.

**This is that guard.** It is written because the gap was not theoretical: the
`VEH-VO-001` correction was silently REVERTED. `cvx/scripts/seed-persona-users.py`
writes directly into `cms-{stage}-storage-vehicles`, still carried the GM literal,
and its 2026-09-22T13:25:33Z run put `1G1FY6S07N4100001` back — so an issue marked
RESOLVED was untrue within two days and nothing noticed. A second instance
(`VEH-MICH-001` in `cms/deployment/scripts/seed_driver_users.py`) was still
carrying a Ford VIN when this guard was written, dormant only because that entry is
create-only.

WHY A SEED-SCRIPT GUARD AND NOT A DATA CHECK
============================================

The 2026-09-20 fix was two `UpdateItem` calls. That is the right *remediation* and
the wrong *prevention*: the seed scripts are the source of truth, they are
re-runnable, and at least one of them overwrites. A guard on live data would also
need credentials and a deployed stack; this one is mechanical and runs in any
checkout, which is what makes it cheap enough to keep.

The repo's existing content guards could not catch this either. The 2026-09-20
report notes `contentBoundary.test.ts` fired correctly on the brand-named
*identifier* twice — but a VIN is an opaque 17-character string, so no brand-string
scan sees `1G1FY6S07N4100001` as General Motors. Only a WMI table does.

WHAT IT DOES NOT COVER
======================

Only VINs written as literals in seed scripts. A VIN that reaches DynamoDB by
another route is invisible here, and closing that needs a live-data check this
deliberately is not.

CORRECTION (2026-09-23, same day): an earlier version of this paragraph claimed
`VEH-ENT-001`'s `4T1B11HK0LU98765` was missed because it had "no seed-script literal
to match". **That was wrong.** The literal was in `seed-persona-users.py` all along;
this guard missed it because the regex required exactly 17 characters and that VIN is
16. The scanner reported clean for a reason that had nothing to do with its stated
scope — which is worth more than the finding itself, because a guard whose stated
coverage does not match its real coverage is worse than no guard. The regex now spans
11-21 characters and length is checked separately.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CVX_REPO = Path(__file__).resolve().parents[2]


def _discover_cms_repo() -> Path | None:
    """Locate the sibling CMS repo, mirroring test_seed_scripts_ev_field_coherence."""
    import os

    env = os.environ.get("CMS_REPO_PATH")
    if env and (Path(env) / "deployment").is_dir():
        return Path(env)
    sibling = CVX_REPO.parent / "connected-mobility-guidance-on-aws"
    return sibling if (sibling / "deployment").is_dir() else None


CMS_REPO = _discover_cms_repo()

#: World Manufacturer Identifier prefixes for real automakers. A VIN's first three
#: characters. Covers the makes the 2026-09-20 audit used (Ford, GM, Tesla, BMW,
#: Mercedes, Honda, Toyota) plus the two that actually appeared in this portfolio's
#: seeds. Not exhaustive by design — an exhaustive WMI registry is thousands of
#: entries and most will never appear here. Add on encounter.
REAL_AUTOMAKER_WMIS: dict[str, str] = {
    # Ford
    "1FA": "Ford", "1FB": "Ford", "1FC": "Ford", "1FD": "Ford",
    "1FM": "Ford", "1FT": "Ford", "2FA": "Ford", "2FM": "Ford",
    "3FA": "Ford", "MAJ": "Ford",
    # General Motors (Chevrolet / GMC / Cadillac / Buick)
    "1G1": "General Motors", "1G2": "General Motors", "1G3": "General Motors",
    "1G4": "General Motors", "1G6": "General Motors", "1GC": "General Motors",
    "1GT": "General Motors", "2G1": "General Motors", "3G1": "General Motors",
    "KL8": "General Motors",
    # Tesla
    "5YJ": "Tesla", "7SA": "Tesla", "LRW": "Tesla",
    # BMW
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW", "4US": "BMW", "5UX": "BMW",
    # Mercedes-Benz
    "WDB": "Mercedes-Benz", "WDC": "Mercedes-Benz", "WDD": "Mercedes-Benz",
    "W1K": "Mercedes-Benz", "4JG": "Mercedes-Benz",
    # Honda
    "1HG": "Honda", "2HG": "Honda", "JHM": "Honda", "5FN": "Honda", "SHH": "Honda",
    # Toyota / Lexus
    "4T1": "Toyota", "5TD": "Toyota", "JTD": "Toyota", "JTE": "Toyota",
    "2T1": "Toyota", "JTH": "Toyota",
    # Volkswagen / Audi
    "3VW": "Volkswagen", "WVW": "Volkswagen", "WAU": "Audi",
    # Nissan
    "1N4": "Nissan", "JN1": "Nissan", "JN8": "Nissan",
    # Stellantis (Chrysler / Jeep / Dodge / Ram)
    "1C3": "Stellantis", "1C4": "Stellantis", "1C6": "Stellantis",
    "2C3": "Stellantis", "3C4": "Stellantis",
    # Hyundai / Kia
    "KMH": "Hyundai", "5NP": "Hyundai", "KNA": "Kia", "KND": "Kia",
    # Volvo / Subaru
    "YV1": "Volvo", "YV4": "Volvo", "JF1": "Subaru", "4S3": "Subaru", "4S4": "Subaru",
}

#: A quoted VIN-ish value assigned to a ``vin`` key.
#:
#: The class is ``[A-Z0-9]`` — every alphanumeric — NOT the ISO 3779 alphabet.
#: ISO 3779 excludes I, O and Q (they are confusable with 1 and 0), and restricting
#: the class to it was the guard's SECOND blind spot, found the same day as the
#: first: this fleet's own synthetic ``DEMO…`` prefix contains an **O**, so every
#: ``DEMO``-prefixed VIN — 7+ of them in live data, and the replacement this guard's
#: own fix introduced — was silently unmatched. The guard reported "clean" while
#: skipping them.
#:
#: The lesson generalises past this file: a validator narrowed to what *real* data
#: looks like cannot see *synthetic* data, and synthetic data is all this repo has.
#: Matching broadly and classifying afterwards is the safer order.
#:
#: Length is 11-21, NOT exactly 17, and that width is load-bearing. The first
#: revision of this guard required exactly 17 and therefore missed
#: ``4T1B11HK0LU98765`` — a Toyota WMI sitting in ``seed-persona-users.py`` — because
#: that literal is **16** characters. A malformed VIN is exactly where a real WMI
#: hides: it is wrong in a way that looks like a typo, so it survives review, and a
#: strict-length scanner reports the file clean. The guard that exists to find real
#: automaker prefixes must not be defeated by a miscounted digit.
_VIN_ASSIGN = re.compile(r"""["']vin["']\s*:\s*["']([A-Z0-9]{11,21})["']""")

#: ISO 3779 VIN length. Enforced as its own finding rather than folded into the WMI
#: check — a wrong-length VIN and a real-automaker VIN are different defects and a
#: reader needs to know which one they have.
_VIN_LENGTH = 17


def _find_seed_scripts(root: Path | None) -> list[Path]:
    """Every ``seed*.py`` in a repo, excluding build and dependency trees.

    Build outputs are excluded because CDK asset staging copies the seed scripts
    verbatim; including them would report every finding several times over and
    make the count meaningless.
    """
    if root is None:
        return []
    skip = ("cdk.out", ".build", ".venv", "node_modules", "site-packages", "__pycache__")
    return [
        p for p in root.rglob("seed*.py")
        if not any(part in skip for part in p.parts)
    ]


def _vin_literals(path: Path) -> list[tuple[int, str]]:
    """(line number, VIN) for every ``"vin": "<17 chars>"`` assignment in *path*."""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in _VIN_ASSIGN.finditer(line):
            out.append((lineno, m.group(1)))
    return out


def _all_vins() -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in _find_seed_scripts(CVX_REPO) + _find_seed_scripts(CMS_REPO):
        for lineno, vin in _vin_literals(path):
            found.append((path, lineno, vin))
    return found


def test_sibling_cms_repo_discovered_or_explicitly_absent():
    """Make the scan's coverage visible rather than silently one-repo.

    Both instances this guard exists for lived in DIFFERENT repos — the GM VIN in
    CVX's persona seeder, the Ford VIN in CMS's driver seeder. A one-repo run would
    have caught one and reported clean.
    """
    if CMS_REPO is None:
        pytest.skip(
            "sibling CMS repo not found; set CMS_REPO_PATH to include it. "
            "CVX-side scanning still ran in the other tests."
        )
    assert (CMS_REPO / "deployment").is_dir(), (
        f"CMS_REPO_PATH={CMS_REPO} does not look like the CMS repo."
    )


def test_seed_scripts_are_discovered():
    """A guard that scans nothing passes for the wrong reason."""
    found = _find_seed_scripts(CVX_REPO) + _find_seed_scripts(CMS_REPO)
    assert found, "no seed*.py found in either repo — the scan globbed nothing."


def test_vin_literals_are_found():
    """Anti-vacuity: the regex must actually match real seeded VINs.

    Without this, a typo in `_VIN_ASSIGN` — or a formatting change that puts the
    key and value on separate lines — makes the guard below pass while inspecting
    an empty list. That is the failure mode this portfolio has shipped repeatedly:
    a green assertion over nothing.
    """
    vins = _all_vins()
    assert vins, (
        "no `\"vin\": \"<17 chars>\"` literal matched in any seed script. Either the "
        "seeds stopped carrying VIN literals, or _VIN_ASSIGN no longer matches how "
        "they are written — check before trusting a green run."
    )


def test_wmi_table_recognises_a_known_real_vin():
    """Positive control on the WMI table itself.

    The two VINs this guard was built for must still be classified as
    real-automaker VINs. If a refactor empties or re-keys REAL_AUTOMAKER_WMIS, the
    scan below would report clean for the exact reason it was written.
    """
    for vin, expected in (("1G1FY6S07N4100001", "General Motors"),
                          ("1FT8W3DT5MEC55401", "Ford"),
                          ("1FTFW1ED5MFB12345", "Ford")):
        assert REAL_AUTOMAKER_WMIS.get(vin[:3]) == expected, (
            f"WMI table no longer classifies {vin} ({vin[:3]}) as {expected}; "
            "this guard cannot detect the defect it exists for."
        )
    # And a synthetic VIN must NOT be flagged, or the guard is unusable.
    assert REAL_AUTOMAKER_WMIS.get("MRD") is None, (
        "the synthetic Meridian prefix MRD is in the real-automaker table; every "
        "correctly-branded row would fail."
    )


def test_regex_catches_a_malformed_length_vin():
    """The widened regex must match a VIN that is not 17 characters.

    Positive control for the exact hole this guard shipped with: a 16-character
    Toyota VIN in a seed script passed the strict-17 version. Without this test a
    future tightening back to ``{17}`` would silently restore the blind spot.
    """
    sample = '    "vin": "4T1B11HK0LU98765",'          # 16 chars, real Toyota WMI
    m = _VIN_ASSIGN.search(sample)
    assert m, (
        "the VIN regex no longer matches a 16-character value; a malformed VIN "
        "carrying a real-automaker WMI would pass this guard unseen, which is the "
        "defect it shipped with on 2026-09-23."
    )
    assert REAL_AUTOMAKER_WMIS.get(m.group(1)[:3]) == "Toyota"


def test_regex_catches_a_non_iso_alphabet_vin():
    """The class must match this fleet's synthetic ``DEMO…`` prefix.

    Positive control for the guard's second blind spot. ISO 3779 excludes I/O/Q, so
    a class built from the ISO alphabet cannot match ``DEMO0000000000013`` — and this
    repo's synthetic VINs are not ISO-valid by construction. Narrowing the class back
    would silently skip every DEMO-prefixed vehicle again.
    """
    sample = '    "vin": "DEMO0000000000013",'
    m = _VIN_ASSIGN.search(sample)
    assert m, (
        "the VIN regex no longer matches a DEMO-prefixed value; every DEMO vehicle "
        "would be skipped unseen, which is the second defect this guard shipped "
        "with on 2026-09-23."
    )
    assert m.group(1) == "DEMO0000000000013"


def test_no_seeded_vin_is_a_wrong_length():
    """ISO 3779 VINs are 17 characters. A wrong length is its own defect.

    Reported separately from the WMI check so a reader knows which problem they
    have. This is also the signal that would have surfaced the Toyota VIN even if
    its WMI had not been in the table.
    """
    bad: list[str] = []
    for path, lineno, vin in _all_vins():
        if len(vin) != _VIN_LENGTH:
            rel = path.relative_to(path.parents[len(path.parents) - 1])
            bad.append(f"{rel}:{lineno}: vin {vin!r} is {len(vin)} chars, expected {_VIN_LENGTH}")

    assert not bad, (
        f"{len(bad)} seeded VIN(s) are not {_VIN_LENGTH} characters:\n\n"
        + "\n".join(f"  - {b}" for b in bad)
        + "\n\nA wrong-length VIN is where a real-automaker prefix hides: it reads "
          "as a typo, so it survives review, and a strict-length scanner skips it."
    )


def test_no_seeded_vin_carries_a_real_automaker_wmi():
    """THE guard. No seed script may assign a VIN owned by a real automaker."""
    violations: list[str] = []
    for path, lineno, vin in _all_vins():
        maker = REAL_AUTOMAKER_WMIS.get(vin[:3])
        if maker:
            rel = path.relative_to(path.parents[len(path.parents) - 1])
            violations.append(
                f"{rel}:{lineno}: vin {vin!r} — WMI {vin[:3]!r} is {maker}"
            )

    assert not violations, (
        f"{len(violations)} seeded VIN(s) carry a real automaker's WMI:\n\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nA VIN is the identifier an operator trusts and the one that appears "
          "in support conversations, so a real-automaker VIN on a synthetic-brand "
          "vehicle is a brand-attribution defect, not cosmetics. Replace it with a "
          "VIN in the project's own synthetic scheme. See "
          "cms/issues/2026-09-20-unrebranded-vins-on-meridian-vehicles/."
    )
