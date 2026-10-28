"""Burn captions by compositing with Pillow, not with FFmpeg's subtitles filter.

Homebrew's ffmpeg 9 formula ships without libass, and Windows builds vary
between 'essentials' and 'full'. Depending on a particular build is a
portability landmine, so captions are drawn here instead: one frame per
cue, each showing that cue's text over its scene's chart.

Shorts are watched muted, so burned-in captions are never optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import VideoSpec
from ..errors import StageError
from ..platform_support import bold_font

# Safe margins. YouTube overlays its own UI across the lower band of a
# Short, so captions sit well above the bottom edge there.
BOTTOM_MARGIN_FRAC = {"16:9": 0.055, "9:16": 0.20}
SIDE_MARGIN_FRAC = 0.07
CAPTION_HEIGHT_FRAC = {"16:9": 0.052, "9:16": 0.042}
OUTLINE_PX = {"16:9": 3, "9:16": 4}

TEXT_FILL = (255, 255, 255, 255)
OUTLINE_FILL = (0, 0, 0, 255)
BAND_FILL = (0, 0, 0, 140)


@dataclass(frozen=True)
class Cue:
    scene: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)


def _load_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = bold_font()
    if path is not None:
        try:
            return ImageFont.truetype(str(path), px)
        except OSError:
            pass
    # Pillow's built-in bitmap font: tiny and fixed-size, but it keeps the
    # pipeline working on a bare system rather than failing the render.
    return ImageFont.load_default()


def _wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font, max_px: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(trial, font=font) > max_px:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [text]


def render_cue_frame(chart: Path, text: str, spec: VideoSpec, out: Path) -> Path:
    """Composite one caption over a chart. Idempotent for a given input."""
    if not chart.exists():
        raise StageError(f"no chart at {chart}")

    base = Image.open(chart).convert("RGBA")
    if base.size != (spec.width, spec.height):
        base = base.resize((spec.width, spec.height), Image.LANCZOS)

    if text.strip():
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        px = max(14, int(spec.height * CAPTION_HEIGHT_FRAC[spec.aspect]))
        font = _load_font(px)
        side = int(spec.width * SIDE_MARGIN_FRAC)
        max_text_w = spec.width - 2 * side
        lines = _wrap_to_width(draw, text.strip(), font, max_text_w)

        line_h = int(px * 1.35)
        block_h = line_h * len(lines)
        bottom = spec.height - int(spec.height * BOTTOM_MARGIN_FRAC[spec.aspect])
        top = bottom - block_h

        pad = int(px * 0.5)
        widest = max(draw.textlength(ln, font=font) for ln in lines)
        band = [
            int(spec.width / 2 - widest / 2) - pad, top - pad,
            int(spec.width / 2 + widest / 2) + pad, bottom + pad,
        ]
        draw.rounded_rectangle(band, radius=pad, fill=BAND_FILL)

        stroke = OUTLINE_PX[spec.aspect]
        for i, line in enumerate(lines):
            y = top + i * line_h
            draw.text(
                (spec.width / 2, y), line, font=font, fill=TEXT_FILL,
                anchor="ma", stroke_width=stroke, stroke_fill=OUTLINE_FILL,
            )
        base = Image.alpha_composite(base, layer)

    out.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out, "PNG", optimize=True)
    return out


def build_frames(cues: list[Cue], chart_for_scene, spec: VideoSpec,
                 out_dir: Path) -> list[tuple[Path, float]]:
    """One frame per cue, paired with that cue's duration."""
    if not cues:
        raise StageError("no cues to render")
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, float]] = []
    for i, cue in enumerate(cues, start=1):
        frame = render_cue_frame(
            chart_for_scene(cue.scene), cue.text, spec,
            out_dir / f"frame_{i:04d}.png",
        )
        frames.append((frame, cue.duration))
    return frames


def render_caption_strip(text: str, spec: VideoSpec, out: Path) -> Path:
    """The caption band alone, on transparency.

    The chart path composites captions onto each still, which works
    because the still does not change. Over moving footage the background
    changes every frame, so the band is rendered once with an alpha
    channel and overlaid by FFmpeg for the span of its cue instead.
    """
    from PIL import Image, ImageDraw

    aspect = "9:16" if spec.height > spec.width else "16:9"
    size = max(18, int(spec.height * CAPTION_HEIGHT_FRAC.get(aspect, 0.030)))
    font = _load_font(size)

    canvas = Image.new("RGBA", (spec.width, spec.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    max_px = int(spec.width * 0.86)
    lines = _wrap_to_width(draw, text, font, max_px)

    line_h = int(size * 1.34)
    block_h = line_h * len(lines)
    pad = int(size * 0.62)
    margin = int(spec.height * BOTTOM_MARGIN_FRAC.get(aspect, 0.055))
    top = spec.height - margin - block_h - pad

    widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
    band_w = int(widest) + pad * 2
    left = (spec.width - band_w) // 2
    # Opaque enough to stay legible over a bright frame, not a black bar.
    draw.rounded_rectangle(
        [left, top, left + band_w, top + block_h + pad],
        radius=int(size * 0.34), fill=(0, 0, 0, 170),
    )
    y = top + pad // 2
    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(((spec.width - width) / 2, y), line,
                  font=font, fill=(255, 255, 255, 255))
        y += line_h

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out
