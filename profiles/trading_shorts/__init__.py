"""Vertical variant of the trading profile.

A cropped horizontal chart is unreadable on a phone, because the axis
labels are the content. Shorts therefore render natively at 1080x1920
with fewer data points and much larger type -- architecture doc, section 12.
"""

from profiles.trading import style

from . import visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "trading_shorts"
DEFAULT_VOICE = "chandan_en"
