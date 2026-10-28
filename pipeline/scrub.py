"""Secret scan. A hard gate.

Runs before any render, not before upload: scrubbing later means a secret
has already been burned into video frames and audio, which is
unrecoverable once published -- architecture doc, section 11.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ScrubError

# Each pattern is (name, regex). Deliberately broad: a false positive costs
# one manual review, a false negative costs a published credential.
# An env-var name carries a prefix before the telling word: the secret in
# SMTP_APP_PASSWORD is no less a secret than the one in PASSWORD.
#
# One bounded character class, not nested quantifiers. The obvious
# spelling -- [A-Za-z0-9]*(?:[_ -][A-Za-z0-9]+)*? -- backtracks
# catastrophically: 1.8 seconds on 8 KB of underscores, and this scanner
# runs over text the pipeline did not write.
ENV_PREFIX = r"[A-Za-z0-9_ -]{0,64}"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("postgres_url", re.compile(r"\bpostgres(?:ql)?://[^\s\"']+")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # Broker and account identifiers. Indian broker client codes are
    # typically 5-10 alphanumerics behind an explicit label.
    ("client_code", re.compile(r"(?i)\b(?:client[_ -]?(?:code|id)|ucc)\b\s*[:=]\s*\S+")),
    ("account_number", re.compile(r"(?i)\b(?:account|acct)[_ -]?(?:no|number|id)\b\s*[:=]\s*\S+")),
    ("demat", re.compile(r"(?i)\b(?:demat|dp[_ -]?id|boid)\b\s*[:=]\s*\S+")),
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    # Label patterns. `\b` is deliberately not used at the front: it does
    # not match between two word characters, so `\bpassword` misses
    # `SMTP_APP_PASSWORD` -- which is to say it missed every credential
    # name this project actually uses. ENV_PREFIX absorbs that prefix.
    ("api_secret_label", re.compile(
        r"(?i)(?<![A-Za-z0-9])" + ENV_PREFIX +
        r"(?:api[_ -]?secret|api[_ -]?key|access[_ -]?token|"
        r"refresh[_ -]?token|app[_ -]?secret|client[_ -]?secret|"
        r"auth[_ -]?token|secret[_ -]?key|private[_ -]?key)"
        r"[A-Za-z0-9_]*\s*[:=]\s*\S+")),
    ("password_label", re.compile(
        r"(?i)(?<![A-Za-z0-9])" + ENV_PREFIX +
        r"passw(?:or)?d[A-Za-z0-9_]*\s*[:=]\s*\S+")),
    # Token shapes for the services this pipeline actually talks to. A
    # label is not always present -- a token pasted into a log has none.
    ("meta_access_token", re.compile(r"\bEAA[A-Za-z0-9]{20,}")),
    ("google_oauth_refresh", re.compile(r"\b1//0[A-Za-z0-9_\-]{20,}")),
    ("google_client_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{15,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    # Absolute home paths leak the OS username.
    ("home_path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._\-]+/")),
)

# Keys whose values are never narrated and are stripped rather than flagged.
STRIP_KEYS = frozenset({
    "client_code", "client_id", "ucc", "account_number", "account_id",
    "demat", "dp_id", "boid", "pan", "api_key", "api_secret", "access_token",
    "refresh_token", "password", "order_id", "broker_order_id", "exchange_order_id",
    "capital", "total_capital", "available_margin",
    "client_secret", "auth_token", "secret_key", "private_key",
    "smtp_app_password", "ig_access_token", "gemini_api_key",
})


@dataclass(frozen=True)
class Finding:
    pattern: str
    path: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"{self.pattern} in {self.path}:{self.line} -> {self.excerpt}"


def _redact(text: str) -> str:
    """Shorten an excerpt so a finding report does not itself leak the secret."""
    cleaned = text.strip()
    if len(cleaned) <= 24:
        return cleaned[:8] + "..."
    return cleaned[:12] + "..." + cleaned[-4:]


def scan_text(text: str, origin: str = "<text>") -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    Finding(name, origin, lineno, _redact(match.group(0)))
                )
    return findings


def scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return []  # binary or unreadable: nothing textual to leak
    return scan_text(text, origin=str(path.name))


def scan_paths(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for p in paths:
        if p.is_file():
            findings.extend(scan_file(p))
    return findings


def strip_sensitive(obj: object) -> object:
    """Remove never-narrated keys from a snapshot before it is written.

    Applied at export time so the sensitive value never reaches disk inside
    the episode directory at all.
    """
    if isinstance(obj, dict):
        return {
            k: strip_sensitive(v)
            for k, v in obj.items()
            if k.lower() not in STRIP_KEYS
        }
    if isinstance(obj, list):
        return [strip_sensitive(v) for v in obj]
    return obj


def assert_clean(paths: list[Path]) -> None:
    """Raise on any finding. There is deliberately no override flag."""
    findings = scan_paths(paths)
    if findings:
        detail = "\n  ".join(str(f) for f in findings)
        raise ScrubError(
            f"{len(findings)} secret finding(s); episode halted:\n  {detail}"
        )


def scan_snapshot(data: object) -> list[Finding]:
    """Scan a parsed snapshot by serialising it back to text."""
    return scan_text(json.dumps(data, indent=2), origin="data.json")


def censor(text: str) -> str:
    """Replace every secret-looking run with a label naming what it was.

    `_redact` shortens an excerpt for a finding report; this one rewrites
    the text itself, so an alert body or a log line can carry a real error
    message without carrying the credential that appeared inside it.
    """
    out = text
    for name, pattern in PATTERNS:
        out = pattern.sub(f"[redacted:{name}]", out)
    return out
