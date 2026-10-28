"""The control panel, against a real HTTP server.

The panel can publish to a real channel, so the tests that matter are the
ones proving it cannot be made to do so improperly: it must not bypass a
gate, must not serve a file outside the episode, must not accept a
cross-site post, and must not expose itself without a token.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from pipeline import dashboard
from pipeline.errors import PipelineError
from pipeline.manifest import Episode, Manifest, Scene


@pytest.fixture
def ready(tmp_path, rendered_reel):
    """An episode far enough along that both gates are in play."""
    episodes = tmp_path / "episodes"
    ep = Episode("777", episodes)
    ep.ensure_dirs()
    m = Manifest.new("777", recipe="fee_drag", profile="trading",
                     title="Fixture", voice="v")
    m.scenes = [Scene(index=1, text="Charges took 41 percent.",
                      visual_spec={"data_key": "breakeven"},
                      duration_sec=4.0)]
    for stage in ("export", "script", "scrub", "voice", "charts", "captions",
                  "assemble", "metadata", "thumbnail"):
        m.mark_done(stage)
    m.mark_done("voice", draft=False)
    m.mark_done("scrub", findings=0)
    ep.snapshot.write_text(json.dumps({"recipe": "fee_drag"}), encoding="utf-8")
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    ep.video.write_bytes(rendered_reel.read_bytes())
    ep.thumbnail(1).write_bytes(b"\x89PNG\r\n\x1a\n")
    ep.thumbnail(2).write_bytes(b"\x89PNG\r\n\x1a\n")
    (ep.dir / "metadata.json").write_text(json.dumps({
        "chosen_title": "Fixture",
        "title_candidates": [
            {"text": "Fixture", "why": "a", "length": 7, "warnings": []},
            {"text": "What fees really cost", "why": "b", "length": 21,
             "warnings": []},
        ],
        "youtube": {"title": "Fixture", "description": "d", "tags": ["t"]},
        "instagram": {"caption": "c"},
    }), encoding="utf-8")
    ep.save_manifest(m)
    return episodes, ep, m


@pytest.fixture
def server(ready):
    episodes, ep, m = ready
    srv, url = dashboard.build("127.0.0.1", 0, None, episodes_dir=episodes)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = url.rstrip("/")
    try:
        yield base, ep, m
    finally:
        srv.shutdown()
        srv.server_close()


def get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(base: str, path: str, fields: dict, headers: dict | None = None
         ) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers=headers or {}, method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "") or ""


def message_of(location: str) -> str:
    query = urllib.parse.urlparse(location).query
    params = urllib.parse.parse_qs(query)
    return (params.get("ok") or params.get("err") or [""])[0]


# ---- pages render -----------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/episode/777", "/health",
                                  "/api/episodes", "/api/job"])
def test_every_page_renders(server, path):
    base, _, _ = server
    status, body = get(base, path)
    assert status == 200 and body


def test_the_episode_page_shows_the_script_and_the_metadata(server):
    base, _, _ = server
    _, body = get(base, "/episode/777")
    text = body.decode()
    assert "Charges took 41 percent." in text
    assert "What fees really cost" in text   # the other title candidate
    assert "Gate 1" in text and "Gate 2" in text


def test_the_api_reports_blockers(server):
    base, _, _ = server
    _, body = get(base, "/api/episodes")
    row = json.loads(body)[0]
    assert row["id"] == "777"
    assert "gate_1_blockers" in row and "gate_2_blockers" in row


# ---- it cannot bypass a gate ------------------------------------------


def test_the_panel_cannot_clear_a_gate_that_the_cli_would_refuse(server):
    """The panel shares pipeline.gates rather than re-checking, so a
    condition the CLI enforces is enforced here too."""
    base, ep, m = server
    m.mark_done("voice", draft=True)      # unpublishable narration
    ep.save_manifest(m)

    status, location = post(base, "/episode/777/approve-2", {})
    assert status == 303
    assert "draft voice engine" in message_of(location)
    assert ep.load_manifest().gate_2_passed_at is None


def test_a_flagged_scene_blocks_gate_1_through_the_panel(server):
    base, ep, _ = server
    status, location = post(base, "/episode/777/flag",
                            {"index": "1", "on": "1"})
    assert status == 303
    status, location = post(base, "/episode/777/approve-1", {})
    assert "flagged" in message_of(location)
    assert ep.load_manifest().gate_1_passed_at is None


def test_clearing_gate_1_actually_persists(server):
    base, ep, _ = server
    status, location = post(base, "/episode/777/approve-1", {})
    assert status == 303, message_of(location)
    assert ep.load_manifest().gate_1_passed_at is not None


def test_publishing_for_real_needs_a_confirmation(server):
    base, _, _ = server
    _, location = post(base, "/episode/777/publish", {})
    assert "needs a confirmation" in message_of(location)


# ---- only vetted titles -----------------------------------------------


def test_a_title_outside_the_candidates_is_refused(server):
    """Free text here would get past the advisory-language check that
    metadata applies to the candidates."""
    base, ep, _ = server
    _, location = post(base, "/episode/777/title",
                       {"text": "Buy now, guaranteed 40% returns"})
    assert "only a generated candidate" in message_of(location)
    assert ep.load_manifest().title == "Fixture"


def test_a_candidate_title_is_accepted(server):
    base, ep, _ = server
    post(base, "/episode/777/title",
         {"text": "What fees really cost"})
    assert ep.load_manifest().title == "What fees really cost"


def test_a_thumbnail_that_does_not_exist_is_refused(server):
    base, ep, _ = server
    _, location = post(base, "/episode/777/thumbnail", {"n": "99"})
    assert "no thumbnail 99" in message_of(location)
    assert ep.load_manifest().chosen_thumbnail is None


def test_choosing_a_thumbnail_persists(server):
    base, ep, _ = server
    post(base, "/episode/777/thumbnail", {"n": "2"})
    assert ep.load_manifest().chosen_thumbnail == 2


# ---- assets -----------------------------------------------------------


def test_an_asset_inside_the_episode_is_served(server):
    base, _, _ = server
    status, body = get(base, "/asset/777/out/video.mp4")
    assert (status == 200 and body[:4] == b"\x00\x00\x00\x20") or status == 200


@pytest.mark.parametrize("path", [
    "/asset/777/../../../etc/passwd",
    "/asset/777/..%2f..%2fmanifest.json",
    "/asset/777/manifest.json",          # inside, but not a servable type
    "/asset/777/run.log",
])
def test_a_file_outside_or_unservable_is_refused(server, path):
    base, _, _ = server
    status, _ = get(base, path)
    assert status == 404


def test_an_unknown_episode_is_refused(server):
    base, _, _ = server
    assert get(base, "/episode/zzz")[0] == 404
    assert get(base, "/asset/zzz/out/video.mp4")[0] == 404


# ---- cross-site posts -------------------------------------------------


def test_a_cross_site_post_is_refused(server):
    """A link in a chat app must not be able to approve an episode."""
    base, ep, _ = server
    status, _ = post(base, "/episode/777/approve-1", {},
                     headers={"Origin": "http://evil.example"})
    assert status == 403
    assert ep.load_manifest().gate_1_passed_at is None


def test_a_same_origin_post_is_allowed(server):
    base, ep, _ = server
    host = urllib.parse.urlparse(base).netloc
    status, _ = post(base, "/episode/777/approve-1", {},
                     headers={"Origin": f"http://{host}"})
    assert status == 303
    assert ep.load_manifest().gate_1_passed_at is not None


# ---- tokens and exposure ----------------------------------------------


def test_it_refuses_to_leave_the_machine_without_a_token():
    """The panel can publish, so an open bind is not an option."""
    with pytest.raises(PipelineError, match="without a token"):
        dashboard.build("0.0.0.0", 0, None)


def test_a_token_lets_it_bind_beyond_localhost():
    srv, url = dashboard.build("0.0.0.0", 0, dashboard.new_token())
    try:
        assert "token=" in url
    finally:
        srv.server_close()


def test_localhost_needs_no_token():
    srv, url = dashboard.build("127.0.0.1", 0, None)
    try:
        assert "token=" not in url
    finally:
        srv.server_close()


@pytest.fixture
def guarded(ready):
    episodes, ep, _ = ready
    srv, url = dashboard.build("127.0.0.1", 0, "the-secret",
                               episodes_dir=episodes)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield url.split("/?")[0], ep
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_wrong_token_is_refused(guarded):
    base, _ = guarded
    assert get(base, "/")[0] == 403
    assert get(base, "/?token=wrong")[0] == 403
    assert get(base, "/?token=the-secret")[0] == 200


def test_a_post_without_the_token_is_refused(guarded):
    base, ep = guarded
    status, _ = post(base, "/episode/777/approve-1", {})
    assert status == 403
    assert ep.load_manifest().gate_1_passed_at is None


def test_a_post_with_the_token_is_accepted(guarded):
    base, ep = guarded
    status, _ = post(base, "/episode/777/approve-1", {"token": "the-secret"})
    assert status == 303
    assert ep.load_manifest().gate_1_passed_at is not None


def test_an_asset_needs_the_token_too(guarded):
    base, _ = guarded
    assert get(base, "/asset/777/out/video.mp4")[0] == 403
    assert get(base, "/asset/777/out/video.mp4?token=the-secret")[0] == 200


# ---- jobs -------------------------------------------------------------


def test_only_one_job_runs_at_a_time(ready):
    """Two renders on one episode would interleave manifest writes."""
    episodes, _, _ = ready
    panel = dashboard.Panel(episodes, None)
    panel.job = dashboard.Job("777", ["777", "--status"], "busy")
    panel.job.started = 0.0          # pretend it is mid-flight
    with pytest.raises(PipelineError, match="already running"):
        panel.start_job("777", ["777", "--status"], "second")


def test_a_job_captures_the_real_command_output(ready):
    """It spawns run.py rather than importing it, so the panel runs the
    same path a terminal would."""
    job = dashboard.Job("777", ["doctor"], "doctor")
    job.run()
    assert job.returncode is not None
    assert not job.running
    text = "\n".join(job.lines)
    assert "run.py doctor" in text
    assert "ffmpeg" in text.lower() or "python" in text.lower()


def test_an_unknown_stage_is_refused(server):
    base, _, _ = server
    _, location = post(base, "/episode/777/run", {"stage": "nosuchstage"})
    assert "no stage" in message_of(location)


def test_an_unknown_action_is_refused(server):
    base, _, _ = server
    status, _ = post(base, "/episode/777/nosuchaction", {})
    assert status in (303, 404)
