"""Trading profile: charts rendered from cost and market data.

The first profile, not the point of the system. The core is topic-agnostic
-- architecture doc, section 1.
"""

from . import style, visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "trading"
DEFAULT_VOICE = "chandan_en"

# Indian retail market audience. No advisory tags -- nothing implying
# calls, tips or targets, per the SEBI boundary in section 14.
HASHTAGS = (
    "#optionstrading", "#fno", "#nifty", "#tradingcosts", "#brokerage",
    "#stockmarketindia", "#tradingeducation", "#riskmanagement",
    "#derivatives", "#tradingpsychology",
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
