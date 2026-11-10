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
    """Section 14 requires one on every video -- the profile's own, not
    an investment notice on whatever the subject happens to be."""
    ep = with_metadata(FULL)
    out = upload.youtube_description(ep, manifest)
    assert "not professional advice" in out.lower()

    import profiles
    trading = upload.youtube_description(ep, manifest, None,
                                         profiles.load("trading"))
    assert "not investment advice" in trading.lower()


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
    assert "not professional advice" in out.lower()


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


# ---- a snapshot that carries no figures -------------------------------


def test_a_recipe_name_without_its_figures_falls_back_to_the_title(
        episode, manifest):
    """A story episode's snapshot holds no numbers. The unguarded version
    died with a bare KeyError on exactly that."""
    import profiles
    from pipeline.stages import metadata

    episode.snapshot.write_text(json.dumps({"recipe": "incident_log"}))
    manifest.title = "The debt nobody mentioned"
    manifest.pass_gate_1()
    metadata.run(episode, manifest, profiles.load("trading"))
    written = json.loads((episode.dir / "metadata.json").read_text())
    assert written["chosen_title"] == "The debt nobody mentioned"


def test_no_figures_and_no_title_says_so_rather_than_crashing(
        episode, manifest):
    import profiles
    from pipeline.errors import StageError
    from pipeline.stages import metadata

    episode.snapshot.write_text(json.dumps({"recipe": "incident_log"}))
    manifest.title = ""
    manifest.pass_gate_1()
    with pytest.raises(StageError, match="no title"):
        metadata.run(episode, manifest, profiles.load("trading"))


# ---- a dry run is not an attempt --------------------------------------


def test_a_dry_run_does_not_record_an_upload_attempt(episode, manifest):
    """A dry run contacts YouTube not at all. Recording it as an attempt
    made the next real run believe it was retrying, and try to reconcile
    against a channel the upload scope cannot read."""
    from pipeline.stages import upload

    upload._record_attempt(manifest, "youtube", counts=False)
    assert manifest.platform_posts == []


def test_a_real_attempt_is_recorded_before_the_call(manifest):
    from pipeline.stages import upload

    row = upload._record_attempt(manifest, "youtube")
    assert row["status"] == "attempting"
    assert len(manifest.platform_posts) == 1


def test_a_dry_run_after_a_real_attempt_does_not_increment_it(manifest):
    from pipeline.stages import upload

    upload._record_attempt(manifest, "youtube")
    upload._record_attempt(manifest, "youtube", counts=False)
    assert manifest.platform_posts[0]["attempts"] == 1
    upload._record_attempt(manifest, "youtube")
    assert manifest.platform_posts[0]["attempts"] == 2


def test_an_insufficient_scope_is_told_apart_from_other_failures():
    """403 also covers quota and suspension, which are not the same."""
    from pipeline.stages import upload

    assert upload._is_scope_error(
        Exception("Request had insufficient authentication scopes."))
    assert upload._is_scope_error(Exception("reason: insufficientPermissions"))
    assert not upload._is_scope_error(Exception("quotaExceeded"))
    assert not upload._is_scope_error(Exception("403 forbidden"))


# ---- the disclaimer belongs to the profile ----------------------------


def test_a_story_does_not_carry_an_investment_disclaimer(episode, manifest):
    """upload.py hardcoded a SEBI investment-adviser notice from when the
    project was only about trading costs, and stapled it onto a folklore
    story about a monkey. It went out on a real video."""
    import profiles
    from pipeline.stages import upload

    text = upload.disclaimer_for(profiles.load("storyboard_shorts"))
    assert "SEBI" not in text
    assert "investment" not in text.lower()


def test_the_trading_profile_still_carries_the_full_notice():
    """Moving it must not lose it where it is actually needed."""
    import profiles
    from pipeline.stages import upload

    text = upload.disclaimer_for(profiles.load("trading"))
    assert "SEBI-registered investment adviser" in text


def test_a_profile_with_its_own_wording_gets_no_second_disclaimer(
        episode, manifest):
    """The old check looked for "not investment advice" specifically, so a
    profile saying "not professional advice" was judged to have none and
    had a contradictory one appended."""
    import profiles
    from pipeline.stages import upload

    profile = profiles.load("storyboard_shorts")
    (episode.dir / "metadata.json").write_text(json.dumps({"youtube": {
        "description": "A story.\n\n" + profile.style.DISCLAIMER}}))
    text = upload.youtube_description(episode, manifest, None, profile)
    assert text.lower().count("not professional advice") == 1
    assert "SEBI" not in text


def test_a_description_with_no_disclaimer_still_gets_one(episode, manifest):
    import profiles
    from pipeline.stages import upload

    (episode.dir / "metadata.json").write_text(json.dumps(
        {"youtube": {"description": "A story with no disclaimer."}}))
    text = upload.youtube_description(episode, manifest, None,
                                      profiles.load("storyboard_shorts"))
    assert "Not professional advice" in text


def test_the_storyboard_disclaimer_claims_no_sources_it_lacks():
    """It said "Sources are listed in the description" when every picture
    was generated and no sources existed."""
    import profiles

    assert "Sources are listed" not in \
        profiles.load("storyboard_shorts").style.DISCLAIMER


def test_a_recipes_tags_do_not_leak_onto_a_story(episode, manifest):
    """A folklore story went out tagged #postmortem and #debugging
    because its recipe was incident_log. A recipe's tags describe its
    subject, and a picture profile is not rendering that subject."""
    import profiles
    from pipeline.stages.metadata import hashtags

    data = {"recipe": "incident_log"}
    story = hashtags(data, profiles.load("storyboard_shorts"))
    assert "#postmortem" not in story
    assert "#storytime" in story

    # And still lead where the profile really is about that subject.
    chart = hashtags(data, profiles.load("engineering"))
    assert chart[0] == "#postmortem"
