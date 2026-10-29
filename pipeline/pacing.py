"""Natural pacing.

Uniform scene lengths and zero silence are two of the clearest tells that
a video was assembled by a machine. Human narration breathes: it pauses
longer after a claim than after a connective, and longer still before
changing subject.

The pause is baked into the scene's own audio rather than inserted
between scenes, so the measured duration still includes it and the
audio/video contract in section 5a is untouched.
"""

from __future__ import annotations

import hashlib

# Pause after a scene, by how it ends. A statement of fact needs room to
# land; a clause that runs on does not.
# Halved from the original values. Measured on a real render: a 15-word
# scene ran 8.97s, of which 1.63s was silence -- 18% dead air, and the
# narration came out at 92 words per minute against 150-160 for ordinary
# speech. A pause that lets a point land is worth it; one that makes the
# viewer leave is not.
TAIL_BY_ENDING = {
    ".": 0.32,
    "?": 0.38,
    "!": 0.34,
    ":": 0.22,
    ",": 0.14,
    "": 0.26,
}

# A scene carrying a number is given slightly longer, because the viewer
# is reading the chart as well as listening.
NUMBER_BONUS = 0.12
# Deterministic jitter so no two scenes land on exactly the same pause.
JITTER = 0.10
# Within a scene, a sentence break gets a short breath.
SENTENCE_PAUSE = 0.22
COMMA_PAUSE = 0.10


def _jitter(seed: str) -> float:
    """Repeatable pseudo-random offset in [-JITTER, +JITTER].

    Deterministic so a re-render of the same episode produces identical
    timing, which the reproducibility guarantee in section 9 requires.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    unit = digest[0] / 255.0          # 0.0 to 1.0
    return (unit * 2 - 1) * JITTER


def tail_pause(text: str, scene_index: int = 0, episode_id: str = "") -> float:
    """How long to hold silence after this scene."""
    stripped = text.rstrip()
    ending = stripped[-1] if stripped else ""
    base = TAIL_BY_ENDING.get(ending, TAIL_BY_ENDING[""])
    if any(ch.isdigit() for ch in text):
        base += NUMBER_BONUS
    base += _jitter(f"{episode_id}:{scene_index}:{ending}")
    return round(max(0.15, base), 3)


def internal_pauses(text: str) -> float:
    """Total breathing room inside a scene, from its punctuation."""
    sentences = text.count(". ") + text.count("? ") + text.count("! ")
    commas = text.count(", ")
    return round(sentences * SENTENCE_PAUSE + commas * COMMA_PAUSE, 3)


def scene_padding(text: str, scene_index: int, episode_id: str) -> float:
    """Silence to append to one scene's narration."""
    return round(tail_pause(text, scene_index, episode_id)
                 + internal_pauses(text), 3)


def describe(scenes) -> dict[str, float]:
    """Pacing summary, for the operator to sanity-check at gate 1."""
    durations = [s.duration_sec for s in scenes if s.duration_sec]
    if not durations:
        return {}
    mean = sum(durations) / len(durations)
    spread = max(durations) - min(durations)
    return {
        "mean_scene_sec": round(mean, 2),
        "shortest_sec": round(min(durations), 2),
        "longest_sec": round(max(durations), 2),
        # A machine-even edit has near-zero variation. Real narration does not.
        "variation_pct": round(100 * spread / mean, 1) if mean else 0.0,
    }
