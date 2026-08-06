"""Stage non-KB persona audio fixtures from CMS DemoClips.

Copies four WAV files from
~/connected-mobility-guidance-on-aws/clients/ios/VSACompanion/DemoClips/
into evals/audio/ for use by the Tier-3 per-persona smoke harness.

Fixture mapping (docs/tech.md § 11):
    check-engine-fleet.wav   → fleet_driver, vehicle_owner
    check-engine-rental.wav  → consumer, retail_advisor

Files are copied (not symlinked) so that the _validate_audio_path
path-traversal guard (which resolves symlinks) continues to hold —
symlinks that resolve outside evals/audio/ would be rejected at test time.

Usage::

    python3 -m evals.scripts.stage_persona_audio [--source-root PATH]

Spec: 2026-06-19-cvx-tier3-persona-smoke-harness, Group 4.3.
"""

from __future__ import annotations

import argparse
import shutil
import wave
from pathlib import Path

_DEST_DIR = Path(__file__).parent.parent / "audio"
_DEFAULT_SOURCE_ROOT = Path.home() / "connected-mobility-guidance-on-aws" / "clients" / "ios" / "VSACompanion" / "DemoClips"

# Files to copy: (source filename, destination filename)
# Source: ~/connected-mobility-guidance-on-aws/.../DemoClips/
_COPIES = [
    ("check-engine-fleet.wav",  "check-engine-fleet.wav"),   # fleet_driver + vehicle_owner
    ("check-engine-rental.wav", "check-engine-rental.wav"),  # consumer + retail_advisor
]


def _validate_wav(path: Path) -> None:
    """Verify path is a mono 16-bit 16 kHz PCM WAV.  Raises if not."""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
    if ch != 1 or sw != 2 or fr != 16000:
        raise ValueError(
            f"{path}: expected mono/16-bit/16000 Hz PCM, got "
            f"channels={ch} sampwidth={sw} framerate={fr}"
        )


def main(source_root: Path | None = None) -> None:
    """Copy DemoClips fixtures into evals/audio/."""
    src_root = source_root or _DEFAULT_SOURCE_ROOT
    _DEST_DIR.mkdir(parents=True, exist_ok=True)

    for src_name, dst_name in _COPIES:
        src = src_root / src_name
        if not src.is_file():
            raise FileNotFoundError(f"Source not found: {src}")
        _validate_wav(src)
        dst = _DEST_DIR / dst_name
        shutil.copy2(src, dst)
        print(f"copied {src} → {dst}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage CMS DemoClips WAV fixtures into evals/audio/")
    parser.add_argument(
        "--source-root",
        default=None,
        help="Path to DemoClips directory (default: ~/connected-mobility-guidance-on-aws/.../DemoClips)",
    )
    args = parser.parse_args()
    main(Path(args.source_root) if args.source_root else None)
