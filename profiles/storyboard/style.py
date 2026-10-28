"""Look for the moving-stills format.

Quieter than the chart profiles: when the picture carries the frame, the
typography should stay out of its way.
"""

from __future__ import annotations

INK = "#F4F1EA"
INK_DIM = "#8D8579"
BACKGROUND = "#14110E"
PAPER = BACKGROUND
ACCENT = "#D9A05B"
GRID = "#2A251E"

DPI = 100
CHANNEL = "Field Notes"

DISCLAIMER = (
    "Narration and imagery are generated. Sources are listed in the "
    "description. Not professional advice."
)

SERIES = "Field Notes"


def money(value: float, decimals: int = 0) -> str:
    """No currency symbol: this profile is not about one country's money."""
    return f"{value:,.{decimals}f}"


def rc_params(type_scale) -> dict[str, object]:
    """Matplotlib defaults.

    This profile renders pictures rather than charts, so nothing here
    draws axes today. The stages treat rc_params as part of the profile
    contract, and a recipe that does want a chart should get this
    profile's colours rather than matplotlib's.
    """
    return {
        "figure.facecolor": BACKGROUND,
        "axes.facecolor": BACKGROUND,
        "savefig.facecolor": BACKGROUND,
        "text.color": INK,
        "axes.labelcolor": INK_DIM,
        "axes.edgecolor": GRID,
        "xtick.color": INK_DIM,
        "ytick.color": INK_DIM,
        "grid.color": GRID,
        "font.size": getattr(type_scale, "body", 16),
        "figure.dpi": DPI,
    }
