"""Motion across a still.

The arithmetic is tested without FFmpeg; the rendering is tested with it,
on real images, because the only question that matters is whether a clip
comes out the length the audio asked for.
"""

from __future__ import annotations

import subprocess

import pytest

from pipeline import platform_support
from pipeline.config import LONGFORM, SHORTS
from pipeline.errors import StageError
from pipeline.stages import kenburns

# ---- frame planning ---------------------------------------------------


def test_a_single_clip_rounds_to_whole_frames():
    assert kenburns.frame_count(4.0, 30) == 120
    assert kenburns.frame_count(7.35, 30) == 220      # 220.5 rounds down


def test_a_zero_or_negative_duration_is_refused():
    for bad in (0.0, -1.0):
        with pytest.raises(StageError, match="cannot render"):
            kenburns.frame_count(bad, 30)


def test_a_clip_always_has_at_least_two_frames():
    assert kenburns.frame_count(0.001, 30) == 2


def test_planning_carries_the_rounding_error():
    """Rounding each scene alone loses up to half a frame every time. At
    30fps that is 333ms over twenty scenes -- more than six times
    assemble's whole budget for the episode."""
    durations = [7.35] * 20
    fps = 30
    alone = sum(kenburns.frame_count(d, fps) for d in durations) / fps
    carried = sum(kenburns.plan_frames(durations, fps)) / fps
    wanted = sum(durations)

    assert abs(alone - wanted) > 0.3, "the naive case should be badly out"
    assert abs(carried - wanted) < 0.02


@pytest.mark.parametrize("durations", [
    [4.0, 7.35, 5.812, 3.4],
    [17.15, 7.35, 15.812, 12.4, 9.03, 11.7, 18.21],
    [1.0] * 50,
    [0.5, 0.5, 0.5],
    [13.333333],
])
def test_a_planned_episode_lands_inside_the_sync_budget(durations):
    """50ms is the whole episode's tolerance in assemble; arithmetic must
    not spend it."""
    for fps in (24, 25, 30):
        total = sum(kenburns.plan_frames(durations, fps)) / fps
        assert abs(total - sum(durations)) < 0.05


def test_planning_never_returns_an_unrenderable_count():
    counts = kenburns.plan_frames([0.01] * 10, 30)
    assert all(c >= 2 for c in counts)


def test_planning_refuses_a_bad_duration():
    with pytest.raises(StageError, match="cannot render"):
        kenburns.plan_frames([4.0, 0.0], 30)


# ---- move choice ------------------------------------------------------


def test_a_move_is_the_same_on_every_rerender():
    """A re-render must reproduce the same video, as everywhere else."""
    assert kenburns.choose_move(3, "042") == kenburns.choose_move(3, "042")
    assert kenburns.plan_moves(8, "042") == kenburns.plan_moves(8, "042")


def test_different_episodes_move_differently():
    assert kenburns.plan_moves(8, "042") != kenburns.plan_moves(8, "043")


def test_an_avoided_move_is_not_chosen():
    first = kenburns.choose_move(1, "x")
    assert kenburns.choose_move(1, "x", avoid=first.name) != first


def test_avoid_accepts_several_names():
    excluded = tuple(m.name for m in kenburns.MOVES[:6])
    chosen = kenburns.choose_move(1, "x", avoid=excluded)
    assert chosen.name not in excluded


def test_excluding_everything_still_returns_a_move():
    """Better a repeat than a crash mid-render."""
    everything = tuple(m.name for m in kenburns.MOVES)
    assert kenburns.choose_move(1, "x", avoid=everything) in kenburns.MOVES


@pytest.mark.parametrize("episode", ["002", "007", "019", "ep-a", "x"])
def test_no_move_repeats_or_alternates(episode):
    """A-A-A reads as automated, and so does A-B-A-B."""
    names = [m.name for m in kenburns.plan_moves(12, episode)]
    assert not any(a == b for a, b in zip(names, names[1:])), "repeat"
    assert not any(a == b for a, b in zip(names, names[2:])), "alternation"


# ---- the filter -------------------------------------------------------


def test_commas_inside_an_expression_are_escaped():
    """zoompan's parser reads a bare comma as the next filter, which is
    how pow(x,3) breaks the whole chain."""
    chain = kenburns.filter_chain(kenburns.MOVES[0], 120, LONGFORM)
    assert "pow(1-on/119\\,3)" in chain
    assert "pow(1-on/119,3)" not in chain


