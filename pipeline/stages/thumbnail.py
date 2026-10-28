"""Stage 8: thumbnail candidates.

Readable at 168x94, which is the smallest size YouTube serves. If it only
works full-size it does not work. Four words maximum, one focal number
-- architecture doc, section 26.

Three candidates are produced; the operator chooses. These are among the
highest-leverage judgments in the system and the ones a model is least
reliable at.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import LONGFORM
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import bold_font
from .export import load_snapshot

MAX_WORDS = 4
# YouTube's smallest served size. Every candidate is checked at this size.
MIN_PREVIEW = (168, 94)


def _font(px: int):
    path = bold_font()
    if path is not None:
        try:
            return ImageFont.truetype(str(path), px)
        except OSError:
            pass
    return ImageFont.load_default()


def _headlines(data: dict) -> list[tuple[str, str]]:
    """Candidate (focal, supporting) pairs, drawn from the snapshot only."""
    out: list[tuple[str, str]] = []
    if data.get("breakeven_points") is not None:
        out.append((f"{data['breakeven_points']}", "points to break even"))
    if data.get("drag_pct") is not None:
        out.append((f"{data['drag_pct']}%", "of your profit, gone"))
    ladder = data.get("ladder") or []
    if ladder and ladder[0].get("drag_pct") is not None:
        out.append((f"{ladder[0]['drag_pct']}%", "on a small win"))
    if not out:
        out.append(("?", data.get("title", "")[:28]))
    return out[:3]


def _render(focal: str, support: str, out: Path, style) -> Path:
    w, h = LONGFORM.width, LONGFORM.height
    img = Image.new("RGB", (w, h), style.BACKGROUND)
    d = ImageDraw.Draw(img)

    # A thick accent rule gives the channel a recognisable silhouette at
    # thumbnail size, where text alone is unreadable.
    d.rectangle([0, 0, int(w * 0.016), h], fill=style.ACCENT)

    focal_font = _font(int(h * 0.34))
    support_font = _font(int(h * 0.085))
    d.text((w * 0.10, h * 0.30), focal, font=focal_font,
           fill=style.ACCENT, anchor="lm")
    words = support.split()
    if len(words) > MAX_WORDS + 2:
        support = " ".join(words[: MAX_WORDS + 2])
    d.text((w * 0.10, h * 0.62), support, font=support_font,
           fill=style.INK, anchor="lm")
    d.text((w * 0.10, h * 0.86), style.CHANNEL, font=_font(int(h * 0.05)),
           fill=style.INK_DIM, anchor="lm")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out


def check_small_legibility(path: Path) -> bool:
    """Downscale to YouTube's smallest size and confirm contrast survives.

    Crude but it catches the common failure: a thumbnail designed at full
    size that turns to mush in a feed.
    """
    small = Image.open(path).convert("L").resize(MIN_PREVIEW, Image.LANCZOS)
    darkest, brightest = small.getextrema()
    return (brightest - darkest) >= 90


def run(ep: Episode, m: Manifest, profile) -> list[Path]:
    log = get(m.id, "thumbnail")
    data = load_snapshot(ep)
    style = profile.style

    made: list[Path] = []
    for i, (focal, support) in enumerate(_headlines(data), start=1):
        p = _render(focal, support, ep.thumbnail(i), style)
        legible = check_small_legibility(p)
        if not legible:
            log.info(f"thumb_{i} has weak contrast at {MIN_PREVIEW[0]}x{MIN_PREVIEW[1]}")
        made.append(p)
        log.debug(f"thumb_{i}: {focal} / {support} legible={legible}")

    if not made:
        raise StageError("no thumbnail candidates produced")
    m.mark_done("thumbnail", count=len(made),
                files=[p.name for p in made],
                legible=[check_small_legibility(p) for p in made])
    log.info(f"{len(made)} candidates; operator picks one at gate 2")
    return made
