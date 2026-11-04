"""Per-scene ambient beds.

A music bed carries the whole episode at one mood. Ambience is the layer
under that: the room a scene happens in. A village street at midday and
a kitchen at night sound different, and a listener notices the absence
long before they could name what is missing.

This is the cheap eighty percent of "sound effects matching the action".
Scoring individual actions needs a cue sheet and a library; a bed per
scene needs neither and carries most of the atmosphere, because what
sells a place is its continuous sound rather than its incidents.

The mood is inferred from the scene's own text, which keeps the whole
thing automatic: no cue list to maintain, and a rewritten scene picks up
a new bed without anyone editing a mapping.

Three rules inherited from `music.py`, for the same reasons:

* Only licence-cleared audio is used. A directory without a LICENCE.md
  is refused rather than ignored -- a copyright claim on a published
  video is expensive to unwind.
* The bed sits far under the voice and ducks when narration is present.
* Absence is fine. No ambience directory means the episode renders
  without it, because a missing bed must never block a render.

Duration is not negotiable here either. The bed is cut to the scene's
measured length, so the audio contract in section 5a is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import StageError
from ..logging_setup import get
from ..platform_support import run_ffmpeg

AMBIENCE_DIR_NAME = "ambience"
LICENCE_FILE = "LICENCE.md"

# Further under the voice than the music bed, which is already at -22.
# Ambience should be felt rather than heard; above about -26 it starts
# competing with consonants.
BED_GAIN_DB = -28.0

# Ducking, matched to music.py so the two layers behave alike.
DUCK_RATIO = 6.0
DUCK_THRESHOLD = 0.03
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 400

# Long enough that a change of place reads as a move, short enough not to
# smear two rooms together.
CROSSFADE_SEC = 0.8


@dataclass(frozen=True)
class Mood:
    """A place, and the words that suggest it."""

    name: str
    cues: tuple[str, ...]

    def matches(self, text: str) -> int:
        """How many of this mood's cues appear. Zero means no match."""
        lowered = text.lower()
        return sum(1 for cue in self.cues
                   if re.search(rf"\b{re.escape(cue)}\b", lowered))


# Ordered by specificity: a scene mentioning both "night" and "market"
# is a night market, so the narrower mood is checked first.
MOODS: tuple[Mood, ...] = (
    Mood("tension", ("silence", "waited", "froze", "stopped", "nobody",
                     "alone", "empty", "missing", "gone", "wrong",
                     "suddenly", "realised", "realized")),
    Mood("crowd", ("market", "crowd", "street", "shouting", "bazaar",
                   "queue", "festival", "village", "town", "people")),
    Mood("interior_night", ("night", "lamp", "candle", "bedroom", "dark",
                            "evening", "midnight", "asleep", "slept")),
    Mood("interior_day", ("kitchen", "room", "home", "house", "table",
                          "indoors", "inside", "family", "sat")),
    Mood("nature", ("river", "forest", "field", "tree", "rain", "wind",
                    "bird", "mountain", "sea", "garden", "farm")),
    Mood("exterior_day", ("morning", "sun", "road", "walked", "outside",
                          "path", "sky", "afternoon", "daylight")),
)

# When nothing matches. Quiet room tone is never wrong; a wrong place is.
DEFAULT_MOOD = "room"


def mood_for(text: str) -> str:
    """The place this scene sounds like.

    Falls back to a neutral room rather than guessing: ambience that
    contradicts the picture is worse than none, because the viewer hears
    a mistake rather than a setting.
    """
    best, score = DEFAULT_MOOD, 0
    for mood in MOODS:
        hits = mood.matches(text)
        if hits > score:
            best, score = mood.name, hits
    return best


def available(project_root: Path) -> dict[str, Path]:
    """Cleared ambience files, keyed by mood.

    A file is matched by stem, so `ambience/crowd.wav` serves the crowd
    mood. Refuses the whole directory if it carries no licence record,
    for the same reason music.py does.
    """
    directory = project_root / AMBIENCE_DIR_NAME
    if not directory.exists():
        return {}

    licence = directory / LICENCE_FILE
    if not licence.exists() or not licence.read_text(
            encoding="utf-8", errors="replace").strip():
        raise StageError(
            f"{directory} has no {LICENCE_FILE}. Every bed must record "
            f"where it came from and under what licence. A copyright "
            f"claim on a published video is expensive to unwind."
        )

    found: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
            found[path.stem.lower()] = path
    return found


