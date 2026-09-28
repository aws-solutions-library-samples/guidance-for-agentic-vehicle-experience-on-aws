"""Regenerate demo voice clips using Amazon Polly neural voices.

Replaces the demo WAVs under
clients/ios/VSACompanion/DemoClips/ with Polly neural-voice output that
sounds like a real human. The iOS DemoAudioInjector expects 16 kHz mono
Int16 LE PCM wrapped in a standard WAV — that's exactly what this
script produces.

Per-persona voices: each demo persona gets a different Polly voice so
Fleet/OEM/Rental are visually AND audibly distinct. File naming
convention: `<phrase>-<persona>.wav`. iOS DemoClip resolves the
filename at runtime based on AppSession.layoutSegment.

  fleet  → Joanna  (US English, female)         — Samantha Carter
  oem    → Matthew (US English, male)           — Marcus Reyes
  rental → Kajal   (Indian English, female)     — Priya Shah

Usage:
    AWS_REGION=us-east-1 python scripts/generate-demo-clips.py
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
SAMPLE_RATE = 16_000
CHANNELS = 1
BITS_PER_SAMPLE = 16

# Output directory — the iOS Xcode project references these by exact
# basename, so the filenames here must match DemoClip enum cases.
OUT_DIR = (
    Path(__file__).parent.parent
    / ".."
    / "connected-mobility-guidance-on-aws"
    / "clients"
    / "ios"
    / "VSACompanion"
    / "DemoClips"
).resolve()


# Persona → Polly voice mapping. All three are neural-engine voices
# so they sound natural; mixing US-female / US-male / Indian-female
# gives clear audible differentiation across personas during a
# screen-shared demo. To swap a persona's voice, edit here and
# re-run the script.
PERSONA_VOICES = {
    "fleet":  "Joanna",
    "oem":    "Matthew",
    "rental": "Kajal",
}


# (suffix, text) pairs. Suffix is appended after the persona-key in
# the WAV filename, e.g. "tire-pressure-low-fleet.wav". Add a new
# line here, regenerate, also add a case in DemoClip.swift and
# register the new WAVs in Xcode.
#
# Single-sentence phrases are deliberate. Two design constraints:
#
#   1. Nova Sonic v2 VAD splits audio at sentence-ending periods —
#      a pause after "My tire pressure is low." plus the period
#      itself was enough for Nova to commit one user turn there
#      and treat "Can you help?" as a separate turn. That
#      fragments the user content into two PARTIAL_TURN streams
#      and makes the post-tool TTS narration even more
#      probabilistic. (Observed agent log session
#      vsa-ios-72DB4DF5-93B 2026-05-22 15:07.)
#   2. Phrases need to be 1.5s+ with natural prosody so the VAD
#      reliably commits end-of-utterance. Very short audio
#      (e.g. 600ms "Yes.") was the original problem — Nova STT
#      didn't always commit at all.
#
# Net: 1.5s+ but single-sentence, with commas / "and" instead of
# mid-phrase periods.
CLIPS = [
    ("tire-pressure-low",   "My tire pressure is low and I need help with that."),
    ("yes",                 "Yes, please go ahead and book that appointment for me."),
    ("yes-tuesday",         "Yes, Tuesday at one PM works great for me, please book it."),
    ("check-engine",        "Why is my check engine light on right now?"),
    ("speak-with-agent",    "I'd like to speak with a human agent please."),
    ("find-service-center", "Could you find a nearby service center for me?"),
    ("inflate-tire-myself", "Hey, can I just inflate the tire myself instead?"),
]


def _wav_header(pcm_bytes: int) -> bytes:
    """Build a 44-byte canonical WAV header for 16 kHz mono Int16 LE.

    The header layout is the standard PCM WAV: RIFF/WAVE container,
    `fmt ` chunk (PCM, channel count, sample rate, byte rate, etc.),
    then `data` chunk with the size of the audio payload. No JUNK or
    FLLR padding chunks — the iOS DemoAudioInjector tolerates them
    anyway, but a clean header is faster and easier to inspect.
    """
    byte_rate = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
    block_align = CHANNELS * BITS_PER_SAMPLE // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + pcm_bytes)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)               # fmt chunk size
        + struct.pack("<H", 1)                # PCM format
        + struct.pack("<H", CHANNELS)
        + struct.pack("<I", SAMPLE_RATE)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", BITS_PER_SAMPLE)
        + b"data"
        + struct.pack("<I", pcm_bytes)
    )


def _synthesize(polly, voice: str, text: str) -> bytes:
    """Call Polly synthesize_speech, return raw PCM bytes."""
    resp = polly.synthesize_speech(
        OutputFormat="pcm",
        SampleRate=str(SAMPLE_RATE),
        VoiceId=voice,
        Engine="neural",
        Text=text,
        TextType="text",
    )
    audio_stream = resp.get("AudioStream")
    if audio_stream is None:
        raise RuntimeError(f"Polly returned no AudioStream for {voice} / {text!r}")
    pcm = audio_stream.read()
    audio_stream.close()
    return pcm


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--persona",
        choices=["fleet", "oem", "rental", "all"],
        default="all",
        help="Generate clips for one persona, or all three (default).",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    polly = boto3.client("polly", region_name=REGION)
    personas = (
        list(PERSONA_VOICES.keys())
        if args.persona == "all"
        else [args.persona]
    )
    for persona in personas:
        voice = PERSONA_VOICES[persona]
        print(f"Persona: {persona:6s}  voice: {voice} (neural)")
        for suffix, text in CLIPS:
            pcm = _synthesize(polly, voice, text)
            wav = _wav_header(len(pcm)) + pcm
            filename = f"{suffix}-{persona}.wav"
            out = OUT_DIR / filename
            out.write_bytes(wav)
            print(f"  {filename:38s}  {len(wav):7d} bytes  {text!r}")
    # Clean up the legacy single-voice WAVs from before persona-suffixing.
    # If they still exist, the bundle would carry duplicates and the
    # iOS resolver would silently use whichever Bundle.main returned
    # first. Removing them here keeps the project file's resource
    # list authoritative.
    for suffix, _ in CLIPS:
        legacy = OUT_DIR / f"{suffix}.wav"
        if legacy.exists():
            legacy.unlink()
            print(f"  removed legacy {legacy.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
