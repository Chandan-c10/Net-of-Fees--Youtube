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
from email.message import EmailMessage

from . import health, scrub
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
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"sent to {ch.to}"


# Known failures and what to do about them. A mail that says what broke
# and not what to do about it makes the reader go and look it up, which is
# the work the mail was supposed to save.
REMEDIES: tuple[tuple[str, str], ...] = (
    ("youtube_token_file", "Run: python tools/youtube_auth.py"),
    ("no youtube credentials",
     "Run: python tools/youtube_auth.py  (add --channel NAME for a second "
     "channel)"),
    ("ig_user_id", "Set IG_USER_ID and IG_ACCESS_TOKEN. See README, "
                   "'Instagram setup'."),
    ("ig_access_token", "Set IG_USER_ID and IG_ACCESS_TOKEN. See README, "
                        "'Instagram setup'."),
    ("voice engine needs", "pip install -r requirements-voice.txt"),
    ("draft voice engine",
     "Re-narrate with a real voice:  run.py <id> --stage voice --force "
     "--voice-engine kokoro"),
    ("quota", "The daily API quota is spent. Retry tomorrow; this does not "
              "consume a retry."),
    ("drift", "Re-run the render:  run.py <id> --stage assemble --force. If "
              "it persists, a scene's measured duration is wrong -- re-run "
              "the voice stage first."),
    ("9:16", "Render the vertical cut from a _shorts profile; Reels must be "
             "1080x1920."),
    ("no vertical cut", "Run the shorts stage:  run.py <id> --stage shorts"),
    ("run the shorts stage", "run.py <id> --stage shorts"),
    ("run the metadata stage", "run.py <id> --stage metadata"),
    ("run voice first", "run.py <id> --stage voice"),
    ("fewer than", "Add more rows to data/incidents.json. The recipe will "
                   "not invent one."),
    ("ffmpeg", "Install FFmpeg, then check it with:  run.py doctor"),
    ("scrub", "A secret was found in the episode data. Remove it from the "
              "source file and re-run the scrub stage."),
    ("no profile", "Check the profile name:  run.py doctor  lists them."),
    ("smtp", "Check the SMTP settings with:  run.py notify-test"),
)


def remedy_for(error_class: str, message: str) -> str | None:
    """A concrete next step, matched on the text of a failure.

    Takes strings rather than an exception so a stored incident can be
    looked up too, without fabricating an exception class to hold it.
    """
    haystack = f"{error_class} {message}".lower()
    for needle, fix in REMEDIES:
        if needle in haystack:
            return fix
    return None


def remedy(exc: BaseException) -> str | None:
    return remedy_for(type(exc).__name__, str(exc))


def _try_send(ch: Channel, subject: str, body: str, log) -> None:
    """Send, or warn. Never raises: a broken alert must not replace the
    error it was reporting."""
    try:
        ch.send(subject, body)
        log.info("alert sent", extra={"to": ch.to})
    except Exception as mail_exc:
        log.warning("could not send alert: %s: %s",
                    type(mail_exc).__name__, mail_exc)


def failure(episode_id: str | None, stage: str | None, exc: BaseException,
            log_path: str | None = None) -> None:
    """Record a broken run, and mail about it once.

    The ledger decides whether to mail: a first sighting does, repeats are
    counted silently, and a problem still open after a day reminds.
    Without that, a stage failing on a timer twice a day would send sixty
    identical mails a month and the inbox would stop being read.

    Swallows its own errors by design -- the process is already heading
    for a non-zero exit, and raising here would replace a useful traceback
    with an SMTP one.
    """
    log = get(episode_id, stage)
    try:
        incident, notify_now = health.failed(
            episode_id, stage or "pipeline", exc, log_path)
    except Exception as ledger_exc:
        log.warning("could not record the failure: %s", ledger_exc)
        incident, notify_now = None, True

    ch = Channel()
    if not ch.configured:
        log.debug("alerts not configured; skipping mail")
        return
    if not notify_now:
        log.info("already reported; not mailing again")
        return

    where = f"episode {episode_id}" if episode_id else "pipeline"
    if stage:
        where += f", stage {stage}"
    detail = (incident.describe() if incident
              else f"{type(exc).__name__} in {where}\n  {exc}")
    fix = remedy(exc)
    parts = ["The system broke. Nothing was published.", "", detail, ""]
    if fix:
        parts += ["What to do:", f"  {fix}", ""]
    parts.append(
        "This stays on the open list until that stage runs successfully.\n"
        "Once it does, you get a note saying it recovered."
    )
    _try_send(ch, f"[net-of-fees] broken: {type(exc).__name__} in {where}",
              "\n".join(parts), log)


def recovered(episode_id: str | None, stage: str) -> None:
    """Close anything open for this stage, and say so if something closed.

    Knowing a thing is fixed is the other half of knowing it broke, and
    silence is indistinguishable from the alerting itself being broken.
    """
    log = get(episode_id, stage)
    try:
        closed = health.succeeded(episode_id, stage)
    except Exception as ledger_exc:
        log.debug("could not update the ledger: %s", ledger_exc)
        return
    if not closed:
        return
    ch = Channel()
    if not ch.configured:
        return
    _try_send(
        ch, f"[net-of-fees] recovered: {stage}",
        f"{stage} ran successfully, so this is now closed:\n\n"
        + "\n\n".join(i.describe() for i in closed)
        + "\n\nNothing further to do.",
        log,
    )


def open_issues(hours: int = 24) -> None:
    """Mail the open list, and only if something is on it.

    Deliberately not a daily all-clear: mail arrives when there is a
    problem, so an empty inbox means nothing is wrong.
    """
    log = get(None, "health")
    ledger = health.Ledger.load()
    if ledger.healthy():
        log.info("nothing open; not mailing")
        return
    ch = Channel()
    if not ch.configured:
        return
    count = len(ledger.open_incidents())
    _try_send(ch, f"[net-of-fees] {count} still broken",
              ledger.digest(hours), log)
