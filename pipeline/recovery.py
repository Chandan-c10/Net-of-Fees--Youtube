"""Retrying the failures that deserve it, and only those.

`MAX_ATTEMPTS = 3` has sat in config.py since the beginning and nothing
ever retried anything. This is that, built with the boundaries that make
automatic retry safe rather than merely automatic.

Three verdicts, and the default is the cautious one.

**transient** -- a timeout, a reset connection, a 502, a 429. The work
was fine and the network was not. Worth retrying immediately with
backoff, and almost always succeeds.

**later** -- a spent API quota. Retrying in thirty seconds is pointless;
this needs tomorrow. Recorded as open, not retried in this run.

**needs_human** -- everything else, including anything unrecognised.
Missing credentials, a secret found in the data, a gate not cleared, a
recipe with too little data, audio drift. None of these improve by being
attempted again, and two of them get worse: retrying a gate failure
would be an attempt to route around the gate, and retrying a drift
failure would hide a real bug behind an intermittent one.

Unrecognised failures are classed as needs_human deliberately. A system
that retries whatever it does not understand is a system that will
eventually do something unrecoverable sixteen times in a row.

What this never does: retry a publish by calling the API again directly.
Neither YouTube nor Instagram has an idempotency key, so a retry has to
go back through the stage, which reconciles against what is already
posted before sending anything. Retrying around that would double-post.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .config import MAX_ATTEMPTS
from .errors import ConfigError, GateError, PipelineError
from .logging_setup import get

T = TypeVar("T")

# Backoff: 2s, 8s, 32s. Jittered, so two pipelines failing at once do not
# then retry in lockstep for ever.
BACKOFF_BASE_SEC = 2.0
BACKOFF_FACTOR = 4.0
BACKOFF_MAX_SEC = 120.0
JITTER_FRACTION = 0.25

TRANSIENT_PATTERNS = (
    "timed out", "timeout", "connection reset", "connection refused",
    "connection aborted", "broken pipe", "temporarily unavailable",
    "try again", "service not available", "bad gateway",
    "gateway timeout", "internal server error", "server error",
    "too many requests", "rate limit", "rate_limit",
    "name or service not known", "temporary failure in name resolution",
    "ssl", "eof occurred", "remote end closed",
    " 429", " 500", " 502", " 503", " 504",
)

# Spent for the day. A later run will work; this one will not.
LATER_PATTERNS = (
    "quota", "quotaexceeded", "dailylimitexceeded",
)

# Never retried, whatever the wording suggests. Matched on word
# boundaries: as plain substrings, "gate" matches "bad gateway", which
# classified a retryable 502 as needing a human.
NEVER_WORDS = (
    r"gate", r"gate[_ ]?[12]", r"approved_at", r"draft voice",
    r"scrub", r"secret", r"consent", r"licence", r"license",
)
NEVER_PATTERN = re.compile(
    r"\b(?:" + "|".join(NEVER_WORDS) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    kind: str          # "transient" | "later" | "needs_human"
    why: str

    @property
    def retry_now(self) -> bool:
        return self.kind == "transient"


def classify(exc: BaseException) -> Verdict:
    """What kind of failure this is. Defaults to needs_human."""
    text = f"{type(exc).__name__} {exc}".lower()

    # A gate is a decision, not an error condition. Retrying one would be
    # an attempt to route around it, so this is checked before anything
    # else and regardless of how the message reads.
    if isinstance(exc, GateError):
        return Verdict("needs_human", "a gate is a decision, not a fault")
    if NEVER_PATTERN.search(text):
        return Verdict("needs_human", "needs a person to resolve")

    # Missing configuration cannot be retried into existence.
    if isinstance(exc, ConfigError):
        return Verdict("needs_human", "configuration is missing or wrong")

    if any(p in text for p in LATER_PATTERNS):
        return Verdict("later", "an API quota is spent for now")
    if any(p in text for p in TRANSIENT_PATTERNS):
        return Verdict("transient", "the network, not the work")
    return Verdict("needs_human", "unrecognised, so not retried")


def backoff(attempt: int, seed: str = "") -> float:
    """Seconds to wait before attempt N. Exponential, with jitter.

    Jittered so two pipelines that fail together do not retry in lockstep
    for ever. Seeded, so a test gets the same number twice.
    """
    base = min(BACKOFF_BASE_SEC * (BACKOFF_FACTOR ** max(0, attempt - 1)),
               BACKOFF_MAX_SEC)
    if not seed:
        return base
    unit = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()[0] / 255.0
    jittered = base * (1.0 + (unit * 2.0 - 1.0) * JITTER_FRACTION)
    # Clamp after the jitter, not before: jittering a capped value pushed
    # it back over the cap, so a stated 120s maximum returned 140s.
    return min(jittered, BACKOFF_MAX_SEC)


def with_retry(work: Callable[[], T], *, what: str,
               attempts: int = MAX_ATTEMPTS,
               sleep: Callable[[float], None] = time.sleep,
               episode_id: str | None = None) -> T:
    """Run `work`, retrying only what is worth retrying.

    Raises the last failure if it runs out of attempts, so the caller and
    the ledger still see the real error rather than a wrapper.
    """
    log = get(episode_id, "recovery")
    last: BaseException | None = None

    for attempt in range(1, max(1, attempts) + 1):
        try:
            return work()
        except PipelineError as exc:
            last = exc
            verdict = classify(exc)
            if not verdict.retry_now:
                log.info(f"{what}: not retrying ({verdict.why})")
                raise
            if attempt >= attempts:
                log.warning(f"{what}: giving up after {attempt} attempts")
                raise
            wait = backoff(attempt, seed=what)
            log.warning(
                f"{what}: {type(exc).__name__} on attempt {attempt} "
                f"({verdict.why}); retrying in {wait:.0f}s"
            )
            sleep(wait)
    # Unreachable while attempts >= 1, but a bare `raise` here would be a
    # confusing RuntimeError if that ever changed.
    raise last if last else PipelineError(f"{what} did not run")


def describe(exc: BaseException) -> str:
    """One line for a log or a mail: what kind of failure, and why."""
    verdict = classify(exc)
    wording = {
        "transient": "worth retrying automatically",
        "later": "retry after the quota resets",
        "needs_human": "needs you",
    }[verdict.kind]
    return f"{wording} ({verdict.why})"


def plan(exc: BaseException) -> dict:
    """The verdict as data, for the ledger and the panel."""
    verdict = classify(exc)
    return {
        "kind": verdict.kind,
        "why": verdict.why,
        "retry_now": verdict.retry_now,
        "max_attempts": MAX_ATTEMPTS,
    }


# Stages that reach a network and are therefore worth wrapping. The
# render stages are CPU-bound and deterministic: a retry would fail
# identically, so it would only waste a minute of compute.
NETWORK_STAGES = frozenset({"export", "script", "upload", "instagram"})


def worth_retrying(stage: str) -> bool:
    return stage in NETWORK_STAGES


_WS = re.compile(r"\s+")


def short(exc: BaseException, limit: int = 110) -> str:
    """A failure on one line, for a status table."""
    text = _WS.sub(" ", str(exc)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
