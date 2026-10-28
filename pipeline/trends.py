"""What the market is paying attention to.

This module answers one question -- what is being discussed right now
that this channel could speak to -- and deliberately stops there. It
surfaces topics with evidence. It does not write scripts and it cannot
create episodes.

That boundary is the point, not a missing feature. YouTube's inauthentic
content policy names "mass-produced, generic, repetitive" output,
including AI content built from templates "without adding the creator's
original, authentic insights or perspective". A system that detected a
trend and emitted fifteen videos would be describing itself. So a trend
is treated as a *subject*: it tells you what to point your own analysis
at, and the recipe still has to find real data to say something about,
and both gates still apply per episode.

Every number here comes from a source's own API and is reported with the
URL it came from and the time it was read. Nothing is modelled,
estimated or inferred. There is no "search volume" field, because no
free source publishes one and inventing it would be worse than leaving
it out.

Sources are official APIs only:

  * Hacker News -- the Firebase API, no auth, no key.
  * YouTube Data API -- the mostPopular chart, by region and category.
    Needs only YOUTUBE_API_KEY, not the OAuth token used for uploading,
    and costs 1 unit of a 10,000 daily quota.

Scraping a search results page would give better coverage and is against
the terms of every service worth scraping, so it is not done here.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .errors import ConfigError, StageError
from .logging_setup import get

HTTP_TIMEOUT_SEC = 20
USER_AGENT = "net-of-fees/1.0 (trend research; contact via repo)"

HN_BASE = "https://hacker-news.firebaseio.com/v0"
HN_STORY_LIMIT = 60

YOUTUBE_BASE = "https://www.googleapis.com/youtube/v3"
# 1 = Film & Animation, 22 = People & Blogs, 27 = Education,
# 28 = Science & Technology. A profile declares which it cares about.
DEFAULT_CATEGORIES = ("28", "27")
DEFAULT_REGION = "US"

# Beyond this a "trend" is history, and the window is stated rather than
# implied so a stale cache cannot pass for current research.
FRESH_HOURS = 72

_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset("""
a an the of to in for on with and or is are was were be been being
how why what when where who which that this these those it its as at
by from into over after before new show hn ask i my your you we our
""".split())


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Signal:
    """One observed piece of attention, as its source reported it."""

    title: str
    source: str
    url: str
    metric_name: str          # what the number is, e.g. "hn_score"
    metric_value: int         # the source's own number, never ours
    published_at: datetime
    observed_at: datetime = field(default_factory=_now)

    @property
    def age_hours(self) -> float:
        return (self.observed_at - self.published_at).total_seconds() / 3600.0

    @property
    def fresh(self) -> bool:
        return self.age_hours <= FRESH_HOURS

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "source": self.source,
            "url": self.url,
            self.metric_name: self.metric_value,
            "published_at": self.published_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "age_hours": round(self.age_hours, 1),
        }


@dataclass
class Topic:
    """A candidate subject, and the signals that suggest it."""

    term: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def mentions(self) -> int:
        return len(self.signals)

    @property
    def best(self) -> Signal | None:
        return max(self.signals, key=lambda s: s.metric_value, default=None)

    @property
    def sources(self) -> list[str]:
        return sorted({s.source for s in self.signals})

    def as_dict(self) -> dict:
        return {
            "term": self.term,
            "mentions": self.mentions,
            "sources": self.sources,
            "evidence": [s.as_dict() for s in self.signals[:5]],
        }


def _fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise StageError(f"{url} returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise StageError(f"cannot reach {url}: {exc.reason}") from exc
    except ValueError as exc:
        raise StageError(f"{url} did not return JSON: {exc}") from exc


# ---- sources ----------------------------------------------------------


class HackerNews:
    """Top stories from the official Firebase API. No key, no auth."""

    name = "hackernews"

    def __init__(self, limit: int = HN_STORY_LIMIT, base: str = HN_BASE):
        self.limit = limit
        self.base = base.rstrip("/")

    def available(self) -> bool:
        return True

    def fetch(self) -> list[Signal]:
        ids = _fetch_json(f"{self.base}/topstories.json")
        if not isinstance(ids, list):
            raise StageError("Hacker News did not return a story list")
        out: list[Signal] = []
        for story_id in ids[: self.limit]:
            item = _fetch_json(f"{self.base}/item/{story_id}.json")
            if not isinstance(item, dict) or item.get("type") != "story":
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append(Signal(
                title=title,
                source=self.name,
                url=item.get("url")
                    or f"https://news.ycombinator.com/item?id={story_id}",
                metric_name="hn_score",
                metric_value=int(item.get("score") or 0),
                published_at=datetime.fromtimestamp(
                    int(item.get("time") or 0), tz=UTC),
            ))
        return out


class YouTubeTrending:
    """The mostPopular chart, by region and category.

    The most directly relevant signal available: these are videos
    YouTube itself is currently surfacing. Needs only an API key, which
    is separate from the OAuth token used to upload.
    """

    name = "youtube"

    def __init__(self, region: str = DEFAULT_REGION,
                 categories: tuple[str, ...] = DEFAULT_CATEGORIES,
                 api_key: str | None = None, base: str = YOUTUBE_BASE):
        self.region = region
        self.categories = categories
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY")
        self.base = base.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self) -> list[Signal]:
        if not self.available():
            raise ConfigError(
                "YOUTUBE_API_KEY is not set, so the trending chart cannot "
                "be read. Create a key in the Google Cloud console; it is "
                "separate from the OAuth token used for uploading."
            )
        out: list[Signal] = []
        for category in self.categories:
            query = urllib.parse.urlencode({
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": self.region,
                "videoCategoryId": category,
                "maxResults": "50",
                "key": self.api_key,
            })
            payload = _fetch_json(f"{self.base}/videos?{query}")
            if not isinstance(payload, dict):
                continue
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                title = (snippet.get("title") or "").strip()
                if not title:
                    continue
                published = snippet.get("publishedAt", "")
                try:
                    when = datetime.fromisoformat(
                        published.replace("Z", "+00:00"))
                except ValueError:
                    when = _now()
                out.append(Signal(
                    title=title,
                    source=self.name,
                    url=f"https://www.youtube.com/watch?v={item.get('id')}",
                    metric_name="view_count",
                    metric_value=int(
                        item.get("statistics", {}).get("viewCount") or 0),
                    published_at=when,
                ))
        return out


# ---- analysis ---------------------------------------------------------


def terms_in(title: str) -> set[str]:
    """Candidate topic words from a headline."""
    return {
        word for word in _WORD.findall(title.lower())
        if word not in STOPWORDS and len(word) > 2 and not word.isdigit()
    }


# A single story well above the rest of the batch is evidence on its own.
# Requiring two mentions discarded a 416-point headline because its words
# appeared once, which is the opposite of what a trend scan is for.
STANDOUT_PERCENTILE = 0.80


def standouts(signals: list[Signal]) -> list[Topic]:
    """Topics from individual signals that dominate their own batch."""
    fresh = [s for s in signals if s.fresh]
    if len(fresh) < 5:
        return [Topic(s.title, [s]) for s in fresh]
    ranked = sorted(fresh, key=lambda s: s.metric_value)
    cutoff = ranked[int(len(ranked) * STANDOUT_PERCENTILE)].metric_value
    return [Topic(s.title, [s]) for s in fresh if s.metric_value >= cutoff]


def cluster(signals: list[Signal], keywords: tuple[str, ...] = (),
            min_mentions: int = 2) -> list[Topic]:
    """Group signals by shared term.

    Crude on purpose. A term appearing in several independent headlines is
    a weak but honest signal; pretending to measure demand would not be.
    `keywords` restricts the result to a profile's own subject area, so an
    engineering channel is not handed a cosmetics trend.
    """
    buckets: dict[str, Topic] = {}
    wanted = {k.lower() for k in keywords}
    for signal in signals:
        if not signal.fresh:
            continue
        for term in terms_in(signal.title):
            if wanted and term not in wanted:
                continue
            buckets.setdefault(term, Topic(term)).signals.append(signal)

    topics = [t for t in buckets.values() if t.mentions >= min_mentions]
    # Most-mentioned first, then by the largest single number behind it.
    topics.sort(
        key=lambda t: (t.mentions,
                       t.best.metric_value if t.best else 0),
        reverse=True,
    )
    return topics


def research(sources: list, keywords: tuple[str, ...] = (),
             min_mentions: int = 2) -> dict:
    """Read every available source and report what they say.

    A source that fails is recorded and skipped rather than aborting the
    run: partial research is useful, and silently returning fewer results
    would not be.
    """
    log = get(None, "trends")
    signals: list[Signal] = []
    used, failed = [], {}
    for source in sources:
        if not source.available():
            failed[source.name] = "not configured"
            continue
        try:
            got = source.fetch()
        except (StageError, ConfigError) as exc:
            failed[source.name] = str(exc)
            log.warning(f"{source.name}: {exc}")
            continue
        signals.extend(got)
        used.append(source.name)
        log.info(f"{source.name}: {len(got)} signals")

    topics = cluster(signals, keywords, min_mentions)
    return {
        "standouts": [t.as_dict() for t in standouts(signals)[:10]],
        "observed_at": _now().isoformat(),
        "window_hours": FRESH_HOURS,
        "sources_used": used,
        "sources_failed": failed,
        "signals_read": len(signals),
        "signals_fresh": sum(1 for s in signals if s.fresh),
        "topics": [t.as_dict() for t in topics],
        "method": (
            "`topics` are terms shared across independent headlines inside "
            "the stated window; `standouts` are individual stories in the "
            "top fifth of their own batch by the source's own metric. "
            "Counts and view figures are each source's own. No search "
            "volume, demand index or revenue figure is produced, because "
            "no free source publishes one."
        ),
    }


def default_sources(profile=None) -> list:
    """The sources to try, narrowed by the profile where it says so."""
    region = os.getenv("TREND_REGION", DEFAULT_REGION)
    categories = DEFAULT_CATEGORIES
    if profile is not None:
        declared = getattr(profile, "YOUTUBE_CATEGORY_ID", None)
        if declared:
            categories = (declared,)
    return [HackerNews(), YouTubeTrending(region=region,
                                          categories=categories)]


def keywords_for(profile) -> tuple[str, ...]:
    """A profile's subject words, for filtering trends to its domain.

    TREND_KEYWORDS must be ordinary words, not hashtags. Hashtags are
    compounds -- "softwareengineering" -- and a headline contains
    "software" and "engineering" separately, so matching terms against
    hashtags found nothing at all. Measured: zero topics from 18 live
    stories. The hashtag fallback is kept, but it is matched as a
    substring of the whole headline rather than as a term.
    """
    declared = getattr(profile, "TREND_KEYWORDS", None)
    if declared:
        return tuple(k.lower().lstrip("#") for k in declared)
    return ()


def loose_keywords_for(profile) -> tuple[str, ...]:
    """Hashtag stems, matched against a whole headline.

    Only used when a profile declares no TREND_KEYWORDS, so a new niche
    still gets something rather than nothing.
    """
    tags = getattr(profile, "HASHTAGS", ()) or ()
    return tuple(t.lower().lstrip("#") for t in tags)


def stale(observed_at: str, hours: int = FRESH_HOURS) -> bool:
    """Whether a saved report is too old to act on."""
    try:
        when = datetime.fromisoformat(observed_at)
    except ValueError:
        return True
    return _now() - when > timedelta(hours=hours)
