"""Fetching a voice reference from a URL.

The reference directory is gitignored, so a cloud runner has none. A URL
fixes that -- and the URL is itself a credential, because a link to your
voice reference is a link to your voice.
"""

from __future__ import annotations

import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pipeline.errors import ConfigError, StageError
from pipeline.voices import fetch

# ---- share links ------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://drive.google.com/file/d/1AbC_defGHI12345/view?usp=sharing",
    "https://drive.google.com/open?id=1AbC_defGHI12345",
    "https://docs.google.com/uc?id=1AbC_defGHI12345",
])
def test_a_drive_share_link_becomes_a_download_link(url):
    """A share link points at a viewer page, not the bytes."""
    assert fetch.direct_url(url) == (
        "https://drive.google.com/uc?export=download&id=1AbC_defGHI12345")


def test_a_folder_link_is_refused():
    with pytest.raises(ConfigError, match="single file"):
        fetch.direct_url("https://drive.google.com/drive/folders/xyz")


def test_any_other_url_is_left_alone():
    url = "https://example.com/signed/voice.zip?sig=abc"
    assert fetch.direct_url(url) == url


# ---- which URL --------------------------------------------------------


def test_a_per_voice_url_wins(monkeypatch):
    monkeypatch.setenv("VOICE_REFERENCE_URL", "https://fallback/x.zip")
    monkeypatch.setenv("VOICE_REFERENCE_URL_CHANDAN_EN", "https://mine/y.zip")
    assert fetch.reference_url("chandan_en") == "https://mine/y.zip"


def test_the_general_url_is_the_fallback(monkeypatch):
    monkeypatch.setenv("VOICE_REFERENCE_URL", "https://fallback/x.zip")
    monkeypatch.delenv("VOICE_REFERENCE_URL_OTHER", raising=False)
    assert fetch.reference_url("other") == "https://fallback/x.zip"


def test_no_url_configured_means_no_url(monkeypatch):
    for name in ("VOICE_REFERENCE_URL", "VOICE_REFERENCE_URL_CHANDAN_EN"):
        monkeypatch.delenv(name, raising=False)
    assert fetch.reference_url("chandan_en") is None


def test_nothing_is_fetched_without_a_url(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_REFERENCE_URL", raising=False)
    assert fetch.fetch("chandan_en", tmp_path) == []


# ---- a real server ----------------------------------------------------


class Serve(BaseHTTPRequestHandler):
    payload: bytes = b""
    content_type: str = "application/octet-stream"
    status: int = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def zip_of(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"RIFFfake-wav-data")
    return buffer.getvalue()


def test_a_zip_of_recordings_is_unpacked(server, tmp_path):
    Serve.payload = zip_of("take_01.wav", "take_02.wav", "notes.txt")
    got = fetch.fetch("v", tmp_path, url=f"{server}/ref.zip")
    assert sorted(p.name for p in got) == ["take_01.wav", "take_02.wav"]
    assert not (tmp_path / "notes.txt").exists(), "only audio is kept"


def test_a_zip_entry_cannot_choose_where_it_lands(server, tmp_path):
    """A path in an archive must not escape the directory."""
    Serve.payload = zip_of("../../escaped.wav", "fine.wav")
    got = fetch.fetch("v", tmp_path, url=f"{server}/ref.zip")
    assert all(p.parent == tmp_path for p in got)
    assert not (tmp_path.parent.parent / "escaped.wav").exists()


def test_a_single_recording_is_used_as_is(server, tmp_path):
    Serve.payload = b"RIFFfake-wav-data"
    got = fetch.fetch("v", tmp_path, url=f"{server}/voice.wav")
    assert len(got) == 1 and got[0].suffix == ".wav"


def test_a_web_page_instead_of_audio_is_explained(server, tmp_path):
    """Drive serves a confirmation page for some files."""
    Serve.payload = b"<!DOCTYPE html><html>Virus scan warning</html>"
    with pytest.raises(StageError, match="web page"):
        fetch.fetch("v", tmp_path, url=f"{server}/ref.zip")


def test_an_archive_with_no_audio_is_refused(server, tmp_path):
    Serve.payload = zip_of("readme.txt")
    with pytest.raises(StageError, match="no audio"):
        fetch.fetch("v", tmp_path, url=f"{server}/ref.zip")


def test_a_404_says_what_to_check(server, tmp_path):
    Serve.status = 404
    Serve.payload = b"nope"
    try:
        with pytest.raises(StageError, match="anyone with the link"):
            fetch.fetch("v", tmp_path, url=f"{server}/ref.zip")
    finally:
        Serve.status = 200


def test_an_unreachable_url_is_reported(tmp_path):
    with pytest.raises(StageError, match="could not reach"):
        fetch.fetch("v", tmp_path, url="http://127.0.0.1:1/ref.zip")


def test_an_existing_reference_is_kept_when_asked(server, tmp_path):
    (tmp_path / "already.wav").write_bytes(b"RIFF")
    Serve.payload = zip_of("new.wav")
    got = fetch.fetch("v", tmp_path, url=f"{server}/ref.zip", keep=True)
    assert [p.name for p in got] == ["already.wav"], "no download needed"


def test_discard_removes_only_audio(tmp_path):
    (tmp_path / "take.wav").write_bytes(b"RIFF")
    (tmp_path / "meta.json").write_text("{}")
    fetch.discard(tmp_path)
    assert not (tmp_path / "take.wav").exists()
    assert (tmp_path / "meta.json").exists(), "only the recording goes"
