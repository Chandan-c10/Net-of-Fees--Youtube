"""Charts for the engineering profile.

Incident data and architecture, not market data. Every figure is a pure
function of the snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from matplotlib.ticker import MaxNLocator

from pipeline.errors import StageError

from . import style

RENDERERS: dict[str, Callable[..., None]] = {}
CAPTION_BAND = {"16:9": 0.20, "9:16": 0.30}

SEVERITY_LABEL = {
    "cosmetic": "cosmetic",
    "degraded": "degraded",
    "wrong_output": "wrong output",
    "money_at_risk": "money at risk",
}
SEVERITY_COLOR = {
    "cosmetic": style.INK_DIM,
    "degraded": style.FIXED,
    "wrong_output": style.PROBLEM,
    "money_at_risk": style.ACCENT,
}


def renderer(key: str) -> Callable:
    def wrap(fn: Callable) -> Callable:
        RENDERERS[key] = fn
        return fn
    return wrap


def supports(aspect: str) -> bool:
    return aspect == "16:9"


def available_keys() -> list[str]:
    return sorted(RENDERERS)


def _figure(spec, ts):
    fig, ax = plt.subplots(
        figsize=(spec.width / style.DPI, spec.height / style.DPI), dpi=style.DPI
    )
    fig.subplots_adjust(left=0.12, right=0.95,
                        bottom=CAPTION_BAND[spec.aspect], top=0.86)
    return fig, ax


def _finish(fig, ax, out: Path, ts, footer: bool = True):
    if footer:
        fig.text(0.008, 0.012, f"{style.CHANNEL}  ·  {style.DISCLAIMER}",
                 color=style.INK_DIM, fontsize=ts.footer, ha="left", va="bottom")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(style.GRID)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=style.BACKGROUND)
    plt.close(fig)


@renderer("title")
def title_card(data, spec, ts, out: Path, **kw):
    fig, ax = _figure(spec, ts)
    ax.axis("off")
    ax.text(0.5, 0.60, kw.get("headline") or data.get("title", style.CHANNEL),
            color=style.INK, fontsize=ts.title * 1.4, ha="center", va="center",
            fontweight="bold", wrap=True)
    if data.get("system"):
        ax.text(0.5, 0.40, data["system"], color=style.INK_DIM,
                fontsize=ts.subtitle, ha="center", va="center")
    _finish(fig, ax, out, ts)


@renderer("detection_gap")
def detection_gap(data, spec, ts, out: Path, **kw):
    """How long each bug ran before anyone noticed. The real finding."""
    incidents = data.get("incidents")
    if not incidents:
        raise StageError("detection_gap needs 'incidents'")
    ordered = sorted(incidents, key=lambda i: int(i["days_hidden"]))
    days = [int(i["days_hidden"]) for i in ordered]
    labels = [i["title"][:38] for i in ordered]
    colors = [SEVERITY_COLOR.get(i["severity"], style.FIXED) for i in ordered]

    fig, ax = _figure(spec, ts)
    bars = ax.barh(range(len(days)), days, color=colors, height=0.6)
    ax.set_yticks(range(len(days)))
    ax.set_yticklabels(labels, fontsize=ts.tick)
    ax.set_xlabel("days live before anyone noticed")
    median = data.get("median_days_hidden")
    ax.set_title(f"Median {median} days before detection", loc="left", pad=22)
    ax.grid(axis="y", visible=False)
    span = max(days) or 1
    for bar, d in zip(bars, days, strict=True):
        ax.text(d + span * 0.015, bar.get_y() + bar.get_height() / 2, f"{d}d",
                color=style.INK, va="center", fontsize=ts.annotation)
    ax.set_xlim(0, span * 1.15)
    if median:
        ax.axvline(median, color=style.INK_DIM, linestyle="--", linewidth=1.2)
    _finish(fig, ax, out, ts)


@renderer("by_layer")
def by_layer(data, spec, ts, out: Path, **kw):
    counts = data.get("by_layer")
    if not counts:
        raise StageError("by_layer needs 'by_layer'")
    names = list(counts)
    values = [counts[n] for n in names]
    fig, ax = _figure(spec, ts)
    ax.bar(names, values, color=style.FIXED, width=0.55)
    ax.set_ylabel("incidents")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_title("Where the bugs actually lived", loc="left", pad=22)
    ax.grid(axis="x", visible=False)
    for i, v in enumerate(values):
        ax.text(i, v + max(values) * 0.03, str(v), ha="center",
                color=style.INK, fontsize=ts.annotation)
    ax.set_ylim(0, max(values) * 1.2)
    _finish(fig, ax, out, ts)


@renderer("by_detection")
def by_detection(data, spec, ts, out: Path, **kw):
    """How each one was found. Usually not by the test suite."""
    counts = data.get("by_detection")
    if not counts:
        raise StageError("by_detection needs 'by_detection'")
    names = list(counts)
    values = [counts[n] for n in names]
    colors = [style.GOOD if n == "test" else style.PROBLEM for n in names]
    fig, ax = _figure(spec, ts)
    ax.barh(names, values, color=colors, height=0.55)
    ax.invert_yaxis()
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    pct = data.get("caught_by_tests_pct")
    ax.set_title(f"Tests caught {pct}% of them", loc="left", pad=22)
    ax.grid(axis="y", visible=False)
    for i, v in enumerate(values):
        ax.text(v + max(values) * 0.02, i, str(v), va="center",
                color=style.INK, fontsize=ts.annotation)
    ax.set_xlim(0, max(values) * 1.18)
    _finish(fig, ax, out, ts)


@renderer("severity_split")
def severity_split(data, spec, ts, out: Path, **kw):
    counts = data.get("by_severity")
    if not counts:
        raise StageError("severity_split needs 'by_severity'")
    items = [(SEVERITY_LABEL.get(k, k), v, SEVERITY_COLOR.get(k, style.FIXED))
             for k, v in counts.items() if v]
    fig, ax = _figure(spec, ts)
    ax.bar([i[0] for i in items], [i[1] for i in items],
           color=[i[2] for i in items], width=0.55)
    ax.set_ylabel("incidents")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    serious, total = data.get("serious"), data.get("total")
    ax.set_title(f"{serious} of {total} produced wrong output or risked money",
                 loc="left", pad=22)
    ax.grid(axis="x", visible=False)
    _finish(fig, ax, out, ts)


@renderer("architecture")
def architecture(data, spec, ts, out: Path, **kw):
    """A layered system diagram, drawn from the layers in the data."""
    layers = list(data.get("by_layer") or {})
    if not layers:
        raise StageError("architecture needs 'by_layer' to name the layers")
    counts = data["by_layer"]

    fig, ax = _figure(spec, ts)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, max(3, len(layers)) + 0.5)
    ax.axis("off")
    ax.set_title("Where each layer failed", loc="left", pad=22)

    for i, name in enumerate(reversed(layers)):
        y = i + 0.3
        n = counts[name]
        hot = n >= max(counts.values())
        box = FancyBboxPatch(
            (1.2, y), 7.0, 0.62, boxstyle="round,pad=0.06",
            linewidth=1.6, edgecolor=style.PROBLEM if hot else style.GRID,
            facecolor=style.PANEL,
        )
        ax.add_patch(box)
        ax.text(1.5, y + 0.31, name, color=style.INK, fontsize=ts.label,
                va="center", fontweight="bold")
        ax.text(8.0, y + 0.31, f"{n} incident{'s' if n != 1 else ''}",
                color=style.PROBLEM if hot else style.INK_DIM,
                fontsize=ts.annotation, va="center", ha="right")
        if i < len(layers) - 1:
            ax.add_patch(FancyArrowPatch(
                (4.7, y + 0.66), (4.7, y + 0.94),
                arrowstyle="-|>", mutation_scale=14,
                color=style.INK_DIM, linewidth=1.2,
            ))
    _finish(fig, ax, out, ts)


def build_visual(scene, data: dict[str, Any], spec, out_path: Path) -> Path:
    key = (scene.visual_spec or {}).get("data_key") or "title"
    if key not in RENDERERS:
        raise StageError(
            f"scene {scene.index} asks for chart {key!r}, which the "
            f"'engineering' profile does not implement. Available: {available_keys()}"
        )
    ts = style.SHORTS_TYPE if spec.aspect == "9:16" else style.LONGFORM_TYPE
    kwargs: dict[str, Any] = {}
    if key == "title":
        kwargs["headline"] = data.get("title") or scene.text.split(".")[0][:70]
    with plt.rc_context(style.rc_params(ts)):
        RENDERERS[key](data, spec, ts, out_path, **kwargs)
    if not out_path.exists():
        raise StageError(f"renderer {key!r} produced no file")
    return out_path


PROFILE_NAME = "engineering"
