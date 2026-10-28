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
