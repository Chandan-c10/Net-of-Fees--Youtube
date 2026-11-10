"""Archiving a finished episode to S3-compatible storage."""

from __future__ import annotations

import datetime as dt
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pipeline import archive
from pipeline.errors import ConfigError, StageError

KEYS = {
    "R2_ACCOUNT_ID": "acct123",
    "R2_BUCKET": "videos",
    "R2_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "R2_SECRET_ACCESS_KEY": "secret-example",
}


@pytest.fixture
def configured(monkeypatch):
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.delenv("R2_PUBLIC_BASE_URL", raising=False)
    return archive.Target()


# ---- configuration ----------------------------------------------------


def test_unconfigured_is_a_valid_state(monkeypatch):
    """Archiving is insurance; insurance must not fail a render."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    target = archive.Target()
    assert not target.configured
    assert target.missing()


def test_the_endpoint_is_derived_from_the_account(configured):
    assert configured.endpoint == "https://acct123.r2.cloudflarestorage.com"


def test_an_explicit_endpoint_wins(monkeypatch):
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("R2_ENDPOINT", "https://s3.example.com")
    assert archive.Target().endpoint == "https://s3.example.com"


def test_a_public_base_changes_only_the_url(monkeypatch, configured):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example.com/")
    assert archive.Target().url_for("a/b.mp4") == "https://cdn.example.com/a/b.mp4"


# ---- signing ----------------------------------------------------------


def _at(target):
    return archive.signed_headers(
        target, "episodes/001/video.mp4", b"hello", "video/mp4",
        dt.datetime(2026, 11, 5, 12, 0, 0, tzinfo=dt.UTC))


def test_a_signature_is_deterministic(configured):
    assert _at(configured) == _at(configured)


def test_a_different_payload_signs_differently(configured):
    other = archive.signed_headers(
        configured, "episodes/001/video.mp4", b"other", "video/mp4",
        dt.datetime(2026, 11, 5, 12, 0, 0, tzinfo=dt.UTC))
    assert other["Authorization"] != _at(configured)["Authorization"]


def test_the_secret_never_appears_in_the_headers(configured):
    assert "secret-example" not in str(_at(configured))


def test_the_body_hash_is_sent(configured):
    import hashlib
    headers = _at(configured)
    assert headers["x-amz-content-sha256"] == hashlib.sha256(b"hello").hexdigest()


# ---- uploading --------------------------------------------------------


LISTING = (b'<?xml version="1.0"?><ListBucketResult>'
           b"<Contents><Size>1024</Size></Contents>"
           b"</ListBucketResult>")


class Store(BaseHTTPRequestHandler):
    received: dict = {}
    status: int = 200
    stored_bytes: int = 1024

    def log_message(self, *a):
        pass

    def do_GET(self):
        """The bucket listing the cap check reads."""
        body = (b'<?xml version="1.0"?><ListBucketResult><Contents>'
                + f"<Size>{self.stored_bytes}</Size>".encode()
                + b"</Contents></ListBucketResult>")
        self.received["listed"] = True
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.received["body"] = self.rfile.read(length)
        self.received["path"] = self.path
        self.received["auth"] = self.headers.get("Authorization")
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def store(monkeypatch):
    Store.received = {}
    Store.status = 200
    Store.stored_bytes = 1024
    archive._USAGE_CACHE.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("R2_ENDPOINT",
                       f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.delenv("R2_PUBLIC_BASE_URL", raising=False)
    try:
        yield Store
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_real_upload_sends_the_bytes(store, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00" * 4096)
    url = archive.put(video, archive.key_for("042", "video.mp4"))
    assert store.received["body"] == b"\x00" * 4096
    assert store.received["path"].endswith("/videos/episodes/042/video.mp4")
    assert store.received["auth"].startswith("AWS4-HMAC-SHA256")
    assert "episodes/042/video.mp4" in url


def test_a_rejected_upload_is_reported(store, tmp_path):
    store.status = 403
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(StageError, match="could not archive"):
        archive.put(video, "k")


def test_an_unreachable_store_is_reported(monkeypatch, tmp_path):
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("R2_ENDPOINT", "http://127.0.0.1:1")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(StageError, match="could not reach"):
        archive.put(video, "k")


def test_uploading_without_configuration_is_refused(monkeypatch, tmp_path):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ConfigError, match="not configured"):
        archive.put(video, "k")


def test_a_missing_file_is_refused(configured, tmp_path):
    with pytest.raises(StageError, match="nothing to archive"):
        archive.put(tmp_path / "gone.mp4", "k")


# ---- it never breaks a render -----------------------------------------


class _Ep:
    def __init__(self, video):
        self.video = video


def test_archiving_does_not_raise_when_unconfigured(monkeypatch, tmp_path,
                                                    manifest):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert archive.archive_episode(_Ep(video), manifest) is None


def test_archiving_does_not_raise_when_the_store_is_down(monkeypatch,
                                                         tmp_path, manifest):
    """The video is already on disk; a failed copy must not fail the
    render it was protecting."""
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("R2_ENDPOINT", "http://127.0.0.1:1")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert archive.archive_episode(_Ep(video), manifest) is None


def test_a_successful_archive_records_the_url(store, tmp_path, manifest):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    url = archive.archive_episode(_Ep(video), manifest)
    assert url
    assert manifest.stage("assemble").data["archive_url"] == url


def test_the_check_reports_missing_configuration(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    ok, detail = archive.test_target()
    assert not ok and "not configured" in detail


def test_the_check_writes_a_probe_object(store):
    ok, detail = archive.test_target()
    assert ok, detail
    assert store.received["path"].endswith("/_check/hello.txt")


@pytest.mark.parametrize("name", ["R2_SECRET_ACCESS_KEY", "R2_ACCESS_KEY_ID"])
def test_storage_credentials_are_editable_and_secret(name):
    from pipeline import settings_store
    assert name in settings_store.EDITABLE
    assert settings_store.is_secret(name)


def test_a_voice_reference_url_is_treated_as_a_credential():
    """Anyone holding it can clone the speaker, and nothing about the
    word "URL" says so."""
    from pipeline import settings_store
    assert settings_store.is_secret("VOICE_REFERENCE_URL")
    assert settings_store.is_secret("VOICE_REFERENCE_URL_CHANDAN_EN")


def test_an_ordinary_url_is_not_a_secret():
    from pipeline import settings_store
    assert not settings_store.is_secret("R2_PUBLIC_BASE_URL")


# ---- the spend cap ----------------------------------------------------
# R2 gives 10 GB free and then bills $0.015/GB/month without asking. A
# storage bill is a poor way to find out an archive grew.


def test_the_cap_defaults_below_the_free_tier(monkeypatch):
    monkeypatch.delenv("R2_STORAGE_CAP_GB", raising=False)
    assert archive.cap_bytes() == int(archive.DEFAULT_CAP_GB * 1e9)
    assert archive.cap_bytes() < 10_000_000_000, "must stop short of billing"


def test_the_cap_can_be_raised_or_disabled(monkeypatch):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "50")
    assert archive.cap_bytes() == 50_000_000_000
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "0")
    assert archive.cap_bytes() == 0


def test_a_nonsense_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "lots")
    assert archive.cap_bytes() == int(archive.DEFAULT_CAP_GB * 1e9)


def test_an_upload_that_would_pass_the_cap_is_refused(monkeypatch, tmp_path,
                                                      configured):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "1")
    monkeypatch.setattr(archive, "bucket_usage",
                        lambda *a, **k: 990_000_000)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 20_000_000)      # 20 MB over a 1 GB cap
    with pytest.raises(StageError, match="cap"):
        archive.put(video, "k")


def test_an_upload_inside_the_cap_is_allowed(monkeypatch, store, tmp_path):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "1")
    monkeypatch.setattr(archive, "bucket_usage", lambda *a, **k: 1_000)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 2048)
    assert archive.put(video, "k")


def test_the_refusal_says_the_video_is_still_on_disk(monkeypatch, tmp_path,
                                                     configured):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "1")
    monkeypatch.setattr(archive, "bucket_usage", lambda *a, **k: 2_000_000_000)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(StageError) as exc:
        archive.put(video, "k")
    assert "still on disk" in str(exc.value)


def test_a_disabled_cap_skips_the_listing(monkeypatch, store, tmp_path):
    """Zero means do not check, so it must not even ask the bucket."""
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "0")

    def forbidden(*a, **k):
        raise AssertionError("it should not list the bucket")
    monkeypatch.setattr(archive, "bucket_usage", forbidden)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert archive.put(video, "k")


def test_budget_reports_how_full_it_is(monkeypatch, configured):
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "10")
    monkeypatch.setattr(archive, "bucket_usage", lambda *a, **k: 8_000_000_000)
    plan = archive.budget(0)
    assert plan["percent"] == 80.0
    assert plan["free"] == 2_000_000_000


def test_a_cap_refusal_does_not_break_the_render(monkeypatch, tmp_path,
                                                 manifest, configured):
    """Archiving is insurance; a full bucket must not fail the episode."""
    monkeypatch.setenv("R2_STORAGE_CAP_GB", "1")
    monkeypatch.setattr(archive, "bucket_usage", lambda *a, **k: 2_000_000_000)

    class _Ep:
        video = tmp_path / "v.mp4"
    _Ep.video.write_bytes(b"x")
    assert archive.archive_episode(_Ep(), manifest) is None


def test_a_listing_is_signed_with_its_query(configured):
    """Query parameters are part of the signature; an unsigned one is a
    403 from the bucket."""
    url, headers = archive.signed_get(
        configured, "", query={"list-type": "2", "max-keys": "1000"})
    assert "list-type=2" in url and "max-keys=1000" in url
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")


# ---- expiry -----------------------------------------------------------


def test_the_default_retention_covers_the_artifact_window():
    """Seven days is how long a cloud render's artifact lasts, which is
    what the archive exists to outlive."""
    assert archive.DEFAULT_RETAIN_DAYS == 7


def test_retention_can_be_changed_or_turned_off(monkeypatch):
    monkeypatch.setenv("R2_RETAIN_DAYS", "30")
    assert archive.retain_days() == 30
    monkeypatch.setenv("R2_RETAIN_DAYS", "0")
    assert archive.retain_days() == 0


def test_nothing_expires_when_retention_is_off(monkeypatch, configured):
    monkeypatch.setenv("R2_RETAIN_DAYS", "0")
    assert archive.expire(configured)["enabled"] is False


def test_expiry_needs_configuration(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    assert archive.expire()["enabled"] is False


def test_a_delete_is_signed_and_carries_no_secret(configured):
    url, headers = archive.signed_delete(configured, "episodes/1/video.mp4")
    assert "secret-example" not in str(headers) and "secret-example" not in url
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert url.endswith("/videos/episodes/1/video.mp4")


@pytest.mark.parametrize("modified,older", [
    ("2020-01-01T00:00:00.000Z", True),
    ("", None),
])
def test_object_age_is_read_from_the_listing(modified, older):
    age = archive._age_days(modified)
    if older is None:
        assert age is None
    else:
        assert age and age > 365


# ---- the backup must not expire itself --------------------------------


def test_expiry_never_touches_the_backup(configured, monkeypatch):
    """The rest of the bucket is a third copy, so deleting by age loses
    nothing. These prefixes are the only copy if the laptop goes, and
    expiring them would be the safety net deleting itself on a
    schedule."""
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    listed = [
        ("episodes/001/video.mp4", 10_000_000, old),
        ("record/001/manifest.json", 4_000, old),
        ("record/001/script.md", 9_000, old),
        ("library/abc123.png", 70_000, old),
    ]
    monkeypatch.setattr(archive, "_list_objects", lambda _t: listed)
    # Posted, so the video itself is eligible; the point of this test is
    # that the record and the library are not.
    monkeypatch.setattr(
        archive, "read_object",
        lambda _k, _t=None: b'{"published_at": "2026-11-01T00:00:00+00:00"}')
    monkeypatch.setenv("R2_RETAIN_DAYS", "7")

    result = archive.expire(configured, dry_run=True)
    gone = {row["key"] for row in result["removed"]}
    assert gone == {"episodes/001/video.mp4"}
    assert result["kept"] == 3


def test_every_permanent_prefix_is_recognised():
    for key in ("record/001/manifest.json", "library/abc.png"):
        assert archive.is_permanent(key)
    for key in ("episodes/001/video.mp4", "recordings/x", "lib/x"):
        assert not archive.is_permanent(key)


def test_a_record_file_is_sent_with_its_own_content_type():
    from pathlib import Path

    assert archive.content_type_for(Path("manifest.json")) == \
        "application/json"
    assert archive.content_type_for(Path("script.md")) == "text/markdown"
    assert archive.content_type_for(Path("a.png")) == "image/png"
    # An unknown suffix must still upload, as bytes.
    assert archive.content_type_for(Path("x.weird")) == \
        "application/octet-stream"


def test_the_record_keys_are_separate_from_the_video():
    assert archive.record_key("001", "manifest.json") == \
        "record/001/manifest.json"
    assert archive.key_for("001", "video.mp4") == "episodes/001/video.mp4"


def test_restoring_without_storage_configured_says_so(monkeypatch, tmp_path):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="not configured"):
        archive.restore_episode("001", tmp_path)


def test_restoring_an_episode_that_was_never_archived_says_so(
        configured, monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "_list_objects", lambda _t: [])
    with pytest.raises(StageError, match="nothing archived"):
        archive.restore_episode("001", tmp_path)


def test_a_restored_video_lands_where_the_pipeline_expects_it(
        configured, monkeypatch, tmp_path):
    """Not next to the manifest: the renderer looks in out/."""
    listed = [("episodes/001/video.mp4", 10, "x"),
              ("record/001/manifest.json", 4, "x")]
    monkeypatch.setattr(archive, "_list_objects", lambda _t: listed)
    written = []

    def fake_fetch(key, out, target=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        written.append(out)
        return out

    monkeypatch.setattr(archive, "fetch", fake_fetch)
    archive.restore_episode("001", tmp_path)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert names == {"001/out/video.mp4", "001/manifest.json"}


# ---- what the bucket holds --------------------------------------------


def test_archived_episodes_are_listed_newest_first(configured, monkeypatch):
    listed = [
        ("record/001/manifest.json", 4_000, "2026-11-01T10:00:00.000Z"),
        ("episodes/001/video.mp4", 10_000_000, "2026-11-01T10:05:00.000Z"),
        ("record/002/manifest.json", 4_000, "2026-11-08T10:00:00.000Z"),
        ("library/abc.png", 70_000, "2026-11-09T10:00:00.000Z"),
    ]
    monkeypatch.setattr(archive, "_list_objects", lambda _t: listed)
    rows = archive.archived_episodes(configured)
    assert [r["id"] for r in rows] == ["002", "001"]
    assert rows[1]["video"] is True
    # The library is not an episode.
    assert all(r["id"] != "abc.png" for r in rows)


def test_an_episode_whose_video_expired_is_still_listed(
        configured, monkeypatch):
    """The record never expires, so an episode with no video left is
    still restorable and still worth showing."""
    monkeypatch.setattr(archive, "_list_objects", lambda _t: [
        ("record/003/manifest.json", 4_000, "2026-11-01T10:00:00.000Z")])
    rows = archive.archived_episodes(configured)
    assert rows[0]["id"] == "003"
    assert rows[0]["video"] is False


def test_listing_without_storage_is_empty_not_an_error(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    assert archive.archived_episodes() == []


def test_an_unposted_video_is_never_expired(configured, monkeypatch):
    """Age alone was enough while the archive was a third copy. It stops
    being enough once this is the backup: an unpublished video past its
    retention is work nobody has judged, deleted on a timer."""
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    monkeypatch.setattr(archive, "_list_objects", lambda _t: [
        ("episodes/001/video.mp4", 10_000_000, old)])
    monkeypatch.setattr(archive, "read_object",
                        lambda _k, _t=None: b'{"published_at": null}')
    monkeypatch.setenv("R2_RETAIN_DAYS", "2")

    result = archive.expire(configured, dry_run=True)
    assert result["removed"] == []
    assert result["held"][0]["why"] == "not posted yet"


def test_a_posted_video_expires_on_schedule(configured, monkeypatch):
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    monkeypatch.setattr(archive, "_list_objects", lambda _t: [
        ("episodes/001/video.mp4", 10_000_000, old)])
    monkeypatch.setattr(
        archive, "read_object",
        lambda _k, _t=None: b'{"published_at": "2026-11-01T00:00:00+00:00"}')
    monkeypatch.setenv("R2_RETAIN_DAYS", "2")

    result = archive.expire(configured, dry_run=True)
    assert [r["key"] for r in result["removed"]] == ["episodes/001/video.mp4"]


def test_a_reel_counts_as_posted(configured):
    assert archive.was_posted(
        {"platform_posts": [{"platform": "instagram", "status": "posted"}]})
    assert not archive.was_posted(
        {"platform_posts": [{"platform": "instagram", "status": "failed"}]})
    assert not archive.was_posted({})


def test_an_unreadable_record_means_keep(configured, monkeypatch):
    """Unknown is not evidence that a video is safe to delete."""
    def explode(_k, _t=None):
        raise StageError("gone")

    monkeypatch.setattr(archive, "read_object", explode)
    assert archive.episode_was_posted("001", configured, {}) is False
