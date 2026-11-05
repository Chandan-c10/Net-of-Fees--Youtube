"""When an approved episode goes out.

Gate 2 is a decision: is this good enough to publish. Pressing publish
afterwards is not a second decision, it is a chore -- and a chore that
has to happen at 08:30 is one that will be missed.

So an approved episode can carry its own release time, and a scheduled
run posts whatever is due. The gate is untouched by this: gates.py
refuses to clear a gate during a scheduled run, so a timer can post what
a person approved and can never approve anything itself. That asymmetry
is the whole design.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import CHANNEL_TZ, MAX_QUEUE_DAYS, PUBLISH_SLOTS


def slots_for(day: datetime) -> list[datetime]:
    """The publishing times on a given day, in channel time."""
    return [
        day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for hour, minute in PUBLISH_SLOTS
    ]


def next_slot(after: datetime | None = None) -> datetime:
    """The first publishing slot at or after `after`.

    Returned in channel time, which is what the operator thinks in; the
    comparison at publish time is done in UTC.
    """
    now = (after or datetime.now(CHANNEL_TZ)).astimezone(CHANNEL_TZ)
    for slot in slots_for(now):
        if slot > now:
            return slot
    return slots_for(now + timedelta(days=1))[0]


def is_due(publish_after: str | None, now: datetime | None = None) -> bool:
    """Whether a release time has arrived.

    No release time means due immediately -- an episode marked for
    automatic publishing with no slot should not sit forever.
    """
    if not publish_after:
        return True
    try:
        when = datetime.fromisoformat(publish_after)
    except ValueError:
        return True
    moment = now or datetime.now(CHANNEL_TZ)
    if when.tzinfo is None:
        when = when.replace(tzinfo=CHANNEL_TZ)
    return moment.astimezone(CHANNEL_TZ) >= when.astimezone(CHANNEL_TZ)


def due_episodes(manifests) -> list:
    """Approved episodes whose time has come and which are not out yet.

    Deliberately strict: an episode must have cleared Gate 2, must not
    already be published, and must have asked for automatic publishing.
    Anything else is left alone.
    """
    out = []
    for m in manifests:
        if not m.approved_at or m.published_at:
            continue
        if m.publish_mode == "manual":
            continue
        if m.publish_mode == "at_slot" and not is_due(m.publish_after):
            continue
        out.append(m)
    return out


def plan_releases(count: int, start: datetime | None = None) -> list[datetime]:
    """One release time per episode, filling the slots in order.

    Two slots a day means two episodes a day; changing PUBLISH_SLOTS
    changes both. Refuses to run past MAX_QUEUE_DAYS, because a queue
    longer than that was written about a different week.
    """
    if count < 1:
        raise ValueError(f"cannot plan {count} releases")
    now = (start or datetime.now(CHANNEL_TZ)).astimezone(CHANNEL_TZ)
    out: list[datetime] = []
    day = now
    while len(out) < count:
        for slot in slots_for(day):
            if slot > now and len(out) < count:
                out.append(slot)
        day += timedelta(days=1)
        if (day - now).days > MAX_QUEUE_DAYS:
            raise ValueError(
                f"{count} episodes would run past {MAX_QUEUE_DAYS} days at "
                f"{len(slots_for(now))} a day. Add slots or queue fewer."
            )
    return out
