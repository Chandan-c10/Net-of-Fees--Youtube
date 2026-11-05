"""Batch drafting and the release plan."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pipeline import config, schedule
from pipeline.config import CHANNEL_TZ, _parse_slots


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CHANNEL_TZ)


# ---- configurable slots -----------------------------------------------


def test_slots_parse_from_a_setting():
    assert _parse_slots("07:00,12:30,21:15") == ((7, 0), (12, 30), (21, 15))


def test_slots_are_sorted_and_deduplicated():
    assert _parse_slots("19:30,08:30,19:30") == ((8, 30), (19, 30))


@pytest.mark.parametrize("bad", ["", "   ", "25:00", "08:70", "half eight",
                                 "8-30"])
def test_a_bad_slot_is_refused(bad):
    with pytest.raises(ValueError):
        _parse_slots(bad)


def test_the_default_is_twice_a_day():
    assert len(config.PUBLISH_SLOTS) == 2


# ---- release planning -------------------------------------------------


def test_one_release_per_episode_in_order():
    plan = schedule.plan_releases(5, at("2026-11-04 10:00"))
    assert len(plan) == 5
    assert plan == sorted(plan), "releases must run forwards"


def test_the_first_release_is_after_now():
    now = at("2026-11-04 10:00")
    assert schedule.plan_releases(1, now)[0] > now


def test_the_number_of_slots_is_the_number_per_day():
    plan = schedule.plan_releases(6, at("2026-11-04 00:01"))
    days = {d.date() for d in plan}
    assert len(plan) / len(days) <= len(config.PUBLISH_SLOTS)


def test_a_queue_that_runs_too_far_ahead_is_refused():
    """A queue longer than a fortnight was written about a different
    week."""
    too_many = len(config.PUBLISH_SLOTS) * (config.MAX_QUEUE_DAYS + 5)
    with pytest.raises(ValueError, match="queue fewer"):
        schedule.plan_releases(too_many, at("2026-11-04 10:00"))


def test_zero_or_negative_is_refused():
    for count in (0, -3):
        with pytest.raises(ValueError, match="cannot plan"):
            schedule.plan_releases(count)


def test_a_planned_episode_is_not_due_before_its_slot():
    plan = schedule.plan_releases(3, at("2026-11-04 10:00"))
    assert not schedule.is_due(plan[-1].isoformat())


def test_a_planned_episode_is_due_once_its_slot_passes():
    when = datetime.now(CHANNEL_TZ) - timedelta(minutes=1)
    assert schedule.is_due(when.isoformat())


# ---- the gate still applies -------------------------------------------


def test_a_batched_episode_is_not_publishable_until_approved():
    """A batch creates work to review, never work that publishes
    itself."""
    from pipeline.manifest import Manifest
    m = Manifest.new("b1", recipe="r", profile="trading", voice="v")
    m.publish_mode = "at_slot"
    m.publish_after = (datetime.now(CHANNEL_TZ) - timedelta(hours=1)).isoformat()
    assert m.approved_at is None
    assert schedule.due_episodes([m]) == [], "unapproved, so not due"
