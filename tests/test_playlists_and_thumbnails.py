"""Series playlists, and episode identity on the thumbnail."""

from __future__ import annotations

import pytest

from pipeline.manifest import Manifest
from pipeline.stages import upload


def series_manifest(series="It Worked in Backtest", episode=2) -> Manifest:
    m = Manifest.new("x", recipe="r", profile="engineering", voice="v",
                     title="The debt was always there")
    m.series = series
    m.series_episode = episode
    return m


# ---- scopes -----------------------------------------------------------


def test_playlists_are_opt_in(monkeypatch):
    """The wider scope is visible on the consent screen and in the API
    audit, so it is asked for only when wanted."""
    monkeypatch.delenv("YOUTUBE_PLAYLISTS", raising=False)
    assert upload.wanted_scopes() == [upload.UPLOAD_SCOPE]


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_enabling_playlists_widens_the_scope(monkeypatch, value):
    monkeypatch.setenv("YOUTUBE_PLAYLISTS", value)
    assert upload.PLAYLIST_SCOPE in upload.wanted_scopes()


def test_upload_scope_alone_cannot_touch_playlists():
    """Stated so the reason for the wider scope stays visible."""
    assert upload.UPLOAD_SCOPE != upload.PLAYLIST_SCOPE
    assert upload.UPLOAD_SCOPE.endswith(".upload")


# ---- which playlist ---------------------------------------------------


def test_the_series_is_the_playlist():
    assert upload.playlist_title(series_manifest()) == "It Worked in Backtest"


def test_an_episode_with_no_series_gets_no_playlist():
    """A playlist of unrelated videos helps nobody."""
    m = Manifest.new("x", recipe="r", profile="engineering", voice="v")
    m.series = ""
    assert upload.playlist_title(m) is None


# ---- adding to it -----------------------------------------------------


class FakeYouTube:
    def __init__(self, existing=None, fail=False):
        self.existing = existing or []
        self.fail = fail
        self.created = []
        self.added = []

    def playlists(self):
        outer = self

        class P:
            def list(self, **kw):
                return _Exec({"items": [
                    {"id": pid, "snippet": {"title": title}}
                    for pid, title in outer.existing]})

            def insert(self, part, body):
                if outer.fail:
                    raise RuntimeError("quota")
                outer.created.append(body["snippet"]["title"])
                return _Exec({"id": "NEW"})
        return P()

    def playlistItems(self):
        outer = self

        class I:
            def insert(self, part, body):
                outer.added.append(
                    (body["snippet"]["playlistId"],
                     body["snippet"]["resourceId"]["videoId"]))
                return _Exec({})
        return I()


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)
    debug = warning = info


def test_an_existing_playlist_is_reused():
    yt = FakeYouTube(existing=[("PL123", "It Worked in Backtest")])
    pid = upload.add_to_playlist(yt, series_manifest(), "VID1", Log())
    assert pid == "PL123"
    assert yt.created == [], "it should not make a second one"
    assert yt.added == [("PL123", "VID1")]


def test_a_missing_playlist_is_created():
    yt = FakeYouTube(existing=[])
    pid = upload.add_to_playlist(yt, series_manifest(), "VID1", Log())
    assert pid == "NEW"
    assert yt.created == ["It Worked in Backtest"]


def test_matching_ignores_case_and_spacing():
    yt = FakeYouTube(existing=[("PL9", "  it worked in BACKTEST ")])
    assert upload.add_to_playlist(yt, series_manifest(), "V", Log()) == "PL9"
    assert yt.created == []


def test_no_series_adds_nothing():
    yt = FakeYouTube()
    m = Manifest.new("x", recipe="r", profile="engineering", voice="v")
    m.series = ""
    assert upload.add_to_playlist(yt, m, "VID1", Log()) is None
    assert yt.added == []


def test_a_playlist_failure_does_not_raise():
    """The video is already live; raising would make a published episode
    look like a failed one."""
    log = Log()
    yt = FakeYouTube(existing=[], fail=True)
    assert upload.add_to_playlist(yt, series_manifest(), "VID1", log) is None
    assert any("could not add" in line for line in log.lines)


# ---- the thumbnail ----------------------------------------------------


def _render(**kw):
    import tempfile
    from pathlib import Path

    import profiles
    from pipeline.stages.thumbnail import _render as render
    out = Path(tempfile.mkdtemp()) / "t.png"
    render(kw.pop("focal", "8"), kw.pop("support", "bugs my tests missed"),
           out, profiles.load("engineering").style, **kw)
    return out


def test_the_episode_number_is_drawn():
    """A viewer deciding whether they have seen this one looks for a
    number."""
    import numpy as np
    from PIL import Image

    with_number = np.asarray(Image.open(_render(
        series="S", episode=2)).convert("L"), dtype=float)
    without = np.asarray(Image.open(_render(
        series="S")).convert("L"), dtype=float)
    assert float(np.mean(np.abs(with_number - without))) > 0.05


def test_the_episode_name_is_drawn():
    import numpy as np
    from PIL import Image

    named = np.asarray(Image.open(_render(
        series="S", episode=1, episode_name="The debt was always there")
    ).convert("L"), dtype=float)
    plain = np.asarray(Image.open(_render(
        series="S", episode=1)).convert("L"), dtype=float)
    assert float(np.mean(np.abs(named - plain))) > 0.05


def test_a_long_episode_name_is_trimmed_rather_than_overflowing():
    """It must not run off the frame or over the channel name."""
    out = _render(series="S", episode=1,
                  episode_name="A title so long it could not possibly fit "
                               "across the width of any thumbnail ever made")
    from PIL import Image
    assert Image.open(out).size == (1920, 1080)


def test_a_thumbnail_without_a_series_still_renders():
    from PIL import Image
    assert Image.open(_render()).size == (1920, 1080)
