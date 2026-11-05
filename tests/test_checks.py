"""Inspecting a rendered episode.

Every check measures the file rather than the stage's report, because a
stage reporting success is exactly the case where something has gone
quietly wrong.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import needs_ffmpeg

from pipeline import checks
from pipeline.manifest import Episode, Manifest, Scene
from pipeline.platform_support import ffmpeg


def audio(path, seconds=2.0, source="sine=frequency=330", level=None):
    args = [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
            "-i", f"{source}:duration={seconds}"]
    if level is not None:
        args += ["-af", f"volume={level}dB"]
    args += ["-t", str(seconds), str(path)]
    subprocess.run(args, capture_output=True, timeout=60, check=False)
    return path


@pytest.fixture
def episode(tmp_path):
    def build(scenes=2, seconds=2.0):
        ep = Episode("chk", tmp_path)
        ep.ensure_dirs()
        m = Manifest.new("chk", recipe="r", profile="trading", voice="v")
        m.scenes = [Scene(index=i, text=f"Scene {i}.", visual_spec={},
                          duration_sec=seconds)
                    for i in range(1, scenes + 1)]
        ep.audio_dir.mkdir(parents=True, exist_ok=True)
        for scene in m.scenes:
            audio(ep.scene_audio(scene), seconds)
        m.mark_done("assemble", duration_sec=seconds * scenes)
        ep.save_manifest(m)
        return ep, m
    return build


# ---- silent scenes ----------------------------------------------------


@needs_ffmpeg
def test_a_silent_scene_is_a_blocker(episode):
    """The commonest silent failure: a voice engine that returns without
    error and writes nothing audible."""
    ep, m = episode(scenes=2)
    audio(ep.scene_audio(m.scenes[1]), 2.0, "anullsrc=r=48000:cl=mono")
    found = checks.check_scene_audio(ep, m)
    assert [f.where for f in found] == ["scene_02"]
    assert found[0].severity == "blocker"
    assert "voice --force" in found[0].fix


@needs_ffmpeg
def test_a_scene_with_speech_passes(episode):
    ep, m = episode(scenes=2)
    assert checks.check_scene_audio(ep, m) == []


@needs_ffmpeg
def test_a_missing_scene_file_is_a_blocker(episode):
    ep, m = episode(scenes=2)
    ep.scene_audio(m.scenes[0]).unlink()
    found = checks.check_scene_audio(ep, m)
    assert found and found[0].severity == "blocker"
    assert "no audio file" in found[0].message


# ---- the mix ----------------------------------------------------------


@needs_ffmpeg
def test_clipping_is_caught(episode, tmp_path):
    ep, m = episode(scenes=1)
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-af", "volume=20dB", "-t", "2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(ep.video)],
        capture_output=True, timeout=120, check=False)
    found = checks.check_mix(ep, m)
    assert any("clipping" in f.message for f in found)


@needs_ffmpeg
def test_a_silent_mix_is_caught(episode):
    ep, m = episode(scenes=1)
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(ep.video)],
        capture_output=True, timeout=120, check=False)
    assert any("silent" in f.message for f in checks.check_mix(ep, m))


# ---- frames -----------------------------------------------------------


@needs_ffmpeg
def test_a_blank_video_is_a_blocker(episode):
    ep, m = episode(scenes=1, seconds=3.0)
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=black:size=320x240:rate=15", "-t", "3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(ep.video)],
        capture_output=True, timeout=120, check=False)
    found = checks.check_frames(ep, m, samples=2)
    assert found and all(f.severity == "blocker" for f in found)


@needs_ffmpeg
def test_a_detailed_video_passes(episode):
    ep, m = episode(scenes=1, seconds=3.0)
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=640x480:rate=15", "-t", "3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(ep.video)],
        capture_output=True, timeout=120, check=False)
    assert checks.check_frames(ep, m, samples=2) == []


# ---- duration ---------------------------------------------------------


def test_a_very_short_episode_is_a_blocker(episode):
    ep, m = episode(scenes=1)
    m.stage("assemble").data["duration_sec"] = 2.0
    found = checks.check_duration(ep, m)
    assert found and found[0].severity == "blocker"


def test_an_over_long_short_is_a_warning(episode):
    ep, m = episode(scenes=1)
    m.stage("assemble").data["duration_sec"] = 200.0
    found = checks.check_duration(ep, m, vertical=True)
    assert found and found[0].severity == "warning"
    assert "Shorts limit" in found[0].message


def test_a_long_episode_is_fine_for_long_form(episode):
    ep, m = episode(scenes=1)
    m.stage("assemble").data["duration_sec"] = 600.0
    assert checks.check_duration(ep, m, vertical=False) == []


# ---- captions ---------------------------------------------------------


def test_a_scene_with_no_caption_is_flagged(episode):
    ep, m = episode(scenes=2)
    (ep.dir / "cues.json").write_text(json.dumps(
        [{"scene": 1, "start": 0, "end": 2, "text": "Only one."}]),
        encoding="utf-8")
    found = checks.check_captions(ep, m)
    assert any(f.where == "scene_02" for f in found)


def test_an_unreadably_fast_caption_is_flagged(episode):
    """Under about two words a second is unreadable on a phone."""
    ep, m = episode(scenes=1)
    (ep.dir / "cues.json").write_text(json.dumps([
        {"scene": 1, "start": 0, "end": 1.0,
         "text": "far too many words to possibly read in one single second"}
    ]), encoding="utf-8")
    assert any("too fast" in f.message for f in checks.check_captions(ep, m))


def test_no_cues_file_is_not_an_error(episode):
    ep, m = episode(scenes=1)
    assert checks.check_captions(ep, m) == []


# ---- the whole run ----------------------------------------------------


@needs_ffmpeg
def test_inspect_reports_clean_when_it_is(episode):
    # Long enough to clear MIN_EPISODE_SEC; a 4s episode is correctly
    # flagged as too short to post.
    ep, m = episode(scenes=3, seconds=3.0)
    result = checks.inspect(ep, m)
    assert result["blockers"] == 0


@needs_ffmpeg
def test_one_broken_check_does_not_hide_the_others(episode, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(checks, "check_mix", explode)
    monkeypatch.setattr(checks, "ALL_CHECKS",
                        (("mix", explode),
                         ("scene audio", checks.check_scene_audio)))
    ep, m = episode(scenes=1)
    audio(ep.scene_audio(m.scenes[0]), 2.0, "anullsrc=r=48000:cl=mono")
    result = checks.inspect(ep, m)
    assert any(f["check"] == "scene_audio" for f in result["findings"]), \
        "the working check still ran"


def test_every_finding_says_what_to_do():
    """A check that says something is wrong without saying which scene,
    or what to do, is a check that gets ignored."""
    finding = checks.Finding("blocker", "x", "scene_01", "it broke", "fix it")
    assert finding.where and finding.fix
    assert "scene_01" in finding.line()
