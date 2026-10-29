"""Assembling an episode from photographic stills rather than charts.

The chart path in assemble.py holds a still for the length of a scene and
composites captions onto it, which works because the still does not
change. A photographic or generated image wants a camera move across it,
and once the background moves every frame, captions cannot be baked into
the picture the same way.

So this path differs in three places and nowhere else:

* Each scene becomes a short clip via kenburns, instead of a still with a
  duration directive.
* Captions are rendered once with an alpha channel and overlaid by
  FFmpeg for the span of their cue, rather than drawn onto each frame.
* The concat demuxer joins clips, which means the final-entry duration
  quirk does not apply -- a clip carries its own length, so the last one
  does not need listing twice.

Everything else is deliberately the same, including the part that
matters: the audio decides the length, and the finished file is measured
and rejected on drift. Scene clip lengths are planned across the whole
episode by kenburns.plan_frames, because rounding each one alone
accumulates in one direction and spends the entire 50ms budget on
arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import LONGFORM, SYNC_TOLERANCE_SEC, VideoSpec
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import concat_path, run_ffmpeg
from ..voices.base import measure_media
from . import kenburns, voice_process
from .caption_overlay import Cue, render_caption_strip


def source_for(ep: Episode, scene_index: int) -> Path:
    """Where this scene's picture comes from.

    `sources/scene_NN.png` is filled either by hand or by an image
    generator. A profile's visuals module renders a fallback card when it
    is absent, so an episode is never blocked on having images.
    """
    return ep.dir / "sources" / f"scene_{scene_index:02d}.png"


def load_cues(ep: Episode) -> list[Cue]:
    path = ep.dir / "cues.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Cue(scene=c["scene"], start=float(c["start"]),
                end=float(c["end"]), text=c["text"]) for c in raw]


def scene_clip(ep: Episode, m: Manifest, scene, frames: int,
               spec: VideoSpec, cues: list[Cue],
               burn_captions: bool, log,
               move: kenburns.Move | None = None) -> Path:
    """One scene: a move across its picture, with its captions over it."""
    picture = ep.scene_visual(scene)
    if not picture.exists():
        raise StageError(
            f"{scene.slug} has no picture at {picture}. Run the charts "
            "stage, which renders one from the source image or a fallback."
        )
    clips_dir = ep.dir / "clips"
    moved = clips_dir / f"{scene.slug}_move.mp4"
    kenburns.render(picture, moved, scene.duration_sec, spec, move=move,
                    scene_index=scene.index, episode_id=m.id, frames=frames)

    mine = [c for c in cues if c.scene == scene.index] if burn_captions else []
    if not mine:
        return moved

    # One transparent strip per cue, overlaid for its own window. The
    # cue times are absolute across the episode, so they are rebased to
    # this clip before being used as filter conditions.
    base = min(c.start for c in mine)
    strips, filters, label = [], [], "[0:v]"
    for i, cue in enumerate(mine):
        strip = clips_dir / f"{scene.slug}_cap_{i:02d}.png"
        render_caption_strip(cue.text, spec, strip)
        strips += ["-i", str(strip)]
        start = max(0.0, cue.start - base)
        end = max(start + 0.05, cue.end - base)
        out_label = f"[v{i}]"
        filters.append(
            f"{label}[{i + 1}:v]overlay=0:0:"
            f"enable='between(t,{start:.3f},{end:.3f})'{out_label}"
        )
        label = out_label
    captioned = clips_dir / f"{scene.slug}.mp4"
    run_ffmpeg([
        "-i", str(moved), *strips,
        "-filter_complex", ";".join(filters),
        "-map", label, "-r", str(spec.fps),
        "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
        "-pix_fmt", "yuv420p", "-frames:v", str(frames),
        str(captioned),
    ])
    if not captioned.exists():
        raise StageError(f"captioning {scene.slug} produced nothing")
    log.debug(f"{scene.slug}: {len(mine)} captions over {frames} frames")
    return captioned


def build_clip_list(clips: list[Path], path: Path) -> Path:
    """A concat list of clips.

    Unlike the still path, no entry is repeated: a clip carries its own
    duration, so the demuxer has nothing to round down and the last frame
    is not truncated.
    """
    if not clips:
        raise StageError("no clips to assemble")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"file '{concat_path(c)}'" for c in clips) + "\n",
        encoding="utf-8",
    )
    return path


def run(ep: Episode, m: Manifest, spec: VideoSpec = LONGFORM,
        burn_captions: bool = True, master_audio: bool = True,
        profile=None, music: bool = True) -> Path:
    """Assemble an episode of moving stills."""
    log = get(m.id, "assemble")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")
    for scene in m.scenes:
        if scene.duration_sec is None:
            raise StageError(
                f"{scene.slug} has no measured duration; run voice first"
            )

    audio = voice_process.run(ep, m, master=master_audio)
    if music:
        from ..config import ROOT
        audio = music_bed(ROOT, audio, m)

    # Planned across the episode: rounding each clip alone loses up to
    # half a frame every time, always in the same direction, and twenty
    # scenes come out a third of a second short.
    durations = [s.duration_sec for s in m.scenes]
    plan = kenburns.plan_frames(durations, spec.fps)
    cues = load_cues(ep)

    # Chosen across the episode, not per scene: picking each one alone
    # has nothing to avoid, and a seven-scene episode came out with
    # drift_left three times.
    moves = kenburns.plan_moves(len(m.scenes), m.id)
    clips = [
        scene_clip(ep, m, scene, frames, spec, cues, burn_captions, log,
                   move=move)
        for scene, frames, move in zip(m.scenes, plan, moves, strict=True)
    ]
    listing = build_clip_list(clips, ep.dir / "concat_clips.txt")

    ep.out_dir.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(audio),
        "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
        "-pix_fmt", "yuv420p", "-r", str(spec.fps),
        "-c:a", "aac", "-b:a", spec.audio_bitrate,
        # Upmixed from the mono voice: some phones route a mono
        # track to one speaker, which a listener hears as a fault.
        "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",   # without this the video runs past the audio
        str(ep.video),
    ])
    if not ep.video.exists() or ep.video.stat().st_size == 0:
        raise StageError("ffmpeg reported success but produced no video")

    expected = m.total_duration()
    actual = measure_media(ep.video)
    drift = abs(actual - expected)
    if drift > SYNC_TOLERANCE_SEC:
        raise StageError(
            f"audio/video drift {drift:.3f}s exceeds the "
            f"{SYNC_TOLERANCE_SEC}s tolerance (expected {expected:.3f}s, "
            f"got {actual:.3f}s). The render is rejected rather than "
            f"published out of sync."
        )

    m.mark_done(
        "assemble",
        duration_sec=round(actual, 3),
        expected_sec=round(expected, 3),
        drift_sec=round(drift, 4),
        scenes=len(clips),
        path="motion",
        moves=[mv.name for mv in moves],
    )
    log.info(f"{actual:.2f}s from {len(clips)} moving stills, "
             f"drift {drift * 1000:.0f}ms")
    return ep.video


def music_bed(root: Path, audio: Path, m: Manifest) -> Path:
    """The music stage, if a licensed track is present."""
    from . import music as music_stage
    return music_stage.apply_if_available(
        root, audio, m.total_duration(), m.id)
