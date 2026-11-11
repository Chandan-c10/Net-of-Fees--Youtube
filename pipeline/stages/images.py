"""Stage: fetch the picture each scene asked for.

Runs before `charts`, because charts is what turns a source picture into
the frame that gets moved. Without this the storyboard profiles rendered
title cards -- correct behaviour for a missing picture, and not what
anyone wants to watch.

Skipped, not failed, for a profile that draws its own frames: a chart is
a view of the data and has no prompt to generate from.
"""

from __future__ import annotations

from ..config import spec_for
from ..errors import StageError
from ..images import enabled, run_for
from ..logging_setup import get
from ..manifest import Episode, Manifest


def wants_pictures(profile) -> bool:
    """Whether this profile's frames come from pictures.

    `MOTION = "kenburns"` is already the declaration that a profile
    carries its frame with a photograph rather than drawing it, and
    assemble dispatches on the same attribute. Reusing it keeps one
    source of truth instead of adding a second flag that can disagree.
    """
    return getattr(profile, "MOTION", None) == "kenburns"


def redraw(ep: Episode, m: Manifest, scenes: list[int], log=None) -> list[int]:
    """Throw away specific scenes' pictures so they are drawn again.

    A generator gets scenes wrong in ways no check can catch: two crows
    where the prompt said one, an animal inside a pot. Fixing that by
    hand meant knowing which file to delete, which is a thing a person
    should not have to know. Removed from the library too -- otherwise
    the wrong picture is the one reused next time.
    """
    log = log or get(m.id, "images")
    from .. import image_library

    known = {s.index for s in m.scenes}
    unknown = sorted(set(scenes) - known)
    if unknown:
        raise StageError(
            f"this episode has no scene {unknown}; it has 1 to "
            f"{max(known) if known else 0}"
        )

    shelf = image_library.load()
    gone = []
    for index in sorted(set(scenes)):
        picture = ep.dir / "sources" / f"scene_{index:02d}.png"
        picture.unlink(missing_ok=True)
        scene = next(s for s in m.scenes if s.index == index)
        raw = (getattr(scene, "image_prompt", "") or "").strip()
        for entry in shelf:
            if raw and entry.prompt.strip() == raw:
                entry.path.unlink(missing_ok=True)
        gone.append(index)
    # Re-read, so the index loses the rows whose files have just gone.
    image_library.save(image_library.load())
    log.info(f"cleared {len(gone)} picture(s): {gone}")
    return gone


def run(ep: Episode, m: Manifest, profile, spec=None,
        force: bool = False) -> dict:
    log = get(m.id, "images")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    if not wants_pictures(profile):
        m.mark_done("images", skipped="this profile draws its own frames")
        log.info("skipped: the frames are drawn, not photographed")
        return {"skipped": True}

    if not enabled():
        m.mark_done("images", skipped="IMAGE_FETCH is off")
        log.info("skipped: IMAGE_FETCH is off, sources/ is yours to fill")
        return {"skipped": True}

    result = run_for(ep, m, spec or spec_for(profile), profile=profile,
                     force=force)
    if not result["made"] and not result["kept"]:
        raise StageError(
            "no scene got a picture. "
            + "; ".join(f"{k}: {v}" for k, v in result["failed"].items())
        )
    m.mark_done("images", **result)
    log.info(f"{len(result['made'])} fetched, {len(result['kept'])} already "
             f"there, {len(result['failed'])} unavailable")
    return result
