#!/usr/bin/env python3
"""Generate the reference recording script.

A clone sounds exactly as human as the audio it was built from. The
material below is chosen to cover three things a short sample misses:
the vocabulary that will actually be narrated, the full range of your
delivery, and unscripted speech, which is where natural rhythm comes from.

    python tools/make_voice_script.py > voices/chandan_en/RECORDING.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NUMBERS = [
    "Your option has to move zero point eight three points before you break even.",
    "Charges took forty one percent of the gross on a two point move.",
    "Sixty two rupees and twenty paise, on a single round trip.",
    "The median bug ran for twenty seven days before anyone noticed.",
    "Seven of the eight produced wrong output or put money at risk.",
    "One lot is seventy five quantity at a hundred rupees of premium.",
    "Two thousand and twenty six. Nineteen thirty hours. Zero point zero five seconds.",
    "Three lakh rupees of turnover, and about one and a half crore across the year.",
]

VOCABULARY = [
    "F and O charges are not one number. They are six separate line items.",
    "S T T applies on the sell side only. Stamp duty applies on the buy side only.",
    "G S T is charged on the services, not on the tax.",
    "SEBI publishes a turnover fee that almost nobody includes in their model.",
    "The N S E lot size changes, and a stale lot size silently breaks position sizing.",
    "Nifty expiry, strike selection, premium decay, implied volatility, open interest.",
    "Slippage, drawdown, expectancy, and the backtest that produced none of them.",
    "Idempotency, a webhook, a race condition, latency, and a partial fill.",
    "The broker A P I accepted the order, then the retry sent it a second time.",
]

PROSODY = [
    ("A finding, slower and with weight",
     "That is the number worth sitting with. Not eight bugs. Twenty seven days."),
    ("An explanation, even and measured",
     "The concat demuxer ignores the final entry's duration, so the last frame "
     "is dropped unless you list it twice."),
    ("A caveat, softer",
     "My numbers are Indian F and O. Yours will differ. The arithmetic does not."),
    ("A question you then answer",
     "So why did the tests miss it? Because the test asserted the function "
     "returned. It never asserted what it returned."),
    ("A list, with rhythm",
     "Integer division returned zero. A clock disagreed with another clock. "
     "And a retry sent an order the broker had already accepted."),
    ("An admission, flat and unforced",
     "I expect the next eight bugs to hide just as well as these did."),
]

UNSCRIPTED = [
    "Explain to a friend what your trading system actually does, start to finish.",
    "Describe the worst bug you have shipped and how you found it.",
    "Explain why a backtest is usually wrong, without preparing first.",
    "Talk through what you would build differently if you started again.",
    "Describe what you were doing when you realised something was broken.",
]


def episode_excerpts(limit: int = 2) -> list[tuple[str, str]]:
    """Pull real script text, since that is the exact register to train on."""
    out: list[tuple[str, str]] = []
    episodes = ROOT / "episodes"
    if not episodes.exists():
        return out
    for d in sorted(episodes.iterdir()):
        script = d / "script.md"
        if not script.exists():
            continue
        text = script.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        body = [ln for ln in text.splitlines()
                if ln.strip() and not ln.startswith(("#", "["))]
        if body:
            out.append((title, "\n".join(body)))
        if len(out) >= limit:
            break
    return out


def main() -> int:
    parts: list[str] = []
    w = parts.append

    w("# Voice reference recording\n")
    w("Target: **45 to 60 minutes, one session, one room, one mic position.**\n")
    w("Read this top to bottom. Do not stop to fix mistakes. If you fumble a ")
    w("line, pause for a second and say it again, and keep both takes.\n")

    w("\n## Before you start\n")
    for item in [
        "Quiet room with soft furnishings. No fan, no AC, no fridge hum.",
        "Mic 15 to 20 cm away, slightly off-axis so plosives do not hit it.",
        "Mark your position. Do not move the mic or your chair for the whole session.",
        "Record WAV at 48 kHz. Not MP3.",
        "**No noise gate, no compression, no EQ while recording.** Keep it raw.",
        "One session. Splitting across days gives a set that cannot be combined.",
    ]:
        w(f"- {item}")

    w("\n## Block 1 - warm up (5 min, discarded)\n")
    w("Talk about your day. Anything. This block is thrown away; it exists so ")
    w("your voice is loose before the material that counts.\n")

    w("\n## Block 2 - your real scripts (15 min)\n")
    w("The most valuable block: this is the exact register the clone will be ")
    w("used in. Read naturally, as if explaining it, not announcing it.\n")
    excerpts = episode_excerpts()
    if excerpts:
        for title, body in excerpts:
            w(f"\n### {title}\n")
            w(body)
    else:
        w("\n_No episode scripts found yet. Run `run.py new` first, or read any ")
        w("two of your own written pieces aloud instead._\n")

    w("\n\n## Block 3 - vocabulary and numbers (10 min)\n")
    w("Every term that will be narrated, said in a sentence rather than as a ")
    w("list. Numbers in as many forms as possible.\n")
    for line in VOCABULARY:
        w(f"\n{line}")
    w("")
    for line in NUMBERS:
        w(f"\n{line}")

    w("\n\n## Block 4 - range (10 min)\n")
    w("Same words, different delivery. A clone trained on one register can ")
    w("only produce one register.\n")
    for how, line in PROSODY:
        w(f"\n**{how}**\n")
        w(f"> {line}\n")

    w("\n## Block 5 - unscripted (15 min)\n")
    w("Put the script down. This block is where natural rhythm comes from, ")
    w("and it is the difference between a clone that reads and one that talks.\n")
    for prompt in UNSCRIPTED:
        w(f"- {prompt}")

    w("\n## What not to do\n")
    for item in [
        "Do not use a radio-announcer voice. Talk the way you talk.",
        "Do not edit the breaths out afterwards. They are most of why speech sounds alive.",
        "Do not record on a phone speakerphone.",
        "Do not record in a kitchen or bathroom. Hard surfaces add reflections you cannot remove.",
        "Do not change mic distance between blocks.",
    ]:
        w(f"- {item}")

    w("\n## Afterwards\n")
    w("Save the raw files to `voices/<name>/reference/`. Back them up somewhere ")
    w("that is neither git nor only your laptop: re-recording months later under ")
    w("different conditions gives a set that cannot be combined with this one.\n")

    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
