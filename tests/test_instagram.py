"""The Instagram stage.

Two properties carry the weight: a second platform must not become a way
around Gate 2, and a retry must not double-post.
"""

import json

import pytest

from pipeline.errors import ConfigError, GateError, StageError
from pipeline.manifest import PUBLISH_STAGES, STAGES
from pipeline.stages import instagram


@pytest.fixture
def approved(manifest):
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    return manifest


@pytest.fixture
def reel(episode, rendered_reel):
    # A real clip, not placeholder bytes: the stage probes it with ffprobe
    # before uploading, so a fake file would fail for the wrong reason.
    episode.shorts_dir.mkdir(parents=True, exist_ok=True)
    path = episode.shorts_dir / "short_01.mp4"
    path.write_bytes(rendered_reel.read_bytes())
    return path


@pytest.fixture
def metadata_file(episode):
    (episode.dir / "metadata.json").write_text(json.dumps({
        "instagram": {"caption": "Charges took 41 percent.\n\n#trading #fees"}
    }))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "17841400000000000")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("IG_VIDEO_URL", raising=False)


# ---- gating -----------------------------------------------------------


def test_instagram_refuses_an_unapproved_episode(episode, manifest, reel):
    with pytest.raises(GateError, match="approved_at"):
        instagram.run(episode, manifest, dry_run=True)


def test_instagram_refuses_a_draft_voice(episode, approved, reel):
    approved.mark_done("voice", draft=True)
    with pytest.raises(GateError, match="draft"):
        instagram.run(episode, approved, dry_run=True)


def test_instagram_is_blocked_by_gate_2(approved):
    approved.gate_2_passed_at = None
    assert approved.gate_blocking("instagram") == "gate_2"


# ---- siblings, not a sequence -----------------------------------------


def test_instagram_does_not_require_the_youtube_upload(approved):
    for name in STAGES[:STAGES.index("upload")]:
        approved.mark_done(name)
    assert approved.unmet_prerequisites("instagram") == []


def test_youtube_does_not_require_instagram(approved):
    for name in STAGES[:STAGES.index("upload")]:
        approved.mark_done(name)
    assert approved.unmet_prerequisites("upload") == []


def test_both_publish_targets_are_registered():
    assert set(PUBLISH_STAGES) <= set(STAGES)


# ---- inputs -----------------------------------------------------------


def test_missing_vertical_cut_is_a_clear_error(episode, approved, metadata_file):
    with pytest.raises(StageError, match=r"(?i)run the shorts stage"):
        instagram.run(episode, approved, dry_run=True)


def test_missing_metadata_is_a_clear_error(episode, approved, reel):
    with pytest.raises(StageError, match="run the metadata stage"):
        instagram.run(episode, approved, dry_run=True)


