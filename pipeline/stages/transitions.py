"""Scene transitions.

A hard cut between every scene is the rhythm of a slide deck. An editor
varies it: a dissolve where the subject continues, a harder cut where it
changes.

Crossfades are produced by blending frames with Pillow rather than with
FFmpeg's xfade, because xfade overlaps clips and shortens the result.
Blended frames borrow time from the scenes either side, so the total
duration is unchanged and audio stays in sync.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..errors import StageError

DISSOLVE_SEC = 0.42
DISSOLVE_FRAMES = 7
# Below this a scene has no time to spare and gets a hard cut instead.
MIN_SCENE_FOR_DISSOLVE = 3.0


def ease_in_out(t: float) -> float:
    """Smoothstep. A linear dissolve looks like a slideshow transition."""
    return t * t * (3.0 - 2.0 * t)


def blend_frames(frame_a: Path, frame_b: Path, out_dir: Path, stem: str,
                 count: int = DISSOLVE_FRAMES) -> list[Path]:
    """Intermediate frames from A to B."""
    if not frame_a.exists() or not frame_b.exists():
        raise StageError(f"cannot blend missing frames: {frame_a}, {frame_b}")
    a = Image.open(frame_a).convert("RGB")
    b = Image.open(frame_b).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)

    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for i in range(count):
        alpha = ease_in_out((i + 1) / (count + 1))
        p = out_dir / f"{stem}_x{i:02d}.png"
        Image.blend(a, b, alpha).save(p, "PNG", optimize=True)
        made.append(p)
    return made


def apply(frames: list[tuple[Path, float]], boundaries: set[int],
          out_dir: Path) -> list[tuple[Path, float]]:
    """Insert dissolves at the given frame indices.

    `boundaries` holds the index of each frame that begins a new scene.
    Each dissolve takes half its time from the outgoing frame and half
    from the incoming one, so nothing is added to the total.
    """
    if not boundaries:
        return frames

    out: list[tuple[Path, float]] = []
    half = DISSOLVE_SEC / 2.0
    i = 0
    while i < len(frames):
        path, dur = frames[i]
        is_boundary = i in boundaries and i > 0
        prev_ok = out and out[-1][1] > half + 0.05
        if is_boundary and prev_ok and dur > MIN_SCENE_FOR_DISSOLVE * 0.25:
            prev_path, prev_dur = out[-1]
            out[-1] = (prev_path, prev_dur - half)
            blended = blend_frames(prev_path, path, out_dir, f"b{i:04d}")
            per = DISSOLVE_SEC / len(blended)
            out.extend((p, per) for p in blended)
            out.append((path, max(0.05, dur - half)))
        else:
            out.append((path, dur))
        i += 1
    return out
