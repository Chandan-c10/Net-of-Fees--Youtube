"""Vertical variant of the engineering profile.

Cropping a horizontal chart loses its axis labels, which are the content,
so Shorts are rendered natively at 1080x1920 with fewer data points and
much larger type -- architecture doc, section 12.
"""

from profiles.engineering import style

from . import visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "engineering_shorts"
DEFAULT_VOICE = "chandan_en"
HASHTAGS = (
    "#softwareengineering", "#algotrading", "#python", "#devlog",
    "#buildinpublic", "#postmortem",
)
