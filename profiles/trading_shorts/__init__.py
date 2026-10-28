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
HASHTAGS = (
    "#optionstrading", "#fno", "#nifty", "#tradingcosts",
    "#stockmarketindia", "#tradingeducation",
)

# YouTube file this under a category, which is a separate field
# from the tags and the hashtags. Education -- this is finance education, not advice.
YOUTUBE_CATEGORY_ID = "27"
# Helps YouTube offer auto-translated titles and captions.
LANGUAGE = "en"

# Subject words for the trend scan. Ordinary words, not
# hashtags: a headline carries "software" and "engineering"
# separately, so matching against "#softwareengineering" found
# nothing at all across 18 live stories.
TREND_KEYWORDS = ('brokerage', 'capital', 'charges', 'costs', 'delivery', 'exchange', 'expiry', 'fees', 'fund', 'futures', 'gains', 'gst', 'index', 'intraday', 'leverage', 'margin', 'market', 'mutual', 'nifty', 'options', 'portfolio', 'returns', 'rupee', 'sebi', 'sensex', 'stt', 'tax', 'turnover')
