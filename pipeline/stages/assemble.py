"""Stage 7: stitch visuals, audio and captions into the video.

The highest-risk module. Scene durations come from the rendered audio,
and a rounding error per scene compounds across the episode into visible
drift -- so the result is measured and rejected if it has drifted.

The concat demuxer ignores the final entry's duration, which is why the
last image is listed twice. Without that the last scene is truncated and
it looks exactly like a sync bug.
"""

from __future__ import annotations

from pathlib import Path

from ..config import LONGFORM, SYNC_TOLERANCE_SEC, VideoSpec
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..platform_support import concat_path, run_ffmpeg
from ..voices.base import measure_media
from . import captions as captions_stage
from . import music as music_stage
from . import transitions as transitions_stage
from . import voice_process
from .caption_overlay import Cue, build_frames, render_cue_frame
from .export import load_snapshot
from .motion import BUILD_SEC, build_in


def build_concat_list(frames: list[tuple[Path, float]], path: Path) -> Path:
    """Write the demuxer list pairing each frame with its duration.

    The final entry is repeated because the concat demuxer ignores the last
    duration; without it the closing frame is truncated and it looks
    exactly like a sync bug.
    """
    if not frames:
        raise StageError("no frames to assemble")
    lines: list[str] = []
    for png, seconds in frames:
        lines.append(f"file '{concat_path(png)}'")
        lines.append(f"duration {seconds:.3f}")
    lines.append(f"file '{concat_path(frames[-1][0])}'")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def scene_frames(ep: Episode, m: Manifest, spec: VideoSpec,
                 burn_captions: bool, profile=None,
                 animate: bool = True) -> list[tuple[Path, float]]:
    """One frame per caption cue when burning, else one per scene.

    When animation is on, each scene opens with a short build-in. The
    build consumes the first part of that scene's own time rather than
    adding any, so the total is unchanged and audio stays in sync.
    """
    for scene in m.scenes:
        if not ep.scene_visual(scene).exists():
            raise StageError(f"no visual for {scene.slug}; run charts first")
        if scene.duration_sec is None:
            raise StageError(f"{scene.slug} has no measured duration; run voice first")

    if not burn_captions:
        plain = [(ep.scene_visual(s), s.duration_sec) for s in m.scenes]
        return plain, set(range(len(plain)))

    raw = captions_stage.read_cues(ep)
    cues = [Cue(c["scene"], c["start"], c["end"], c["text"]) for c in raw]

    # Cue durations must reconstruct the scene durations exactly, or the
    # video drifts away from the audio it was timed against.
    per_scene: dict[int, float] = {}
    for c in cues:
        per_scene[c.scene] = per_scene.get(c.scene, 0.0) + c.duration
    for scene in m.scenes:
        got = per_scene.get(scene.index, 0.0)
        if abs(got - scene.duration_sec) > 0.05:
            raise StageError(
                f"{scene.slug}: cues total {got:.3f}s but the scene is "
                f"{scene.duration_sec:.3f}s. Re-run captions."
            )

    by_scene = {s.index: ep.scene_visual(s) for s in m.scenes}
    frames = build_frames(cues, lambda i: by_scene[i], spec, ep.dir / "frames")

    if not animate or profile is None:
        starts, idx = set(), 0
        for scene in m.scenes:
            starts.add(idx)
            idx += sum(1 for c in cues if c.scene == scene.index)
        return frames, starts

    data = load_snapshot(ep)
    out: list[tuple[Path, float]] = []
    boundaries: set[int] = set()
    cue_index = 0
    for scene in m.scenes:
        boundaries.add(len(out))
        scene_cues = [c for c in cues if c.scene == scene.index]
        scene_frames_ = frames[cue_index:cue_index + len(scene_cues)]
        cue_index += len(scene_cues)
        if not scene_frames_:
            continue

        builds = build_in(scene, data, spec, profile, ep.dir / "builds")
        if not builds:
            out.extend(scene_frames_)
            continue

        # The build borrows time from this scene's first cue, so the cue
        # text stays on screen for what remains of it.
        first_path, first_dur = scene_frames_[0]
        borrowed = min(BUILD_SEC, first_dur * 0.6)
        per_build = borrowed / len(builds)

        for b in builds:
            captioned = render_cue_frame(
                b.path, scene_cues[0].text, spec,
                ep.dir / "builds" / f"{b.path.stem}_cap.png",
            )
            out.append((captioned, per_build))
        out.append((first_path, max(0.05, first_dur - borrowed)))
        out.extend(scene_frames_[1:])
    return out, boundaries


# Captions are composited with Pillow in caption_overlay.py rather than
# burned by FFmpeg's subtitles filter: that filter needs libass, which
# Homebrew's ffmpeg 9 formula omits and Windows 'essentials' builds lack.
# Drawing them ourselves makes the render independent of the FFmpeg build.


def run(ep: Episode, m: Manifest, spec: VideoSpec = LONGFORM,
        burn_captions: bool = True, master_audio: bool = True,
        profile=None, animate: bool = True, dissolve: bool = True,
        music: bool = True) -> Path:
    log = get(m.id, "assemble")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    audio = voice_process.run(ep, m, master=master_audio)

    if music:
        from ..config import ROOT
        audio = music_stage.apply_if_available(
            ROOT, audio, m.total_duration(), m.id
        )

    frames, boundaries = scene_frames(ep, m, spec, burn_captions,
                                      profile, animate)
    if dissolve:
        frames = transitions_stage.apply(frames, boundaries,
                                         ep.dir / "transitions")
    listing = build_concat_list(frames, ep.dir / "concat_video.txt")

    args = [
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(audio),
    ]
    vf = [f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=decrease",
          f"pad={spec.width}:{spec.height}:(ow-iw)/2:(oh-ih)/2:color=black",
          "format=yuv420p"]
    args += [
        "-vf", ",".join(vf),
        "-r", str(spec.fps),
        "-c:v", "libx264", "-preset", spec.preset, "-crf", str(spec.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", spec.audio_bitrate,
        "-ac", "2",   # mono routes to one speaker on some phones
        "-movflags", "+faststart",
        "-shortest",  # without this the video runs past the audio
        str(ep.video),
    ]
    ep.out_dir.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(args)

    if not ep.video.exists() or ep.video.stat().st_size == 0:
        raise StageError("ffmpeg reported success but produced no video")

    expected = m.total_duration()
    actual = measure_media(ep.video)
    drift = abs(actual - expected)
    if drift > SYNC_TOLERANCE_SEC:
        raise StageError(
            f"audio/video drift {drift:.3f}s exceeds the {SYNC_TOLERANCE_SEC}s "
            f"tolerance (expected {expected:.3f}s, got {actual:.3f}s). "
            f"The render is rejected rather than published out of sync."
        )

    m.mark_done(
        "assemble",
        duration_sec=round(actual, 3),
        expected_sec=round(expected, 3),
        drift_sec=round(drift, 4),
        size_bytes=ep.video.stat().st_size,
        resolution=f"{spec.width}x{spec.height}",
        captions_burned=burn_captions,
        frames=len(frames),
        animated=animate and profile is not None,
        dissolves=dissolve,
    )
    log.info(f"{actual:.2f}s, drift {drift*1000:.0f}ms, "
             f"{ep.video.stat().st_size/1_048_576:.1f} MB")
    return ep.video
