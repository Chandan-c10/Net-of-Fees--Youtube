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
CHANNEL_TZ = ZoneInfo(os.getenv("CHANNEL_TZ", "Asia/Kolkata"))


def _parse_slots(raw: str) -> tuple[tuple[int, int], ...]:
    """Publishing times, as "HH:MM,HH:MM".

    Settable rather than fixed: two a day at 08:30 and 19:30 suits one
    channel and nothing else, and the number of slots is also the number
    of episodes a day the queue will release.
    """
    slots: list[tuple[int, int]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            hour, minute = (int(part) for part in piece.split(":"))
        except ValueError as exc:
            raise ValueError(
                f"bad slot {piece!r} in PUBLISH_SLOTS; expected HH:MM"
            ) from exc
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"slot {piece!r} is not a real time")
        slots.append((hour, minute))
    if not slots:
        raise ValueError("PUBLISH_SLOTS is empty; at least one is needed")
    return tuple(sorted(set(slots)))


PUBLISH_SLOTS = _parse_slots(os.getenv("PUBLISH_SLOTS", "08:30,19:30"))

# Kept for anything that still names them individually.
MORNING_SLOT = PUBLISH_SLOTS[0]
EVENING_SLOT = PUBLISH_SLOTS[-1]

# How many episodes a batch produces in one go. Drafting several at once
# and releasing them on the slots is the point of the queue: the work
# happens when you have time, the posting happens on schedule.
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))

# Refuse to queue more than this far ahead. A queue longer than a
# fortnight is one whose early episodes were written about a different
# week.
MAX_QUEUE_DAYS = int(os.getenv("MAX_QUEUE_DAYS", "14"))


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

    # Binaries, API keys and SMTP settings are deliberately absent: each is
    # read from the environment where it is used, by the module that knows
    # how to fail usefully without it. A second copy here would drift.

    def profile_dir(self) -> Path:
        return PROFILES_DIR / self.profile

    def voice_dir(self) -> Path:
        return VOICES_DIR / self.voice


def load() -> Settings:
    """Read settings from the environment. Called once per run."""
    return Settings()
