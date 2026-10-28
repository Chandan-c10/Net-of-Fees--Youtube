"""The finishing pass: grade, grain, vignette and drift.

What separates assembled slides from edited video is mostly texture and
motion, not content. A frame that never moves reads as a slide however
good the chart on it is.

Every effect here is duration-preserving. The pass runs over the
assembled video and returns a file of exactly the same length, so the
audio contract in section 5a is untouched and the drift check still holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import VideoSpec
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import run_ffmpeg


@dataclass(frozen=True)
class Look:
    """A grade. Values are deliberately restrained -- a heavy grade on flat
    chart colours reads as a filter, not as film."""

    name: str
    contrast: float = 1.04
    saturation: float = 1.06
    gamma: float = 0.99
    vignette_strength: float = 0.28
    grain: float = 0.0
    drift_pct: float = 1.8      # how far the frame travels over the episode


LOOKS = {
    # The default. No grain: flat chart backgrounds carry no texture for
    # grain to sit in, so it is invisible at normal viewing distance while
    # defeating H.264's compression. Measured at 26x the file size for no
    # visible difference.
    "clean": Look("clean"),
    # Grain costs roughly 25x the bitrate on synthetic footage. Worth it
    # only over photographic or screen-recorded material, never over charts.
    "filmic": Look("filmic", contrast=1.08, saturation=1.10,
                   vignette_strength=0.38, grain=6.0, drift_pct=2.6),
    # No treatment, for checking the raw render.
    "none": Look("none", contrast=1.0, saturation=1.0, gamma=1.0,
                 vignette_strength=0.0, grain=0.0, drift_pct=0.0),
}


def build_filter(look: Look, spec: VideoSpec, duration: float) -> str:
    """Compose the finishing chain.

    The drift is a slow crop that travels across the frame over the whole
    episode. Rendering slightly oversized first means the crop never runs
    out of pixels at the edges.
    """
    parts: list[str] = []

    if look.drift_pct > 0 and duration > 1:
        over = 1.0 + look.drift_pct / 100.0
        big_w = int(spec.width * over) // 2 * 2
        big_h = int(spec.height * over) // 2 * 2
        travel_x = big_w - spec.width
        travel_y = big_h - spec.height
        # A slow diagonal drift, easing at both ends so it never appears
        # to start or stop abruptly.
        ease = f"(1-cos(PI*min(t/{duration:.3f},1)))/2"
        parts.append(f"scale={big_w}:{big_h}:flags=lanczos")
        parts.append(
            f"crop={spec.width}:{spec.height}:"
            f"'{travel_x}*{ease}':'{travel_y}*(1-{ease})'"
        )

    if look.contrast != 1.0 or look.saturation != 1.0 or look.gamma != 1.0:
        parts.append(
            f"eq=contrast={look.contrast}:saturation={look.saturation}"
            f":gamma={look.gamma}"
        )

    if look.vignette_strength > 0:
        # PI/5 is a wide, soft falloff. Narrower angles look like a lens
        # fault rather than a deliberate grade.
        parts.append("vignette=angle=PI/5:mode=forward")

    if look.grain > 0:
        # Temporal grain only: static noise looks like a dirty sensor.
        parts.append(f"noise=alls={int(look.grain)}:allf=t")

    parts.append("format=yuv420p")
    return ",".join(parts)


def run(ep: Episode, m: Manifest, look_name: str = "clean",
        spec: VideoSpec | None = None) -> Path:
    """Apply the finishing pass in place, keeping the original as .raw.mp4."""
    from ..config import LONGFORM
    from ..voices.base import measure_media

    log = get(m.id, "finish")
    spec = spec or LONGFORM

    if look_name not in LOOKS:
        raise StageError(f"unknown look {look_name!r}; expected {sorted(LOOKS)}")
    look = LOOKS[look_name]
    if look.name == "none":
        log.info("no finishing pass requested")
        return ep.video

    if not ep.video.exists():
        raise StageError(f"no video at {ep.video}; run assemble first")

    before = measure_media(ep.video)
    raw = ep.out_dir / "video.raw.mp4"
    ep.video.replace(raw)

    run_ffmpeg([
        "-i", str(raw),
        "-vf", build_filter(look, spec, before),
        "-r", str(spec.fps),
        "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
        "-c:a", "copy",            # audio is already mastered; never re-encode
        "-movflags", "+faststart",
        str(ep.video),
    ])

    after = measure_media(ep.video)
    if abs(after - before) > 0.05:
        ep.video.unlink(missing_ok=True)
        raw.replace(ep.video)
        raise StageError(
            f"finishing pass changed duration by {abs(after-before):.3f}s "
            f"and was reverted. Every effect must be duration-preserving."
        )

    m.stage("assemble").data["look"] = look.name
    m.stage("assemble").data["finished"] = True
    log.info(f"look={look.name}, {after:.2f}s unchanged, "
             f"{ep.video.stat().st_size/1_048_576:.1f} MB")
    return ep.video
