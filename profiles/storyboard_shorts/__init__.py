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

# What the pictures look like. Read by pipeline/images.py, which falls
# back to a photographic style when a profile says nothing -- and that
# default fought the story contract, which was asking for a storybook
# illustration at the same time. One source now, and it is this one.
IMAGE_STYLE = (
    "3D animated film still, stylised character animation, "
    "soft global illumination, warm saturated colour, "
    "rounded shapes, shallow depth of field"
)

# Instagram weights the leading tags most heavily.
# Ordered by how people actually search for this, most specific first.
# YouTube shows the first three above the title, so those three have to
# earn their place: they name the collection, the kind of story, and
# who it is for.
HASHTAGS = (
    "#panchatantra", "#moralstories", "#storiesforkids",
    "#bedtimestories", "#kidsstories", "#shortstories",
    "#moralstory", "#englishstories", "#animalstories",
    "#storytime", "#fables", "#storiesinenglish",
)

# Appended to the title, because a title that says only "The Monkey and
# the Crocodile" competes with every other telling of it. The suffix is
# what somebody actually types.
TITLE_SUFFIX = "Panchatantra Stories for Kids | Moral Story in English"

# Named in the description. Traditional tales are public domain, and
# saying which collection a story comes from is both honest and the
# phrase people search.
STORY_SOURCE = "Panchatantra"

YOUTUBE_CATEGORY_ID = "27"
LANGUAGE = "en"

TREND_KEYWORDS = (
    "history", "ancient", "archive", "discovery", "empire", "ocean",
    "space", "map", "language", "ruins", "mystery", "forgotten",
)
