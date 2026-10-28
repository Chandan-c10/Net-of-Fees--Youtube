"""The render stages that had no coverage: pacing, motion, transitions,
captions, mastering, music, metadata and the finishing pass."""

from __future__ import annotations

import pytest
from conftest import needs_ffmpeg

from pipeline.config import LONGFORM
from pipeline.errors import StageError

# ---- pacing -----------------------------------------------------------

def test_pacing_is_deterministic():
    """A re-render must reproduce identical timing -- section 9."""
    from pipeline.pacing import scene_padding
    a = scene_padding("Charges took 41 percent.", 3, "007")
    b = scene_padding("Charges took 41 percent.", 3, "007")
    assert a == b


def test_pacing_varies_between_scenes():
    """Uniform scene length is a clear tell of machine assembly."""
    from pipeline.pacing import scene_padding
    pads = {scene_padding("A claim, with 5 in it.", i, "007") for i in range(6)}
    assert len(pads) > 1, "every scene received an identical pause"


def test_a_statement_gets_more_room_than_a_clause():
    from pipeline.pacing import tail_pause
    assert tail_pause("It ended.", 1, "x") > tail_pause("and then,", 1, "x")


# ---- motion -----------------------------------------------------------

def test_ease_out_is_monotonic_and_bounded():
    from pipeline.stages.motion import ease_out
    vals = [ease_out(i / 10) for i in range(11)]
    assert vals == sorted(vals) and vals[0] == 0.0 and vals[-1] == 1.0


def test_single_number_cards_are_not_animated():
    from pipeline.stages.motion import should_animate
    assert not should_animate("breakeven", 8.0)
    assert not should_animate("title", 8.0)
    assert should_animate("fee_stack", 8.0)


def test_short_scenes_skip_the_build():
    from pipeline.stages.motion import should_animate
    assert not should_animate("fee_stack", 1.0)


# ---- transitions ------------------------------------------------------

def test_dissolve_preserves_total_duration(tmp_path):
    """A dissolve borrows time; it must never add any."""
    from PIL import Image

    from pipeline.stages.transitions import apply

    frames = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (160, 90), (i * 40, 0, 0)).save(p)
        frames.append((p, 4.0))
    before = sum(d for _, d in frames)
    after = apply(frames, {1, 2, 3}, tmp_path / "x")
    assert abs(sum(d for _, d in after) - before) < 0.01
    assert len(after) > len(frames), "no dissolve frames were inserted"


def test_dissolve_skipped_when_there_is_no_time(tmp_path):
    from PIL import Image

    from pipeline.stages.transitions import apply
    frames = []
    for i in range(3):
        p = tmp_path / f"s{i}.png"
        Image.new("RGB", (160, 90)).save(p)
        frames.append((p, 0.1))
    assert apply(frames, {1, 2}, tmp_path / "x") == frames


# ---- captions ---------------------------------------------------------

def test_caption_frame_matches_the_target_resolution(tmp_path):
    from PIL import Image

    from pipeline.stages.caption_overlay import render_cue_frame
    src = tmp_path / "chart.png"
    Image.new("RGB", (640, 360), (10, 12, 18)).save(src)
    out = render_cue_frame(src, "A caption that wraps across two lines here.",
                           LONGFORM, tmp_path / "cue.png")
    assert Image.open(out).size == LONGFORM.size


def test_cues_never_cross_a_scene_boundary(episode):
    """A caption on screen while the wrong chart shows is a correctness bug."""
    from pipeline.stages import captions, voice
    m = episode.load_manifest()
    m.pass_gate_1()
    voice.run(episode, m, engine_name="silent")
    captions.run(episode, m, method="derive")
    cues = captions.read_cues(episode)
    for scene in m.scenes:
        mine = [c for c in cues if c["scene"] == scene.index]
        total = sum(c["end"] - c["start"] for c in mine)
        assert abs(total - scene.duration_sec) < 0.05


# ---- music ------------------------------------------------------------

def test_music_without_a_licence_is_refused(tmp_path):
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "bed.wav").write_bytes(b"x")
    from pipeline.stages.music import find_track
    with pytest.raises(StageError, match="LICENCE"):
        find_track(tmp_path)


def test_no_music_directory_is_not_an_error(tmp_path):
    from pipeline.stages.music import find_track
    assert find_track(tmp_path) is None


# ---- metadata ---------------------------------------------------------

def test_advisory_language_in_a_title_is_blocked():
    """A legal boundary, not a style preference -- section 14."""
    from pipeline.stages.metadata import TitleCandidate
    bad = TitleCandidate("Buy this before expiry", "x")
    assert any("ADVISORY" in w for w in bad.warnings)


def test_clickbait_is_flagged():
    from pipeline.stages.metadata import TitleCandidate
    assert any("clickbait" in w for w in
               TitleCandidate("You won't believe this", "x").warnings)


def test_overlong_titles_are_flagged():
    from pipeline.stages.metadata import TITLE_MAX, TitleCandidate
    long = TitleCandidate("x" * (TITLE_MAX + 10), "x")
    assert any("truncates" in w for w in long.warnings)


def test_hashtags_are_deduplicated_and_capped():
    import profiles
    from pipeline.stages.metadata import hashtags
    tags = hashtags({"recipe": "fee_drag"}, profiles.load("trading"), limit=5)
    assert len(tags) == 5 and len({t.lower() for t in tags}) == 5


# ---- mastering and finishing ------------------------------------------

@needs_ffmpeg
def test_mastering_produces_mono_at_the_configured_rate(episode):
    import json as _json
    import subprocess

    from pipeline.config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
    from pipeline.stages import voice, voice_process
    m = episode.load_manifest()
    m.pass_gate_1()
    voice.run(episode, m, engine_name="silent")
    out = voice_process.run(episode, m, master=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout
    stream = _json.loads(probe)["streams"][0]
    assert int(stream["channels"]) == AUDIO_CHANNELS
    assert int(stream["sample_rate"]) == AUDIO_SAMPLE_RATE


def test_finishing_filter_is_duration_preserving():
    """Every effect in the chain must leave length untouched."""
    from pipeline.stages.finish import LOOKS, build_filter
    for name, look in LOOKS.items():
        chain = build_filter(look, LONGFORM, 60.0)
        for forbidden in ("setpts", "trim", "xfade", "fps=", "atrim"):
            assert forbidden not in chain, f"{name} uses {forbidden}, which alters duration"


def test_default_look_has_no_grain():
    """Grain cost 26x the file size for no visible gain on flat charts."""
    from pipeline.stages.finish import LOOKS
    assert LOOKS["clean"].grain == 0.0
    assert LOOKS["none"].vignette_strength == 0.0
