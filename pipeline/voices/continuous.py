"""Narrating a whole script in one take, and cutting pictures under it.

The pipeline was scene-based end to end: one line, one picture, and a
pause at every boundary while the frame changed. That is backwards. A
narrator does not stop so the camera can cut -- the read runs on, and
the pictures change underneath it.

Doing that needs two things that pull against each other. The audio must
be one continuous take, or the prosody breaks at every join. And the
pipeline must still know where each line falls inside it, or there is
nothing to cut on.

Kokoro gives the first and not the second: it will read two hundred
characters as a single utterance and report only the total length. So
the timing is recovered separately.

**How the offsets are found.** Each line is synthesised alone to measure
how long it takes to say -- those takes are thrown away, they exist only
as a ruler. Their relative lengths are then scaled onto the continuous
take's measured duration. Line lengths reflect what is actually being
said, including the model's own pauses and emphasis, which is why this
is far better than counting characters: "Eleven coins. Eleven mornings."
and "The whole village had forgotten." are the same length in
characters and not in speech.

The contract in section 5a is untouched. The total still comes from
measuring the rendered file, and the per-scene offsets are a division of
that measured total, so they sum to it exactly.

The cost is synthesising twice. Kokoro renders about ten seconds of
speech in a second on a laptop, so a sixty-second episode spends an
extra few seconds to stop sounding like a slideshow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import StageError
from ..logging_setup import get

SAMPLE_RATE = 24_000

# Below this a measured line is noise rather than speech, and dividing
# by the total would hand it a meaningless share.
MIN_LINE_SEC = 0.05


@dataclass(frozen=True)
class Span:
    """Where one line sits inside the continuous take."""

    index: int
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def distribute(measured: list[float], total: float) -> list[float]:
    """Scale per-line measurements onto the real total.

    The measurements are a ruler, not the audio. What matters is their
    proportion, and that the result adds up to the total exactly --
    otherwise the visuals drift from the sound they were cut to.
    """
    if not measured:
        return []
    if total <= 0:
        raise StageError(f"cannot distribute {total}s across lines")
    floor = [max(MIN_LINE_SEC, value) for value in measured]
    scale = total / sum(floor)
    scaled = [value * scale for value in floor]
    # Give the rounding remainder to the last line so the sum is exact
    # rather than nearly right.
    scaled[-1] += total - sum(scaled)
    return scaled


def spans_for(texts: list[str], measured: list[float],
              total: float) -> list[Span]:
    """Start and end for every line within the continuous take."""
    if len(texts) != len(measured):
        raise StageError(
            f"{len(texts)} lines but {len(measured)} measurements"
        )
    out: list[Span] = []
    clock = 0.0
    for index, (text, length) in enumerate(
            zip(texts, distribute(measured, total), strict=True), start=1):
        out.append(Span(index, text, round(clock, 4),
                        round(clock + length, 4)))
        clock += length
    return out


def narrate(engine, scenes, out_path: Path, episode_id: str = "",
            log=None) -> list[Span]:
    """Read the whole script in one take. Returns where each line falls.

    `engine` must expose `render_text(text) -> numpy array`, which is
    what the Kokoro voice provides. The continuous take is written to
    `out_path`; the per-line takes are measured and discarded.
    """
    import numpy as np
    import soundfile as sf

    log = log or get(episode_id or None, "narration")
    texts = [scene.text.strip() for scene in scenes if scene.text.strip()]
    if not texts:
        raise StageError("nothing to narrate")

    # The ruler. Thrown away; only the lengths are kept.
    measured: list[float] = []
    for text in texts:
        samples = engine.render_text(text)
        measured.append(len(samples) / SAMPLE_RATE)

    # The take that is actually used. One utterance, so the model keeps
    # a single contour across the whole script.
    joined = " ".join(texts)
    audio = engine.render_text(joined)
    total = len(audio) / SAMPLE_RATE

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), np.asarray(audio), SAMPLE_RATE, subtype="PCM_16")

    spans = spans_for(texts, measured, total)
    log.info(f"one take of {total:.2f}s across {len(spans)} lines")
    drift = abs(spans[-1].end - total)
    if drift > 0.01:
        raise StageError(
            f"the line offsets sum to {spans[-1].end:.3f}s but the take is "
            f"{total:.3f}s; the visuals would drift from the sound"
        )
    return spans
