"""Cross-platform resolution: binaries, fonts, compute device, paths.

Everything that differs between macOS, Windows and Linux is decided here
and nowhere else. Stages call these helpers rather than branching on
sys.platform themselves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from .errors import ConfigError

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def os_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "linux"


# ---- binaries ---------------------------------------------------------

_INSTALL_HINT = {
    "windows": (
        "winget install Gyan.FFmpeg   (or: choco install ffmpeg-full)\n"
        "    then reopen the terminal so PATH refreshes"
    ),
    "macos": "brew install ffmpeg",
    "linux": "sudo apt install ffmpeg",
}


def find_binary(name: str, env_var: str | None = None) -> str:
    """Locate an executable, honouring an override env var.

    On Windows the .exe suffix is implicit; shutil.which handles PATHEXT.
    """
    if env_var:
        override = os.getenv(env_var)
        if override:
            if Path(override).exists() or shutil.which(override):
                return override
            raise ConfigError(
                f"{env_var} is set to {override!r} but no such executable exists"
            )
    found = shutil.which(name)
    if found:
        return found
    hint = _INSTALL_HINT[os_name()]
    raise ConfigError(
        f"{name} not found on PATH.\n"
        f"  Install it with:\n    {hint}\n"
        f"  Or set {env_var or name.upper() + '_BIN'} to its full path."
    )


@lru_cache(maxsize=4)
def ffmpeg() -> str:
    return find_binary("ffmpeg", "FFMPEG_BIN")


@lru_cache(maxsize=4)
def ffprobe() -> str:
    return find_binary("ffprobe", "FFPROBE_BIN")


def ffmpeg_available() -> bool:
    """True when both binaries resolve. Used to skip tests, never to degrade."""
    try:
        ffmpeg()
        ffprobe()
        return True
    except ConfigError:
        return False


def run_ffmpeg(args: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run FFmpeg with stderr captured and a non-zero exit raised.

    No shell, so paths containing spaces need no quoting -- which matters
    because the project may live under a path like 'Pro it'.
    """
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise ConfigError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    return proc


# ---- concat demuxer paths ---------------------------------------------


def concat_path(path: Path) -> str:
    """Format a path for FFmpeg's concat demuxer.

    The demuxer treats a backslash as an escape character, so a native
    Windows path silently breaks the list. Forward slashes are accepted on
    every platform, and single quotes are doubled per the demuxer's own
    escaping rule.
    """
    text = PurePosixPath(path.resolve().as_posix()).as_posix()
    return text.replace("'", "'\\''")


# ---- fonts ------------------------------------------------------------

_FONT_CANDIDATES = {
    "windows": [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ],
    "macos": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ],
    "linux": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
}


@lru_cache(maxsize=4)
def bold_font() -> Path | None:
    """A bold TrueType face for thumbnail text, or None to fall back.

    Returning None rather than raising keeps thumbnail generation working
    on a bare system; the caller draws with Pillow's built-in bitmap font.
    """
    override = os.getenv("THUMBNAIL_FONT")
    if override and Path(override).exists():
        return Path(override)
    for candidate in _FONT_CANDIDATES[os_name()]:
        p = Path(candidate)
        if p.exists():
            return p
    return None


# ---- compute device --------------------------------------------------


@dataclass(frozen=True)
class Device:
    kind: str  # "cuda" | "mps" | "cpu"
    detail: str

    @property
    def is_accelerated(self) -> bool:
        return self.kind in ("cuda", "mps")


def detect_device() -> Device:
    """Pick the best torch device available.

    Apple Silicon exposes MPS; a Windows machine with an NVIDIA card
    exposes CUDA. Everything falls back to CPU, which works but is slow
    enough that Phase 1 must measure it before trusting the plan.
    """
    override = os.getenv("TORCH_DEVICE")
    if override:
        return Device(override, f"forced by TORCH_DEVICE={override}")
    try:
        import torch
    except ImportError:
        return Device("cpu", "torch not installed")
    if torch.cuda.is_available():
        return Device("cuda", torch.cuda.get_device_name(0))
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return Device("mps", "Apple Silicon GPU")
    return Device("cpu", f"{os.cpu_count()} cores")


def _describe_binary(name: str, env_var: str) -> str:
    """What find_binary would return, or why it would fail."""
    try:
        return find_binary(name, env_var)
    except ConfigError as exc:
        return f"MISSING ({exc})"


def describe_environment() -> dict[str, object]:
    """Everything a bug report needs about the host."""
    device = detect_device()
    font = bold_font()
    return {
        "os": os_name(),
        "python": sys.version.split()[0],
        # Via find_binary, not shutil.which: the override wins there, so
        # resolving it differently here would report one binary while the
        # render used another.
        "ffmpeg": _describe_binary("ffmpeg", "FFMPEG_BIN"),
        "ffprobe": _describe_binary("ffprobe", "FFPROBE_BIN"),
        "device": f"{device.kind} ({device.detail})",
        "thumbnail_font": str(font) if font else "built-in bitmap fallback",
    }
