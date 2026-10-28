"""Trading profile: charts rendered from cost and market data.

The first profile, not the point of the system. The core is topic-agnostic
-- architecture doc, section 1.
"""

from . import style, visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "trading"
DEFAULT_VOICE = "chandan_en"
