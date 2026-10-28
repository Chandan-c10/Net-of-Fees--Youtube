"""The voice contract.

Duration is always measured from the rendered file, never estimated from
word count. That measurement is the load-bearing contract of the whole
pipeline: FFmpeg shows each scene's visual for exactly as long as its
audio runs -- architecture doc, section 5a.
"""

from __future__ import annotations

import json
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..errors import StageError


@dataclass(frozen=True)
class VoiceResult:
    path: Path
    duration_sec: float
    engine: str
    draft: bool = False


@runtime_checkable
class VoiceEngine(Protocol):
    name: str
    draft: bool

    def speak(self, text: str, out_path: Path) -> VoiceResult:
        """Render `text` to `out_path` and return it with its measured duration."""
        ...


def measure_wav(path: Path) -> float:
    """Duration of a PCM wav, from its own header. No external tool needed."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                raise StageError(f"{path.name} reports a zero sample rate")
            return frames / float(rate)
    except wave.Error as exc:
        raise StageError(f"cannot read {path.name} as wav: {exc}") from exc


def measure_media(path: Path, ffprobe: str = "ffprobe") -> float:
    """Duration of any media file, via ffprobe. Used for non-wav inputs."""
    cmd = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError as exc:
        raise StageError(
            f"{ffprobe} not found. Install FFmpeg: brew install ffmpeg"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise StageError(f"ffprobe failed on {path.name}: {exc.stderr.strip()}") from exc
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise StageError(f"ffprobe gave no duration for {path.name}") from exc


def load_lexicon(voice_dir: Path) -> dict[str, str]:
    """Pronunciation overrides for this voice.

    Finance vocabulary is mispronounced by every engine tried so far, so a
    lexicon is required rather than optional -- architecture doc, section 17.
    """
    path = voice_dir / "lexicon.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise StageError(f"lexicon at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise StageError(f"lexicon at {path} must be an object of term -> spoken form")
    return {str(k): str(v) for k, v in raw.items()}


def apply_lexicon(text: str, lexicon: dict[str, str]) -> str:
    """Replace terms with their spoken forms, longest first, word-bounded."""
    if not lexicon:
        return text
    for term in sorted(lexicon, key=len, reverse=True):
        spoken = lexicon[term]
        # Word boundaries fail around '&' and '/', so bound on non-word chars.
        pattern = re.compile(
            r"(?<![\w&/])" + re.escape(term) + r"(?![\w&/])", re.IGNORECASE
        )
        text = pattern.sub(spoken, text)
    return text


def assert_consented(voice_dir: Path) -> None:
    """A voice with no consent record fails validation.

    Cloning one's own voice is fine. Any other voice needs the speaker's
    documented permission -- architecture doc, section 14.
    """
    if not voice_dir.exists():
        raise StageError(
            f"no voice directory at {voice_dir}. "
            f"Create it with reference/, meta.json and consent.md"
        )
    consent = voice_dir / "consent.md"
    if not consent.exists() or not consent.read_text().strip():
        raise StageError(
            f"voice {voice_dir.name!r} has no consent.md. "
            f"Every voice must record who it belongs to and their permission."
        )
