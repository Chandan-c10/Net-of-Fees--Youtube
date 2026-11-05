"""Deleting what an episode no longer needs.

A rendered episode occupies about 50 MB, and most of that is working
material: scene WAVs that exist only to be mixed, per-scene clips that
exist only to be concatenated, frames that exist only to be encoded.
Two episodes a day fills a laptop in a year for no benefit.

The rule that makes deletion safe is not an age. It is this: **nothing
is removed that cannot be recovered or has not already gone out.**

So an episode's working files go once it is rendered, and its video goes
only when the episode has actually been published or archived somewhere
durable. An unpublished video is the only copy of work you have not seen
the result of yet, and no retention setting is worth losing that.

The manifest is never deleted. It is the record of what happened, it is
a few kilobytes, and without it the episode did not exist.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .logging_setup import get

# 0 means keep everything, which is the default: deleting by default is
# how someone loses work they did not know was scheduled for removal.
DEFAULT_WORKING_DAYS = 0
DEFAULT_VIDEO_DAYS = 0

# Directories that only ever hold intermediate material. Everything here
# is reproducible by re-running a stage.
WORKING_DIRS = ("audio", "clips", "frames", "transitions", "visuals",
                "work", "sources")

# Kept whatever the setting says.
KEEP_ALWAYS = ("manifest.json", "script.md", "metadata.json", "cues.json",
               "captions.srt", "run.log")


def _days(name: str, fallback: float) -> float:
    """A retention period in days.

    Float rather than int so shorter periods are expressible: 0.5 is
    twelve hours, 0.25 is six. Days is the unit because that is how the
    setting reads, not because a day is the smallest useful interval.
    """
    raw = os.getenv(name, str(fallback)).strip()
    if raw.lower().endswith("h"):          # "6h" is friendlier than 0.25
        try:
            return max(0.0, float(raw[:-1]) / 24.0)
        except ValueError:
            return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        return fallback


def working_days() -> float:
    return _days("RETAIN_WORKING_DAYS", DEFAULT_WORKING_DAYS)


def video_days() -> float:
    return _days("RETAIN_VIDEO_DAYS", DEFAULT_VIDEO_DAYS)


@dataclass(frozen=True)
class Plan:
    """What would be removed from one episode, and what it frees."""

    episode_id: str
    working: list[Path]
    video: Path | None
    bytes_freed: int
    kept_because: str | None = None

    @property
    def anything(self) -> bool:
        return bool(self.working or self.video)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _age_days(when: str | None) -> float | None:
    if not when:
        return None
    try:
        stamp = datetime.fromisoformat(when)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() / 86400


def safe_to_remove_video(m) -> tuple[bool, str]:
    """Whether this episode's video is recoverable.

    Published means YouTube holds it. Archived means the bucket does.
    Either is a copy; neither is, and the file stays.
    """
    if m.published_at:
        return True, "published"
    if m.stage("assemble").data.get("archive_url"):
        return True, "archived"
    return False, "not published or archived"


def is_exempt(m) -> bool:
    """Whether this episode opts out of retention entirely.

    One episode is often worth keeping whole -- the first one, or the
    one being used to debug something -- and a global setting cannot
    express that.
    """
    return bool(getattr(m, "keep_files", False))


def plan_for(ep, m, now: datetime | None = None) -> Plan:
    """What could be removed from this episode right now."""
    if is_exempt(m):
        return Plan(m.id, [], None, 0, "kept by request")
    keep_working = working_days()
    keep_video = video_days()
    working: list[Path] = []
    freed = 0

    rendered = m.stage("assemble").at if m.is_done("assemble") else None
    age = _age_days(rendered)

    if keep_working and age is not None and age >= keep_working:
        for name in WORKING_DIRS:
            path = ep.dir / name
            if path.exists():
                working.append(path)
                freed += _size(path)

    video = None
    reason = None
    if keep_video and ep.video.exists():
        published_age = _age_days(m.published_at) if m.published_at else age
        if published_age is not None and published_age >= keep_video:
            ok, why = safe_to_remove_video(m)
            if ok:
                video = ep.video
                freed += _size(ep.video)
            else:
                reason = why
    return Plan(m.id, working, video, freed, reason)


def apply(plan: Plan, ep, log=None) -> int:
    """Carry out a plan. Returns bytes freed."""
    log = log or get(plan.episode_id, "prune")
    for path in plan.working:
        shutil.rmtree(path, ignore_errors=True)
    if plan.video:
        plan.video.unlink(missing_ok=True)
    if plan.anything:
        log.info(f"freed {plan.bytes_freed / 1e6:.1f} MB")
    return plan.bytes_freed


def sweep(episodes_dir: Path, dry_run: bool = False) -> dict:
    """Prune every episode that qualifies.

    Reports what was kept and why as well as what went, because a
    retention setting that quietly keeps everything is one you would
    rather know about.
    """
    from .manifest import Episode

    log = get(None, "prune")
    if not working_days() and not video_days():
        return {"enabled": False, "freed": 0, "episodes": [],
                "note": "retention is off; nothing is deleted"}

    freed = 0
    touched, kept = [], []
    for directory in sorted(p for p in episodes_dir.iterdir() if p.is_dir()):
        if not (directory / "manifest.json").exists():
            continue
        ep = Episode(directory.name, episodes_dir)
        try:
            m = ep.load_manifest()
        except Exception as exc:  # a broken manifest is not prune's problem
            log.warning(f"{directory.name}: {exc}")
            continue
        plan = plan_for(ep, m)
        if plan.kept_because:
            kept.append({"episode": m.id, "why": plan.kept_because})
        if not plan.anything:
            continue
        touched.append({
            "episode": m.id,
            "working": [p.name for p in plan.working],
            "video": bool(plan.video),
            "mb": round(plan.bytes_freed / 1e6, 1),
        })
        if not dry_run:
            freed += apply(plan, ep, log)
        else:
            freed += plan.bytes_freed

    return {
        "enabled": True,
        "dry_run": dry_run,
        "freed_mb": round(freed / 1e6, 1),
        "episodes": touched,
        "kept": kept,
        "working_days": working_days(),
        "video_days": video_days(),
    }


def usage(episodes_dir: Path) -> dict:
    """How much disk the episodes are using, for the settings page."""
    if not episodes_dir.exists():
        return {"episodes": 0, "mb": 0.0}
    total = 0
    count = 0
    for directory in episodes_dir.iterdir():
        if directory.is_dir() and (directory / "manifest.json").exists():
            count += 1
            total += _size(directory)
    return {"episodes": count, "mb": round(total / 1e6, 1)}
