"""Episode 1: what one F&O trade actually costs.

Tier A. Built entirely from published charge schedules, so it carries no
market-data licensing exposure at all -- architecture doc, section 17, Q3.

RATES MUST BE VERIFIED before publishing. They change with budgets and
circulars, and publishing a stale rate is exactly the kind of wrong number
section 14 forbids. Every rate below carries its basis in `source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import register

# Published statutory and exchange charges for equity F&O.
# VERIFY these against the current circulars before any publish.
RATE_CARD: dict[str, dict[str, object]] = {
    "brokerage_per_order": {
        "value": 20.0,
        "unit": "INR per executed order",
        "note": "flat-fee discount broker; verify your own broker's card",
        "source": "broker published tariff",
    },
    "stt_futures_sell": {
        "value": 0.0002,
        "unit": "fraction of sell turnover",
        "note": "STT on futures, sell side only",
        "source": "Securities Transaction Tax schedule",
    },
    "stt_options_sell": {
        "value": 0.001,
        "unit": "fraction of sell premium",
        "note": "STT on options, sell side, on premium",
        "source": "Securities Transaction Tax schedule",
    },
    "exchange_txn_futures": {
        "value": 0.0000173,
        "unit": "fraction of turnover",
        "note": "exchange transaction charge, futures",
        "source": "exchange circular",
    },
    "exchange_txn_options": {
        "value": 0.0003503,
        "unit": "fraction of premium turnover",
        "note": "exchange transaction charge, options, on premium",
        "source": "exchange circular",
    },
    "sebi_turnover": {
        "value": 0.000001,
        "unit": "fraction of turnover",
        "note": "SEBI turnover fee",
        "source": "SEBI circular",
    },
    "stamp_duty_futures": {
        "value": 0.00002,
        "unit": "fraction of buy turnover",
        "note": "stamp duty, buy side only",
        "source": "Indian Stamp Act schedule",
    },
    "stamp_duty_options": {
        "value": 0.00003,
        "unit": "fraction of buy premium",
        "note": "stamp duty, buy side only, on premium",
        "source": "Indian Stamp Act schedule",
    },
    "gst": {
        "value": 0.18,
        "unit": "fraction of (brokerage + exchange + sebi)",
        "note": "GST applies to services, not to STT or stamp duty",
        "source": "GST schedule",
    },
}


@dataclass
class Leg:
    """One side of a trade."""

    side: str  # "buy" | "sell"
    qty: int
    price: float
    lot_size: int = 1

    @property
    def turnover(self) -> float:
        return self.qty * self.lot_size * self.price


@dataclass
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange_txn
            + self.sebi_fee + self.stamp_duty + self.gst,
            2,
        )

    def as_dict(self) -> dict[str, float]:
        d = {k: round(v, 2) for k, v in asdict(self).items()}
        d["total"] = self.total
        return d


def compute_charges(
    entry: Leg, exit_: Leg, instrument: str = "options"
) -> ChargeBreakdown:
    """Charges for a round trip. Pure arithmetic over the rate card.

    STT and stamp duty are one-sided by law, which is the detail retail
    cost calculators most often get wrong.
    """
    if instrument not in ("options", "futures"):
        raise ValueError(f"instrument must be options or futures, got {instrument!r}")

    def rate(key: str) -> float:
        return float(RATE_CARD[key]["value"])  # type: ignore[arg-type]

    buy = entry if entry.side == "buy" else exit_
    sell = exit_ if exit_.side == "sell" else entry
    total_turnover = entry.turnover + exit_.turnover

    b = ChargeBreakdown()
    b.brokerage = rate("brokerage_per_order") * 2  # one per executed order

    if instrument == "options":
        b.stt = sell.turnover * rate("stt_options_sell")
        b.exchange_txn = total_turnover * rate("exchange_txn_options")
        b.stamp_duty = buy.turnover * rate("stamp_duty_options")
    else:
        b.stt = sell.turnover * rate("stt_futures_sell")
        b.exchange_txn = total_turnover * rate("exchange_txn_futures")
        b.stamp_duty = buy.turnover * rate("stamp_duty_futures")

    b.sebi_fee = total_turnover * rate("sebi_turnover")
    # GST applies to services only, never to STT or stamp duty.
    b.gst = (b.brokerage + b.exchange_txn + b.sebi_fee) * rate("gst")
    return b


@dataclass
class FeeDragRecipe:
    name: str = "fee_drag"
    tier: str = "A"
    title_template: str = "What one F&O trade really costs"
    scenarios: list[dict] = field(default_factory=list)

    def export(self, **params: object) -> dict:
        """Build the snapshot. Deterministic: no network, no database."""
        lot_size = int(params.get("lot_size", 75))  # type: ignore[arg-type]
        lots = int(params.get("lots", 1))  # type: ignore[arg-type]
        entry_premium = float(params.get("entry_premium", 100.0))  # type: ignore[arg-type]
        exit_premium = float(params.get("exit_premium", 110.0))  # type: ignore[arg-type]
        instrument = str(params.get("instrument", "options"))

        entry = Leg("buy", lots, entry_premium, lot_size)
        exit_ = Leg("sell", lots, exit_premium, lot_size)
        charges = compute_charges(entry, exit_, instrument)

        gross = (exit_premium - entry_premium) * lots * lot_size
        net = round(gross - charges.total, 2)
        drag_pct = round((charges.total / gross) * 100, 1) if gross else None

        # How large a move is needed just to break even on costs.
        breakeven_points = round(charges.total / (lots * lot_size), 2)

        ladder = []
        for move in (2, 5, 10, 20, 50):
            g = move * lots * lot_size
            c = compute_charges(
                Leg("buy", lots, entry_premium, lot_size),
                Leg("sell", lots, entry_premium + move, lot_size),
                instrument,
            )
            ladder.append({
                "move_points": move,
                "gross": round(g, 2),
                "charges": c.total,
                "net": round(g - c.total, 2),
                "drag_pct": round((c.total / g) * 100, 1) if g else None,
            })

        return {
            "recipe": self.name,
            "tier": self.tier,
            "instrument": instrument,
            "trade": {
                "lots": lots,
                "lot_size": lot_size,
                "quantity": lots * lot_size,
                "entry_premium": entry_premium,
                "exit_premium": exit_premium,
                "move_points": round(exit_premium - entry_premium, 2),
            },
            "gross_pnl": round(gross, 2),
            "charges": charges.as_dict(),
            "net_pnl": net,
            "drag_pct": drag_pct,
            "breakeven_points": breakeven_points,
            "ladder": ladder,
            "rate_card": RATE_CARD,
            "verify_before_publish": [
                "Confirm every rate in rate_card against the current circular",
                "Confirm your broker's own brokerage tariff",
            ],
        }


register(FeeDragRecipe())