def plan(scenes, project_root: Path) -> list[tuple[str, Path | None]]:
    """The bed for each scene, in order.

    A mood with no file falls back to the default, and a missing default
    simply means that scene has no bed -- one absent file must not cost
    the whole episode its atmosphere.
    """
    beds = available(project_root)
    out: list[tuple[str, Path | None]] = []
    for scene in scenes:
        mood = mood_for(scene.text)
        path = beds.get(mood) or beds.get(DEFAULT_MOOD)
        out.append((mood, path))
    return out


def _segment(source: Path, seconds: float, out: Path, fade: bool) -> Path:
    """One scene's worth of a bed, looped if the file is short."""
    chain = ["aloop=loop=-1:size=2e9", f"atrim=0:{seconds:.3f}",
             "asetpts=N/SR/TB"]
    if fade:
        chain.append(f"afade=t=in:st=0:d={CROSSFADE_SEC}")
        chain.append(
            f"afade=t=out:st={max(0.0, seconds - CROSSFADE_SEC):.3f}"
            f":d={CROSSFADE_SEC}")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(source), "-af", ",".join(chain),
                "-t", f"{seconds:.3f}", str(out)])
    return out


def build_bed(scenes, project_root: Path, work: Path,
              total_sec: float) -> Path | None:
    """One continuous ambience track for the whole episode.

    Built per scene then concatenated, so each scene keeps its own place
    and the joins are crossfaded rather than cut.
    """
    log = get(None, "ambience")
    chosen = plan(scenes, project_root)
    if not any(path for _, path in chosen):
        log.debug("no ambience files; rendering without")
        return None

    work.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    for scene, (mood, source) in zip(scenes, chosen, strict=True):
        seconds = scene.duration_sec
        if seconds is None:
            raise StageError(
                f"{scene.slug} has no measured duration; run voice first"
            )
        piece = work / f"amb_{scene.index:02d}.wav"
        if source is None:
            # Silence of exactly this length keeps the timeline aligned.
            run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-t", f"{seconds:.3f}", str(piece)])
        else:
            _segment(source, seconds, piece, fade=True)
            log.debug(f"{scene.slug}: {mood}")
        pieces.append(piece)

    listing = work / "ambience.txt"
    listing.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in pieces) + "\n",
        encoding="utf-8")
    joined = work / "ambience.wav"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
                "-t", f"{total_sec:.3f}", str(joined)])
    return joined


def apply_if_available(project_root: Path, narration: Path, scenes,
                       total_sec: float, episode_id: str,
                       work: Path | None = None) -> Path:
    """Mix ambience under the narration, or return it untouched.

    Returns the narration unchanged when there is nothing to add, so the
    caller needs no branch and a missing directory cannot fail a render.
    """
    log = get(episode_id, "ambience")
    work = work or narration.parent / "ambience"
    try:
        bed = build_bed(scenes, project_root, work, total_sec)
    except StageError:
        raise
    except Exception as exc:
        log.warning(f"could not build ambience: {exc}")
        return narration
    if bed is None:
        return narration

    out = narration.parent / "narration_ambient.wav"
    # The bed ducks against the narration, exactly as the music bed does,
    # so the two layers never fight the voice independently.
    run_ffmpeg([
        "-i", str(narration), "-i", str(bed),
        "-filter_complex",
        f"[1:a]volume={BED_GAIN_DB}dB[amb];"
        f"[amb][0:a]sidechaincompress="
        f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
        f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[ducked];"
        f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0"
        f"[mixed]",
        "-map", "[mixed]", "-t", f"{total_sec:.3f}", str(out),
    ])
    if not out.exists():
        log.warning("ambience mix produced nothing; using the voice alone")
        return narration
    log.info(f"ambience mixed under {len(scenes)} scenes")
    return out


def describe(scenes, project_root: Path) -> dict:
    """What each scene would get, for the log and the panel."""
    chosen = plan(scenes, project_root)
    return {
        "moods": [mood for mood, _ in chosen],
        "with_bed": sum(1 for _, path in chosen if path),
        "available": sorted(available(project_root)),
    }
