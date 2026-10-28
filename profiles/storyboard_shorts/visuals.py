"""The vertical cut of the storyboard profile.

Same pictures, same cards, 9:16. Separate because a profile renders one
aspect so its type scale can be tuned for it -- a caption sized for a
1920-wide frame is unreadable at 1080 wide on a phone.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.config import VideoSpec

from ..storyboard import visuals as landscape
from ..storyboard.visuals import available_keys, binds_data_keys, fallback_card, source_for

__all__ = ["available_keys", "binds_data_keys", "build_visual",
           "fallback_card", "source_for", "supports"]

ASPECTS = ("9:16",)


def supports(aspect: str) -> bool:
    return aspect in ASPECTS


def build_visual(scene, data: dict, spec: VideoSpec, out: Path,
                 **kw: object) -> Path:
    return landscape.build_visual(scene, data, spec, out, **kw)
