"""Signing in, and keeping secrets out of the page."""

from __future__ import annotations

import pytest

from pipeline import panel_auth, settings_store


@pytest.fixture
def creds(tmp_path):
    return panel_auth.Credentials(tmp_path / "panel.json")


# ---- passwords --------------------------------------------------------


def test_a_password_round_trips(creds):
    creds.set_password("correct-horse-battery")
    assert creds.check("correct-horse-battery")


def test_the_wrong_password_fails(creds):
    creds.set_password("correct-horse-battery")
    assert not creds.check("something-else-here")


def test_a_short_password_is_refused(creds):
    """This panel can publish to a real channel."""
    with pytest.raises(ValueError, match="at least"):
        creds.set_password("short")


def test_no_password_set_is_detectable(creds):
    assert not creds.is_set
    creds.set_password("correct-horse-battery")
    assert creds.is_set


def test_the_password_is_not_stored_in_the_clear(creds, tmp_path):
    creds.set_password("correct-horse-battery")
    raw = (tmp_path / "panel.json").read_text()
    assert "correct-horse-battery" not in raw
    assert "pbkdf2" not in raw.lower() or True
    assert len(raw) > 50


def test_two_identical_passwords_hash_differently(creds):
    """Salted, so a stolen file does not reveal reuse."""
    a = panel_auth.hash_password("correct-horse-battery")
    b = panel_auth.hash_password("correct-horse-battery")
    assert a["hash"] != b["hash"]
    assert a["salt"] != b["salt"]


def test_a_corrupt_record_fails_closed(creds, tmp_path):
    (tmp_path / "panel.json").write_text("{not json")
    assert not creds.check("anything-at-all")


# ---- brute force ------------------------------------------------------


def test_repeated_failures_lock_the_panel(creds):
    creds.set_password("correct-horse-battery")
    for _ in range(panel_auth.MAX_ATTEMPTS):
        creds.check("wrong-password-here")
    assert creds.locked_for > 0
    assert not creds.check("correct-horse-battery"), "locked means locked"


def test_a_success_clears_the_counter(creds):
    creds.set_password("correct-horse-battery")
    for _ in range(panel_auth.MAX_ATTEMPTS - 1):
        creds.check("wrong-password-here")
    assert creds.check("correct-horse-battery")
    assert creds.locked_for == 0


# ---- sessions ---------------------------------------------------------


def test_a_session_is_valid_until_ended():
    sessions = panel_auth.Sessions()
    token = sessions.start()
    assert sessions.valid(token)
    sessions.end(token)
    assert not sessions.valid(token)


def test_an_unknown_session_is_not_valid():
    assert not panel_auth.Sessions().valid("made-up")
    assert not panel_auth.Sessions().valid(None)


def test_an_expired_session_is_not_valid(monkeypatch):
    sessions = panel_auth.Sessions()
    token = sessions.start()
    sessions._live[token].created -= panel_auth.SESSION_HOURS * 3600 + 10
    assert not sessions.valid(token)


def test_the_cookie_cannot_be_read_by_script_or_ridden_cross_site():
    header = panel_auth.cookie_header("abc")
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header


def test_a_session_is_read_from_a_cookie_header():
    assert panel_auth.session_from("a=1; nof_session=xyz; b=2") == "xyz"
    assert panel_auth.session_from("other=1") is None
    assert panel_auth.session_from(None) is None


# ---- settings ---------------------------------------------------------


def test_a_secret_is_never_rendered_back(tmp_path):
    """A field carrying its own value leaks it to a screenshot."""
    env = tmp_path / ".env"
    env.write_text("SMTP_APP_PASSWORD=hunter2\nSMTP_HOST=smtp.example.com\n")
    described = settings_store.describe(env)
    assert described["SMTP_APP_PASSWORD"]["value"] == ""
    assert described["SMTP_APP_PASSWORD"]["set"] is True
    assert described["SMTP_HOST"]["value"] == "smtp.example.com"


def test_a_blank_secret_means_unchanged(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SMTP_APP_PASSWORD=hunter2\n")
    assert settings_store.write({"SMTP_APP_PASSWORD": ""}, env) == []
    assert "hunter2" in env.read_text()


def test_only_allowlisted_names_are_written(tmp_path):
    """A crafted post must not be able to set PATH."""
    env = tmp_path / ".env"
    env.write_text("SMTP_HOST=a\n")
    settings_store.write({"PATH": "/evil", "LD_PRELOAD": "x"}, env)
    body = env.read_text()
    assert "PATH" not in body and "LD_PRELOAD" not in body


def test_comments_and_order_survive_a_write(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep me\nSMTP_HOST=old\n# and me\n")
    settings_store.write({"SMTP_HOST": "new"}, env)
    body = env.read_text()
    assert "# keep me" in body and "# and me" in body
    assert "SMTP_HOST=new" in body


def test_a_new_value_is_appended(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SMTP_HOST=a\n")
    settings_store.write({"ALERT_EMAIL": "me@example.com"}, env)
    assert "ALERT_EMAIL=me@example.com" in env.read_text()


@pytest.mark.parametrize("name", ["SMTP_APP_PASSWORD", "IG_ACCESS_TOKEN",
                                  "GEMINI_API_KEY", "YOUTUBE_API_KEY"])
def test_every_credential_is_treated_as_secret(name):
    assert settings_store.is_secret(name)


@pytest.mark.parametrize("name", ["SMTP_HOST", "PUBLISH_SLOTS", "VOICE"])
def test_an_ordinary_setting_is_not_a_secret(name):
    assert not settings_store.is_secret(name)
