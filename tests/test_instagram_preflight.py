"""Preflight, against real files produced by real FFmpeg.

Every fixture here is rendered at test time rather than mocked, because
the whole point of preflight is what ffprobe reports about actual bytes.
Each file violates exactly one of Instagram's requirements, so a failure
names the rule that broke.
"""

import subprocess

import pytest

from pipeline import platform_support
from pipeline.errors import StageError
from pipeline.stages.instagram import preflight

pytestmark = pytest.mark.skipif(
    not platform_support.ffmpeg_available(),
    reason="needs FFmpeg to render the fixtures",
)

VERTICAL = "testsrc=size=1080x1920:rate=30"
TONE = "sine=frequency=440"


def _render(path, *, video=VERTICAL, seconds=5.0, vcodec="libx264",
            acodec="aac", audio=True) -> None:
    args = ["-y", "-v", "error", "-f", "lavfi", "-i", video]
    if audio:
        args += ["-f", "lavfi", "-i", TONE]
    args += ["-t", str(seconds), "-c:v", vcodec, "-pix_fmt", "yuv420p"]
    args += ["-c:a", acodec] if audio else ["-an"]
    args += [str(path)]
    proc = subprocess.run([platform_support.ffmpeg(), *args],
                          capture_output=True, text=True,
                          timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr


def test_a_real_vertical_reel_passes(tmp_path):
    path = tmp_path / "good.mp4"
    _render(path, seconds=6.0)
    got = preflight(path)
    assert (got["width"], got["height"]) == (1080, 1920)
    assert got["video_codec"] == "h264" and got["audio_codec"] == "aac"
    assert 5.9 <= got["duration_sec"] <= 6.2


def test_a_real_landscape_file_is_refused(tmp_path):
    path = tmp_path / "landscape.mp4"
    _render(path, video="testsrc=size=1920x1080:rate=30")
    with pytest.raises(StageError, match="9:16"):
        preflight(path)


def test_a_real_square_file_is_refused(tmp_path):
    path = tmp_path / "square.mp4"
    _render(path, video="testsrc=size=1080x1080:rate=30")
    with pytest.raises(StageError, match="aspect"):
        preflight(path)


def test_a_real_silent_file_is_refused(tmp_path):
    """A reel with no audio is nearly always a render that lost its track."""
    path = tmp_path / "silent.mp4"
    _render(path, audio=False)
    with pytest.raises(StageError, match="no audio stream"):
        preflight(path)


def test_a_real_mp3_track_is_refused(tmp_path):
    path = tmp_path / "mp3.mp4"
    _render(path, acodec="libmp3lame")
    with pytest.raises(StageError, match="accepts aac"):
        preflight(path)


def test_a_real_mpeg4_stream_is_refused(tmp_path):
    path = tmp_path / "mpeg4.mp4"
    _render(path, vcodec="mpeg4")
    with pytest.raises(StageError, match="h264"):
        preflight(path)


def test_a_real_file_under_three_seconds_is_refused(tmp_path):
    path = tmp_path / "short.mp4"
    _render(path, seconds=1.5)
    with pytest.raises(StageError, match="under 3s"):
        preflight(path)


def test_a_file_that_is_not_media_is_refused(tmp_path):
    path = tmp_path / "notavideo.mp4"
    path.write_bytes(b"this is not a video")
    with pytest.raises(StageError, match="could not read"):
        preflight(path)


def test_hevc_is_accepted_when_the_build_supports_it(tmp_path):
    """Instagram takes HEVC as well as H.264; skip where the local FFmpeg
    cannot produce it rather than assert on the build."""
    path = tmp_path / "hevc.mp4"
    try:
        _render(path, vcodec="libx265")
    except AssertionError:
        pytest.skip("this FFmpeg has no libx265")
    assert preflight(path)["video_codec"] == "hevc"
