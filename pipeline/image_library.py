"""Pictures kept so they can be used again.

Drawing every scene fresh is wasteful twice over. The free generator
refuses about half of what it is asked, so a fourteen-scene episode can
take three passes; and most scenes do not need a picture of their own.
A story returns to the same man on the same doorstep four times. Four
slightly different men on four slightly different doorsteps is worse
than one man seen four ways, not better -- the pictures are there to
give the ear something to look at, not to illustrate every clause.

So a picture drawn once is kept, with the prompt that produced it, and a
later scene asking for something close enough gets that picture back.
The library is shared across episodes: a second story about the same
village starts with pictures already in hand.

**What "close enough" means.** The prompts are compared as sets of
content words, and a scene reuses a picture when they overlap past a
threshold. That is deliberately blunt. It will not notice that "head
lowered, exhausted" and "face softening, eyes wet" are different
moments -- but it does not need to, because those prompts share little
enough of their wording to stay apart. What it catches is the case that
matters: the same subject, described the same way, asked for twice.

Set IMAGE_REUSE to 0 to draw everything fresh.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import ROOT
from .logging_setup import get

DEFAULT_DIR = ROOT / "state" / "images"


def library_dir() -> Path:
    """Where the pictures are kept.

    A function rather than a constant so a test can point it somewhere
    temporary. A suite that writes into the real library would both
    pollute it and read pictures it did not put there.
    """
    import os
    raw = os.getenv("IMAGE_LIBRARY_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_DIR


def index_path() -> Path:
    return library_dir() / "index.json"

# How much of two prompts must coincide before one picture serves both.
#
# Measured on a real fourteen-scene script, where two shots of the same
# monkey in the same village scored 0.43 and two shots of the same man
# in different emotional states scored 0.38. Those are close enough
# together that no threshold separates them reliably, which is why the
# blunter control below exists: a budget says how many new pictures an
# episode may draw, and everything past it reuses. Tune that, not this.
DEFAULT_THRESHOLD = 0.45

# Words that say how to shoot rather than what is in frame. Left out of
# the comparison: two prompts are not the same picture because both say
# "medium shot", and they are not different pictures because one does.
IGNORED = frozenset({
    "the", "and", "with", "into", "that", "this", "from", "over", "for",
    "a", "an", "of", "in", "on", "at", "his", "her", "their", "its",
    "shot", "close", "closeup", "medium", "wide", "extreme", "up",
    "cinematic", "still", "natural", "light", "shallow", "depth",
    "field", "muted", "colour", "color", "photographic", "text", "no",
    "photo", "photograph", "image", "picture", "view", "seen",
})


@dataclass(frozen=True)
class Entry:
    """One picture in the library, and what it was drawn for."""

    file: str
    prompt: str
    provider: str = ""
    license: str = ""
    source_url: str = ""
    creator: str = ""
    native: str = ""
    added: str = ""

    @property
    def path(self) -> Path:
        return library_dir() / self.file

    def credit(self) -> dict:
        return {"provider": self.provider, "license": self.license,
                "source_url": self.source_url, "creator": self.creator}


def enabled() -> bool:
    import os
    return os.getenv("IMAGE_REUSE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def max_new() -> int:
    """How many pictures one episode may draw. 0 means no limit.

    The honest control. A similarity score cannot tell "the same man,
    calmer" from "the same man, broken", so asking it to decide how many
    pictures a story needs is asking the wrong question. This asks the
    right one: how many do you want to wait for? Everything past the
    budget reuses the closest picture the episode already has, which is
    what "I do not need a new image for every scene" means in practice.
    """
    import os
    try:
        return max(0, int(os.getenv("IMAGE_MAX_NEW", "0")))
    except ValueError:
        return 0


def threshold() -> float:
    import os
    try:
        return max(0.0, min(1.0, float(os.getenv("IMAGE_REUSE_THRESHOLD",
                                                 str(DEFAULT_THRESHOLD)))))
    except ValueError:
        return DEFAULT_THRESHOLD


def words(prompt: str) -> set[str]:
    """The content words of a prompt, for comparison."""
    return {w for w in re.findall(r"[a-z]{3,}", prompt.lower())
            if w not in IGNORED}


def similarity(one: str, two: str) -> float:
    """How much two prompts coincide, from 0 to 1.

    Jaccard rather than a count, so a long prompt and a short one that
    happen to share five words are not called the same picture.
    """
    a, b = words(one), words(two)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load() -> list[Entry]:
    index = index_path()
    if not index.exists():
        return []
    try:
        raw = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        entry = Entry(**{k: v for k, v in row.items()
                         if k in Entry.__dataclass_fields__})
        # An index entry whose file has gone is not an error; the
        # library is a cache and the directory is safe to empty.
        if entry.path.exists():
            out.append(entry)
    return out


def save(entries: list[Entry]) -> None:
    library_dir().mkdir(parents=True, exist_ok=True)
    index_path().write_text(
        json.dumps([e.__dict__ for e in entries], indent=2),
        encoding="utf-8")


def closest(prompt: str, entries: list[Entry]) -> tuple[Entry, float] | None:
    """The nearest stored picture, however far off it is.

    Used only once an episode has spent its budget, and only against
    pictures from that same episode, where "however far off" still means
    a picture from this story.
    """
    best, score = None, -1.0
    for entry in entries:
        value = similarity(prompt, entry.prompt)
        if value > score:
            best, score = entry, value
    return (best, max(0.0, score)) if best is not None else None


def find(prompt: str, entries: list[Entry] | None = None,
         limit: float | None = None) -> tuple[Entry, float] | None:
    """The best stored picture for this prompt, if one is close enough."""
    if not enabled():
        return None
    bar = threshold() if limit is None else limit
    best, score = None, 0.0
    for entry in (entries if entries is not None else load()):
        value = similarity(prompt, entry.prompt)
        if value > score:
            best, score = entry, value
    if best is not None and score >= bar:
        return best, score
    return None


def _name_for(prompt: str) -> str:
    import hashlib
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16] + ".png"


def add(prompt: str, picture: Path, credit: dict | None = None,
        native: str = "") -> Entry:
    """Keep a drawn picture so a later scene can use it."""
    library_dir().mkdir(parents=True, exist_ok=True)
    credit = credit or {}
    entry = Entry(
        file=_name_for(prompt), prompt=prompt.strip(),
        provider=credit.get("provider", ""),
        license=credit.get("license", ""),
        source_url=credit.get("source_url", ""),
        creator=credit.get("creator", ""),
        native=native,
        added=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    shutil.copy2(picture, entry.path)
    entries = [e for e in load() if e.file != entry.file]
    entries.append(entry)
    save(entries)
    return entry


def reuse(entry: Entry, out: Path) -> Path:
    """Put a stored picture where a scene expects to find it.

    Copied rather than linked: the episode directory should stay
    self-contained, so emptying the library never breaks an episode that
    has already been rendered.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(entry.path, out)
    return out


def usage() -> dict:
    """What the library holds, for the settings page."""
    entries = load()
    total = sum(e.path.stat().st_size for e in entries if e.path.exists())
    return {"pictures": len(entries), "mb": round(total / 1e6, 1)}


def forget_all(log=None) -> int:
    """Empty the library. Returns how many went.

    Nothing is lost that cannot be drawn again, and every episode keeps
    its own copies, so this is safe in a way that deleting an episode's
    pictures is not.
    """
    log = log or get(None, "images")
    entries = load()
    for entry in entries:
        entry.path.unlink(missing_ok=True)
    index_path().unlink(missing_ok=True)
    log.info(f"library emptied, {len(entries)} pictures removed")
    return len(entries)
