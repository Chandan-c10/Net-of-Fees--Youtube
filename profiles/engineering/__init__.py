"""Engineering profile: architecture, incidents and benchmarks.

Globally relevant and English-only. A developer audience accepts a
faceless channel and a synthetic voice without hesitation, so the trust
problem that affects finance-education content does not arise here.
"""

from . import style, visuals

__all__ = ["style", "visuals"]
PROFILE_NAME = "engineering"
DEFAULT_VOICE = "chandan_en"

# Global developer audience. Broad tags first, specific ones after --
# Instagram weights the leading tags most heavily.
HASHTAGS = (
    "#softwareengineering", "#algotrading", "#quant", "#python",
    "#backtesting", "#fintech", "#systemsdesign", "#tradingsystems",
    "#devlog", "#buildinpublic", "#distributedsystems", "#sre",
)
