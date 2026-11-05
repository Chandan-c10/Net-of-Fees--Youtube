"""The moving-stills assembly path, rendered for real.

This path had one test and it only read source code. It is the second
riskiest module after assemble.py -- it owns the same sync contract --
and every defect found in it so far came from watching a video rather
than running the suite.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import needs_ffmpeg

from pipeline.config import LONGFORM, SHORTS
from pipeline.errors import StageError
from pipeline.manifest import Episode, Manifest, Scene
from pipeline.platform_support import ffmpeg, ffprobe
from pipeline.stages import motion_assemble
from pipeline.voices.base import measure_media


def probe(path, entries: str) -> str:
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60, check=False)
    return out.stdout.strip()


@pytest.fixture
def episode_with_pictures(tmp_path):
    """Three scenes with real pictures and real narration."""
    def build(durations=(3.0, 2.5, 4.0), spec=SHORTS):
        ep = Episode("mot", tmp_path)
        ep.ensure_dirs()
        m = Manifest.new("mot", recipe="r", profile="storyboard_shorts",
                         voice="v", title="Motion")
        m.scenes = [
            Scene(index=i, text=f"Scene {i} says something.",
                  visual_spec={"data_key": "picture"}, duration_sec=d)
            for i, d in enumerate(durations, 1)
        ]
        ep.visuals_dir.mkdir(parents=True, exist_ok=True)
        for scene in m.scenes:
            subprocess.run(
                [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc=size=1400x2400:rate=1",
                 "-frames:v", "1", str(ep.scene_visual(scene))],
                capture_output=True, timeout=60, check=False)
        ep.audio_dir.mkdir(parents=True, exist_ok=True)
        for scene in m.scenes:
            subprocess.run(
                [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
                 "-i", f"sine=frequency=330:duration={scene.duration_sec}",
                 "-t", str(scene.duration_sec), str(ep.scene_audio(scene))],
                capture_output=True, timeout=60, check=False)
            m.mark_done("voice", draft=False)
        ep.save_manifest(m)
        return ep, m, spec
    return build


# ---- the sync contract ------------------------------------------------


@needs_ffmpeg
def test_the_render_matches_the_measured_audio(episode_with_pictures):
    """The property the whole pipeline rests on."""
    ep, m, spec = episode_with_pictures()
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    expected = m.total_duration()
    assert abs(measure_media(ep.video) - expected) < 0.05


@needs_ffmpeg
@pytest.mark.parametrize("durations", [
    (2.0, 2.0), (3.7, 1.9, 4.3), (1.5, 1.5, 1.5, 1.5, 1.5),
])
def test_drift_stays_inside_the_budget(episode_with_pictures, durations):
    ep, m, spec = episode_with_pictures(durations)
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    drift = m.stage("assemble").data["drift_sec"]
    assert abs(drift) < 0.05, f"{drift * 1000:.0f}ms"


@needs_ffmpeg
def test_a_scene_without_a_measured_duration_is_refused(
        episode_with_pictures):
    ep, m, spec = episode_with_pictures()
    m.scenes[1].duration_sec = None
    with pytest.raises(StageError, match="run voice first"):
        motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)


@needs_ffmpeg
def test_a_missing_picture_says_which_stage_makes_it(episode_with_pictures):
    ep, m, spec = episode_with_pictures()
    ep.scene_visual(m.scenes[1]).unlink()
    with pytest.raises(StageError, match="charts stage"):
        motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)


# ---- geometry ---------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("spec", [SHORTS, LONGFORM])
def test_the_output_has_the_requested_shape(episode_with_pictures, spec):
    ep, m, _ = episode_with_pictures((2.0, 2.0), spec)
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    assert probe(ep.video, "stream=width,height") == \
        f"{spec.width},{spec.height}"


@needs_ffmpeg
def test_the_output_carries_stereo_audio(episode_with_pictures):
    ep, m, spec = episode_with_pictures((2.0, 2.0))
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    out = subprocess.run(
        [ffprobe(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(ep.video)],
        capture_output=True, text=True, timeout=60, check=False)
    assert out.stdout.strip() == "2"


# ---- the picture actually moves ---------------------------------------


@needs_ffmpeg
def test_the_frame_changes_over_a_scene(episode_with_pictures, tmp_path):
    """A still held for a scene reads as a slide deck. This path exists
    to stop that, so it is worth asserting the pixels move."""
    import numpy as np
    from PIL import Image

    ep, m, spec = episode_with_pictures((5.0,))
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    frames = []
    for at in ("0.3", "4.5"):
        out = tmp_path / f"f{at}.png"
        subprocess.run(
            [ffmpeg(), "-v", "error", "-ss", at, "-i", str(ep.video),
             "-frames:v", "1", "-y", str(out)],
            capture_output=True, timeout=60, check=False)
        frames.append(np.asarray(Image.open(out).convert("L"), dtype=float))
    change = float(np.mean(np.abs(frames[0] - frames[1])))
    assert change > 1.0, f"the picture barely moved ({change:.2f})"


@needs_ffmpeg
def test_the_camera_does_not_repeat_across_scenes(episode_with_pictures):
    ep, m, spec = episode_with_pictures((2.0, 2.0, 2.0, 2.0))
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    moves = m.stage("assemble").data["moves"]
    assert len(moves) == 4
    assert not any(a == b for a, b in zip(moves, moves[1:])), moves


# ---- captions ---------------------------------------------------------


@needs_ffmpeg
def test_captions_are_overlaid_when_cues_exist(episode_with_pictures):
    ep, m, spec = episode_with_pictures((3.0, 3.0))
    (ep.dir / "cues.json").write_text(json.dumps([
        {"scene": 1, "start": 0.0, "end": 2.8, "text": "First line."},
        {"scene": 2, "start": 3.0, "end": 5.8, "text": "Second line."},
    ]), encoding="utf-8")
    motion_assemble.run(ep, m, spec=spec, burn_captions=True,
                        music=False, ambient=False)
    assert ep.video.exists()
    assert (ep.dir / "clips" / "scene_01.mp4").exists(), "captioned clip"


@needs_ffmpeg
def test_captions_can_be_skipped(episode_with_pictures):
    ep, m, spec = episode_with_pictures((2.0, 2.0))
    (ep.dir / "cues.json").write_text(json.dumps(
        [{"scene": 1, "start": 0.0, "end": 1.8, "text": "x"}]),
        encoding="utf-8")
    motion_assemble.run(ep, m, spec=spec, burn_captions=False,
                        music=False, ambient=False)
    assert not (ep.dir / "clips" / "scene_01.mp4").exists()


@needs_ffmpeg
def test_missing_cues_are_not_an_error(episode_with_pictures):
    ep, m, spec = episode_with_pictures((2.0, 2.0))
    assert not (ep.dir / "cues.json").exists()
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    assert ep.video.exists()


# ---- the clip list ----------------------------------------------------


def test_the_clip_list_repeats_nothing(tmp_path):
    """Unlike the still path: a clip carries its own duration, so the
    demuxer has nothing to round down."""
    clips = [tmp_path / f"c{i}.mp4" for i in range(3)]
    for c in clips:
        c.write_bytes(b"x")
    listing = motion_assemble.build_clip_list(clips, tmp_path / "l.txt")
    lines = listing.read_text().strip().splitlines()
    assert len(lines) == 3, "no entry should be repeated"


def test_an_empty_clip_list_is_refused(tmp_path):
    with pytest.raises(StageError, match="no clips"):
        motion_assemble.build_clip_list([], tmp_path / "l.txt")


@needs_ffmpeg
def test_the_manifest_records_what_was_built(episode_with_pictures):
    ep, m, spec = episode_with_pictures((2.0, 2.0, 2.0))
    motion_assemble.run(ep, m, spec=spec, music=False, ambient=False)
    data = m.stage("assemble").data
    assert data["path"] == "motion"
    assert data["scenes"] == 3
    assert data["duration_sec"] > 0
    assert len(data["moves"]) == 3
