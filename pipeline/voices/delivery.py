"""Making a synthesised read sound like someone talking.

Kokoro given a whole paragraph reads it at one speed, with its own idea
of where the breaths go, and the pause after it is digital silence. That
last detail is the clearest tell of all: a real recording never goes to
exactly zero, so a listener hears the cut even when they could not say
what they heard.

Three things happen here.

1. The scene is split into beats -- sentences, and long sentences at
   their commas -- and each is synthesised separately, so the pauses land
   where a person would actually breathe rather than where the model
   guessed.

2. Each beat gets its own speed, within a few percent. A sentence
   carrying the headline number slows down, because that is what someone
   does when they want you to hear a number. A short sentence after a
   long one slows too, since that is a point being landed. Connective
   tissue speeds up slightly. The range is deliberately narrow: past
   about six percent it stops sounding like emphasis and starts sounding
   like a fault.

3. Pauses are filled with room tone, not zeros, and beats are joined
   through short fades so the concatenation does not click.

Everything is derived from a SHA-256 of the episode and scene, so a
re-render produces a byte-identical read. That is required, not merely
nice: the timing contract in section 5a measures the rendered audio, and
a read that varied between runs would move every visual with it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SAMPLE_RATE = 24_000

# Speed multipliers. A human varies more than this across a whole talk,
# but within a single paragraph the swing is small, and overshooting it
# is what makes narration sound unwell rather than expressive.
BASE_SPEED = 1.0
EMPHASIS_SLOWER = 0.94      # the beat carrying the number
LANDING_SLOWER = 0.96       # a short beat after a long one
CONNECTIVE_FASTER = 1.04    # "and here is where they lived"
SPEED_JITTER = 0.02         # keeps consecutive beats from matching exactly
SPEED_FLOOR, SPEED_CEIL = 0.90, 1.08

# Pause after a beat, by what ended it. Longer than the gaps inside a
# sentence, shorter than the gap between scenes, which pacing.py owns.
PAUSE_BY_ENDING = {
    ".": 0.30,
    "?": 0.38,
    "!": 0.32,
    ":": 0.26,
    ";": 0.22,
    ",": 0.13,
    "": 0.20,
}
PAUSE_JITTER = 0.05

# Room tone sits far below the voice: present, never audible as noise.
# -62 dBFS measured as RMS; much above -55 and it reads as hiss.
ROOM_TONE_DBFS = -62.0

# Long enough to hide a join, short enough not to eat a consonant.
FADE_SEC = 0.008

# A sentence longer than this is split at its commas, because a single
# breath does not carry much further than this in practice.
LONG_BEAT_CHARS = 130

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_NUMBER = re.compile(r"\d")
_CONNECTIVE = re.compile(
    r"^(and|but|so|then|now|here|that|which|because|after|before|"
    r"meanwhile|still|also)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Beat:
    """One synthesised span, and the gap that follows it."""

    text: str
    speed: float
    pause_sec: float

    @property
    def ending(self) -> str:
        stripped = self.text.rstrip()
        return stripped[-1] if stripped else ""


def _unit(seed: str) -> float:
    """A stable 0.0-1.0 from a seed, so re-renders match."""
    return hashlib.sha256(seed.encode("utf-8")).digest()[0] / 255.0


def _signed(seed: str) -> float:
    return _unit(seed) * 2.0 - 1.0


def split_beats(text: str) -> list[str]:
    """Sentences, with over-long ones broken at their commas.

    Splitting on commas only when a sentence is long: a short sentence
    read in two pieces sounds hesitant, while a long one read in one
    breath sounds rushed.
    """
    beats: list[str] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= LONG_BEAT_CHARS or "," not in sentence:
            beats.append(sentence)
            continue
        # Keep each comma with the clause it closes, so the pause lands
        # after the comma rather than before the next word.
        parts = [p.strip() for p in sentence.split(",")]
        current = ""
        for i, part in enumerate(parts):
            piece = part + ("," if i < len(parts) - 1 else "")
            if current and len(current) + len(piece) > LONG_BEAT_CHARS:
                beats.append(current.strip())
                current = piece
            else:
                current = f"{current} {piece}".strip()
        if current.strip():
            beats.append(current.strip())
    return beats


def plan(text: str, scene_index: int = 0, episode_id: str = "") -> list[Beat]:
    """How this scene should be read, beat by beat.

    A pure function of its arguments, which is what makes a re-render
    reproduce the same audio.
    """
    raw = split_beats(text)
    beats: list[Beat] = []
    for i, body in enumerate(raw):
        seed = f"{episode_id}:{scene_index}:{i}:{body[:24]}"
        previous = raw[i - 1] if i else ""

        speed = BASE_SPEED
        if _NUMBER.search(body):
            speed = EMPHASIS_SLOWER
        elif previous and len(body) < 45 <= len(previous):
            speed = LANDING_SLOWER
        elif _CONNECTIVE.match(body):
            speed = CONNECTIVE_FASTER
        speed += _signed(seed + ":speed") * SPEED_JITTER
        speed = max(SPEED_FLOOR, min(SPEED_CEIL, speed))

        ending = body.rstrip()[-1] if body.rstrip() else ""
        pause = PAUSE_BY_ENDING.get(ending, PAUSE_BY_ENDING[""])
        pause += _signed(seed + ":pause") * PAUSE_JITTER
        # The gap after the final beat belongs to pacing.scene_padding,
        # which owns the space between scenes.
        if i == len(raw) - 1:
            pause = 0.0
        beats.append(Beat(body, round(speed, 4), round(max(0.0, pause), 4)))
    return beats


def room_tone(samples: int, seed: str):
    """Low-level shaped noise to sit under a pause.

    Digital silence between phrases is the single clearest sign of a
    synthetic read. This is deliberately well below audibility as noise;
    it exists so the floor does not drop to nothing.
    """
    import numpy as np

    if samples <= 0:
        return np.zeros(0, dtype=np.float32)
    rng = np.random.default_rng(
        int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))
    noise = rng.standard_normal(samples).astype(np.float32)
    # One pole of low-pass so it reads as room rather than hiss.
    for _ in range(2):
        noise = np.convolve(noise, np.array([0.5, 0.5], dtype=np.float32),
                            mode="same")
    rms = float(np.sqrt(np.mean(noise ** 2))) or 1.0
    target = 10.0 ** (ROOM_TONE_DBFS / 20.0)
    return (noise / rms * target).astype(np.float32)


def _fade(block, sample_rate: int):
    """Taper both ends so a join does not click."""
    import numpy as np

    n = min(int(FADE_SEC * sample_rate), len(block) // 2)
    if n <= 0:
        return block
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = block.copy()
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


def stitch(segments: list, beats: list[Beat], seed: str,
           sample_rate: int = SAMPLE_RATE):
    """Join rendered beats with their pauses.

    `segments` are the audio arrays, one per beat, in order.
    """
    import numpy as np

    if len(segments) != len(beats):
        raise ValueError(
            f"{len(segments)} segments for {len(beats)} beats; they must "
            "correspond one to one"
        )
    pieces = []
    for i, (audio, beat) in enumerate(zip(segments, beats, strict=True)):
        pieces.append(_fade(np.asarray(audio, dtype=np.float32), sample_rate))
        if beat.pause_sec > 0:
            pieces.append(room_tone(int(beat.pause_sec * sample_rate),
                                    f"{seed}:{i}"))
    return (np.concatenate(pieces) if pieces
            else np.zeros(0, dtype=np.float32))


def describe(text: str, scene_index: int = 0, episode_id: str = "") -> dict:
    """What the plan looks like, for the log and for `run.py voices`."""
    beats = plan(text, scene_index, episode_id)
    speeds = [b.speed for b in beats]
    return {
        "beats": len(beats),
        "speed_min": round(min(speeds), 3) if speeds else 0.0,
        "speed_max": round(max(speeds), 3) if speeds else 0.0,
        "pause_total_sec": round(sum(b.pause_sec for b in beats), 3),
    }
