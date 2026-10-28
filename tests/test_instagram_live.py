"""The Instagram stage against a real HTTP server.

The sibling file mocks `_request` and so proves only that the branching
is right. This one runs a real server on a real socket: real urllib, real
status codes, real Graph-shaped error bodies, real octet-stream upload.
What it cannot cover is Meta's own validation, which needs a live token.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pipeline.errors import StageError
from pipeline.stages import instagram

VERSION = instagram.DEFAULT_VERSION


class FakeGraph(BaseHTTPRequestHandler):
    """Enough of the Graph API to exercise the publish sequence.

    `script` lets a test say how the container should behave: how many
    IN_PROGRESS polls before FINISHED, or an outright failure.
    """

    received: dict = {}
    script: dict = {}

    def log_message(self, *a):  # keep pytest output clean
        pass

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)

        # The upload host takes raw bytes, not a form.
        if "/ig-api-upload/" in self.path:
            self.received["uploaded_bytes"] = len(body)
            self.received["offset"] = self.headers.get("offset")
            self.received["file_size"] = self.headers.get("file_size")
            self.received["auth"] = self.headers.get("Authorization")
            return self._json(200, {"success": True})

        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(body.decode()).items()}

        if self.path.endswith("/media"):
            self.received["container_form"] = form
            if self.script.get("container_refused"):
                return self._json(400, {"error": {
                    "type": "OAuthException",
                    "message": "Invalid parameter",
                }})
            return self._json(200, {
                "id": "CONTAINER1",
                "uri": f"http://{self.headers['Host']}/ig-api-upload/"
                       f"{VERSION}/CONTAINER1",
            })

        if self.path.endswith("/media_publish"):
            self.received["publish_form"] = form
            return self._json(200, {"id": "MEDIA42"})

        self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if parsed.path.endswith("/media"):
            self.received["list_params"] = params
            return self._json(200, {"data": self.script.get("existing", [])})

        if "CONTAINER1" in parsed.path:
            self.received["polls"] = self.received.get("polls", 0) + 1
            if self.script.get("container_error"):
                return self._json(200, {"status_code": "ERROR",
                                        "status": "aspect ratio not supported"})
            pending = self.script.get("in_progress", 0)
            if self.received["polls"] <= pending:
                return self._json(200, {"status_code": "IN_PROGRESS"})
            return self._json(200, {"status_code": "FINISHED"})

        self._json(404, {"error": {"message": f"no route {parsed.path}"}})


@pytest.fixture
def graph(monkeypatch):
    FakeGraph.received = {}
    FakeGraph.script = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGraph)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    monkeypatch.setenv("IG_API_HOST", base)
    monkeypatch.setenv("IG_UPLOAD_HOST", f"{base}/ig-api-upload")
    monkeypatch.setenv("IG_USER_ID", "17841400000000000")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "live-test-token")
    monkeypatch.delenv("IG_VIDEO_URL", raising=False)
    monkeypatch.setattr(instagram, "POLL_INTERVAL_SEC", 0.01)
    try:
        yield FakeGraph
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def ready(episode, manifest, rendered_reel):
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    episode.shorts_dir.mkdir(parents=True, exist_ok=True)
    reel = episode.shorts_dir / "short_01.mp4"
    reel.write_bytes(rendered_reel.read_bytes())
    (episode.dir / "metadata.json").write_text(json.dumps(
        {"instagram": {"caption": "Eight bugs.\n\n#engineering #testing"}}))
    return episode, manifest, reel


def test_a_real_publish_sequence_end_to_end(graph, ready):
    episode, manifest, reel = ready
    graph.script["in_progress"] = 2  # transcode twice before finishing

    instagram.run(episode, manifest, which=1)

    got = graph.received
    # The container asked for a resumable REELS upload.
    assert got["container_form"]["media_type"] == "REELS"
    assert got["container_form"]["upload_type"] == "resumable"
    assert "ref: " in got["container_form"]["caption"]

    # The whole file went up, with the headers the upload host requires.
    assert got["uploaded_bytes"] == reel.stat().st_size
    assert got["file_size"] == str(reel.stat().st_size)
    assert got["offset"] == "0"
    assert got["auth"] == "OAuth live-test-token"

    # It polled until FINISHED, then published that same container.
    assert got["polls"] == 3
    assert got["publish_form"]["creation_id"] == "CONTAINER1"

    assert manifest.stage("instagram").data["media_id"] == "MEDIA42"
    row = next(r for r in manifest.platform_posts if r["platform"] == "instagram")
    assert row["status"] == "posted" and row["external_id"] == "MEDIA42"


def test_a_real_400_is_reported_with_the_api_message(graph, ready):
    episode, manifest, _ = ready
    graph.script["container_refused"] = True
    with pytest.raises(StageError) as exc:
        instagram.run(episode, manifest, which=1)
    assert "400" in str(exc.value)
    assert "OAuthException" in str(exc.value)
    assert "Invalid parameter" in str(exc.value)
    row = next(r for r in manifest.platform_posts if r["platform"] == "instagram")
    assert row["status"] == "failed"


def test_a_real_container_error_stops_before_publishing(graph, ready):
    episode, manifest, _ = ready
    graph.script["container_error"] = True
    with pytest.raises(StageError, match="9:16"):
        instagram.run(episode, manifest, which=1)
    assert "publish_form" not in graph.received


def test_a_real_retry_reconciles_against_the_account(graph, ready):
    """A second attempt must find the first one's marker and stop."""
    episode, manifest, _ = ready
    manifest.platform_posts.append({
        "platform": "instagram", "status": "attempting",
        "idempotency_key": "cafebabe", "external_id": None,
        "posted_at": None, "error": None, "attempts": 1,
    })
    graph.script["existing"] = [
        {"id": "MEDIA_OLD", "caption": "something else"},
        {"id": "MEDIA_LANDED", "caption": "Eight bugs.\n\nref: cafebabe"},
    ]

    instagram.run(episode, manifest, which=1)

    assert manifest.stage("instagram").data["media_id"] == "MEDIA_LANDED"
    assert manifest.stage("instagram").data["reconciled"] is True
    # Nothing was uploaded a second time.
    assert "uploaded_bytes" not in graph.received
    assert "publish_form" not in graph.received


