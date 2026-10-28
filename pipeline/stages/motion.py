"""Motion: chart build-ins.

A static image held for eight seconds then cut reads as a slide deck,
which is the third-clearest tell that a video was machine-assembled.
Letting a chart draw itself in over the first second makes the frame feel
authored rather than pasted.

Implemented by rendering the same chart at several reveal fractions, then
giving each frame a slice of the scene's opening. The scene's total
duration is unchanged, so the audio contract in section 5a holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import VideoSpec
from ..errors import StageError

# A build longer than this starts to feel slow on a second viewing.
BUILD_SEC = 0.9
BUILD_FRAMES = 9
# Charts that carry a single number gain nothing from a build.
STATIC_KEYS = frozenset({"title", "breakeven"})


def ease_out(t: float) -> float:
    """Cubic ease-out. Fast at the start, settling at the end -- the shape
    motion looks natural in. Linear reveals look mechanical."""
    return 1.0 - pow(1.0 - t, 3)


@dataclass(frozen=True)
class BuildFrame:
    path: Path
    duration: float
    reveal: float


def should_animate(data_key: str | None, scene_duration: float) -> bool:
    """Only animate when there is room and the chart has something to reveal."""
    if not data_key or data_key in STATIC_KEYS:
        return False
    return scene_duration > BUILD_SEC * 2


def build_in(scene, data: dict, spec: VideoSpec, profile,
             out_dir: Path) -> list[BuildFrame]:
    """Render the opening build for one scene.

    Returns an empty list when the profile's renderer does not support a
    reveal fraction, so animation degrades to a static frame rather than
    failing the render.
    """
    key = (scene.visual_spec or {}).get("data_key")
    if not should_animate(key, scene.duration_sec or 0.0):
        return []
    if not getattr(profile.visuals, "SUPPORTS_REVEAL", False):
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    slice_sec = BUILD_SEC / BUILD_FRAMES
    frames: list[BuildFrame] = []
    for i in range(BUILD_FRAMES):
        reveal = ease_out((i + 1) / BUILD_FRAMES)
        path = out_dir / f"{scene.slug}_build_{i:02d}.png"
        try:
            profile.visuals.build_visual(scene, data, spec, path, reveal=reveal)
        except TypeError as exc:
            raise StageError(
                f"profile {profile.PROFILE_NAME!r} declares SUPPORTS_REVEAL "
                f"but its build_visual does not accept reveal=: {exc}"
            ) from exc
        frames.append(BuildFrame(path, slice_sec, reveal))
    return frames
