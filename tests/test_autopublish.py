"""Automatic publishing, and the line it must not cross."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pipeline import schedule
from pipeline.config import CHANNEL_TZ
from pipeline.dashboard import Panel, standing_of
from pipeline.errors import GateError, PipelineError
from pipeline.manifest import PUBLISH_MODES, Manifest


def approved(mode="manual", published=False, after=None) -> Manifest:
    m = Manifest.new("x", recipe="r", profile="trading", voice="v")
    m.pass_gate_1()
    m.pass_gate_2()
    m.publish_mode = mode
    m.publish_after = after
    if published:
        m.published_at = "2026-11-04T00:00:00Z"
    return m


# ---- slots ------------------------------------------------------------


@pytest.mark.parametrize("at,expect_hour", [("06:00", 8), ("09:00", 19)])
def test_the_next_slot_is_the_next_one_today(at, expect_hour):
    now = datetime.fromisoformat(f"2026-11-04 {at}").replace(tzinfo=CHANNEL_TZ)
    assert schedule.next_slot(now).hour == expect_hour


def test_after_the_last_slot_it_rolls_to_tomorrow():
    now = datetime.fromisoformat("2026-11-04 20:00").replace(tzinfo=CHANNEL_TZ)
    assert schedule.next_slot(now).day == 5


def test_a_time_that_has_passed_is_due():
    past = (datetime.now(CHANNEL_TZ) - timedelta(hours=1)).isoformat()
    assert schedule.is_due(past)


def test_a_future_time_is_not_due():
    ahead = (datetime.now(CHANNEL_TZ) + timedelta(hours=2)).isoformat()
    assert not schedule.is_due(ahead)


def test_no_time_at_all_is_due_now():
    """An episode marked for automatic publishing must not sit forever."""
    assert schedule.is_due(None)


def test_an_unparseable_time_is_due_rather_than_stuck():
    assert schedule.is_due("not a date")


# ---- what gets picked up ----------------------------------------------


def test_manual_is_never_picked_up():
    assert schedule.due_episodes([approved("manual")]) == []


def test_on_approval_is_picked_up_immediately():
    assert len(schedule.due_episodes([approved("on_approval")])) == 1


def test_at_slot_waits_for_its_time():
    ahead = (datetime.now(CHANNEL_TZ) + timedelta(hours=2)).isoformat()
    assert schedule.due_episodes([approved("at_slot", after=ahead)]) == []
    past = (datetime.now(CHANNEL_TZ) - timedelta(minutes=1)).isoformat()
    assert len(schedule.due_episodes([approved("at_slot", after=past)])) == 1


def test_an_unapproved_episode_is_never_picked_up():
    """The line: a timer posts what a person approved, and nothing else."""
    m = Manifest.new("x", recipe="r", profile="trading", voice="v")
    m.publish_mode = "on_approval"
    assert m.approved_at is None
    assert schedule.due_episodes([m]) == []


def test_an_already_published_episode_is_not_republished():
    assert schedule.due_episodes([approved("on_approval", published=True)]) == []


# ---- the gate still holds ---------------------------------------------


def test_automatic_publishing_cannot_clear_a_gate(monkeypatch, tmp_path):
    """Whatever the publish mode, a scheduled run may not approve."""
    from pipeline import gates
    from pipeline.manifest import Episode

    ep = Episode("x", tmp_path)
    ep.ensure_dirs()
    m = Manifest.new("x", recipe="r", profile="trading", voice="v")
    m.publish_mode = "on_approval"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    with pytest.raises(GateError, match="human decision"):
        gates.approve_gate_2(ep, m)
    assert m.gate_2_passed_at is None


def test_every_mode_is_known():
    assert set(PUBLISH_MODES) == {"manual", "on_approval", "at_slot"}


def test_the_panel_refuses_an_unknown_mode(tmp_path):
    panel = Panel(tmp_path, None)
    with pytest.raises(PipelineError, match="unknown publish mode"):
        panel.set_publish_mode("x", "whenever")


def test_an_approved_episode_shows_as_ready_to_publish():
    summary = {"stages": {}, "published": False, "gate_1": True,
               "gate_2": True, "gate_1_blockers": [], "gate_2_blockers": []}
    assert standing_of(summary, False).label == "ready to publish"
