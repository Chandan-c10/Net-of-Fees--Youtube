"""Brand for the engineering profile.

A separate channel from trading, so a separate look: cooler, more
technical, less financial. One channel per profile -- architecture doc,
section 10.
"""

from __future__ import annotations

from profiles.trading.style import TypeScale, rc_params as _rc  # shared shape

INK = "#E6EDF3"
INK_DIM = "#8B949E"
BACKGROUND = "#0B0F14"
PANEL = "#111820"
GRID = "#1E2730"

# Semantic: a problem is warm, a fix is cool, the finding is the accent.
PROBLEM = "#F0883E"
FIXED = "#2F81F7"
GOOD = "#3FB950"
ACCENT = "#A371F7"

SERIES = (FIXED, PROBLEM, GOOD, ACCENT, "#39C5CF", "#DB61A2")

LONGFORM_TYPE = TypeScale(title=32, subtitle=19, label=16, tick=14,
                          annotation=15, footer=12)
SHORTS_TYPE = TypeScale(title=44, subtitle=28, label=26, tick=22,
                        annotation=24, footer=18)

DPI = 100
CHANNEL = "Net of Fees"
DISCLAIMER = "Engineering notes. Not investment advice."
CURRENCY = "₹"


def money(value: float, decimals: int = 0) -> str:
    whole = f"{abs(value):,.{decimals}f}"
    return f"{'-' if value < 0 else ''}{CURRENCY}{whole}"


def rc_params(type_scale: TypeScale) -> dict[str, object]:
    """Same structure as the trading profile, this profile's colours."""
    params = _rc(type_scale)
    params.update({
        "figure.facecolor": BACKGROUND,
        "axes.facecolor": BACKGROUND,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_DIM,
        "axes.titlecolor": INK,
        "grid.color": GRID,
        "xtick.color": INK_DIM,
        "ytick.color": INK_DIM,
        "text.color": INK,
        "savefig.facecolor": BACKGROUND,
        "legend.labelcolor": INK,
        "font.family": "monospace",   # a code audience reads mono as native
    })
    return params
