"""Pictures for the moving-stills format.

Unlike the chart profiles, this one does not draw the frame -- it takes
it. A scene's picture comes from `sources/scene_NN.png`, which an image
generator or a person fills.

When a source is missing it renders a typographic card rather than
failing. That is the difference between a profile you can run today and
one that is blocked until an image pipeline exists: the episode renders,
you see the timing and the narration working, and the cards are replaced
by real pictures as they arrive.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.config import VideoSpec
from pipeline.platform_support import bold_font

from . import style

# Rendered larger than the frame so kenburns has real pixels to crop
# into; a card cropped from a frame-sized image goes soft.
OVERSCAN = 1.3

ASPECTS = ("16:9",)


def supports(aspect: str) -> bool:
    """One aspect per profile, so the type scale can be tuned for it.
    The vertical cut lives in storyboard_shorts."""
    return aspect in ASPECTS


def available_keys() -> list[str]:
    """Named for the contract; nothing here dispatches on the key."""
    return ["picture", "title", "quote"]


def binds_data_keys() -> bool:
    """False: the frame is a picture, not a view of the data.

    The chart profiles promise that a scene's data_key resolves to
    something they can draw, and Gate 1 checks it -- that is what catches
    an invented scene. This profile draws the same thing whatever the key
    says, so it has no such promise to keep, and enforcing one would
    reject every recipe not written for it.
    """
    return False


def _font(px: int):
    from PIL import ImageFont
    path = bold_font()
    if path:
        try:
            return ImageFont.truetype(str(path), px)
        except OSError:
            pass
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_px: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_px or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fallback_card(text: str, spec: VideoSpec, out: Path,
                  index: int = 0) -> Path:
    """A typographic stand-in for a picture that has not arrived.

    Deliberately plain. It should read as an intentional title card, not
    as a broken image, so an episode rendered before the pictures exist
    is still watchable.
    """
    width = int(spec.width * OVERSCAN)
    height = int(spec.height * OVERSCAN)
    canvas = Image.new("RGB", (width, height), style.PAPER)
    draw = ImageDraw.Draw(canvas)

    # A faint rule, offset per scene so consecutive cards are not
    # identical frames.
    y = int(height * (0.26 + 0.04 * (index % 4)))
    draw.line([(int(width * 0.1), y), (int(width * 0.3), y)],
              fill=style.ACCENT, width=max(2, height // 400))

    size = max(28, int(height * 0.055))
    font = _font(size)
    lines = _wrap(draw, text.strip(), font, int(width * 0.78))[:5]
    line_h = int(size * 1.35)
    top = (height - line_h * len(lines)) // 2
    for i, line in enumerate(lines):
        draw.text((int(width * 0.11), top + i * line_h), line,
                  font=font, fill=style.INK)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def source_for(out: Path, scene) -> Path:
    """Where this scene's supplied picture would be.

    Derived from the output path rather than taken as an argument, because
    `build_visual` is the contract every profile implements and charts.py
    does not pass the episode. `out` is <episode>/visuals/scene_NN.png,
    so the sources directory is its sibling.
    """
    return out.parent.parent / "sources" / f"scene_{scene.index:02d}.png"


def build_visual(scene, data: dict, spec: VideoSpec, out: Path,
                 **_: object) -> Path:
    """This scene's picture: the supplied source, or a card.

    A source is re-saved at whatever resolution it arrived with, not
    downscaled to the frame, so kenburns has the most pixels available to
    crop into.
    """
    source = source_for(out, scene)
    if source.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(source) as im:
                im.convert("RGB").save(out)
            return out
        except OSError as exc:
            # A truncated download should not stop the episode; the card
            # stands in and the log says which picture to replace.
            from pipeline.logging_setup import get
            get(None, "visuals").warning(
                f"{source.name} could not be read ({exc}); using a card")
    return fallback_card(scene.text, spec, out, scene.index)
