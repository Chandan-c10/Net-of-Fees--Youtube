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
