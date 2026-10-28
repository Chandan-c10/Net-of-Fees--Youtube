"""Draft engine: silence of a realistic length.

Lets the pipeline be exercised end to end with no model installed, so
assembly, captions and timing can be verified before any download. Its
output is marked draft and the upload stage refuses to publish it.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from ..pacing import scene_padding
from .base import VoiceResult, measure_wav

# Narration pace. Only used to size the silence, never to report duration --
# duration is still measured from the written file.
WORDS_PER_MINUTE = 150
MIN_SCENE_SEC = 1.2


class SilentVoice:
    name = "silent"
    draft = True

    def __init__(self, sample_rate: int = 48000, wpm: int = WORDS_PER_MINUTE,
                 episode_id: str = "", **_: object):
        self.sample_rate = sample_rate
        self.wpm = wpm
        self.episode_id = episode_id
        self._scene = 0

    def _estimate(self, text: str) -> float:
        words = len([w for w in text.split() if w.strip()])
        spoken = (words / self.wpm) * 60.0 if words else 0.0
        return max(MIN_SCENE_SEC, spoken)

    def speak(self, text: str, out_path: Path) -> VoiceResult:
        self._scene += 1
        # Pause is baked into the scene's audio, so the measured duration
        # includes it and the timing contract stays intact.
        seconds = self._estimate(text) + scene_padding(
            text, self._scene, self.episode_id
        )
        frames = int(seconds * self.sample_rate)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(struct.pack("<h", 0) * frames)
        return VoiceResult(
            path=out_path,
            duration_sec=measure_wav(out_path),  # measured, not estimated
            engine=self.name,
            draft=True,
        )
