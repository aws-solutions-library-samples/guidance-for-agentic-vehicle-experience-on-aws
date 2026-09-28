"""Generate persona-smoke WAV fixtures via Amazon Polly.

Synthesizes the six audio clips used by the Tier-3 per-persona smoke harness —
two knowledge-phrased prompts for the KB personas (service_advisor, technician)
and four service/account prompts for the liveness personas (fleet_driver,
consumer, retail_advisor, vehicle_owner). Output is WAV files in evals/audio/.

Usage::

    python3 -m evals.scripts.generate_kb_prompt_audio

Cost: ~$0.000016/char × 6 prompts ≈ negligible; idempotent (overwrites).
All prompts confirmed against the live runtime — see the spec decisions.md
"Post-deploy routing map". Spec: 2026-06-19-cvx-tier3-persona-smoke-harness.
"""

from __future__ import annotations

import wave
from pathlib import Path

import boto3

# Output directory (repo-relative)
_AUDIO_DIR = Path(__file__).parent.parent / "audio"

# Polly region — us-east-1; fixture generation is one-time and not
# region-namespace-sensitive (Polly output is local files, not a cloud resource).
_POLLY_REGION = "us-east-1"

# Trailing silence: ≥1.6 s at 16 kHz mono 16-bit (1 frame = 2 bytes).
_SILENCE_FRAMES = int(16000 * 1.6)  # 25600 frames
_SILENCE_BYTES = b"\x00\x00" * _SILENCE_FRAMES

# Fixture definitions: (filename, polly_text)
# All prompts CONFIRMED against the live staging runtime (2026-06-19 probe —
# see the spec decisions.md "Post-deploy routing map"). Single sentence, no
# internal periods (Nova Sonic v2 VAD splits on periods).
#
#   KB personas (knowledge-phrased → elicit the live `retrieve` KB tool):
#     - tsb-catalyst-p0420.wav        service_advisor
#     - persona-warranty-powertrain.wav  technician
#   Non-KB personas (liveness — each elicits an agent tool on the audio turn):
#     - persona-service-history.wav   fleet_driver   → get_service_history
#     - persona-account-details.wav   consumer       → lookup_vehicle_identity
#     - persona-support-cases.wav     retail_advisor → get_service_history
#     - persona-service-records.wav   vehicle_owner  → get_service_history
#
# NOTE: the prior fixtures (check-engine-*.wav from CMS DemoClips,
# dtc-p0420-procedure.wav) were FAULT-phrased and routed to auto-triage, never
# eliciting the persona/KB tools — replaced with these knowledge/service
# prompts (decisions.md). The check-engine DemoClips are no longer used by the
# smoke; stage_persona_audio.py is retained only for ad-hoc DemoClip access.
_FIXTURES = [
    (
        "tsb-catalyst-p0420.wav",
        "What does the technical service bulletin say about catalyst efficiency code P zero four two zero",
    ),
    (
        "persona-warranty-powertrain.wav",
        "Look up the warranty policy for the powertrain",
    ),
    (
        "persona-service-history.wav",
        "Show me my recent service history",
    ),
    (
        "persona-account-details.wav",
        "What are my account details",
    ),
    (
        "persona-support-cases.wav",
        "Show my recent support cases",
    ),
    (
        "persona-service-records.wav",
        "Show me my vehicle service records",
    ),
]


def _synthesize(text: str, out_path: Path) -> None:
    """Synthesize *text* via Polly (neural/PCM) and write a RIFF WAV to *out_path*."""
    polly = boto3.client("polly", region_name=_POLLY_REGION)
    resp = polly.synthesize_speech(
        Text=text,
        VoiceId="Joanna",
        Engine="neural",
        OutputFormat="pcm",   # headerless raw 16-bit signed mono PCM
        SampleRate="16000",   # matches INPUT_SAMPLE_RATE in ws_client.py
    )
    pcm = resp["AudioStream"].read()

    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)    # 16-bit
        w.setframerate(16000)
        w.writeframes(pcm)
        w.writeframes(_SILENCE_BYTES)  # ≥1.6 s trailing silence (Nova VAD)


def main() -> None:
    """Generate all KB-prompt WAV fixtures into evals/audio/."""
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in _FIXTURES:
        out = _AUDIO_DIR / filename
        _synthesize(text, out)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
