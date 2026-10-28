"""What actually reaches YouTube.

The metadata stage generated a description, a tag list and three
thumbnail candidates. Upload used none of them: it built its own minimal
description, sent no tags field at all, and never called
thumbnails().set(). These tests pin the wiring so it cannot rot again.
"""

from __future__ import annotations

import json

import pytest

from pipeline.stages import upload

FULL = {
    "youtube": {
        "title": "8 bugs. My tests caught 1.",
        "description": "Eight bugs reached my system.\n\nIn this episode:\n- one",
        "tags": ["postmortem", "debugging", "python"],
    }
}


@pytest.fixture
def with_metadata(episode):
    def write(payload):
        (episode.dir / "metadata.json").write_text(
            json.dumps(payload), encoding="utf-8")
        return episode
    return write


# ---- description ------------------------------------------------------


def test_the_crafted_description_is_what_goes_out(with_metadata, manifest):
    ep = with_metadata(FULL)
    out = upload.youtube_description(ep, manifest)
    assert out.startswith("Eight bugs reached my system.")
    assert "In this episode:" in out


def test_the_disclaimer_is_appended_when_absent(with_metadata, manifest):
    """Section 14 requires it on every video."""
    ep = with_metadata(FULL)
    out = upload.youtube_description(ep, manifest)
    assert "not investment advice" in out.lower()


def test_the_disclaimer_is_not_duplicated(with_metadata, manifest):
    payload = {"youtube": {"description":
               "Body text.\n\nEducational content only. Not investment advice."}}
    out = upload.youtube_description(with_metadata(payload), manifest)
    assert out.lower().count("not investment advice") == 1


def test_the_episode_id_is_appended(with_metadata, manifest):
    out = upload.youtube_description(with_metadata(FULL), manifest)
    assert f"Episode {manifest.id}" in out


def test_a_missing_metadata_file_falls_back(episode, manifest):
    """An episode uploaded before the metadata stage ran still gets a
    lawful description rather than an empty one."""
    out = upload.youtube_description(episode, manifest)
    assert manifest.title in out
    assert "not investment advice" in out.lower()


def test_an_empty_description_falls_back(with_metadata, manifest):
    ep = with_metadata({"youtube": {"description": "   "}})
    assert manifest.title in upload.youtube_description(ep, manifest)


def test_corrupt_metadata_does_not_stop_the_upload(episode, manifest):
    (episode.dir / "metadata.json").write_text("{not json", encoding="utf-8")
    out = upload.youtube_description(episode, manifest)
    assert manifest.title in out


# ---- tags -------------------------------------------------------------


def test_the_generated_tags_are_sent(with_metadata):
    assert upload.youtube_tags(with_metadata(FULL)) == [
        "postmortem", "debugging", "python"]


def test_a_leading_hash_is_stripped(with_metadata):
    ep = with_metadata({"youtube": {"tags": ["#postmortem", "debugging"]}})
    assert upload.youtube_tags(ep) == ["postmortem", "debugging"]


def test_tags_are_trimmed_to_youtube_s_budget(with_metadata):
    """YouTube caps the total, and rejects the whole request if it is
    exceeded rather than truncating for you."""
    ep = with_metadata({"youtube": {"tags": [f"tag{i:03d}" for i in range(200)]}})
    tags = upload.youtube_tags(ep)
    assert tags
    assert sum(len(t) + 1 for t in tags) <= upload.TAGS_TOTAL_MAX


def test_an_overlong_single_tag_is_dropped(with_metadata):
    ep = with_metadata({"youtube": {"tags": ["ok", "x" * 60]}})
    assert upload.youtube_tags(ep) == ["ok"]


def test_blank_tags_are_dropped(with_metadata):
    ep = with_metadata({"youtube": {"tags": ["ok", "", "  ", "#"]}})
    assert upload.youtube_tags(ep) == ["ok"]


def test_no_metadata_means_no_tags(episode):
    assert upload.youtube_tags(episode) == []


# ---- the insert body --------------------------------------------------


