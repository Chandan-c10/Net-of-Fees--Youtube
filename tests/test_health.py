"""The failure ledger.

Two properties carry the weight: the same problem seen twice must not
mail twice, and an incident must stay open until the thing that broke
actually works again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import health, notify
from pipeline.errors import ConfigError, GateError, StageError


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "health.json"


# ---- fingerprinting ---------------------------------------------------


def test_the_same_problem_with_different_numbers_is_one_incident():
    """The message carries a scene number, a duration or a path that
    changes every run. Keying on raw text would open a fresh incident
    each time and mail about each one."""
    a = health.fingerprint("voice", "StageError", "no audio for scene 3")
    b = health.fingerprint("voice", "StageError", "no audio for scene 7")
    assert a == b


def test_paths_do_not_distinguish_incidents():
    a = health.fingerprint("x", "E", "cannot read /Users/a/ep/001/f.wav")
    b = health.fingerprint("x", "E", "cannot read /Users/b/ep/999/f.wav")
    assert a == b


def test_hex_ids_do_not_distinguish_incidents():
    a = health.fingerprint("x", "E", "upload ref deadbeefcafe failed")
    b = health.fingerprint("x", "E", "upload ref 0123456789ab failed")
    assert a == b


def test_genuinely_different_problems_stay_separate():
    a = health.fingerprint("voice", "StageError", "no audio")
    assert a != health.fingerprint("voice", "StageError", "model missing")
    assert a != health.fingerprint("assemble", "StageError", "no audio")
    assert a != health.fingerprint("voice", "ConfigError", "no audio")


# ---- not flooding the inbox -------------------------------------------


def test_the_first_sighting_mails(ledger_path):
    _, should_mail = health.failed("001", "voice", StageError("boom"),
                                   path=ledger_path)
    assert should_mail


def test_a_repeat_is_counted_but_not_mailed(ledger_path):
    """A stage failing twice a day would otherwise send sixty identical
    mails a month."""
    health.failed("001", "voice", StageError("no audio for scene 3"),
                  path=ledger_path)
    incident, should_mail = health.failed(
        "002", "voice", StageError("no audio for scene 9"), path=ledger_path)
    assert not should_mail
    assert incident.occurrences == 2


def test_a_different_problem_mails_even_while_one_is_open(ledger_path):
    health.failed("001", "voice", StageError("no audio"), path=ledger_path)
    _, should_mail = health.failed("001", "assemble", StageError("drift"),
                                   path=ledger_path)
    assert should_mail
    assert len(health.Ledger.load(ledger_path).open_incidents()) == 2


def test_a_long_standing_problem_reminds(ledger_path):
    ledger = health.Ledger()
    stale = (datetime.now(UTC)
             - timedelta(hours=health.RENOTIFY_HOURS + 1)).isoformat()
    ledger.record_failure("001", "voice", StageError("boom"))
    ledger.incidents[0].notified_at = stale
    ledger.save(ledger_path)

    _, should_mail = health.failed("001", "voice", StageError("boom"),
                                   path=ledger_path)
    assert should_mail, "a problem still open after a day should remind"


# ---- only a success closes it -----------------------------------------


def test_a_success_closes_the_open_incident(ledger_path):
    health.failed("001", "voice", StageError("boom"), path=ledger_path)
    closed = health.succeeded("002", "voice", path=ledger_path)
    assert len(closed) == 1
    assert health.Ledger.load(ledger_path).healthy()


def test_a_success_elsewhere_does_not_close_it(ledger_path):
    """Nothing clears on a timer or by proxy: a problem nobody fixed has
    not gone away."""
    health.failed("001", "voice", StageError("boom"), path=ledger_path)
    assert health.succeeded("001", "charts", path=ledger_path) == []
    assert not health.Ledger.load(ledger_path).healthy()


def test_a_closed_incident_does_not_reopen_itself(ledger_path):
    health.failed("001", "voice", StageError("boom"), path=ledger_path)
    health.succeeded("001", "voice", path=ledger_path)
    incident, should_mail = health.failed("003", "voice",
                                          StageError("boom"),
                                          path=ledger_path)
    assert should_mail, "it broke again, so it should mail again"
    assert incident.occurrences == 1, "a new incident, not the old one"


# ---- persistence ------------------------------------------------------


def test_the_ledger_survives_the_process(ledger_path):
    health.failed("001", "voice", StageError("boom"), path=ledger_path)
    reloaded = health.Ledger.load(ledger_path)
    assert len(reloaded.open_incidents()) == 1
    assert reloaded.open_incidents()[0].stage == "voice"


def test_a_corrupt_ledger_does_not_stop_a_render(ledger_path):
    ledger_path.write_text("{not json", encoding="utf-8")
    ledger = health.Ledger.load(ledger_path)
    assert ledger.healthy()
    ledger.record_failure("001", "voice", StageError("boom"))
    ledger.save(ledger_path)
    assert json.loads(ledger_path.read_text())["incidents"]


def test_the_ledger_cannot_grow_without_limit(ledger_path):
    ledger = health.Ledger()
    for i in range(health.MAX_RUNS + 80):
        ledger.record_success("001", f"stage{i}")
    ledger.save(ledger_path)
    assert len(health.Ledger.load(ledger_path).runs) <= health.MAX_RUNS


# ---- what the mail says -----------------------------------------------


def test_an_incident_says_where_to_look(ledger_path):
    incident, _ = health.failed("042", "assemble", StageError("drift 120ms"),
                                "episodes/042/run.log", path=ledger_path)
    described = incident.describe()
    assert "042" in described and "assemble" in described
    assert "episodes/042/run.log" in described
    assert "first seen" in described


def test_the_digest_says_nothing_is_broken_when_nothing_is(ledger_path):
    assert "Nothing is broken" in health.Ledger.load(ledger_path).digest()


def test_the_digest_lists_what_is_open(ledger_path):
    health.failed("001", "voice", StageError("no audio"), path=ledger_path)
    digest = health.Ledger.load(ledger_path).digest()
    assert "1 still open" in digest
    assert "no audio" in digest


@pytest.mark.parametrize("exc,expected", [
    (GateError("narrated with the draft voice engine"), "--stage voice"),
    (ConfigError("the Kokoro voice engine needs: torch"), "requirements-voice"),
    (StageError("rejected a 120ms drift between audio and video"), "assemble"),
    (ConfigError("no YouTube credentials at secrets/x.json"), "youtube_auth"),
    (StageError("Reels want 9:16"), "_shorts"),
    (StageError("YouTube daily quota exhausted"), "tomorrow"),
])
def test_a_known_failure_carries_its_fix(exc, expected):
    """A mail naming the error but not the fix makes the reader go and
    look it up, which is the work the mail was supposed to save."""
    fix = notify.remedy(exc)
    assert fix and expected in fix


def test_an_unknown_failure_simply_has_no_fix():
    assert notify.remedy(StageError("something nobody has seen")) is None


def test_a_stored_incident_can_be_looked_up_by_text(ledger_path):
    """The CLI reads incidents back from disk, where there is no exception
    object left to inspect."""
    incident, _ = health.failed(
        "001", "voice", GateError("narrated with the draft voice engine"),
        path=ledger_path)
    assert notify.remedy_for(incident.error_class, incident.message)


# ---- mail only on a problem -------------------------------------------


def test_nothing_is_mailed_when_nothing_is_open(ledger_path, monkeypatch):
    """Mail arrives when there is a problem, so an empty inbox means
    nothing is wrong."""
    sent = []
    monkeypatch.setattr(notify.Channel, "send",
                        lambda self, s, b: sent.append(s))
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("ALERT_EMAIL", "a@b.c")
    monkeypatch.setattr(health, "LEDGER", ledger_path)
    notify.open_issues()
    assert sent == []


def test_the_open_list_is_mailed_when_something_is_open(ledger_path,
                                                        monkeypatch):
    sent = []
    monkeypatch.setattr(notify.Channel, "send",
                        lambda self, s, b: sent.append((s, b)))
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("ALERT_EMAIL", "a@b.c")
    monkeypatch.setattr(health, "LEDGER", ledger_path)
    health.failed("001", "voice", StageError("no audio"), path=ledger_path)
    notify.open_issues()
    assert sent and "still broken" in sent[0][0]
    assert "no audio" in sent[0][1]
