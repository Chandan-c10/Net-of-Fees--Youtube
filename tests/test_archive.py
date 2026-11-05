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


class Store(BaseHTTPRequestHandler):
    received: dict = {}
    status: int = 200

    def log_message(self, *a):
        pass

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
