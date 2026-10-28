"""Failure alerts over SMTP.

The user asked to be told by mail when something breaks. Two rules shape
this module:

1. An alert must never mask the failure it is reporting. Every send is
   wrapped; a dead mail server produces a warning in the log and the
   original exception still reaches the exit code unchanged.
2. An alert body carries an error message, and error messages quote
   paths, URLs and sometimes credentials. Everything goes through
   `scrub.censor` before it leaves the machine.

Unconfigured is a valid state, not an error: a laptop run with no SMTP
settings simply does not mail, and says so at debug level.
"""

from __future__ import annotations

import os
import smtplib
import socket
from email.message import EmailMessage

from . import scrub
from .logging_setup import get

TIMEOUT_SEC = 20

# Gmail and most providers: 587 STARTTLS, or 465 implicit TLS.
IMPLICIT_TLS_PORT = 465


class Channel:
    """An SMTP destination assembled from the environment."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER")
        self.password = os.getenv("SMTP_APP_PASSWORD")
        self.to = os.getenv("ALERT_EMAIL")
        # A relay on localhost commonly takes unauthenticated plaintext on
        # 25. Default stays on, so the public-provider path is encrypted
        # unless someone deliberately turns it off.
        self.starttls = os.getenv("SMTP_STARTTLS", "1") not in ("0", "false", "no")

    @property
    def authenticating(self) -> bool:
        return bool(self.user and self.password)

    @property
    def configured(self) -> bool:
        # A local relay needs no credentials, so host and recipient are the
        # only hard requirements; a half-filled credential pair is not.
        if not (self.host and self.to):
            return False
        return bool(self.user) == bool(self.password)

    def missing(self) -> list[str]:
        names = {"SMTP_HOST": self.host, "ALERT_EMAIL": self.to}
        gaps = [k for k, v in names.items() if not v]
        if bool(self.user) != bool(self.password):
            gaps.append("SMTP_USER and SMTP_APP_PASSWORD together")
        return gaps

    def send(self, subject: str, body: str) -> None:
        """Deliver one message. Raises on failure; callers decide what that means."""
        msg = EmailMessage()
        msg["Subject"] = scrub.censor(subject)
        msg["From"] = self.user or f"net-of-fees@{self.host}"
        msg["To"] = self.to
        msg.set_content(scrub.censor(body))

        if self.port == IMPLICIT_TLS_PORT:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT_SEC)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_SEC)
        with server:
            if self.port != IMPLICIT_TLS_PORT and self.starttls:
                server.starttls()
            if self.authenticating:
                server.login(self.user, self.password)
            server.send_message(msg)


def test_channel() -> tuple[bool, str]:
    """Prove the mail path works before a real failure depends on it.

    An alert channel nobody has exercised is not an alert channel, so
    `run.py notify-test` calls this and reports what happened.
    """
    ch = Channel()
    if not ch.configured:
        return False, "not configured: missing " + ", ".join(ch.missing())
    try:
        ch.send(
            "[net-of-fees] alert channel test",
            "This is a test of the failure-alert channel.\n\n"
            "If you are reading it, a real break will reach you here too.",
        )
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"sent to {ch.to}"


def failure(episode_id: str | None, stage: str | None, exc: BaseException) -> None:
    """Report a broken run. Swallows its own errors by design.

    Called from the top-level handler, where the process is already on its
    way to a non-zero exit. Raising here would replace a useful traceback
    with an SMTP one, so this never raises.
    """
    log = get(episode_id, stage)
    ch = Channel()
    if not ch.configured:
        log.debug("alerts not configured; skipping mail")
        return

    where = f"episode {episode_id}" if episode_id else "pipeline"
    if stage:
        where += f", stage {stage}"
    subject = f"[net-of-fees] {type(exc).__name__} in {where}"
    body = (
        f"{where} failed.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "The run stopped at this point; nothing was published. Check the\n"
        "episode log for the full traceback, then re-run the stage once\n"
        "the cause is fixed."
    )
    try:
        ch.send(subject, body)
        log.info("failure alert sent", extra={"to": ch.to})
    except Exception as mail_exc:  # noqa: BLE001 - must not mask `exc`
        log.warning(
            "could not send failure alert: %s: %s",
            type(mail_exc).__name__, mail_exc,
        )
