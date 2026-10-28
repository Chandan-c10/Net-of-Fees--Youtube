"""Which account an episode publishes to.

One deployment, several content streams. The profile system already made
the topic swappable; this makes the destination swappable too, so an
engineering profile and a trading profile can go to different YouTube
channels and different Instagram accounts from the same checkout and the
same schedule.

A profile declares its channel key, or inherits the profile name. Each
credential is then looked up per channel first and falls back to the
single-channel name, so an existing one-channel setup keeps working
untouched:

    secrets/youtube_token_engineering.json   <- this channel
    secrets/youtube_token.json               <- the fallback

    IG_USER_ID_ENGINEERING                   <- this channel
    IG_USER_ID                               <- the fallback

Nothing here reads a credential value into the manifest or the logs. The
channel key is a name; the secrets stay in the environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import ConfigError
from .manifest import Manifest

SECRETS_DIR = Path("secrets")

# A channel key becomes part of an env var name and a filename, so it is
# constrained rather than trusted.
CHANNEL_KEY = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")


def key_for(m: Manifest) -> str:
    """The channel this episode publishes to.

    A profile may declare CHANNEL to share one account across several
    profiles -- a long-form profile and its _shorts sibling usually post
    to the same channel.
    """
    try:
        import profiles
        declared = getattr(profiles.load(m.profile), "CHANNEL", None)
    except Exception:
        declared = None
    key = (declared or m.profile or "default").lower()
    # A _shorts profile is the same channel as its parent unless it says
    # otherwise, because that is what the sibling exists for.
    if not declared and key.endswith("_shorts"):
        key = key[: -len("_shorts")]
    if not CHANNEL_KEY.match(key):
        raise ConfigError(
            f"bad channel key {key!r}: lowercase letters, digits and "
            "underscore only"
        )
    return key


def _env(name: str, key: str) -> str | None:
    """A per-channel value, else the single-channel one."""
    return os.getenv(f"{name}_{key.upper()}") or os.getenv(name)


def youtube_token_path(key: str) -> Path:
    """Where this channel's OAuth token lives.

    Checked in order: an explicit per-channel override, the per-channel
    default filename, an explicit global override, then the original
    single-channel filename.
    """
    explicit = os.getenv(f"YOUTUBE_TOKEN_FILE_{key.upper()}")
    if explicit:
        return Path(explicit)
    per_channel = SECRETS_DIR / f"youtube_token_{key}.json"
    if per_channel.exists():
        return per_channel
    return Path(os.getenv("YOUTUBE_TOKEN_FILE",
                          str(SECRETS_DIR / "youtube_token.json")))


def instagram_credentials(key: str) -> tuple[str | None, str | None]:
    return _env("IG_USER_ID", key), _env("IG_ACCESS_TOKEN", key)


def describe(key: str) -> dict[str, str]:
    """What is configured for this channel, for `doctor` and the panel.

    Reports presence, never a value.
    """
    token = youtube_token_path(key)
    user_id, access = instagram_credentials(key)
    return {
        "channel": key,
        "youtube": (f"{token} (present)" if token.exists()
                    else f"{token} (missing)"),
        "instagram": ("configured" if user_id and access
                      else "not configured"),
    }


def known() -> list[str]:
    """Every channel the installed profiles publish to."""
    import profiles
    keys = set()
    for name in profiles.available():
        try:
            m = Manifest.new("probe", recipe="r", profile=name, voice="v")
            keys.add(key_for(m))
        except ConfigError:
            continue
    return sorted(keys)
