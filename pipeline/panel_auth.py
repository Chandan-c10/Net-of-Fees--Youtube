"""A password for the control panel.

The panel already refuses to serve beyond this machine without a token,
and a token in the URL is fine for a script. For a person on a phone it
is not: it sits in browser history, it is visible over a shoulder, and
it travels whenever the link is shared.

So: a password, a session cookie, and a token that still works for
anything automated.

Nothing here is novel, and that is deliberate. PBKDF2 from the standard
library, a random session id, HttpOnly and SameSite=Strict on the
cookie, constant-time comparison, and a lockout after repeated failures.
A single-user panel does not need more, and inventing more would be how
it gets weaker.

The password hash lives in `secrets/panel.json`, which is gitignored
along with everything else in that directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

CREDENTIAL_FILE = Path("secrets") / "panel.json"

# PBKDF2 rounds. High enough that guessing is slow, low enough that a
# login does not feel broken on a laptop.
ROUNDS = 240_000
SALT_BYTES = 16

SESSION_COOKIE = "nof_session"
SESSION_HOURS = 12

# Repeated failures lock the panel briefly. A single-user tool does not
# need an account system; it needs an attacker to be unable to try
# thousands of passwords a minute.
MAX_ATTEMPTS = 6
LOCKOUT_SECONDS = 300

MIN_PASSWORD = 10


@dataclass
class Session:
    token: str
    created: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > SESSION_HOURS * 3600


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, ROUNDS)
    return {"salt": salt.hex(), "hash": digest.hex(), "rounds": ROUNDS}


def verify_password(password: str, record: dict) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        rounds = int(record.get("rounds", ROUNDS))
        expected = bytes.fromhex(record["hash"])
    except (KeyError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, rounds)
    return hmac.compare_digest(digest, expected)


class Credentials:
    """The stored password, and the attempt counter guarding it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CREDENTIAL_FILE
        self._failures: list[float] = []

    # ---- the password -------------------------------------------------

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @property
    def is_set(self) -> bool:
        return self.load() is not None

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(
                f"a password needs at least {MIN_PASSWORD} characters; "
                "this panel can publish to your channel"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hash_password(password), indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass   # Windows; the directory is already user-scoped

    # ---- attempts -----------------------------------------------------

    @property
    def locked_for(self) -> int:
        """Seconds remaining on a lockout, or zero."""
        cutoff = time.time() - LOCKOUT_SECONDS
        self._failures = [t for t in self._failures if t > cutoff]
        if len(self._failures) < MAX_ATTEMPTS:
            return 0
        return int(LOCKOUT_SECONDS - (time.time() - self._failures[0]))

    def check(self, password: str) -> bool:
        """Verify, counting failures. Locked out always fails."""
        if self.locked_for:
            return False
        record = self.load()
        if record is None or not verify_password(password, record):
            self._failures.append(time.time())
            return False
        self._failures.clear()
        return True


class Sessions:
    """Live logins. In memory, so restarting the panel logs everyone out."""

    def __init__(self) -> None:
        self._live: dict[str, Session] = {}

    def start(self) -> str:
        token = secrets.token_urlsafe(32)
        self._live[token] = Session(token)
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        session = self._live.get(token)
        if session is None:
            return False
        if session.expired:
            del self._live[token]
            return False
        return True

    def end(self, token: str | None) -> None:
        if token:
            self._live.pop(token, None)

    def count(self) -> int:
        for token, session in list(self._live.items()):
            if session.expired:
                del self._live[token]
        return len(self._live)


def cookie_header(token: str, secure: bool = False) -> str:
    """A session cookie.

    HttpOnly so a script cannot read it, SameSite=Strict so another site
    cannot ride it, and Secure when served over HTTPS.
    """
    parts = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly",
             "SameSite=Strict", f"Max-Age={SESSION_HOURS * 3600}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def session_from(cookie_header_value: str | None) -> str | None:
    """Pull the session id out of a Cookie header."""
    if not cookie_header_value:
        return None
    for part in cookie_header_value.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value or None
    return None
