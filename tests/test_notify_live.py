"""The alert channel against a real SMTP server.

The sibling file monkeypatches `Channel.send` and so proves the policy
around sending. This one speaks the real protocol over a real socket:
smtplib drives the conversation, the server collects the DATA, and the
test reads back the message that actually crossed the wire.
"""

import socket
import threading

import pytest

from pipeline import notify
from pipeline.errors import StageError


class TinySMTP(threading.Thread):
    """A single-connection SMTP sink. Enough of RFC 5321 for smtplib.

    Deliberately not aiosmtpd: a real server here means no new dependency
    and no event loop inside the test.
    """

    daemon = True

    def __init__(self, reject_at: str | None = None) -> None:
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.reject_at = reject_at
        self.message = ""
        self.envelope: list[str] = []
        self.ready = threading.Event()

    def run(self) -> None:
        conn, _ = self.sock.accept()
        f = conn.makefile("rwb")

        def say(line: str) -> None:
            f.write(line.encode() + b"\r\n")
            f.flush()

        say("220 tiny.test ESMTP")
        in_data = False
        body: list[str] = []
        while True:
            raw = f.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")

            if in_data:
                if line == ".":
                    in_data = False
                    self.message = "\n".join(body)
                    say("250 2.0.0 queued")
                    continue
                body.append(line)
                continue

            verb = line.split(" ")[0].upper()
            if verb == "EHLO":
                # No STARTTLS advertised: this test exercises the relay path.
                say("250-tiny.test")
                say("250 8BITMIME")
            elif verb == "HELO":
                say("250 tiny.test")
            elif verb in ("MAIL", "RCPT"):
                if self.reject_at == verb:
                    say("550 5.1.1 rejected")
                else:
                    self.envelope.append(line)
                    say("250 2.1.0 ok")
            elif verb == "DATA":
                say("354 end with .")
                in_data = True
            elif verb == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
        f.close()
        conn.close()
        self.sock.close()
        self.ready.set()


@pytest.fixture
def relay(monkeypatch):
    server = TinySMTP()
    server.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(server.port))
    monkeypatch.setenv("SMTP_STARTTLS", "0")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    yield server


def test_a_real_message_crosses_the_wire(relay):
    notify.Channel().send("subject line", "body line")
    relay.join(timeout=5)

    assert "Subject: subject line" in relay.message
    assert "body line" in relay.message
    assert "To: ops@example.com" in relay.message
    assert any("ops@example.com" in line for line in relay.envelope)


def test_a_real_failure_alert_names_the_episode(relay):
    notify.failure("realtest", "assemble", StageError("drift was 120ms"))
    relay.join(timeout=5)

    assert "StageError" in relay.message
    assert "realtest" in relay.message
    assert "assemble" in relay.message
    assert "drift was 120ms" in relay.message
    assert "nothing was published" in relay.message


def test_a_real_credential_is_not_in_the_bytes_that_were_sent(relay):
    """The property that matters, checked on the wire rather than on the
    arguments: the token must not leave the machine."""
    leaked = "AKIAIOSFODNN7EXAMPLE"
    notify.failure("realtest", "upload",
                   StageError(f"rejected key {leaked}"))
    relay.join(timeout=5)

    assert leaked not in relay.message
    assert "[redacted:aws_access_key]" in relay.message


def test_a_real_rejection_does_not_mask_the_original_failure(monkeypatch):
    server = TinySMTP(reject_at="RCPT")
    server.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(server.port))
    monkeypatch.setenv("SMTP_STARTTLS", "0")
    monkeypatch.setenv("ALERT_EMAIL", "nobody@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)

    # The server says 550. failure() must swallow it.
    notify.failure("realtest", "voice", StageError("the real problem"))


def test_notify_test_succeeds_against_a_real_server(relay):
    ok, detail = notify.test_channel()
    relay.join(timeout=5)
    assert ok, detail
    assert "ops@example.com" in detail
    assert "alert channel test" in relay.message


def test_a_closed_port_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")
    monkeypatch.setenv("SMTP_STARTTLS", "0")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    ok, detail = notify.test_channel()
    assert not ok and detail