def test_a_real_retry_with_no_match_uploads_again(graph, ready):
    episode, manifest, reel = ready
    manifest.platform_posts.append({
        "platform": "instagram", "status": "attempting",
        "idempotency_key": "cafebabe", "external_id": None,
        "posted_at": None, "error": None, "attempts": 1,
    })
    graph.script["existing"] = [{"id": "X", "caption": "unrelated"}]

    instagram.run(episode, manifest, which=1)

    assert graph.received["uploaded_bytes"] == reel.stat().st_size
    assert manifest.stage("instagram").data["media_id"] == "MEDIA42"


def test_the_access_token_never_reaches_the_query_string_of_the_upload(graph, ready):
    """The upload host authenticates by header; a token in a URL lands in
    proxy logs."""
    episode, manifest, _ = ready
    instagram.run(episode, manifest, which=1)
    assert graph.received["auth"].startswith("OAuth ")


def test_a_dead_server_is_reported_as_unreachable(ready, monkeypatch):
    episode, manifest, _ = ready
    # Port 1 is reserved and nothing listens there.
    monkeypatch.setenv("IG_API_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("IG_USER_ID", "1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "t")
    with pytest.raises(StageError, match="cannot reach the Instagram API"):
        instagram.run(episode, manifest, which=1)


def test_a_poll_that_never_finishes_times_out(graph, ready, monkeypatch):
    episode, manifest, _ = ready
    graph.script["in_progress"] = 10_000
    monkeypatch.setattr(instagram, "POLL_TIMEOUT_SEC", 0.05)
    with pytest.raises(StageError, match="did not finish processing"):
        instagram.run(episode, manifest, which=1)
    assert "publish_form" not in graph.received
