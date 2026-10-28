"""Vertical sibling of the storyboard profile.

Posts to the same account: a vertical cut is the same channel's content
in a different shape, which is what channels.key_for assumes unless a
profile says otherwise.
"""

from ..storyboard import style
from . import visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "storyboard_shorts"
DEFAULT_VOICE = "chandan_en"
MOTION = "kenburns"

# Instagram weights the leading tags most heavily.
HASHTAGS = (
    "#storytime", "#history", "#didyouknow", "#folklore", "#explainer",
    "#reels", "#documentary", "#mythology", "#whatif", "#facts",
)

YOUTUBE_CATEGORY_ID = "27"
LANGUAGE = "en"

TREND_KEYWORDS = (
    "history", "ancient", "archive", "discovery", "empire", "ocean",
    "space", "map", "language", "ruins", "mystery", "forgotten",
)