def test_the_insert_body_carries_the_tags(with_metadata, manifest, monkeypatch):
    """The body had no tags field at all, so the list was generated and
    then dropped on the floor."""
    ep = with_metadata(FULL)
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    ep.video.write_bytes(b"\x00" * 64)

    captured = {}

    class FakeVideos:
        def insert(self, part, body, media_body):
            captured["body"] = body
            return FakeRequest()

    class FakeRequest:
        def next_chunk(self):
            return None, {"id": "VID123"}

    class FakeYouTube:
        def __init__(self, *a, **k):      # _client takes a channel now
            pass
        def videos(self):
            return FakeVideos()
        def thumbnails(self):
            raise AssertionError("no thumbnail was chosen")

    monkeypatch.setattr(upload, "_client", FakeYouTube)
    monkeypatch.setattr(upload, "MediaFileUpload", object, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules, "googleapiclient.http",
        type("mod", (), {"MediaFileUpload": lambda *a, **k: object()}))

    upload.run(ep, manifest)

    assert captured["body"]["snippet"]["tags"] == [
        "postmortem", "debugging", "python"]
    assert "Eight bugs reached my system." in \
        captured["body"]["snippet"]["description"]
    assert manifest.stage("upload").data["tags"] == 3


def test_the_chosen_thumbnail_is_set_after_upload(with_metadata, manifest,
                                                  monkeypatch):
    """The stage rendered three candidates and the operator picks one; the
    pick never reached YouTube because nothing called thumbnails().set()."""
    ep = with_metadata(FULL)
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    manifest.chosen_thumbnail = 2
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    ep.video.write_bytes(b"\x00" * 64)
    ep.thumbnail(2).write_bytes(b"\x89PNG\r\n\x1a\n")

    called = {}

    class FakeThumbs:
        def set(self, videoId, media_body):
            called["video"] = videoId
            return type("R", (), {"execute": lambda self: {}})()

    class FakeVideos:
        def insert(self, part, body, media_body):
            return type("R", (), {
                "next_chunk": lambda self: (None, {"id": "VID9"})})()

    class FakeYouTube:
        def __init__(self, *a, **k):      # _client takes a channel now
            pass
        def videos(self):
            return FakeVideos()
        def thumbnails(self):
            return FakeThumbs()

    monkeypatch.setattr(upload, "_client", FakeYouTube)
    monkeypatch.setitem(
        __import__("sys").modules, "googleapiclient.http",
        type("mod", (), {"MediaFileUpload": lambda *a, **k: object()}))

    upload.run(ep, manifest)
    assert called["video"] == "VID9"


def test_a_missing_chosen_thumbnail_does_not_fail_the_upload(
        with_metadata, manifest, monkeypatch, caplog):
    """The video is already live by then; raising would make a published
    episode look like a failed one."""
    ep = with_metadata(FULL)
    manifest.chosen_thumbnail = 4          # never rendered
    log = []

    class FakeYouTube:
        def thumbnails(self):
            raise AssertionError("must not be called for a missing file")

    upload._set_thumbnail(
        FakeYouTube(), ep, manifest, "VID1",
        type("L", (), {"info": lambda s, m: log.append(m),
                       "warning": lambda s, m: log.append(m)})())
    assert any("missing" in m for m in log)


# ---- category and language --------------------------------------------


def test_the_category_follows_the_profile(manifest):
    """Hardcoding it meant a new topic kept the old topic's category."""
    from pipeline.manifest import Manifest
    eng = Manifest.new("x", recipe="incident_log", profile="engineering",
                       voice="v")
    trade = Manifest.new("y", recipe="fee_drag", profile="trading", voice="v")
    assert upload._snippet_extras(eng)["categoryId"] == "28"
    assert upload._snippet_extras(trade)["categoryId"] == "27"


def test_the_language_is_declared_for_auto_translation(manifest):
    extras = upload._snippet_extras(manifest)
    assert extras["defaultLanguage"] == "en"
    assert extras["defaultAudioLanguage"] == "en"


def test_an_unknown_profile_falls_back_rather_than_crashing(manifest):
    """Upload runs after the render, so a profile problem has already
    surfaced; it must not turn into a failed publish."""
    manifest.profile = "nosuchprofile"
    assert upload._snippet_extras(manifest)["categoryId"] == \
        upload.FALLBACK_CATEGORY_ID
