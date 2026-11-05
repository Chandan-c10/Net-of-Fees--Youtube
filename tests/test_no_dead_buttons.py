"""Every button must reach a handler.

This has now gone wrong twice. A handler was lost in an edit, which made
its import unused, which made ruff remove the import, which hid the
evidence -- and the page still rendered a button posting to a route that
answered 404. The suite stayed green throughout, because no test ever
pressed a button.

So: render every page, extract every form action, and check the server
answers each one with something other than "no such action".
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from pipeline import dashboard
from pipeline.manifest import Episode, Manifest, Scene

ACTION = re.compile(r'<form[^>]*action="([^"]+)"', re.IGNORECASE)


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A panel with one episode far enough along to show every control."""
    from pipeline import panel_auth, settings_store
    monkeypatch.setattr(panel_auth, "CREDENTIAL_FILE", tmp_path / "panel.json")
    monkeypatch.setattr(settings_store, "ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("SMTP_HOST=example.com\n")

    episodes = tmp_path / "episodes"
    ep = Episode("777", episodes)
    ep.ensure_dirs()
    m = Manifest.new("777", recipe="fee_drag", profile="trading",
                     title="Fixture", voice="v")
    m.scenes = [Scene(index=1, text="A scene.", visual_spec={},
                      duration_sec=3.0)]
    for stage in ("export", "script", "scrub", "voice", "charts", "captions",
                  "assemble", "metadata", "thumbnail"):
        m.mark_done(stage)
    m.mark_done("voice", draft=False)
    m.mark_done("scrub", findings=0)
    m.pass_gate_1()
    ep.snapshot.write_text(json.dumps({"recipe": "fee_drag"}), encoding="utf-8")
    ep.video.parent.mkdir(parents=True, exist_ok=True)
    ep.video.write_bytes(b"\x00" * 128)
    ep.thumbnail(1).write_bytes(b"\x89PNG\r\n\x1a\n")
    (ep.dir / "metadata.json").write_text(json.dumps({
        "chosen_title": "Fixture",
        "title_candidates": [{"text": "Fixture", "why": "a", "length": 7,
                              "warnings": []}],
        "youtube": {"title": "Fixture", "description": "d", "tags": []},
        "instagram": {"caption": "c"},
    }), encoding="utf-8")
    ep.save_manifest(m)

    srv, url = dashboard.build("127.0.0.1", 0, None, episodes_dir=episodes)
    srv.RequestHandlerClass.panel.credentials = panel_auth.Credentials(
        tmp_path / "panel.json")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield url.rstrip("/")
    finally:
        srv.shutdown()
        srv.server_close()


def fetch(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.read().decode()


def post(base: str, path: str) -> int:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    request = urllib.request.Request(base + path, data=b"dry=1", method="POST")
    try:
        return urllib.request.build_opener(NoRedirect).open(
            request, timeout=30).status
    except urllib.error.HTTPError as exc:
        return exc.code


PAGES = ("/", "/settings", "/health", "/episode/777")


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(live, page):
    assert fetch(live, page)


def test_every_button_reaches_a_handler(live):
    """The test that was missing. A 404 here means a dead button."""
    actions = set()
    for page in PAGES:
        for action in ACTION.findall(fetch(live, page)):
            if action.startswith("/"):
                actions.add(action.split("?")[0])
    assert actions, "no forms found; the pages are not rendering controls"

    dead = [a for a in sorted(actions) if post(live, a) == 404]
    assert not dead, f"buttons posting to a route that does not exist: {dead}"


def test_the_pages_offer_the_controls_they_should(live):
    """Guards the guard: if the pages stopped rendering forms, the test
    above would pass by finding nothing."""
    found = set()
    for page in PAGES:
        found.update(a.split("?")[0] for a in ACTION.findall(fetch(live, page)))
    for expected in ("/settings", "/new", "/prune", "/storage-test",
                     "/notify-test", "/connect-youtube"):
        assert expected in found, f"{expected} is no longer offered"


def test_an_unknown_route_really_does_404(live):
    """Proves the check above can fail."""
    assert post(live, "/definitely-not-a-route") == 404
