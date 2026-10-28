"""Music bed with ducking.

A bed under narration is one of the strongest signals of produced rather
than assembled video, and its absence is immediately noticeable. Ducking
is what keeps it from fighting the voice: the bed drops automatically
whenever the narration is present.

The track is yours to supply. Only a whitelisted, licence-cleared file is
accepted -- architecture doc, section 14 forbids scraped audio, and a
copyright claim on a published video is expensive to unwind.
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
from ..platform_support import run_ffmpeg

MUSIC_DIR_NAME = "music"
LICENCE_FILE = "LICENCE.md"

# The bed sits well under the voice. Louder than this and it competes.
BED_GAIN_DB = -22.0
# How hard the bed drops while narration is present.
DUCK_RATIO = 8.0
DUCK_THRESHOLD = 0.03
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 450

FADE_IN_SEC = 1.5
FADE_OUT_SEC = 2.5


def find_track(project_root: Path, name: str | None = None) -> Path | None:
    """Locate a cleared music file, or None to render without a bed."""
    music_dir = project_root / MUSIC_DIR_NAME
    if not music_dir.exists():
        return None

    licence = music_dir / LICENCE_FILE
    if not licence.exists() or not licence.read_text().strip():
        raise StageError(
            f"{music_dir} has no {LICENCE_FILE}. Every track must record "
            f"where it came from and under what licence. A copyright claim "
            f"on a published video is expensive to unwind."
        )

    if name:
        chosen = music_dir / name
        if not chosen.exists():
            raise StageError(f"no music track at {chosen}")
        return chosen

    for pattern in ("*.wav", "*.mp3", "*.flac", "*.m4a"):
        found = sorted(music_dir.glob(pattern))
        if found:
            return found[0]
    return None


def mix(voice: Path, track: Path, out: Path, duration: float,
        bed_gain_db: float = BED_GAIN_DB) -> Path:
    """Mix a ducked bed under the narration.

    sidechaincompress drives the bed's gain from the voice signal, so the
    duck follows the actual speech rather than a fixed envelope.
    """
    fade_start = max(0.0, duration - FADE_OUT_SEC)
    filter_complex = (
        # Loop the bed to cover the episode, then trim to exact length.
        f"[1:a]aloop=loop=-1:size=2e9,atrim=0:{duration:.3f},"
        f"volume={bed_gain_db}dB,"
        f"afade=t=in:st=0:d={FADE_IN_SEC},"
        f"afade=t=out:st={fade_start:.3f}:d={FADE_OUT_SEC}[bed];"
        # Duck the bed using the voice as the sidechain key.
        f"[bed][0:a]sidechaincompress="
        f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
        f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[ducked];"
        f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
        f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={LOUDNESS_TRUE_PEAK}:"
        f"LRA={LOUDNESS_RANGE}[out]"
    )
    run_ffmpeg([
        "-i", str(voice), "-i", str(track),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(out),
    ])
    return out


def apply_if_available(project_root: Path, voice: Path, duration: float,
                       episode_id: str, name: str | None = None) -> Path:
    """Mix a bed when one is available, else return the voice untouched."""
    log = get(episode_id, "music")
    track = find_track(project_root, name)
    if track is None:
        log.debug("no music track; rendering voice only")
        return voice
    mixed = voice.with_name("final_with_music.wav")
    mix(voice, track, mixed, duration)
    log.info(f"bed: {track.name} at {BED_GAIN_DB} dB, ducked under narration")
    return mixed
