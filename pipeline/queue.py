"""What to make next.

A channel that runs by itself needs to know what the next episode is
about, and that is the one thing no amount of automation supplies. So
it comes from a list: `stories/QUEUE.md`, one title per line, which a
person adds to whenever they think of something.

That is deliberately the only input the channel needs. Everything else
-- writing, pictures, narration, rendering, posting, tidying up -- runs
without anyone. Running out of titles is the one way it stops, and it
says so loudly rather than quietly making nothing.

A title is taken as done when an episode already carries it, so the
same story is never made twice and the list needs no editing after a
run.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import EPISODES_DIR, ROOT
from .logging_setup import get

QUEUE_FILE = ROOT / "stories" / "QUEUE.md"

# A title only has to survive being matched against another title, so
# the comparison ignores everything that is not a word.
_WORDS = re.compile(r"[a-z0-9]+")


def _key(title: str) -> str:
    return " ".join(_WORDS.findall(title.lower()))


def titles(path: Path | None = None) -> list[str]:
    """Every title in the queue, in order.

    A title is a list item: a line beginning with "- ". Anything else
    is prose, a heading or a comment. That rule exists because the
    first version took every non-comment line, read the instructions at
    the top of the file as four stories, and would cheerfully have made
    a video called "One title per line".
    """
    path = path or QUEUE_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        title = line[2:].strip()
        if title and not title.startswith("~~"):   # ~~struck through~~
            out.append(title)
    return out


def _was_posted(manifest: dict) -> bool:
    """Whether this episode actually went out somewhere."""
    for row in manifest.get("platform_posts") or []:
        if row.get("status") == "posted" or row.get("external_id"):
            return True
    return False


def made(episodes_dir: Path | None = None) -> set[str]:
    """Every title that has actually been published.

    Read from the manifests rather than from a list of what has been
    done, because a list of what has been done is a second place for
    the truth to live and the two will disagree.

    A record is not a video. A draft that got as far as a script and
    stopped used to count here, so one abandoned test episode removed
    "The Jackal and the Drum" from the queue for good: the story was
    never made, never posted, and never offered again. Counting only
    what went out also makes a failed run self-healing -- tomorrow
    picks the same story up and finishes it, instead of stepping over
    it and leaving a hole in the channel.
    """
    episodes_dir = episodes_dir or EPISODES_DIR
    if not episodes_dir.exists():
        return set()
    import json
    done = set()
    for directory in episodes_dir.iterdir():
        manifest = directory / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        title = data.get("title", "")
        if title and _was_posted(data):
            # The published title carries a search suffix; the story's
            # own name is what the queue holds.
            done.add(_key(title.split("|")[0]))
    return done


def remaining(path: Path | None = None,
              episodes_dir: Path | None = None) -> list[str]:
    """Titles not yet made, in order."""
    done = made(episodes_dir)
    return [t for t in titles(path) if _key(t) not in done]


def next_title(path: Path | None = None,
               episodes_dir: Path | None = None) -> str | None:
    """The next story to make, or None when the list is spent."""
    left = remaining(path, episodes_dir)
    return left[0] if left else None


def episode_id_for(title: str, episodes_dir: Path | None = None) -> str:
    """A short, stable, filesystem-safe id for a title.

    Derived from the title rather than counted, so the same story asked
    for twice lands on the same episode instead of a second one.
    """
    import hashlib
    slug = "-".join(_WORDS.findall(title.lower())[:4]) or "story"
    digest = hashlib.sha256(_key(title).encode()).hexdigest()[:4]
    return f"{slug[:24]}-{digest}"


def report(path: Path | None = None,
           episodes_dir: Path | None = None) -> dict:
    """What is left, for the panel and for the scheduled run."""
    left = remaining(path, episodes_dir)
    all_titles = titles(path)
    log = get(None, "queue")
    if not all_titles:
        log.warning(f"no story queue at {path or QUEUE_FILE}")
    elif not left:
        log.warning("every story in the queue has been made; add more "
                    "titles or the channel stops")
    return {
        "total": len(all_titles),
        "remaining": len(left),
        "next": left[0] if left else None,
        "upcoming": left[:5],
        "exhausted": bool(all_titles) and not left,
    }
