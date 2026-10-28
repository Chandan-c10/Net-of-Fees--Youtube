"""Stage 9: cut vertical Shorts from the finished long-form.

Shorts are derived, never written independently. One week's research
yields ten to fourteen clips, which covers two posts a day for the
following week -- architecture doc, section 12.

The extractor proposes and the operator disposes. It has no authority to
publish, which keeps a bad automatic cut off the channel.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import SHORTS
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import concat_path, run_ffmpeg
from .caption_overlay import Cue, build_frames
from .captions import read_cues
from .export import load_snapshot

MIN_SEC = 15.0
MAX_SEC = 60.0
DEFAULT_MAX_CLIPS = 14


@dataclass
class ShortCandidate:
    index: int
    source_scene: int
    start: float
    end: float
    text: str
    hook: str
    status: str = "proposed"
    path: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def _standalone_score(text: str) -> float:
    """How well a scene reads without the video around it.

    A clip that only makes sense having watched the long-form is dropped.
    """
    score = 0.0
    if any(ch.isdigit() for ch in text):
        score += 2.0           # a number is the whole point
    if "%" in text or "₹" in text:
        score += 1.0
    lowered = text.lower()
    for marker in ("which is", "that is", "so ", "but ", "now watch"):
        if lowered.startswith(marker):
            score -= 1.5       # refers back to something unseen
    for dependency in ("as i showed", "earlier", "that chart", "as we saw"):
        if dependency in lowered:
            score -= 2.0
    words = len(text.split())
    if 25 <= words <= 110:
        score += 1.0
    return score


def extract(m: Manifest, max_clips: int = DEFAULT_MAX_CLIPS) -> list[ShortCandidate]:
    """Propose clips. Never publishes, never decides."""
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    scored = []
    clock = 0.0
    for scene in m.scenes:
        if scene.duration_sec is None:
            raise StageError(f"scene {scene.index} has no measured duration")
        start, end = clock, clock + scene.duration_sec
        clock = end
        if not (MIN_SEC <= scene.duration_sec <= MAX_SEC):
            continue
        hook = scene.text.strip().split(".")[0][:80]
        scored.append((_standalone_score(scene.text), scene, start, end, hook))

    scored.sort(key=lambda t: t[0], reverse=True)
    chosen = sorted(scored[:max_clips], key=lambda t: t[1].index)
    return [
        ShortCandidate(i, sc.index, st, en, sc.text, hook)
        for i, (_s, sc, st, en, hook) in enumerate(chosen, start=1)
    ]


def _render_clip(ep: Episode, m: Manifest, cand: ShortCandidate,
                 profile, data: dict) -> Path:
    scene = m.scene(cand.source_scene)
    cues = [Cue(**{k: c[k] for k in ("scene", "start", "end", "text")})
            for c in read_cues(ep) if c["scene"] == cand.source_scene]
    if not cues:
        raise StageError(f"no cues for scene {cand.source_scene}")

    # Re-render the visual natively vertical. Cropping the 16:9 chart would
    # lose the axis labels, which are the content.
    work = ep.shorts_dir / f"{cand.index:02d}"
    work.mkdir(parents=True, exist_ok=True)
    chart = profile.visuals.build_visual(scene, data, SHORTS, work / "chart.png")

    frames = build_frames(
        [Cue(c.scene, c.start, c.end, c.text) for c in cues],
        lambda _i: chart, SHORTS, work / "frames",
    )
    listing = work / "concat.txt"
    lines = []
    for png, seconds in frames:
        lines.append(f"file '{concat_path(png)}'")
        lines.append(f"duration {seconds:.3f}")
    lines.append(f"file '{concat_path(frames[-1][0])}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    audio = ep.scene_audio(scene)
    if not audio.exists():
        raise StageError(f"no audio for scene {scene.index}")

    out = ep.shorts_dir / f"short_{cand.index:02d}.mp4"
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(audio),
        "-vf", f"scale={SHORTS.width}:{SHORTS.height},format=yuv420p",
        "-r", str(SHORTS.fps),
        "-c:v", "libx264", "-preset", SHORTS.preset, "-crf", str(SHORTS.crf),
        "-c:a", "aac", "-b:a", SHORTS.audio_bitrate,
        "-movflags", "+faststart", "-shortest", str(out),
    ])
    return out


def run(ep: Episode, m: Manifest, profile, max_clips: int = DEFAULT_MAX_CLIPS) -> None:
    log = get(m.id, "shorts")
    if not m.gate_2_passed_at:
        raise StageError("gate 2 has not been cleared")

    shorts_profile = profile
    if not profile.visuals.supports("9:16"):
        import profiles as _profiles
        shorts_profile = _profiles.load(f"{profile.PROFILE_NAME}_shorts")

    data = load_snapshot(ep)
    candidates = extract(m, max_clips)
    if not candidates:
        raise StageError(
            f"no scene is between {MIN_SEC}s and {MAX_SEC}s, so no Short can "
            f"be cut. Shorten the scenes or widen the bounds."
        )

    ep.shorts_dir.mkdir(parents=True, exist_ok=True)
    for cand in candidates:
        cand.path = str(_render_clip(ep, m, cand, shorts_profile, data))
        log.debug(f"short {cand.index}: scene {cand.source_scene}, {cand.duration}s")

    (ep.shorts_dir / "candidates.json").write_text(
        json.dumps([asdict(c) for c in candidates], indent=2), encoding="utf-8"
    )
    m.mark_done("shorts", count=len(candidates),
                scenes=[c.source_scene for c in candidates])
    log.info(f"{len(candidates)} clips proposed; operator keeps or drops each")
