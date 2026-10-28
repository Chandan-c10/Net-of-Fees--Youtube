"""Trend research.

Tested against a real HTTP server rather than mocks, as elsewhere. The
rule the tests exist to hold: every number reported is a source's own,
and nothing is modelled, estimated or invented.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pipeline import trends
from pipeline.errors import ConfigError, StageError


def _signal(title: str, value: int, hours_old: float = 1.0,
            source: str = "hackernews") -> trends.Signal:
    now = datetime.now(UTC)
    return trends.Signal(
        title=title, source=source, url=f"https://x/{value}",
        metric_name="hn_score", metric_value=value,
        published_at=now - timedelta(hours=hours_old), observed_at=now,
    )


# ---- freshness --------------------------------------------------------


def test_a_recent_signal_is_fresh():
    assert _signal("a", 1, hours_old=2).fresh


def test_an_old_signal_is_not_a_trend():
    assert not _signal("a", 1, hours_old=trends.FRESH_HOURS + 1).fresh


def test_a_stale_report_is_detected():
    old = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    assert trends.stale(old)
    assert not trends.stale(datetime.now(UTC).isoformat())


def test_an_unparseable_timestamp_counts_as_stale():
    """Better to re-read than to act on a report of unknown age."""
    assert trends.stale("not a date")


# ---- no invented metrics ----------------------------------------------


def test_a_signal_reports_its_own_metric_by_name():
    """"hn_score" and "view_count" are not interchangeable, and neither is
    a demand score we made up."""
    as_dict = _signal("a", 42).as_dict()
    assert as_dict["hn_score"] == 42
    assert "score" not in as_dict
    assert "demand" not in as_dict


def test_no_report_field_implies_a_measurement_we_cannot_make():
    report = trends.research([], keywords=())
    forbidden = {"search_volume", "demand", "demand_index", "rpm",
                 "revenue", "estimated_views", "cpm"}
    assert not forbidden & set(report)


def test_the_method_is_stated_in_the_report():
    report = trends.research([], keywords=())
    assert "No search volume" in report["method"]


# ---- terms and clustering ---------------------------------------------


def test_stopwords_are_not_topics():
    assert trends.terms_in("How to fix the database") == {"fix", "database"}


def test_bare_numbers_are_not_topics():
    assert "2026" not in trends.terms_in("Python 2026 release")


def test_a_term_in_several_headlines_becomes_a_topic():
    signals = [_signal("Postgres latency spike", 100),
               _signal("Reducing latency in Rust", 90),
               _signal("Unrelated story", 80)]
    topics = trends.cluster(signals, min_mentions=2)
    assert [t.term for t in topics] == ["latency"]
    assert topics[0].mentions == 2


def test_stale_signals_are_excluded_from_clustering():
    signals = [_signal("latency one", 100, hours_old=500),
               _signal("latency two", 90, hours_old=500)]
    assert trends.cluster(signals, min_mentions=2) == []


def test_keywords_restrict_topics_to_a_subject():
    """An engineering channel should not be handed a cosmetics trend."""
    signals = [_signal("latency in Postgres", 100),
               _signal("latency tuning", 90),
               _signal("lipstick shades trending", 500),
               _signal("lipstick of the year", 400)]
    topics = trends.cluster(signals, keywords=("latency",), min_mentions=2)
    assert [t.term for t in topics] == ["latency"]


def test_a_standout_single_story_is_not_discarded():
    """Requiring two mentions threw away a 416-point headline because its
    words appeared once, which is the opposite of the point."""
    signals = [_signal(f"minor story {i}", 5) for i in range(10)]
    signals.append(_signal("One enormous story", 900))
    found = [t.term for t in trends.standouts(signals)]
    assert "One enormous story" in found


def test_a_small_batch_returns_everything_as_a_standout():
    """With four signals there is no batch to stand out from."""
    signals = [_signal(f"s{i}", i) for i in range(4)]
    assert len(trends.standouts(signals)) == 4


# ---- profile wiring ---------------------------------------------------


def test_profile_keywords_are_words_not_hashtags():
    """A headline says "software" and "engineering" separately. Matching
    terms against "#softwareengineering" found nothing across 18 live
    stories, which is why TREND_KEYWORDS exists."""
    import profiles
    for name in ("engineering", "trading"):
        keywords = trends.keywords_for(profiles.load(name))
        assert keywords, f"{name} declares no TREND_KEYWORDS"
        assert not any(k.startswith("#") for k in keywords)
        assert all(" " not in k for k in keywords)


def test_profile_keywords_actually_match_plain_headlines():
    import profiles
    keywords = trends.keywords_for(profiles.load("engineering"))
    signals = [_signal("Postgres latency spike in production", 100),
               _signal("Tracking latency regressions", 90)]
    assert trends.cluster(signals, keywords, min_mentions=2)


def test_the_youtube_category_follows_the_profile():
    import profiles
    sources = trends.default_sources(profiles.load("engineering"))
    youtube = next(s for s in sources if s.name == "youtube")
    assert youtube.categories == ("28",)


# ---- a real server ----------------------------------------------------


class FakeHN(BaseHTTPRequestHandler):
    stories: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/topstories"):
            body = json.dumps(list(self.stories)).encode()
        else:
            story_id = int(self.path.split("/")[-1].replace(".json", ""))
            body = json.dumps(self.stories.get(story_id, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def hn_server():
    now = int(datetime.now(UTC).timestamp())
    FakeHN.stories = {
        1: {"type": "story", "title": "Postgres latency spike",
            "score": 420, "time": now, "url": "https://a"},
        2: {"type": "story", "title": "Cutting latency in half",
            "score": 180, "time": now},
        3: {"type": "job", "title": "We are hiring", "score": 5,
            "time": now},
        4: {"type": "story", "title": "", "score": 9, "time": now},
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_real_fetch_reads_scores_and_times(hn_server):
    signals = trends.HackerNews(limit=10, base=hn_server).fetch()
    assert len(signals) == 2, "a job post and an untitled story are not stories"
    top = max(signals, key=lambda s: s.metric_value)
    assert top.metric_value == 420
    assert top.metric_name == "hn_score"
    assert top.url == "https://a"


def test_a_story_without_a_url_falls_back_to_its_thread(hn_server):
    signals = trends.HackerNews(limit=10, base=hn_server).fetch()
    threaded = next(s for s in signals if "half" in s.title)
    assert "news.ycombinator.com/item?id=2" in threaded.url


def test_research_over_a_real_server(hn_server):
    report = trends.research([trends.HackerNews(limit=10, base=hn_server)],
                             min_mentions=2)
    assert report["sources_used"] == ["hackernews"]
    assert report["signals_read"] == 2
    assert [t["term"] for t in report["topics"]] == ["latency"]
    assert report["topics"][0]["evidence"][0]["hn_score"] == 420


def test_an_unreachable_source_is_recorded_not_fatal():
    """Partial research is useful; silently returning less is not."""
    report = trends.research(
        [trends.HackerNews(limit=2, base="http://127.0.0.1:1")])
    assert report["sources_used"] == []
    assert "hackernews" in report["sources_failed"]


def test_an_unconfigured_source_is_skipped_with_a_reason(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    report = trends.research([trends.YouTubeTrending()])
    assert report["sources_failed"]["youtube"] == "not configured"


def test_youtube_says_what_key_it_needs(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        trends.YouTubeTrending().fetch()
    assert "YOUTUBE_API_KEY" in str(exc.value)
    assert "separate from the OAuth token" in str(exc.value)


def test_a_non_json_response_is_a_clear_error(hn_server):
    with pytest.raises(StageError):
        trends.HackerNews(limit=1, base="https://example.com").fetch()
