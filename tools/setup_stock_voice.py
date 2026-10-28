#!/usr/bin/env python3
"""Create a voice directory for a Kokoro stock voice.

    python tools/setup_stock_voice.py am_michael

Writes consent.md, meta.json and a lexicon seeded from your existing
voices, so the interim voice pronounces your vocabulary correctly from
the first render.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.voices.kokoro import KNOWN_VOICES

CONSENT = """# Voice provenance - {name}

**This is not a clone of any real person.**

Source: Kokoro-82M, voice `{voice}` ({description}).
Licence: Apache 2.0 - commercial use permitted.

No consent from a speaker is required, because no speaker's voice was
copied. The model generates this voice; it does not imitate anyone.

## Why this voice exists

An interim voice, used until a personal reference recording is available.
Output from it may be published.

## The rule this does not bend

Any voice that *is* a real person's requires that person's documented
permission. A voice taken from the internet - a creator, a public figure,
anyone - is impersonation under YouTube policy and carries
personality-rights exposure. See the architecture doc, section 14.
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: setup_stock_voice.py <voice>\n", file=sys.stderr)
        print("Available:", file=sys.stderr)
        for n, d in KNOWN_VOICES.items():
            print(f"  {n:12s} {d}", file=sys.stderr)
        return 1

    voice = sys.argv[1]
    if voice not in KNOWN_VOICES:
        print(f"unknown voice {voice!r}. Run with no arguments to list them.",
              file=sys.stderr)
        return 1

    name = f"stock_{voice}"
    d = ROOT / "voices" / name
    (d / "reference").mkdir(parents=True, exist_ok=True)

    (d / "consent.md").write_text(
        CONSENT.format(name=name, voice=voice,
                       description=KNOWN_VOICES[voice]), encoding="utf-8"
    )
    (d / "meta.json").write_text(json.dumps({
        "name": name,
        "engine": "kokoro",
        "voice": voice,
        "description": KNOWN_VOICES[voice],
        "language": "hi-IN" if voice.startswith("h") else "en",
        "licence": "Apache-2.0",
        "is_clone_of_real_person": False,
        "interim": True,
    }, indent=2), encoding="utf-8")

    # Reuse an existing lexicon so pronunciation is right immediately.
    seed = ROOT / "voices" / "chandan_en" / "lexicon.json"
    lex = json.loads(seed.read_text(encoding="utf-8")) if seed.exists() else {}
    (d / "lexicon.json").write_text(json.dumps(lex, indent=2), encoding="utf-8")

    print(f"Created voices/{name}/")
    print(f"  voice       {voice} - {KNOWN_VOICES[voice]}")
    print(f"  lexicon     {len(lex)} terms")
    print("\nUse it:")
    print(f"  python run.py <id> --voice-engine kokoro --voice {name} "
          f"--stage voice --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
