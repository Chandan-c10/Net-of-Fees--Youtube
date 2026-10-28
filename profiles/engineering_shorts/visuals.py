"""Vertical engineering charts. Same incident data, redrawn for a phone.

Six data points or fewer, labels at least 36px equivalent, double line
weight, and margins that clear YouTube's own Shorts overlay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from pipeline.errors import StageError
from profiles.engineering import style
from profiles.engineering.visuals import SEVERITY_COLOR

MAX_POINTS = 6


def supports(aspect: str) -> bool:
    return aspect == "9:16"


def available_keys() -> list[str]:
    return ["detection_gap", "by_layer", "by_detection", "severity_split", "title"]


def _figure(spec, ts):
    fig, ax = plt.subplots(
        figsize=(spec.width / style.DPI, spec.height / style.DPI), dpi=style.DPI
    )
    # 12% top and 22% bottom are dead zones under the Shorts UI.
    fig.subplots_adjust(left=0.20, right=0.93, bottom=0.30, top=0.76)
    return fig, ax


def _finish(fig, ax, out: Path):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(style.GRID)
        ax.spines[side].set_linewidth(2.0)  # thin lines vanish after compression
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=style.BACKGROUND)
    plt.close(fig)


def _detection_gap(data, spec, ts, out, **kw):
    incidents = data.get("incidents")
    if not incidents:
        raise StageError("detection_gap needs 'incidents'")
    # Only the longest-hidden few: more is illegible at phone size.
    worst = sorted(incidents, key=lambda i: -int(i["days_hidden"]))[:MAX_POINTS]
    worst.reverse()
    days = [int(i["days_hidden"]) for i in worst]
    colors = [SEVERITY_COLOR.get(i["severity"], style.FIXED) for i in worst]

    fig, ax = _figure(spec, ts)
    ax.barh(range(len(days)), days, color=colors, height=0.6)
    ax.set_yticks(range(len(days)))
    ax.set_yticklabels([i["layer"] for i in worst], fontsize=ts.tick)
    ax.set_xlabel("days hidden", fontsize=ts.label)
    ax.set_title(f"Median {data.get('median_days_hidden')} days",
                 fontsize=ts.title, pad=30, loc="left")
    ax.grid(axis="y", visible=False)
    _finish(fig, ax, out)


def _counts(key: str, title: str):
    def render(data, spec, ts, out, **kw):
        counts = data.get(key)
        if not counts:
            raise StageError(f"{key} is missing from the snapshot")
        items = [(k, v) for k, v in counts.items() if v][:MAX_POINTS]
        fig, ax = _figure(spec, ts)
        ax.barh([k.replace("_", " ") for k, _ in items], [v for _, v in items],
                color=style.FIXED, height=0.55)
        ax.invert_yaxis()
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_title(title, fontsize=ts.title, pad=30, loc="left")
        ax.grid(axis="y", visible=False)
        _finish(fig, ax, out)
    return render


def _title(data, spec, ts, out, **kw):
    fig, ax = _figure(spec, ts)
    ax.axis("off")
    ax.text(0.5, 0.5, kw.get("headline") or data.get("title", style.CHANNEL),
            color=style.INK, fontsize=ts.title, ha="center", va="center",
            wrap=True, fontweight="bold")
    _finish(fig, ax, out)


RENDERERS = {
    "detection_gap": _detection_gap,
    "by_layer": _counts("by_layer", "Where they lived"),
    "by_detection": _counts("by_detection", "How they were found"),
    "severity_split": _counts("by_severity", "How bad"),
    "title": _title,
}


def build_visual(scene, data: dict[str, Any], spec, out_path: Path,
                 reveal: float | None = None) -> Path:
    key = (scene.visual_spec or {}).get("data_key") or "title"
    # A long-form chart with no vertical twin falls back to the title card
    # rather than producing an illegible crop.
    fn = RENDERERS.get(key, _title)
    with plt.rc_context(style.rc_params(style.SHORTS_TYPE)):
        fn(data, spec, style.SHORTS_TYPE, out_path,
           headline=scene.text.split(".")[0][:60])
    if not out_path.exists():
        raise StageError(f"vertical renderer {key!r} produced no file")
    return out_path
