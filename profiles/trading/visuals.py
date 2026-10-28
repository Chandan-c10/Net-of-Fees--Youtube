"""Charts for the trading profile, rendered from the snapshot.

The chart is the content: no stock footage, no AI images. Every figure is
a pure function of data.json, so re-rendering an old episode from its
frozen snapshot reproduces the same image.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless: no display needed, works identically on CI
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from pipeline.errors import StageError

from . import style

RENDERERS: dict[str, Callable[..., None]] = {}

# This profile's bar charts can draw themselves in. The motion stage checks
# this flag before attempting a build-in.
SUPPORTS_REVEAL = True


def renderer(key: str) -> Callable:
    def wrap(fn: Callable) -> Callable:
        RENDERERS[key] = fn
        return fn
    return wrap


def supports(aspect: str) -> bool:
    """Long-form only. Vertical lives in the trading_shorts profile."""
    return aspect == "16:9"


def available_keys() -> list[str]:
    return sorted(RENDERERS)


# ---- helpers ----------------------------------------------------------


# Captions are composited into the lower band of every frame, so the plot
# area must stop short of it or the axis labels sit under the caption.
CAPTION_BAND = {"16:9": 0.20, "9:16": 0.30}


def _figure(spec, type_scale):
    w = spec.width / style.DPI
    h = spec.height / style.DPI
    fig, ax = plt.subplots(figsize=(w, h), dpi=style.DPI)
    fig.subplots_adjust(
        left=0.10, right=0.96,
        bottom=CAPTION_BAND[spec.aspect], top=0.86,
    )
    return fig, ax


def _finish(fig, ax, out_path: Path, type_scale, footer: bool = True) -> None:
    if footer:
        fig.text(
            0.008, 0.012, f"{style.CHANNEL}  ·  {style.DISCLAIMER}",
            color=style.INK_DIM, fontsize=type_scale.footer, ha="left", va="bottom",
        )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(style.GRID)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # bbox_inches is deliberately left at None: tight bounding would crop
    # the reserved caption band and change the output resolution.
    fig.savefig(out_path, facecolor=style.BACKGROUND, bbox_inches=None)
    plt.close(fig)


def _money_axis(ax, decimals: int = 0) -> None:
    ax.yaxis.set_major_formatter(
        FuncFormatter(lambda v, _: style.money(v, decimals))
    )


# ---- renderers --------------------------------------------------------

CHARGE_LABELS = {
    "brokerage": "Brokerage",
    "stt": "STT",
    "exchange_txn": "Exchange",
    "sebi_fee": "SEBI fee",
    "stamp_duty": "Stamp duty",
    "gst": "GST",
}


@renderer("title")
def title_card(data: dict[str, Any], spec, type_scale, out_path: Path, **kw) -> None:
    """Opening card. Scene 1 states the finding; this frames it."""
    fig, ax = _figure(spec, type_scale)
    ax.axis("off")
    headline = kw.get("headline") or data.get("title") or style.CHANNEL
    sub = kw.get("subtitle") or ""
    ax.text(0.5, 0.58, headline, color=style.INK, fontsize=type_scale.title * 1.5,
            ha="center", va="center", fontweight="bold", wrap=True)
    if sub:
        ax.text(0.5, 0.40, sub, color=style.INK_DIM, fontsize=type_scale.subtitle,
                ha="center", va="center")
    _finish(fig, ax, out_path, type_scale)


@renderer("fee_stack")
def fee_stack(data: dict[str, Any], spec, type_scale, out_path: Path,
              annotate: list[str] | None = None, reveal: float = 1.0, **kw) -> None:
    """Every charge on one round trip, largest first."""
    charges = data.get("charges")
    if not charges:
        raise StageError("fee_stack needs a 'charges' object in the snapshot")
    items = [(CHARGE_LABELS.get(k, k), v) for k, v in charges.items()
             if k != "total" and v]
    if not items:
        raise StageError("fee_stack found no non-zero charges")
    items.sort(key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in items]
    full = [v for _, v in items]
    # A partial reveal grows the bars; the axis stays fixed so nothing
    # jumps as the chart settles.
    values = [v * reveal for v in full]
    highlight = {CHARGE_LABELS.get(a, a) for a in (annotate or [])}
    colors = [style.ACCENT if lb in highlight else style.COST for lb in labels]

    fig, ax = _figure(spec, type_scale)
    bars = ax.barh(labels, values, color=colors, height=0.62)
    ax.invert_yaxis()
    ax.set_title(f"One trade, {style.money(charges['total'], 2)} in charges", loc="left", pad=22)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: style.money(v, 0)))
    ax.grid(axis="y", visible=False)
    span = max(full) or 1
    if reveal >= 0.999:
        for bar, v in zip(bars, full, strict=True):
            ax.text(v + span * 0.015, bar.get_y() + bar.get_height() / 2,
                    style.money(v, 2), color=style.INK, va="center",
                    fontsize=type_scale.annotation)
    ax.set_xlim(0, span * 1.22)
    _finish(fig, ax, out_path, type_scale)


@renderer("gross_vs_net")
def gross_vs_net(data: dict[str, Any], spec, type_scale, out_path: Path, **kw) -> None:
    """What you made against what you kept."""
    gross = data.get("gross_pnl")
    net = data.get("net_pnl")
    total = (data.get("charges") or {}).get("total")
    if gross is None or net is None or total is None:
        raise StageError("gross_vs_net needs gross_pnl, net_pnl and charges.total")

    fig, ax = _figure(spec, type_scale)
    ax.bar(["Gross profit"], [gross], color=style.NEUTRAL, width=0.45)
    ax.bar(["What you keep"], [net], color=style.KEPT, width=0.45)
    ax.bar(["What you keep"], [total], bottom=[net], color=style.COST, width=0.45,
           label="Charges")
    drag = data.get("drag_pct")
    subtitle = f"charges took {drag}%" if drag is not None else "after charges"
    ax.set_title(f"{style.money(gross)} gross, {style.money(net)} kept — {subtitle}",
                 loc="left", pad=22)
    _money_axis(ax)
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right")
    for x, v in ((0, gross), (1, net)):
        ax.text(x, v, f"  {style.money(v, 2)}", color=style.INK, ha="center",
                va="bottom", fontsize=type_scale.annotation)
    _finish(fig, ax, out_path, type_scale)


@renderer("drag_ladder")
def drag_ladder(data: dict[str, Any], spec, type_scale, out_path: Path,
                reveal: float = 1.0, **kw) -> None:
    """How cost drag falls away as the move gets bigger.

    This is the chart that carries the episode: a small win is mostly fees.
    """
    ladder = data.get("ladder")
    if not ladder:
        raise StageError("drag_ladder needs a 'ladder' list in the snapshot")
    moves = [r["move_points"] for r in ladder]
    drag = [r["drag_pct"] for r in ladder]
    xs = list(range(len(moves)))

    fig, ax = _figure(spec, type_scale)
    colors = [style.COST if d is not None and d >= 20 else style.KEPT for d in drag]
    shown = [(d or 0) * reveal for d in drag]
    bars = ax.bar(xs, shown, color=colors, width=0.56)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{m} pt" for m in moves])
    ax.set_xlabel("Size of the move in premium")
    ax.set_ylabel("Share of gross lost to charges")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title("A small win is mostly charges", loc="left", pad=22)
    ax.grid(axis="x", visible=False)
    top = max(d for d in drag if d is not None)
    if reveal >= 0.999:
        for bar, d in zip(bars, drag, strict=True):
            if d is None:
                continue
            ax.text(bar.get_x() + bar.get_width() / 2, d + top * 0.03,
                    f"{d:.0f}%", color=style.INK, ha="center",
                    fontsize=type_scale.annotation)
    ax.set_ylim(0, top * 1.2)
    _finish(fig, ax, out_path, type_scale)


@renderer("breakeven")
def breakeven(data: dict[str, Any], spec, type_scale, out_path: Path, **kw) -> None:
    """The move needed just to cover costs. The episode's hook."""
    points = data.get("breakeven_points")
    if points is None:
        raise StageError("breakeven needs 'breakeven_points' in the snapshot")
    qty = (data.get("trade") or {}).get("quantity")
    total = (data.get("charges") or {}).get("total")

    fig, ax = _figure(spec, type_scale)
    ax.axis("off")
    ax.text(0.5, 0.62, f"{points} points", color=style.ACCENT,
            fontsize=type_scale.title * 2.4, ha="center", va="center",
            fontweight="bold")
    ax.text(0.5, 0.40, "before you have made a single rupee",
            color=style.INK, fontsize=type_scale.subtitle * 1.1,
            ha="center", va="center")
    if qty and total:
        ax.text(0.5, 0.27,
                f"{qty} qty  ·  {style.money(total, 2)} of charges to cover",
                color=style.INK_DIM, fontsize=type_scale.label, ha="center", va="center")
    _finish(fig, ax, out_path, type_scale)