def test_unconfigured_credentials_name_what_is_missing(monkeypatch):
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("IG_ACCESS_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        instagram._credentials()
    assert "IG_USER_ID" in str(exc.value) and "IG_ACCESS_TOKEN" in str(exc.value)


def test_dry_run_posts_nothing(episode, approved, reel, metadata_file, monkeypatch):
    def forbid(*a, **k):
        raise AssertionError("a dry run must not call the API")
    monkeypatch.setattr(instagram, "_request", forbid)
    instagram.run(episode, approved, dry_run=True)
    assert approved.stage("instagram").data["dry_run"] is True


# ---- caption ----------------------------------------------------------


def test_caption_carries_the_reconciliation_marker(episode, approved, metadata_file):
    out = instagram.caption_for(episode, approved, "abc123")
    assert out.endswith("ref: abc123")


def test_an_overlong_caption_loses_text_not_the_marker(episode, approved):
    (episode.dir / "metadata.json").write_text(json.dumps(
        {"instagram": {"caption": "x" * 5000}}))
    out = instagram.caption_for(episode, approved, "abc123")
    assert len(out) <= instagram.CAPTION_MAX
    assert out.endswith("ref: abc123")


def test_too_many_hashtags_is_refused(episode, approved):
    (episode.dir / "metadata.json").write_text(json.dumps(
        {"instagram": {"caption": " ".join(f"#t{i}" for i in range(40))}}))
    with pytest.raises(StageError, match="hashtags"):
        instagram.caption_for(episode, approved, "abc123")


def test_an_empty_caption_is_refused(episode, approved):
    (episode.dir / "metadata.json").write_text(json.dumps(
        {"instagram": {"caption": "   "}}))
    with pytest.raises(StageError, match="no instagram caption"):
        instagram.caption_for(episode, approved, "abc123")


# ---- idempotency ------------------------------------------------------


def test_a_retry_that_finds_the_post_does_not_upload_again(
        episode, approved, reel, metadata_file, creds, monkeypatch):
    """The protocol that matters: a crash after Instagram accepted must
    not produce a second reel."""
    approved.platform_posts.append({
        "platform": "instagram", "status": "attempting",
        "idempotency_key": "deadbeef", "external_id": None,
        "posted_at": None, "error": None, "attempts": 1,
    })

    monkeypatch.setattr(instagram, "_reconcile",
                        lambda u, t, ref: "17900000000000001")

    def forbid(*a, **k):
        raise AssertionError("must not create a container on a landed retry")
    monkeypatch.setattr(instagram, "_create_container", forbid)

    instagram.run(episode, approved, which=1)

    st = approved.stage("instagram")
    assert st.done and st.data["reconciled"] is True
    assert st.data["media_id"] == "17900000000000001"


def test_a_full_post_records_the_media_id(
        episode, approved, reel, metadata_file, creds, monkeypatch):
    calls = []
    monkeypatch.setattr(instagram, "_create_container",
                        lambda u, t, c, v: (calls.append("create") or ("c1", "uri")))
    monkeypatch.setattr(instagram, "_upload_bytes",
                        lambda uri, t, p, log: calls.append("upload"))
    monkeypatch.setattr(instagram, "_await_container",
                        lambda c, t, log: calls.append("await"))
    monkeypatch.setattr(instagram, "_post",
                        lambda path, params: {"id": "17900000000000002"})

    instagram.run(episode, approved, which=1)

    assert calls == ["create", "upload", "await"]
    assert approved.stage("instagram").data["media_id"] == "17900000000000002"
    row = next(r for r in approved.platform_posts if r["platform"] == "instagram")
    assert row["status"] == "posted"
    assert row["external_id"] == "17900000000000002"


def test_a_failed_post_is_recorded_on_the_row(
        episode, approved, reel, metadata_file, creds, monkeypatch):
    monkeypatch.setattr(instagram, "_create_container",
                        lambda u, t, c, v: (_ for _ in ()).throw(
                            StageError("container refused")))
    with pytest.raises(StageError, match="container refused"):
        instagram.run(episode, approved, which=1)
    row = next(r for r in approved.platform_posts if r["platform"] == "instagram")
    assert row["status"] == "failed" and "container refused" in row["error"]


def test_a_public_url_skips_the_byte_upload(
        episode, approved, reel, metadata_file, creds, monkeypatch):
    monkeypatch.setenv("IG_VIDEO_URL", "https://cdn.example.com/reel.mp4")
    seen = {}
    monkeypatch.setattr(instagram, "_create_container",
                        lambda u, t, c, v: (seen.update(url=v) or ("c1", None)))
    monkeypatch.setattr(instagram, "_upload_bytes",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not upload bytes")))
    monkeypatch.setattr(instagram, "_await_container", lambda c, t, log: None)
    monkeypatch.setattr(instagram, "_post", lambda p, q: {"id": "m1"})

    instagram.run(episode, approved, which=1)
    assert seen["url"] == "https://cdn.example.com/reel.mp4"


def test_a_container_error_explains_the_format_requirements(
        approved, creds, monkeypatch):
    monkeypatch.setattr(instagram, "_get",
                        lambda p, q: {"status_code": "ERROR", "status": "bad aspect"})
    with pytest.raises(StageError, match="9:16"):
        instagram._await_container("c1", "t", _NullLog())


class _NullLog:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass


# ---- input validation -------------------------------------------------


@pytest.mark.parametrize("which", [0, -1, -99])
def test_a_reel_number_below_one_is_refused(episode, which):
    """The cuts are numbered from 1; asking for 0 is a mistake, not a
    missing file."""
    with pytest.raises(ConfigError, match="must be 1 or higher"):
        instagram.reel_path(episode, which)


def test_an_out_of_range_reel_lists_what_exists(episode, rendered_reel):
    episode.shorts_dir.mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        (episode.shorts_dir / f"short_{n:02d}.mp4").write_bytes(
            rendered_reel.read_bytes())
    with pytest.raises(StageError, match=r"short_01\.mp4"):
        instagram.reel_path(episode, 9)


def test_a_missing_shorts_dir_says_to_run_the_stage(episode):
    with pytest.raises(StageError, match="Run the shorts stage first"):
        instagram.reel_path(episode, 1)
