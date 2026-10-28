"""Vertical charts. Same data, redrawn for a phone.

Rules from the architecture doc, section 12: six data points or fewer,
labels at least 36px equivalent, double line weight, and safe margins that
clear YouTube's own UI overlay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from pipeline.errors import StageError
from profiles.trading import style
from profiles.trading.visuals import CHARGE_LABELS

MAX_POINTS = 6  # more is illegible at phone size


def supports(aspect: str) -> bool:
    return aspect == "9:16"


def available_keys() -> list[str]:
    return ["breakeven", "fee_stack", "drag_ladder", "title"]


def _figure(spec, ts):
    fig, ax = plt.subplots(
        figsize=(spec.width / style.DPI, spec.height / style.DPI), dpi=style.DPI
    )
    # 12% top and 22% bottom are dead zones under YouTube's Shorts overlay.
    fig.subplots_adjust(left=0.16, right=0.94, bottom=0.30, top=0.76)
    return fig, ax


def _finish(fig, ax, out: Path, ts):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(style.GRID)
        ax.spines[side].set_linewidth(2.0)  # thin lines vanish after compression
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=style.BACKGROUND)
    plt.close(fig)


def _breakeven(data, spec, ts, out, **kw):
    pts = data.get("breakeven_points")
    if pts is None:
        raise StageError("breakeven needs 'breakeven_points'")
    fig, ax = _figure(spec, ts)
    ax.axis("off")
    ax.text(0.5, 0.70, str(pts), color=style.ACCENT, fontsize=ts.title * 3.0,
            ha="center", va="center", fontweight="bold")
    ax.text(0.5, 0.46, "points", color=style.ACCENT, fontsize=ts.title,
            ha="center", va="center", fontweight="bold")
    ax.text(0.5, 0.26, "to break even", color=style.INK, fontsize=ts.subtitle,
            ha="center", va="center")
    _finish(fig, ax, out, ts)


def _fee_stack(data, spec, ts, out, **kw):
    charges = data.get("charges")
    if not charges:
        raise StageError("fee_stack needs 'charges'")
    items = [(CHARGE_LABELS.get(k, k), v) for k, v in charges.items()
             if k != "total" and v]
    items.sort(key=lambda kv: kv[1], reverse=True)
    items = items[:MAX_POINTS]
    fig, ax = _figure(spec, ts)
    ax.barh([k for k, _ in items], [v for _, v in items],
            color=style.COST, height=0.6)
    ax.invert_yaxis()
    ax.set_title(f"{style.money(charges['total'], 0)} in charges",
                 fontsize=ts.title, pad=30, loc="left")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: style.money(v, 0)))
    ax.grid(axis="y", visible=False)
    _finish(fig, ax, out, ts)


def _drag_ladder(data, spec, ts, out, **kw):
    ladder = (data.get("ladder") or [])[:MAX_POINTS]
    if not ladder:
        raise StageError("drag_ladder needs 'ladder'")
    fig, ax = _figure(spec, ts)
    xs = list(range(len(ladder)))
    drag = [r["drag_pct"] for r in ladder]
    ax.bar(xs, drag, color=[style.COST if d >= 20 else style.KEPT for d in drag],
           width=0.6)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{r['move_points']}" for r in ladder], fontsize=ts.tick)
    ax.set_xlabel("move in points", fontsize=ts.label)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title("Lost to charges", fontsize=ts.title, pad=30, loc="left")
    ax.grid(axis="x", visible=False)
    _finish(fig, ax, out, ts)


def _title(data, spec, ts, out, **kw):
    fig, ax = _figure(spec, ts)
    ax.axis("off")
    ax.text(0.5, 0.5, kw.get("headline") or data.get("title", style.CHANNEL),
            color=style.INK, fontsize=ts.title, ha="center", va="center",
            wrap=True, fontweight="bold")
    _finish(fig, ax, out, ts)


RENDERERS = {
    "breakeven": _breakeven,
    "fee_stack": _fee_stack,
    "drag_ladder": _drag_ladder,
    "title": _title,
}


def build_visual(scene, data: dict[str, Any], spec, out_path: Path) -> Path:
    key = (scene.visual_spec or {}).get("data_key") or "title"
    # Any long-form chart with no vertical twin falls back to the title
    # card rather than producing an illegible crop.
    fn = RENDERERS.get(key, _title)
    with plt.rc_context(style.rc_params(style.SHORTS_TYPE)):
        fn(data, spec, style.SHORTS_TYPE, out_path,
           headline=scene.text.split(".")[0][:60])
    if not out_path.exists():
        raise StageError(f"vertical renderer {key!r} produced no file")
    return out_path