# ---- entry point ------------------------------------------------------


def build_visual(scene, data: dict[str, Any], spec, out_path: Path,
                 reveal: float | None = None) -> Path:
    """Render one scene's visual. Called by the charts stage.

    A scene with no chart key gets the title card, so a script can narrate
    without a figure and still produce a frame.
    """
    key = (scene.visual_spec or {}).get("data_key") or "title"
    if key not in RENDERERS:
        raise StageError(
            f"scene {scene.index} asks for chart {key!r}, which the "
            f"{PROFILE_NAME!r} profile does not implement. "
            f"Available: {available_keys()}"
        )
    type_scale = style.SHORTS_TYPE if spec.aspect == "9:16" else style.LONGFORM_TYPE
    kwargs: dict[str, Any] = {}
    if reveal is not None and key not in ("title", "breakeven"):
        kwargs["reveal"] = reveal
    if scene.visual_spec.get("annotate"):
        kwargs["annotate"] = scene.visual_spec["annotate"]
    if key == "title":
        kwargs["headline"] = data.get("title") or scene.text.split(".")[0][:70]
    with plt.rc_context(style.rc_params(type_scale)):
        RENDERERS[key](data, spec, type_scale, out_path, **kwargs)
    if not out_path.exists():
        raise StageError(f"renderer {key!r} produced no file at {out_path}")
    return out_path


PROFILE_NAME = "trading"
