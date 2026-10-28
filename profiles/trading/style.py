"""Brand for the trading profile.

One file is what makes every video look like one channel. Changing a
colour here changes every future render, which is why it lives in git
rather than in a settings screen.
"""

from __future__ import annotations

from dataclasses import dataclass

# Channel: Net of Fees. Dark, high-contrast, built to read on a phone.
INK = "#F2F4F7"
INK_DIM = "#9AA4B2"
BACKGROUND = "#0D1117"
PANEL = "#161B22"
GRID = "#242C38"

# Semantic colours. Cost is always warm, retained value always cool.
COST = "#E5534B"
KEPT = "#3FB950"
NEUTRAL = "#58A6FF"
ACCENT = "#D29922"

SERIES = (NEUTRAL, COST, KEPT, ACCENT, "#A371F7", "#39C5CF")


@dataclass(frozen=True)
class TypeScale:
    """Point sizes at the profile's own render DPI."""

    title: int
    subtitle: int
    label: int
    tick: int
    annotation: int
    footer: int


# Long-form sits on a monitor or a phone; Shorts are phone-only, so every
# size steps up and the data density steps down.
LONGFORM_TYPE = TypeScale(title=34, subtitle=20, label=17, tick=15, annotation=16, footer=13)
SHORTS_TYPE = TypeScale(title=46, subtitle=30, label=28, tick=24, annotation=26, footer=20)

DPI = 100
CURRENCY = "₹"  # INR
DISCLAIMER = "Educational only. Not investment advice."
CHANNEL = "Net of Fees"


def rc_params(type_scale: TypeScale) -> dict[str, object]:
    """matplotlib settings for this profile. Applied per figure, not globally."""
    return {
        "figure.facecolor": BACKGROUND,
        "axes.facecolor": BACKGROUND,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_DIM,
        "axes.titlecolor": INK,
        "axes.titlesize": type_scale.title,
        "axes.labelsize": type_scale.label,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK_DIM,
        "ytick.color": INK_DIM,
        "xtick.labelsize": type_scale.tick,
        "ytick.labelsize": type_scale.tick,
        "text.color": INK,
        "font.size": type_scale.label,
        "savefig.facecolor": BACKGROUND,
        "legend.frameon": False,
        "legend.labelcolor": INK,
        "legend.fontsize": type_scale.label,
    }


def money(value: float, decimals: int = 0) -> str:
    """Indian-format currency with thousands separators."""
    whole = f"{abs(value):,.{decimals}f}"
    sign = "-" if value < 0 else ""
    return f"{sign}{CURRENCY}{whole}"
