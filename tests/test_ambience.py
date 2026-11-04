"""Per-scene ambient beds.

The property that matters is the same one that matters everywhere in
this pipeline: the audio decides the length, so mixing a bed under the
narration must not change it by a millisecond.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import needs_ffmpeg

from pipeline.errors import StageError
from pipeline.manifest import Scene
from pipeline.platform_support import ffmpeg
from pipeline.stages import ambience
from pipeline.voices.base import measure_media


def scene(index: int, text: str, seconds: float = 3.0) -> Scene:
    return Scene(index=index, text=text, visual_spec={"data_key": "picture"},
                 duration_sec=seconds)


# ---- mood inference ---------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("The market was shouting, the crowd pressing in.", "crowd"),
    ("She walked by the river under the trees.", "nature"),
    ("He waited alone in the empty silence.", "tension"),
    ("The lamp burned late into the night.", "interior_night"),
])
def test_a_scene_sounds_like_its_words(text, expected):
    assert ambience.mood_for(text) == expected


def test_an_unremarkable_scene_falls_back_to_a_room():
    """Ambience that contradicts the picture is worse than none: the
    viewer hears a mistake rather than a setting."""
    assert ambience.mood_for("He counted them again.") == ambience.DEFAULT_MOOD


def test_mood_inference_is_deterministic():
    text = "The market at night was loud and empty at once."
    assert ambience.mood_for(text) == ambience.mood_for(text)


def test_a_cue_must_be_a_whole_word():
    """"sunday" is not "sun", and a substring match would put a calendar
    reference outdoors."""
    assert ambience.mood_for("It happened on a Sunday.") == \
        ambience.DEFAULT_MOOD


# ---- licence policy ---------------------------------------------------


def test_a_missing_directory_is_not_an_error(tmp_path):
    """No ambience means an episode renders without it; it must never
    block a render."""
    assert ambience.available(tmp_path) == {}


def test_a_directory_without_a_licence_is_refused(tmp_path):
    (tmp_path / "ambience").mkdir()
    (tmp_path / "ambience" / "crowd.wav").write_bytes(b"\x00")
    with pytest.raises(StageError, match="LICENCE"):
        ambience.available(tmp_path)


def test_an_empty_licence_is_refused(tmp_path):
    directory = tmp_path / "ambience"
    directory.mkdir()
    (directory / "LICENCE.md").write_text("   ", encoding="utf-8")
    with pytest.raises(StageError, match="LICENCE"):
        ambience.available(tmp_path)


def test_cleared_files_are_keyed_by_mood(tmp_path):
    directory = tmp_path / "ambience"
    directory.mkdir()
    (directory / "LICENCE.md").write_text("CC0, freesound.org", encoding="utf-8")
    for name in ("crowd.wav", "nature.mp3", "notes.txt"):
        (directory / name).write_bytes(b"\x00")
    found = ambience.available(tmp_path)
    assert set(found) == {"crowd", "nature"}, "only audio files are beds"


# ---- planning ---------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    """A small cleared library of real, short audio."""
    directory = tmp_path / "ambience"
    directory.mkdir()
    (directory / "LICENCE.md").write_text("Synthesised for tests.",
                                          encoding="utf-8")
    for name, freq in (("crowd", 200), ("nature", 400), ("room", 90)):
        subprocess.run(
            [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anoisesrc=d=2:c=pink:a=0.05,highpass=f={freq}",
             "-t", "2", str(directory / f"{name}.wav")],
            capture_output=True, timeout=60, check=False)
    return tmp_path


@needs_ffmpeg
def test_a_scene_without_its_own_bed_falls_back_to_the_room(library):
    scenes = [scene(1, "He waited alone in the silence.")]   # tension
    (mood, path), = ambience.plan(scenes, library)
    assert mood == "tension"
    assert path is not None and path.stem == "room", "fell back"


@needs_ffmpeg
def test_each_scene_gets_the_bed_its_words_suggest(library):
    scenes = [scene(1, "The market was shouting."),
              scene(2, "She walked by the river.")]
    moods = [m for m, _ in ambience.plan(scenes, library)]
    assert moods == ["crowd", "nature"]


# ---- the property that matters ----------------------------------------


@needs_ffmpeg
def test_mixing_a_bed_does_not_change_the_length(library, tmp_path):
    """The audio decides the length. A bed that stretched it would move
    every visual in the episode."""
    narration = tmp_path / "voice.wav"
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=9", "-t", "9", str(narration)],
        capture_output=True, timeout=60, check=False)
    before = measure_media(narration)

    scenes = [scene(1, "The market was shouting.", 3.0),
              scene(2, "She walked by the river.", 3.0),
              scene(3, "He waited alone.", 3.0)]
    out = ambience.apply_if_available(library, narration, scenes, before,
                                      "t", work=tmp_path / "work")

    assert out != narration, "a bed should have been mixed"
    assert abs(measure_media(out) - before) < 0.05, (
        "the ambience mix changed the episode length"
    )


@needs_ffmpeg
def test_no_library_returns_the_narration_untouched(tmp_path):
    narration = tmp_path / "voice.wav"
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=220:duration=3", "-t", "3", str(narration)],
        capture_output=True, timeout=60, check=False)
    scenes = [scene(1, "Anything at all.", 3.0)]
    out = ambience.apply_if_available(tmp_path, narration, scenes, 3.0, "t")
    assert out == narration


@needs_ffmpeg
def test_a_scene_with_no_measured_duration_is_refused(library, tmp_path):
    scenes = [Scene(index=1, text="x", visual_spec={}, duration_sec=None)]
    with pytest.raises(StageError, match="run voice first"):
        ambience.build_bed(scenes, library, tmp_path / "w", 3.0)


@needs_ffmpeg
def test_the_bed_sits_under_the_voice(library, tmp_path):
    """Above about -26 dB it competes with consonants."""
    assert ambience.BED_GAIN_DB <= -26.0
    from pipeline.stages import music
    assert ambience.BED_GAIN_DB < music.BED_GAIN_DB, (
        "ambience should sit under the music bed, not over it"
    )


@needs_ffmpeg
def test_describe_reports_what_each_scene_would_get(library):
    scenes = [scene(1, "The market was shouting."), scene(2, "Nothing.")]
    got = ambience.describe(scenes, library)
    assert got["moods"] == ["crowd", "room"]
    assert got["with_bed"] == 2
    assert "crowd" in got["available"]
