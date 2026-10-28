"""Fee arithmetic, hand-verified.

Section 2's Tier B rule: recompute a sample by hand and confirm the engine
agrees. These are those hand computations.
"""

from __future__ import annotations

import pytest

from pipeline.recipes.fee_drag import RATE_CARD, Leg, compute_charges


def test_brokerage_is_per_order_not_per_trade():
    c = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75))
    assert c.brokerage == pytest.approx(40.0)  # 20 x 2 orders


def test_stt_is_sell_side_only():
    """Hand check: sell turnover 110 x 75 = 8250, x 0.001 = 8.25."""
    c = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75))
    assert c.stt == pytest.approx(8250 * 0.001)


def test_stamp_duty_is_buy_side_only():
    """Hand check: buy turnover 100 x 75 = 7500, x 0.00003 = 0.225."""
    c = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75))
    assert c.stamp_duty == pytest.approx(7500 * 0.00003)


def test_gst_excludes_stt_and_stamp_duty():
    """GST applies to services only. This is the detail most calculators miss."""
    c = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75))
    expected = (c.brokerage + c.exchange_txn + c.sebi_fee) * 0.18
    assert c.gst == pytest.approx(expected)
    assert c.gst < (c.brokerage + c.stt + c.stamp_duty) * 0.18


def test_total_is_the_sum_of_its_parts():
    c = compute_charges(Leg("buy", 2, 250, 50), Leg("sell", 2, 240, 50))
    parts = (c.brokerage + c.stt + c.exchange_txn
             + c.sebi_fee + c.stamp_duty + c.gst)
    assert c.total == pytest.approx(round(parts, 2))


def test_futures_and_options_use_different_rates():
    opt = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75), "options")
    fut = compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75), "futures")
    assert opt.stt != fut.stt
    assert opt.exchange_txn != fut.exchange_txn


def test_unknown_instrument_is_rejected():
    with pytest.raises(ValueError, match="options or futures"):
        compute_charges(Leg("buy", 1, 100, 75), Leg("sell", 1, 110, 75), "crypto")


def test_cost_drag_falls_as_the_move_grows():
    """The episode's actual finding, asserted."""
    from pipeline.recipes import get
    ladder = get("fee_drag").export()["ladder"]
    drags = [r["drag_pct"] for r in ladder]
    assert drags == sorted(drags, reverse=True)
    assert drags[0] > 30, "a small win should be dominated by charges"


def test_rate_card_carries_its_source():
    """Every rate must state where it came from, for the verify step."""
    for key, entry in RATE_CARD.items():
        assert entry.get("source"), f"{key} has no source"
        assert entry.get("unit"), f"{key} has no unit"
