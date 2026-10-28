"""Single source of truth for every setting.

Nothing here is configurable through the console. A change in this file
changes every future render, which is why it lives in git -- see the
architecture doc, section 7.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT / "episodes"
PROFILES_DIR = ROOT / "profiles"
VOICES_DIR = ROOT / "voices"

# All timestamps are stored UTC. Slot times are expressed in this zone and
# converted on write -- architecture doc, section 24.
CHANNEL_TZ = ZoneInfo("Asia/Kolkata")
MORNING_SLOT = (8, 30)
EVENING_SLOT = (19, 30)


@dataclass(frozen=True)
class VideoSpec:
    """Encoder settings. Long-form and Shorts differ only in geometry."""

    width: int
    height: int
    fps: int = 30
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def aspect(self) -> str:
        return "16:9" if self.width >= self.height else "9:16"


LONGFORM = VideoSpec(width=1920, height=1080)
SHORTS = VideoSpec(width=1080, height=1920)

# Mastering chain. Loudness is normalised across the whole episode, never
# per scene -- per-scene gain makes loudness jump between scenes.
LOUDNESS_TARGET_LUFS = -16.0
LOUDNESS_TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1  # speech is mono; stereo doubles size and carries nothing

# Assembly tolerance. The assembled video must match the sum of scene
# durations within this margin, or the render is rejected as drifted.
SYNC_TOLERANCE_SEC = 0.05

# Retry policy for queued work.
MAX_ATTEMPTS = 3
DEAD_CLAIM_MINUTES = 60
HEARTBEAT_TIMEOUT_HOURS = 6


@dataclass(frozen=True)
class Settings:
    """Runtime settings, assembled from the environment."""

    profile: str = field(default_factory=lambda: os.getenv("PROFILE", "trading"))
    voice: str = field(default_factory=lambda: os.getenv("VOICE", "chandan_en"))
    voice_engine: str = field(default_factory=lambda: os.getenv("VOICE_ENGINE", "silent"))
    script_engine: str = field(default_factory=lambda: os.getenv("SCRIPT_ENGINE", "template"))
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "llama3.1"))
    ffmpeg: str = field(default_factory=lambda: os.getenv("FFMPEG_BIN", "ffmpeg"))
    ffprobe: str = field(default_factory=lambda: os.getenv("FFPROBE_BIN", "ffprobe"))
    alert_email: str | None = field(default_factory=lambda: os.getenv("ALERT_EMAIL"))

    def profile_dir(self) -> Path:
        return PROFILES_DIR / self.profile

    def voice_dir(self) -> Path:
        return VOICES_DIR / self.voice


def load() -> Settings:
    """Read settings from the environment. Called once per run."""
    return Settings()
