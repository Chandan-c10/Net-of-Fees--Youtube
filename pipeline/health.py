"""A record of what has been breaking, kept until it is fixed.

Three things this exists to do.

**Remember.** A failure is written to a ledger that survives the process,
so "what broke last night" is answerable in the morning without reading
log files. An incident stays open until a later run of the same stage
succeeds, which is what closes it.

**Not flood the inbox.** The naive design mails on every failure, so a
stage that fails on a timer twice a day sends sixty mails a month saying
the same thing, and the inbox becomes something you stop reading. Here
the first occurrence mails, repeats are counted silently, and an
incident still open after a day mails again as a reminder. Recovery
mails once too, because "it is fixed" is worth knowing.

**Say where to look.** Every incident carries the stage, the episode and
the path of the log that holds the traceback, so the mail answers
"where" and not only "what".

Deduplication is by fingerprint, not by message: the message usually
carries a path, a count or an id that changes between runs, and treating
those as different incidents would defeat the whole purpose. The
fingerprint strips them.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .logging_setup import get

STATE_DIR = Path("state")
LEDGER = STATE_DIR / "health.json"

# How long an incident may stay open before it mails again. Short enough
# that a real outage is not forgotten, long enough that a twice-daily
# failure does not send twice-daily mail.
RENOTIFY_HOURS = 24

# Keep a bounded history so the file cannot grow without limit.
MAX_RESOLVED = 100
MAX_RUNS = 200

# Details that change between runs and must not create a new incident.
_VARIABLE = (
    (re.compile(r"/[^\s'\"]+"), "<path>"),              # absolute paths
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hex>"),         # ids, hashes
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),            # counts, durations
)


def _now() -> datetime:
    return datetime.now(UTC)


def fingerprint(stage: str, error_class: str, message: str) -> str:
    """A stable key for "this same problem".

    The message is normalised first, because it usually carries a path, a
    duration or an id that differs every run; keying on the raw text
    would open a fresh incident each time and mail about each one.
    """
    normalised = message.strip().lower()
    for pattern, placeholder in _VARIABLE:
        normalised = pattern.sub(placeholder, normalised)
    return f"{stage}:{error_class}:{normalised[:160]}"


@dataclass
class Incident:
    """One problem, from the first time it was seen until it stopped."""

    fingerprint: str
    stage: str
    error_class: str
    message: str
    episode_id: str | None = None
    log_path: str | None = None
    first_seen: str = field(default_factory=lambda: _now().isoformat())
    last_seen: str = field(default_factory=lambda: _now().isoformat())
    occurrences: int = 1
    status: str = "open"
    resolved_at: str | None = None
    notified_at: str | None = None

    @property
    def open(self) -> bool:
        return self.status == "open"

    def due_for_reminder(self, hours: int = RENOTIFY_HOURS) -> bool:
        if not self.notified_at:
            return True
        try:
            last = datetime.fromisoformat(self.notified_at)
        except ValueError:
            return True
        return _now() - last > timedelta(hours=hours)

    def describe(self) -> str:
        where = self.stage
        if self.episode_id:
            where = f"episode {self.episode_id}, stage {self.stage}"
        since = self.first_seen[:16].replace("T", " ")
        times = ("once" if self.occurrences == 1
                 else f"{self.occurrences} times")
        lines = [
            f"{self.error_class} in {where}",
            f"  {self.message}",
            "",
            f"  first seen   {since} UTC ({times} so far)",
        ]
        if self.log_path:
            lines.append(f"  full log     {self.log_path}")
        return "\n".join(lines)


@dataclass
class Ledger:
    """The persisted state. Loaded, changed, saved atomically."""

    incidents: list[Incident] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)

    # ---- persistence --------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Ledger:
        path = path or LEDGER
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt ledger must not stop a render. Start a new one;
            # the old file is overwritten on the next save.
            get(None, "health").warning(f"{path} is unreadable; starting over")
            return cls()
        return cls(
            incidents=[Incident(**i) for i in raw.get("incidents", [])],
            runs=list(raw.get("runs", [])),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or LEDGER
        path.parent.mkdir(parents=True, exist_ok=True)
        self._trim()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"incidents": [asdict(i) for i in self.incidents],
                        "runs": self.runs}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)   # atomic, as with the manifest

    def _trim(self) -> None:
        resolved = [i for i in self.incidents if not i.open]
        if len(resolved) > MAX_RESOLVED:
            keep = set(id(i) for i in resolved[-MAX_RESOLVED:])
            self.incidents = [i for i in self.incidents
                              if i.open or id(i) in keep]
        self.runs = self.runs[-MAX_RUNS:]

    # ---- queries ------------------------------------------------------

    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents if i.open]

    def find(self, key: str) -> Incident | None:
        return next((i for i in self.incidents
                     if i.fingerprint == key and i.open), None)

    def healthy(self) -> bool:
        return not self.open_incidents()

    # ---- recording ----------------------------------------------------

    def record_failure(self, episode_id: str | None, stage: str,
                       exc: BaseException,
                       log_path: str | None = None) -> tuple[Incident, bool]:
        """Open or update an incident. Returns it, and whether to mail.

        Mails on the first sighting, then not again until the reminder
        window has passed, so a stage failing on a timer twice a day does
        not send sixty identical mails a month.
        """
        key = fingerprint(stage, type(exc).__name__, str(exc))
        existing = self.find(key)
        now = _now().isoformat()

        if existing:
            existing.last_seen = now
            existing.occurrences += 1
            existing.message = str(exc)
            if episode_id:
                existing.episode_id = episode_id
            should_notify = existing.due_for_reminder()
            if should_notify:
                existing.notified_at = now
            self._log_run(episode_id, stage, "failed")
            return existing, should_notify

        incident = Incident(
            fingerprint=key, stage=stage, error_class=type(exc).__name__,
            message=str(exc), episode_id=episode_id, log_path=log_path,
            first_seen=now, last_seen=now, notified_at=now,
        )
        self.incidents.append(incident)
        self._log_run(episode_id, stage, "failed")
        return incident, True

    def record_success(self, episode_id: str | None,
                       stage: str) -> list[Incident]:
        """Close anything open for this stage. Returns what was closed.

        A later success is the only thing that resolves an incident --
        nothing is cleared on a timer, because a problem nobody fixed has
        not gone away.
        """
        now = _now().isoformat()
        closed = []
        for incident in self.incidents:
            if incident.open and incident.stage == stage:
                incident.status = "resolved"
                incident.resolved_at = now
                closed.append(incident)
        self._log_run(episode_id, stage, "ok")
        return closed

    def _log_run(self, episode_id: str | None, stage: str,
                 outcome: str) -> None:
        self.runs.append({
            "at": _now().isoformat(),
            "episode": episode_id,
            "stage": stage,
            "outcome": outcome,
        })

    # ---- reporting ----------------------------------------------------

    def summary(self, hours: int = 24) -> dict:
        """What happened recently, and what is still wrong."""
        cutoff = _now() - timedelta(hours=hours)
        recent = []
        for run in self.runs:
            try:
                when = datetime.fromisoformat(run["at"])
            except (ValueError, KeyError):
                continue
            if when >= cutoff:
                recent.append(run)
        failed = [r for r in recent if r["outcome"] == "failed"]
        return {
            "window_hours": hours,
            "healthy": self.healthy(),
            "runs": len(recent),
            "failures": len(failed),
            "open_incidents": len(self.open_incidents()),
            "stages_failing": sorted({r["stage"] for r in failed}),
        }

    def digest(self, hours: int = 24) -> str:
        """One readable report, for the daily mail."""
        stats = self.summary(hours)
        lines = [
            f"Last {hours} hours: {stats['runs']} stage runs, "
            f"{stats['failures']} failed.",
            "",
        ]
        open_now = self.open_incidents()
        if not open_now:
            lines.append("Nothing is broken.")
            return "\n".join(lines)

        lines.append(f"{len(open_now)} still open:")
        lines.append("")
        for incident in open_now:
            lines.append(incident.describe())
            lines.append("")
        lines.append(
            "An entry stays here until the same stage runs successfully. "
            "Re-run it once the cause is fixed and it clears itself."
        )
        return "\n".join(lines)


# ---- the operations the rest of the pipeline calls --------------------


def failed(episode_id: str | None, stage: str, exc: BaseException,
           log_path: str | None = None,
           path: Path | None = None) -> tuple[Incident, bool]:
    ledger = Ledger.load(path)
    incident, notify_now = ledger.record_failure(
        episode_id, stage, exc, log_path)
    ledger.save(path)
    return incident, notify_now


def succeeded(episode_id: str | None, stage: str,
              path: Path | None = None) -> list[Incident]:
    ledger = Ledger.load(path)
    closed = ledger.record_success(episode_id, stage)
    ledger.save(path)
    return closed
