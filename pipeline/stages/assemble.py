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
from . import voice_process
from .caption_overlay import Cue, build_frames


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
                 burn_captions: bool) -> list[tuple[Path, float]]:
    """One frame per caption cue when burning, else one per scene."""
    for scene in m.scenes:
        if not ep.scene_visual(scene).exists():
            raise StageError(f"no visual for {scene.slug}; run charts first")
        if scene.duration_sec is None:
            raise StageError(f"{scene.slug} has no measured duration; run voice first")

    if not burn_captions:
        return [(ep.scene_visual(s), s.duration_sec) for s in m.scenes]

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
    return build_frames(cues, lambda i: by_scene[i], spec, ep.dir / "frames")


# Captions are composited with Pillow in caption_overlay.py rather than
# burned by FFmpeg's subtitles filter: that filter needs libass, which
# Homebrew's ffmpeg 9 formula omits and Windows 'essentials' builds lack.
# Drawing them ourselves makes the render independent of the FFmpeg build.


def run(ep: Episode, m: Manifest, spec: VideoSpec = LONGFORM,
        burn_captions: bool = True, master_audio: bool = True) -> Path:
    log = get(m.id, "assemble")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    audio = voice_process.run(ep, m, master=master_audio)
    frames = scene_frames(ep, m, spec, burn_captions)
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
    )
    log.info(f"{actual:.2f}s, drift {drift*1000:.0f}ms, "
             f"{ep.video.stat().st_size/1_048_576:.1f} MB")
    return ep.video
