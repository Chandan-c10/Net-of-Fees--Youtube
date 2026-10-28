"""Draft engine: silence of a realistic length.

Lets the pipeline be exercised end to end with no model installed, so
assembly, captions and timing can be verified before any download. Its
output is marked draft and the upload stage refuses to publish it.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

from .base import VoiceResult, measure_wav

# Narration pace. Only used to size the silence, never to report duration --
# duration is still measured from the written file.
WORDS_PER_MINUTE = 150
TAIL_SILENCE_SEC = 0.35
MIN_SCENE_SEC = 1.2


class SilentVoice:
    name = "silent"
    draft = True

    def __init__(self, sample_rate: int = 48000, wpm: int = WORDS_PER_MINUTE, **_: object):
        self.sample_rate = sample_rate
        self.wpm = wpm

    def _estimate(self, text: str) -> float:
        words = len([w for w in text.split() if w.strip()])
        spoken = (words / self.wpm) * 60.0 if words else 0.0
        return max(MIN_SCENE_SEC, spoken + TAIL_SILENCE_SEC)

    def speak(self, text: str, out_path: Path) -> VoiceResult:
        seconds = self._estimate(text)
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
