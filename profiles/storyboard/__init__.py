"""Storyboard profile: narration over moving stills.

For formats where the picture carries the frame -- folklore, history,
what-if explainers -- rather than a chart. Pictures come from
`sources/scene_NN.png`; a typographic card stands in until they do.

Globally relevant and English-first. Nothing here is specific to a
subject, which is the point: the subject lives in the recipe and the
pictures.
"""

from . import style, visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "storyboard"
DEFAULT_VOICE = "chandan_en"

# Moving stills, not held stills. assemble dispatches on this.
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

HASHTAGS = (
    "#panchatantra", "#moralstories", "#storiesforkids",
    "#bedtimestories", "#kidsstories", "#shortstories",
    "#moralstory", "#englishstories", "#animalstories",
    "#storytime", "#fables", "#storiesinenglish",
)

TITLE_SUFFIX = "Panchatantra Stories for Kids | Moral Story in English"
STORY_SOURCE = "Panchatantra"

# 27 = Education. A narrated explainer is education, not entertainment,
# and the category is a separate field from the tags.
YOUTUBE_CATEGORY_ID = "27"
LANGUAGE = "en"

TREND_KEYWORDS = (
    "history", "ancient", "archive", "discovery", "empire", "ocean",
    "space", "map", "language", "ruins", "artefact", "manuscript",
    "expedition", "mystery", "forgotten", "civilisation", "migration",
    "climate", "fossil", "shipwreck",
)
