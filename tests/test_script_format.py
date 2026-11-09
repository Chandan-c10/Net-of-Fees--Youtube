"""Script parsing: the contract between script_gen and every later stage."""

from __future__ import annotations

import pytest

from pipeline import script_format
from pipeline.errors import ValidationError
from pipeline.script_format import parse, render, to_scenes, validate_against_snapshot

GOOD = """# Title

[SCENE 1 | chart: breakeven]
Charges took 41 percent.

[SCENE 2 | chart: fee_stack | annotate: stt, gst]
Six line items, 62 rupees.
"""


def test_parses_scenes_and_attributes():
    scenes = to_scenes(parse(GOOD))
    assert [s.index for s in scenes] == [1, 2]
    assert scenes[1].visual_spec["data_key"] == "fee_stack"
    assert scenes[1].visual_spec["annotate"] == ["stt", "gst"]


def test_round_trip_is_lossless():
    scenes = to_scenes(parse(GOOD))
    again = to_scenes(parse(render(scenes, "Title")))
    assert [s.index for s in again] == [s.index for s in scenes]
    assert again[1].visual_spec == scenes[1].visual_spec


@pytest.mark.parametrize("bad,match", [
    ("[SCENE 1]\ntext\n[SCENE 3]\nmore", "no gaps"),
    ("[SCENE 1]\ntext\n[SCENE 1]\nmore", "duplicate"),
    ("no headers here", "no \\[SCENE"),
    ("[SCENE 1]\n", "no narration"),
    ("[SCENE 1 | bogus: x]\ntext", "unknown scene attribute"),
    ("[SCENE 2]\na\n[SCENE 1]\nb", "out of order"),
])
def test_malformed_scripts_are_rejected(bad, match):
    with pytest.raises(ValidationError, match=match):
        parse(bad)


def test_missing_data_key_returns_a_validation_error(fixture_snapshot):
    """The check that catches a scene the model invented."""
    import profiles
    scenes = to_scenes(parse("[SCENE 1 | chart: invented_key]\ntext here"))
    with pytest.raises(ValidationError, match="absent from"):
        validate_against_snapshot(scenes, fixture_snapshot,
                                  profiles.load("trading"))


def test_known_data_keys_pass(fixture_snapshot):
    import profiles
    scenes = to_scenes(parse(GOOD))
    validate_against_snapshot(scenes, fixture_snapshot, profiles.load("trading"))


def test_a_profiles_charts_are_valid_keys(fixture_snapshot):
    """A chart the profile implements needs no matching snapshot field."""
    import profiles
    scenes = to_scenes(parse("[SCENE 1 | chart: drag_ladder]\n41 percent gone."))
    validate_against_snapshot(scenes, fixture_snapshot, profiles.load("trading"))


# ---- the picture directive --------------------------------------------


def test_an_image_line_is_parsed_and_kept_out_of_the_narration():
    """The directive must never reach the voice engine.

    A line left in the body gets read aloud, and "IMAGE: a man in his
    fifties" in the middle of a story is unrecoverable.
    """
    scenes = script_format.to_scenes(script_format.parse(
        "[SCENE 1]\n\n"
        "IMAGE: a man on a doorstep at dusk, head lowered; medium shot\n\n"
        "He had not told anyone about the debt.\n"))
    assert scenes[0].text == "He had not told anyone about the debt."
    assert "IMAGE" not in scenes[0].text
    assert scenes[0].image_prompt.startswith("a man on a doorstep")


def test_the_directive_is_case_insensitive():
    scenes = script_format.to_scenes(script_format.parse(
        "[SCENE 1]\n\nimage: eleven coins, close up\n\nEleven coins.\n"))
    assert scenes[0].image_prompt == "eleven coins, close up"


def test_the_word_image_inside_a_sentence_is_still_narration():
    scenes = script_format.to_scenes(script_format.parse(
        "[SCENE 1]\n\nThe image of him stayed with her.\n"))
    assert scenes[0].text == "The image of him stayed with her."
    assert scenes[0].image_prompt == ""


def test_two_image_lines_in_one_scene_are_refused():
    with pytest.raises(ValidationError, match="two IMAGE"):
        script_format.parse(
            "[SCENE 1]\n\nIMAGE: a doorstep\nIMAGE: a table\n\nA line.\n")


def test_a_scene_of_only_an_image_line_has_no_narration():
    """Stripping the directive must not turn a real error into silence."""
    with pytest.raises(ValidationError, match="no narration"):
        script_format.parse("[SCENE 1]\n\nIMAGE: a doorstep at dusk\n")


def test_a_picture_script_survives_a_round_trip():
    original = script_format.to_scenes(script_format.parse(
        "[SCENE 1]\n\nIMAGE: a doorstep at dusk; wide shot\n\nOne.\n\n"
        "[SCENE 2 | voice: chandan_en]\n\nIMAGE: coins, close up\n\nTwo.\n"))
    again = script_format.to_scenes(
        script_format.parse(script_format.render(original, "T")))
    assert [(s.text, s.image_prompt) for s in again] == \
           [(s.text, s.image_prompt) for s in original]
