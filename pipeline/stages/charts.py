"""Stage 5: render one visual per scene from the snapshot.

The chart is a pure function of data.json, so re-rendering an archived
episode reproduces the same image.
"""

from __future__ import annotations

from ..config import LONGFORM
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from .export import load_snapshot


def run(ep: Episode, m: Manifest, profile, spec=LONGFORM) -> None:
    log = get(m.id, "charts")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")
    if not profile.visuals.supports(spec.aspect):
        raise StageError(
            f"profile {profile.PROFILE_NAME!r} does not support {spec.aspect}"
        )

    data = load_snapshot(ep)
    ep.visuals_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    for scene in m.scenes:
        out = ep.scene_visual(scene)
        profile.visuals.build_visual(scene, data, spec, out)
        rendered.append(out.name)
        log.debug(f"{scene.slug} -> {out.name}")

    m.mark_done("charts", count=len(rendered),
                size=f"{spec.width}x{spec.height}", files=rendered)
    log.info(f"{len(rendered)} visuals at {spec.width}x{spec.height}")
