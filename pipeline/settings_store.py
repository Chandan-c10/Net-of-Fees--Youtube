"""Reading and writing `.env` from the panel.

Credentials have always lived in `.env`, never in the repo and never in
a manifest. The panel editing that file is the same thing as you editing
it by hand, so this does not move a secret anywhere new.

What it must never do is show one back. A field that renders its current
value is a field that leaks it to a screenshot, a shoulder, or a browser
that offered to remember it. So every secret reports only whether it is
set, and writing a blank leaves the existing value alone.
"""

from __future__ import annotations

import re
from pathlib import Path

# Absolute, not relative to the working directory: the panel and the
# loader must agree on one file however the process was started.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Values that are never rendered back, only reported as set or not.
# KEY_ID is here because R2_ACCESS_KEY_ID ends in _ID and so fell
# outside an end-anchored match on KEY -- which would have rendered an
# access key back into the page. The public half of a key pair is still
# a credential.
SECRET = re.compile(
    r"(KEY|KEY_ID|TOKEN|SECRET|PASSWORD|CREDENTIAL)$", re.IGNORECASE)

# What the panel is allowed to write. An allowlist rather than anything
# the form posts, so a crafted request cannot set PATH or LD_PRELOAD.
EDITABLE = (
    "ALERT_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
    "SMTP_APP_PASSWORD", "SMTP_STARTTLS",
    "IG_USER_ID", "IG_ACCESS_TOKEN", "IG_API_VERSION",
    "PUBLISH_SLOTS", "CHANNEL_TZ", "BATCH_SIZE", "MAX_QUEUE_DAYS",
    "VOICE", "VOICE_ENGINE", "VOICE_SPEED", "PROFILE",
    "GEMINI_API_KEY", "SCRIPT_ENGINE", "TREND_REGION", "YOUTUBE_API_KEY",
    "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_ENDPOINT", "R2_PUBLIC_BASE_URL", "R2_REGION",
    "R2_STORAGE_CAP_GB", "R2_RETAIN_DAYS",
    "VOICE_REFERENCE_URL", "VOICE_REFERENCE_KEY",
    "RETAIN_WORKING_DAYS", "RETAIN_VIDEO_DAYS",
    "IMAGE_FETCH", "IMAGE_PROVIDERS", "IMAGE_STYLE",
    "IMAGE_LOCAL_URL", "IMAGE_LOCAL_STEPS", "POLLINATIONS_MODEL",
    "IMAGE_MAX_NEW", "IMAGE_REUSE", "IMAGE_REUSE_THRESHOLD",
    "IMAGE_PACE_SEC", "YOUTUBE_PRIVACY", "YOUTUBE_PLAYLISTS",
)

_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")


# Names a suffix cannot classify. A voice reference URL is a credential
# -- anyone holding it can clone the speaker -- and nothing about the
# word "URL" says so.
ALWAYS_SECRET = frozenset({"VOICE_REFERENCE_URL", "IG_VIDEO_URL"})


def is_secret(name: str) -> bool:
    upper = name.upper()
    if upper in ALWAYS_SECRET or upper.startswith("VOICE_REFERENCE_URL"):
        return True
    return bool(SECRET.search(name))


def read(path: Path | None = None) -> dict[str, str]:
    """Every value currently set, including commented-out ones as absent."""
    path = path or ENV_FILE
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def describe(path: Path | None = None) -> dict[str, dict]:
    """What the settings page shows.

    A secret reports `set` and nothing else; an ordinary value reports
    itself, because seeing the current publishing slots is the point.
    """
    values = read(path)
    out: dict[str, dict] = {}
    for name in EDITABLE:
        current = values.get(name, "")
        out[name] = {
            "secret": is_secret(name),
            "set": bool(current),
            "value": "" if is_secret(name) else current,
        }
    return out


def write(changes: dict[str, str], path: Path | None = None) -> list[str]:
    """Apply changes to `.env`, preserving comments and order.

    A blank value for a secret means "leave it alone", because the form
    never knew the old value to send back. A blank for anything else
    clears it. Returns the names actually changed.
    """
    path = path or ENV_FILE
    existing = read(path)
    lines = (path.read_text(encoding="utf-8", errors="replace").splitlines()
             if path.exists() else [])

    applied: dict[str, str] = {}
    for name, value in changes.items():
        if name not in EDITABLE:
            continue          # allowlist; a crafted post cannot add keys
        value = value.strip()
        if is_secret(name) and not value:
            continue          # blank means unchanged, not cleared
        if existing.get(name, "") == value:
            continue
        applied[name] = value
    if not applied:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _LINE.match(line)
        if match and match.group(1) in applied:
            name = match.group(1)
            out.append(f"{name}={applied[name]}")
            seen.add(name)
        else:
            out.append(line)
    for name, value in applied.items():
        if name not in seen:
            out.append(f"{name}={value}")

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)   # it holds credentials
    except OSError:
        pass
    return sorted(applied)
