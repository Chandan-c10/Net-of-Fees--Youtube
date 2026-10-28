"""The alert channel, and the two properties it must never violate:
it must not leak a credential, and it must not mask a failure.
"""

import smtplib

import pytest

from pipeline import notify, scrub
from pipeline.errors import StageError

CREDS = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USER": "me@example.com",
    "SMTP_APP_PASSWORD": "app-password",
    "ALERT_EMAIL": "me@example.com",
}


@pytest.fixture
def configured(monkeypatch):
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)


def test_censor_replaces_a_secret_with_its_label():
    out = scrub.censor("token sk-abcdefghijklmnopqrstuvwxyz012345 was rejected")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "[redacted:openai_key]" in out


def test_censor_leaves_ordinary_text_alone():
    msg = "assemble rejected a 120ms drift between audio and video"
    assert scrub.censor(msg) == msg


def test_unconfigured_channel_names_what_is_missing(monkeypatch):
    for k in CREDS:
        monkeypatch.delenv(k, raising=False)
    ch = notify.Channel()
    assert not ch.configured
    # Credentials are optional: a local relay needs neither.
    assert set(ch.missing()) == {"SMTP_HOST", "ALERT_EMAIL"}


def test_half_a_credential_pair_is_refused(monkeypatch):
    """A user part with no password is a typo, not a local relay."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ALERT_EMAIL", "me@example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    ch = notify.Channel()
    assert not ch.configured
    assert any("together" in g for g in ch.missing())


def test_an_unauthenticated_relay_is_configured(monkeypatch):
    for k in CREDS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    ch = notify.Channel()
    assert ch.configured and not ch.authenticating


def test_configured_channel_is_complete(configured):
    ch = notify.Channel()
    assert ch.configured
    assert ch.missing() == []


def test_failure_is_silent_when_unconfigured(monkeypatch):
    for k in CREDS:
        monkeypatch.delenv(k, raising=False)
    # No exception, no send attempt.
    notify.failure("001", "voice", StageError("boom"))


def test_failure_survives_a_dead_mail_server(configured, monkeypatch):
    """The property that matters: a broken alert must not replace the
    error it was reporting."""
    def explode(self, subject, body):
        raise smtplib.SMTPConnectError(421, "service not available")

    monkeypatch.setattr(notify.Channel, "send", explode)
    notify.failure("001", "voice", StageError("the real failure"))


def test_failure_does_not_mail_a_credential(configured, monkeypatch):
    sent = {}

    def capture(self, subject, body):
        sent["subject"] = scrub.censor(subject)
        sent["body"] = scrub.censor(body)

    monkeypatch.setattr(notify.Channel, "send", capture)
    leaky = StageError("upload refused: AKIAIOSFODNN7EXAMPLE is not valid")
    notify.failure("007", "upload", leaky)

    assert "AKIAIOSFODNN7EXAMPLE" not in sent["body"]
    assert "[redacted:aws_access_key]" in sent["body"]


def test_failure_body_names_the_episode_and_stage(configured, monkeypatch):
    sent = {}
    monkeypatch.setattr(notify.Channel, "send",
                        lambda self, s, b: sent.update(subject=s, body=b))
    notify.failure("042", "assemble", StageError("drift too high"))
    assert "042" in sent["subject"] and "assemble" in sent["subject"]
    assert "nothing was published" in sent["body"]


def test_test_channel_reports_missing_config(monkeypatch):
    for k in CREDS:
        monkeypatch.delenv(k, raising=False)
    ok, detail = notify.test_channel()
    assert not ok and "not configured" in detail


def test_test_channel_reports_a_refused_connection(configured, monkeypatch):
    def explode(self, subject, body):
        raise smtplib.SMTPAuthenticationError(535, "bad credentials")

    monkeypatch.setattr(notify.Channel, "send", explode)
    ok, detail = notify.test_channel()
    assert not ok and "SMTPAuthenticationError" in detail