def test_the_zoom_eases_rather_than_running_linear():
    """A linear zoom is the clearest sign of an automated pan."""
    chain = kenburns.filter_chain(kenburns.MOVES[0], 120, LONGFORM)
    assert "pow(" in chain, "no easing term"


def test_a_push_and_a_pull_are_not_the_same_chain():
    push = kenburns.filter_chain(kenburns.MOVES[0], 120, LONGFORM)
    pull = kenburns.filter_chain(kenburns.MOVES[1], 120, LONGFORM)
    assert push != pull


def test_a_panning_move_offsets_the_crop():
    centred = kenburns.filter_chain(kenburns.MOVES[0], 120, LONGFORM)
    panned = kenburns.filter_chain(kenburns.MOVES[2], 120, LONGFORM)
    assert "iw-iw/zoom" in panned
    assert "iw-iw/zoom" not in centred


def test_the_source_is_overscanned_before_cropping():
    """Cropping into a frame-sized image and scaling back up is how you
    get a soft picture."""
    chain = kenburns.filter_chain(kenburns.MOVES[0], 120, LONGFORM)
    assert f"scale={int(LONGFORM.width * kenburns.OVERSCAN)}" in chain


def test_the_chain_targets_the_requested_frame_size():
    for spec in (LONGFORM, SHORTS):
        chain = kenburns.filter_chain(kenburns.MOVES[0], 120, spec)
        assert f"s={spec.width}x{spec.height}" in chain


# ---- rendering, with real FFmpeg --------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    not platform_support.ffmpeg_available(),
    reason="needs FFmpeg",
)


