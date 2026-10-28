"""How a scene is read.

No model is needed for any of this: the plan is a pure function of the
text, and the stitching is arithmetic on arrays. What these cannot check
is how it sounds, which needs ears and the model installed.
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline.voices import delivery

PARAGRAPH = (
    "Eight bugs reached my live trading system. My test suite caught one "
    "of them. The other seven were found by the system doing something I "
    "could see was wrong, which is a slower and more expensive way to "
    "find a bug. That is the number worth sitting with. Not eight bugs."
)


# ---- splitting --------------------------------------------------------


def test_a_paragraph_becomes_one_beat_per_sentence():
    beats = delivery.split_beats("One. Two. Three.")
    assert beats == ["One.", "Two.", "Three."]


def test_a_short_sentence_is_not_split_at_its_commas():
    """Read in two pieces, a short sentence sounds hesitant."""
    short = "Here is every charge, on one round trip."
    assert delivery.split_beats(short) == [short]


def test_a_long_sentence_is_split_at_its_commas():
    """Read in one breath, a long sentence sounds rushed."""
    long = ("The other seven were found by the system doing something I "
            "could see was wrong, which is a slower and more expensive way "
            "to find a bug.")
    beats = delivery.split_beats(long)
    assert len(beats) > 1
    assert beats[0].endswith(","), "the comma stays with the clause it closes"


def test_a_long_sentence_with_no_comma_stays_whole():
    long = "x" * 200 + "."
    assert delivery.split_beats(long) == [long]


def test_splitting_loses_no_words():
    original = set(PARAGRAPH.replace(",", " ").split())
    rejoined = set(" ".join(delivery.split_beats(PARAGRAPH))
                   .replace(",", " ").split())
    assert original == rejoined


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_text_produces_no_beats(text):
    assert delivery.split_beats(text) == []
    assert delivery.plan(text) == []


# ---- modulation -------------------------------------------------------


def test_a_beat_carrying_a_number_slows_down():
    """What someone does when they want you to hear a number."""
    with_number = delivery.plan("Charges took 41 percent.")[0]
    without = delivery.plan("Charges took a large share.")[0]
    assert with_number.speed < without.speed


def test_a_connective_beat_speeds_up():
    plain = delivery.plan("The system found them.")[0]
    linking = delivery.plan("And the system found them.")[0]
    assert linking.speed > plain.speed


def test_a_short_beat_after_a_long_one_slows_to_land_it():
    beats = delivery.plan(
        "The other seven were found by the system doing something I could "
        "see was obviously wrong and expensive to discover that way. "
        "It mattered."
    )
    assert beats[-1].speed < delivery.BASE_SPEED


def test_speed_stays_inside_a_narrow_band():
    """Past a few percent it stops sounding like emphasis."""
    for scene in range(12):
        for beat in delivery.plan(PARAGRAPH, scene, "ep"):
            assert delivery.SPEED_FLOOR <= beat.speed <= delivery.SPEED_CEIL


def test_consecutive_beats_do_not_share_an_identical_speed():
    """Identical speeds across a paragraph are what make it sound flat."""
    speeds = [b.speed for b in delivery.plan(PARAGRAPH, 1, "002")]
    assert len(set(speeds)) > 1


# ---- pauses -----------------------------------------------------------


def test_a_question_pauses_longer_than_a_statement():
    question = delivery.plan("Why? It mattered.")[0]
    statement = delivery.plan("It failed. It mattered.")[0]
    assert question.pause_sec > statement.pause_sec


def test_a_comma_pauses_less_than_a_full_stop():
    long = ("Here is every single charge on one full round trip of the "
            "position you opened this morning, and the total of all of "
            "them comes to sixty two rupees exactly, which is the part "
            "that nobody ever mentions to you.")
    beats = delivery.plan(long)
    comma_beat = next(b for b in beats if b.ending == ",")
    assert comma_beat.pause_sec < delivery.PAUSE_BY_ENDING["."]


def test_the_last_beat_has_no_pause_of_its_own():
    """The gap between scenes belongs to pacing.scene_padding; two owners
    would double it."""
    assert delivery.plan(PARAGRAPH, 1, "002")[-1].pause_sec == 0.0


def test_no_pause_is_negative():
    for scene in range(10):
        for beat in delivery.plan(PARAGRAPH, scene, "ep"):
            assert beat.pause_sec >= 0.0


# ---- determinism ------------------------------------------------------


def test_the_same_scene_plans_identically():
    """Required, not merely tidy: the timing contract measures rendered
    audio, so a read that varied would move every visual with it."""
    assert delivery.plan(PARAGRAPH, 3, "042") == \
        delivery.plan(PARAGRAPH, 3, "042")


def test_a_different_scene_reads_differently():
    assert delivery.plan(PARAGRAPH, 1, "042") != \
        delivery.plan(PARAGRAPH, 2, "042")


def test_a_different_episode_reads_differently():
    assert delivery.plan(PARAGRAPH, 1, "042") != \
        delivery.plan(PARAGRAPH, 1, "043")


def test_room_tone_is_reproducible():
    a = delivery.room_tone(2400, "seed")
    b = delivery.room_tone(2400, "seed")
    assert np.array_equal(a, b)
    assert not np.array_equal(a, delivery.room_tone(2400, "other"))


# ---- room tone --------------------------------------------------------


def test_room_tone_sits_below_audibility():
    """It exists so the floor does not drop to nothing. Much above
    -55 dBFS and it reads as hiss."""
    tone = delivery.room_tone(24_000, "s")
    rms_db = 20 * np.log10(np.sqrt(np.mean(tone ** 2)))
    assert -70 < rms_db < -55


def test_room_tone_is_not_digital_silence():
    """The whole point: silence between phrases is the clearest sign of a
    synthetic read."""
    tone = delivery.room_tone(2400, "s")
    assert np.any(tone != 0.0)


def test_room_tone_has_no_dc_offset():
    tone = delivery.room_tone(24_000, "s")
    assert abs(float(np.mean(tone))) < 1e-3


def test_zero_length_room_tone_is_empty_not_an_error():
    for n in (0, -1):
        assert len(delivery.room_tone(n, "s")) == 0


# ---- stitching --------------------------------------------------------


def _segments(beats, seconds=0.4, level=0.3):
    return [np.full(int(seconds * delivery.SAMPLE_RATE), level,
                    dtype=np.float32) for _ in beats]


def test_stitching_adds_every_pause():
    beats = delivery.plan(PARAGRAPH, 1, "002")
    segments = _segments(beats)
    out = delivery.stitch(segments, beats, "seed")
    expected = (sum(len(s) for s in segments)
                + sum(int(b.pause_sec * delivery.SAMPLE_RATE) for b in beats))
    assert len(out) == expected


def test_stitching_fades_the_joins():
    """An abrupt join clicks."""
    beats = delivery.plan("One. Two.", 1, "x")
    out = delivery.stitch(_segments(beats), beats, "seed")
    assert abs(float(out[0])) < 0.05, "should fade in"
    assert abs(float(out[-1])) < 0.05, "should fade out"


def test_stitching_does_not_clip():
    beats = delivery.plan(PARAGRAPH, 1, "002")
    out = delivery.stitch(_segments(beats, level=0.95), beats, "seed")
    assert float(np.max(np.abs(out))) <= 1.0


def test_a_segment_count_mismatch_is_an_error():
    """Silently dropping a beat would desync the whole scene."""
    beats = delivery.plan(PARAGRAPH, 1, "002")
    with pytest.raises(ValueError, match="one to one"):
        delivery.stitch(_segments(beats)[:-1], beats, "seed")


def test_stitching_nothing_returns_an_empty_array():
    assert len(delivery.stitch([], [], "seed")) == 0


# ---- the report -------------------------------------------------------


def test_describe_reports_the_spread():
    got = delivery.describe(PARAGRAPH, 1, "002")
    assert got["beats"] > 3
    assert got["speed_min"] < got["speed_max"]
    assert got["pause_total_sec"] > 0
