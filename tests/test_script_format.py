"""Script parsing: the contract between script_gen and every later stage."""

from __future__ import annotations

import pytest

from pipeline.errors import ValidationError
from pipeline.script_format import (parse, render, to_scenes,
                                    validate_against_snapshot)

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
    scenes = to_scenes(parse("[SCENE 1 | chart: invented_key]\ntext here"))
    with pytest.raises(ValidationError, match="absent from the snapshot"):
        validate_against_snapshot(scenes, fixture_snapshot)


def test_known_data_keys_pass(fixture_snapshot):
    scenes = to_scenes(parse(GOOD))
    validate_against_snapshot(scenes, fixture_snapshot)