@pytest.fixture
def still(tmp_path):
    """A real image, wider than the frame, like a generated one would be."""
    path = tmp_path / "source.png"
    proc = subprocess.run(
        [platform_support.ffmpeg(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=2560x1440:rate=1",
         "-frames:v", "1", str(path)],
        capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return path


@needs_ffmpeg
@pytest.mark.parametrize("duration", [4.0, 7.35, 2.0])
def test_a_rendered_clip_matches_its_frame_count(still, tmp_path, duration):
    out = tmp_path / "clip.mp4"
    frames = kenburns.frame_count(duration, LONGFORM.fps)
    kenburns.render(still, out, duration, LONGFORM, frames=frames)
    got = kenburns.measure(out)
    assert abs(got - frames / LONGFORM.fps) < kenburns.DURATION_TOLERANCE_SEC


@needs_ffmpeg
def test_a_planned_sequence_renders_to_the_right_total(still, tmp_path):
    """The property the whole module exists for."""
    durations = [3.0, 4.35, 2.812]
    plan = kenburns.plan_frames(durations, LONGFORM.fps)
    total = 0.0
    for i, (duration, frames) in enumerate(zip(durations, plan), 1):
        out = tmp_path / f"clip_{i}.mp4"
        kenburns.render(still, out, duration, LONGFORM,
                        scene_index=i, episode_id="t", frames=frames)
        total += kenburns.measure(out)
    assert abs(total - sum(durations)) < 0.05


@needs_ffmpeg
def test_a_short_scene_is_held_rather_than_moved(still, tmp_path):
    """Below a second or so, a move is a twitch."""
    out = tmp_path / "held.mp4"
    kenburns.render(still, out, 0.6, LONGFORM)
    assert abs(kenburns.measure(out) - 0.6) < kenburns.DURATION_TOLERANCE_SEC


@needs_ffmpeg
def test_a_vertical_clip_comes_out_vertical(still, tmp_path):
    out = tmp_path / "reel.mp4"
    kenburns.render(still, out, 3.0, SHORTS)
    proc = subprocess.run(
        [platform_support.ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, timeout=60, check=False)
    assert proc.stdout.strip() == f"{SHORTS.width},{SHORTS.height}"


@needs_ffmpeg
def test_a_source_smaller_than_the_frame_still_renders(tmp_path):
    """A generated image may come back smaller than 1920 wide."""
    small = tmp_path / "small.png"
    subprocess.run(
        [platform_support.ffmpeg(), "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=1",
         "-frames:v", "1", str(small)],
        capture_output=True, text=True, timeout=60, check=False)
    out = tmp_path / "upscaled.mp4"
    kenburns.render(small, out, 3.0, LONGFORM)
    assert abs(kenburns.measure(out) - 3.0) < kenburns.DURATION_TOLERANCE_SEC


def test_a_missing_source_is_a_clear_error(tmp_path):
    with pytest.raises(StageError, match="no source image"):
        kenburns.render(tmp_path / "nope.png", tmp_path / "o.mp4",
                        3.0, LONGFORM)


def test_the_motion_assembler_plans_moves_across_the_episode():
    """kenburns.render picks its own move when given none, with nothing
    to avoid, so a seven-scene episode rendered drift_left three times.
    The assembler must plan them together."""
    import inspect

    from pipeline.stages import motion_assemble
    source = inspect.getsource(motion_assemble.run)
    assert "plan_moves" in source, "the assembler must use plan_moves"
    assert "choose_move" not in source, \
        "picking per scene has nothing to avoid"


# ---- geometry ---------------------------------------------------------
# zoompan scales its crop region to `s`, so feeding it a square and
# asking for 1080x1920 stretched the picture instead of framing it. A
# circle came out at ratio 0.562, which is exactly 9/16. Looking at a
# rendered photo did not catch it; a circle does.


@needs_ffmpeg
@pytest.mark.parametrize("spec", [LONGFORM, SHORTS])
def test_a_square_source_is_not_stretched(tmp_path, spec):
    from PIL import Image, ImageDraw

    # Small, and placed where the head-biased crop keeps it. A circle
    # large enough to be clipped would be measured as an arc, which is
    # wider than tall, and the test would fail for the wrong reason.
    src = tmp_path / "circle.png"
    im = Image.new("RGB", (1000, 1000), (20, 20, 30))
    ImageDraw.Draw(im).ellipse([350, 330, 650, 630], outline=(255, 80, 80),
                               width=12)
    im.save(src)

    out = tmp_path / "out.mp4"
    kenburns.render(src, out, 2.0, spec, move=kenburns.MOVES[0])

    frame = tmp_path / "frame.png"
    subprocess.run(
        [platform_support.ffmpeg(), "-v", "error", "-ss", "0.1",
         "-i", str(out), "-frames:v", "1", "-y", str(frame)],
        capture_output=True, timeout=60, check=False)

    import numpy as np
    arr = np.array(Image.open(frame).convert("L"))
    ys, xs = np.where(arr > 60)
    ratio = (xs.max() - xs.min()) / (ys.max() - ys.min())
    assert abs(ratio - 1.0) < 0.05, (
        f"a circle rendered at ratio {ratio:.3f}; the picture is being "
        f"stretched to fill {spec.width}x{spec.height}"
    )


@needs_ffmpeg
def test_a_wide_source_is_not_stretched_into_a_vertical_frame(tmp_path):
    from PIL import Image, ImageDraw

    src = tmp_path / "wide.png"
    im = Image.new("RGB", (1600, 900), (20, 20, 30))
    ImageDraw.Draw(im).ellipse([700, 300, 900, 500], outline=(255, 80, 80),
                               width=10)
    im.save(src)
    out = tmp_path / "out.mp4"
    kenburns.render(src, out, 2.0, SHORTS, move=kenburns.MOVES[0])

    frame = tmp_path / "f.png"
    subprocess.run(
        [platform_support.ffmpeg(), "-v", "error", "-ss", "0.1",
         "-i", str(out), "-frames:v", "1", "-y", str(frame)],
        capture_output=True, timeout=60, check=False)
    import numpy as np
    arr = np.array(Image.open(frame).convert("L"))
    ys, xs = np.where(arr > 60)
    ratio = (xs.max() - xs.min()) / (ys.max() - ys.min())
    assert abs(ratio - 1.0) < 0.08, f"circle came out at {ratio:.3f}"


def test_the_crop_keeps_heads_rather_than_centring():
    """Faces sit above the middle of nearly every frame, so a centred
    crop takes the chin and leaves the hair."""
    chain = kenburns.filter_chain(kenburns.MOVES[0], 60, SHORTS)
    assert f"*{kenburns.VERTICAL_CROP_BIAS}" in chain
    assert kenburns.VERTICAL_CROP_BIAS < 0.5
