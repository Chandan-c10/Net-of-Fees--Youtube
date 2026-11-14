"""What to make next, and what counts as already made.

The queue is the one thing the channel needs from a person. Getting
"already made" wrong is quiet and permanent: a story drops out of the
list and is never offered again.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import queue

QUEUE = """# Story queue

## Panchatantra

- The Jackal and the Drum
- The Brahmin and the Mongoose
- The Talkative Tortoise
"""


def _queue_file(tmp_path: Path) -> Path:
    path = tmp_path / "QUEUE.md"
    path.write_text(QUEUE, encoding="utf-8")
    return path


def _episode(root: Path, eid: str, title: str, posted: bool,
             stages=("export", "script", "scrub")) -> None:
    directory = root / eid
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": eid,
        "title": title,
        "stages": {s: {"done": True} for s in stages},
        "platform_posts": ([{"platform": "youtube", "status": "posted",
                             "external_id": "abc123"}] if posted else []),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest),
                                             encoding="utf-8")


def test_a_published_story_is_not_offered_again(tmp_path):
    episodes = tmp_path / "episodes"
    _episode(episodes, "e1",
             "The Brahmin and the Mongoose | Panchatantra Stories for Kids",
             posted=True)
    left = queue.remaining(_queue_file(tmp_path), episodes)
    assert "The Brahmin and the Mongoose" not in left
    assert left[0] == "The Jackal and the Drum"


def test_a_draft_that_never_went_out_stays_in_the_queue(tmp_path):
    """The defect this file was written for.

    One abandoned test episode -- a script and nothing else, backed up
    with everything else -- removed "The Jackal and the Drum" from the
    queue for good. It was never made, never posted, and never offered
    again.
    """
    episodes = tmp_path / "episodes"
    _episode(episodes, "e1", "The Jackal and the Drum", posted=False)
    left = queue.remaining(_queue_file(tmp_path), episodes)
    assert "The Jackal and the Drum" in left, (
        "a record is not a video; an unpublished draft must not retire "
        "the story"
    )
    assert queue.next_title(_queue_file(tmp_path),
                            episodes) == "The Jackal and the Drum"


def test_a_failed_run_is_picked_up_again(tmp_path):
    """Self-healing: tomorrow finishes what today started."""
    episodes = tmp_path / "episodes"
    _episode(episodes, "e1", "The Jackal and the Drum", posted=False,
             stages=("export", "script", "scrub", "voice", "images",
                     "assemble"))
    assert queue.next_title(_queue_file(tmp_path),
                            episodes) == "The Jackal and the Drum"


def test_an_external_id_alone_counts_as_posted(tmp_path):
    """Some rows carry the id without the word."""
    episodes = tmp_path / "episodes"
    directory = episodes / "e1"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "title": "The Jackal and the Drum",
        "platform_posts": [{"platform": "youtube", "external_id": "xyz"}],
    }), encoding="utf-8")
    assert "The Jackal and the Drum" not in queue.remaining(
        _queue_file(tmp_path), episodes)


def test_a_failed_post_does_not_retire_the_story(tmp_path):
    episodes = tmp_path / "episodes"
    directory = episodes / "e1"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "title": "The Jackal and the Drum",
        "platform_posts": [{"platform": "youtube", "status": "failed",
                            "error": "quota"}],
    }), encoding="utf-8")
    assert "The Jackal and the Drum" in queue.remaining(
        _queue_file(tmp_path), episodes)


def test_prose_in_the_queue_file_is_not_a_story(tmp_path):
    path = tmp_path / "QUEUE.md"
    path.write_text("# Story queue\n\nOne title per line.\n\n- A real one\n",
                    encoding="utf-8")
    assert queue.titles(path) == ["A real one"]
