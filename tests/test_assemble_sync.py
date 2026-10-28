"""Audio/video sync: the failure this project most likely dies of.

Scene durations come from the rendered audio, and a rounding error per
scene compounds across the episode into visible drift.
"""

from __future__ import annotations

import pytest

from conftest import needs_ffmpeg
from pipeline.config import LONGFORM, SYNC_TOLERANCE_SEC
from pipeline.errors import StageError
from pipeline.stages import assemble, captions, charts, voice
from pipeline.stages.assemble import build_concat_list
from pipeline.voices.base import measure_media

import profiles


def _render(ep, m):
    m.pass_gate_1()
    voice.run(ep, m, engine_name="silent")
    charts.run(ep, m, profiles.load(m.profile))
    captions.run(ep, m, method="derive")
    return assemble.run(ep, m)


@needs_ffmpeg
def test_video_duration_matches_the_sum_of_scene_durations(episode):
    m = episode.load_manifest()
    video = _render(episode, m)
    expected = m.total_duration()
    actual = measure_media(video)
    assert abs(actual - expected) <= SYNC_TOLERANCE_SEC, (
        f"drift {abs(actual - expected):.3f}s exceeds tolerance"
    )


@needs_ffmpeg
def test_assembled_video_has_both_streams(episode):
    import subprocess, json
    m = episode.load_manifest()
    video = _render(episode, m)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout
    kinds = {s["codec_type"] for s in json.loads(out)["streams"]}
    assert kinds == {"video", "audio"}


@needs_ffmpeg
def test_render_is_idempotent(episode):
    """Running a completed stage again changes nothing."""
    m = episode.load_manifest()
    _render(episode, m)
    first = episode.video.stat().st_size
    durations = [s.duration_sec for s in m.scenes]

    voice.run(episode, m, engine_name="silent")
    assert [s.duration_sec for s in m.scenes] == durations
    assemble.run(episode, m)
    assert abs(episode.video.stat().st_size - first) / first < 0.02


def test_concat_list_repeats_the_final_frame(tmp_path):
    """The demuxer ignores the last duration; without the repeat the
    closing frame is truncated and it looks exactly like a sync bug."""
    frames = [(tmp_path / f"f{i}.png", 1.5) for i in range(3)]
    for p, _ in frames:
        p.write_bytes(b"x")
    listing = build_concat_list(frames, tmp_path / "list.txt")
    lines = listing.read_text().strip().splitlines()
    assert lines[-1] == lines[-3], "final file entry is not repeated"
    assert lines.count("file '" + frames[-1][0].resolve().as_posix() + "'") == 2


def test_concat_paths_never_contain_backslashes(tmp_path):
    """A native Windows path silently breaks the demuxer, which treats a
    backslash as an escape character."""
    frames = [(tmp_path / "a.png", 1.0)]
    frames[0][0].write_bytes(b"x")
    listing = build_concat_list(frames, tmp_path / "list.txt")
    assert "\\" not in listing.read_text()


def test_assemble_refuses_scenes_without_durations(episode):
    m = episode.load_manifest()
    m.pass_gate_1()
    with pytest.raises(StageError, match="run voice first"):
        assemble.run(episode, m)
