"""Master the narration: EQ, compression, de-ess, loudness.

Normalisation runs across the whole episode, never per scene. loudnorm
computes gain from the entire file, so per-scene normalisation gives each
scene a slightly different correction and the loudness audibly jumps at
every cut. This ordering is a correctness requirement, not a preference.
"""

from __future__ import annotations

from pathlib import Path

from ..config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    LOUDNESS_RANGE,
    LOUDNESS_TARGET_LUFS,
    LOUDNESS_TRUE_PEAK,
)
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import concat_path, run_ffmpeg

# Applied identically to every episode. The consistency is what makes a
# channel sound produced rather than homemade.
FILTER_CHAIN = ",".join([
    "highpass=f=80",                        # rumble, desk thumps
    "equalizer=f=250:t=q:w=1.2:g=-2.5",     # cut mud
    "equalizer=f=3000:t=q:w=1.4:g=2.0",     # lift presence
    "acompressor=threshold=-18dB:ratio=3:attack=12:release=180:makeup=2",
    "deesser=i=0.4",                        # sibilance, a cloning artifact
    f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}",
    "alimiter=limit=0.97",
])


def concat_scene_audio(ep: Episode, m: Manifest, out: Path) -> Path:
    """Join scene WAVs in index order, without re-encoding gaps in."""
    missing = [s.slug for s in m.scenes if not ep.scene_audio(s).exists()]
    if missing:
        raise StageError(f"missing scene audio: {missing}; run voice first")

    listing = ep.audio_dir / "concat_audio.txt"
    lines = [f"file '{concat_path(ep.scene_audio(s))}'" for s in m.scenes]
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(out),
    ])
    return out


def run(ep: Episode, m: Manifest, master: bool = True) -> Path:
    log = get(m.id, "voice_process")
    raw = ep.audio_dir / "joined.wav"
    concat_scene_audio(ep, m, raw)

    if not master:
        raw.replace(ep.final_audio)
        log.info("mastering skipped")
        return ep.final_audio

    run_ffmpeg([
        "-i", str(raw), "-af", FILTER_CHAIN,
        "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(ep.final_audio),
    ])
    log.info(f"mastered to {LOUDNESS_TARGET_LUFS} LUFS")
    return ep.final_audio
