"""Picture fetching, against a real server over a real socket.

The providers are HTTP clients, and the failures that matter are HTTP
ones: a 500, a text error at status 200, a dead link among live search
results, a placeholder that decodes but is a solid colour. A mocked
`urlopen` reproduces none of those, so these tests start a real server
on a loopback port and let urllib talk to it. No credentials, no outside
network, no API.
"""

from __future__ import annotations

import io
import json
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pipeline import images
from pipeline.errors import ConfigError, StageError

PNG = "image/png"

# Comfortably over images.MIN_SIDE, and small enough to encode quickly.
SERVED = 704


@lru_cache(maxsize=16)
def _png(width: int = SERVED, height: int = SERVED,
         flat: bool = False) -> bytes:
    """A real PNG, encoded once per shape and reused.

    A gradient rather than noise: `validate` only asks that the pixels
    vary, and random pixels make a three-megabyte file that is slow to
    encode and slow to serve. Cached because several tests want the same
    picture.
    """
    import numpy as np
    from PIL import Image

    if flat:
        pixels = np.full((height, width, 3), 128, dtype="uint8")
    else:
        ramp = np.linspace(0, 255, width, dtype="uint8")
        pixels = np.repeat(np.tile(ramp, (height, 1))[:, :, None], 3, axis=2)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="PNG")
    return buffer.getvalue()


class _Routes(BaseHTTPRequestHandler):
    """Serves whatever the test put in `cls.routes`."""

    routes: dict[str, tuple[int, str, bytes]] = {}
    seen: list[str] = []

    def _respond(self) -> None:
        path = self.path.split("?")[0]
        self.seen.append(self.path)
        status, kind, body = self.routes.get(
            path, (404, "text/plain", b"nothing here"))
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *_args) -> None:        # quiet test output
        pass


@pytest.fixture(autouse=True)
def isolated_library(tmp_path, monkeypatch):
    """A library of its own per test.

    The real one lives in the repo. A suite that wrote into it would
    pollute it and, worse, read pictures it did not put there -- which
    is how a fetch test stops fetching.
    """
    monkeypatch.setenv("IMAGE_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("IMAGE_REUSE", "0")
    monkeypatch.delenv("IMAGE_MAX_NEW", raising=False)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The pace between scenes and the backoff between attempts are real
    seconds against a real endpoint. Here they are zero, so a suite that
    exercises them does not take ten minutes."""
    monkeypatch.setenv("IMAGE_PACE_SEC", "0")
    monkeypatch.setattr(images, "BACKOFF_SEC", (0.0, 0.0))


@pytest.fixture
def server():
    """A real HTTP server on a loopback port, torn down after the test."""
    _Routes.routes = {}
    _Routes.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Routes)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, _Routes
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
def local(server):
    """`open_url` that really fetches, from the test server.

    The provider functions have the public hostnames built in, which is
    correct -- a provider should not take its own address as an
    argument. So the host is rewritten here and the request is otherwise
    genuine: real socket, real headers, real status codes.
    """
    httpd, routes = server
    host, port = httpd.server_address[0], httpd.server_address[1]
    # Bound now, not looked up later: a test that installs this as the
    # module's transport would otherwise make it call itself.
    real_open = images._open

    def open_url(url, data=None, headers=None):
        import urllib.parse
        parts = urllib.parse.urlsplit(url)
        rewritten = urllib.parse.urlunsplit(
            ("http", f"{host}:{port}", parts.path, parts.query, ""))
        return real_open(rewritten, data, headers)

    return open_url, routes


# ---- configuration ----------------------------------------------------


def test_the_default_is_generation_only(monkeypatch):
    """Stock was the fallback until a real episode came back with a
    Hubble photograph in a village story. A wrong picture looks
    deliberate; a missing one shows a card and gets reported."""
    monkeypatch.delenv("IMAGE_PROVIDERS", raising=False)
    assert images.providers() == ("pollinations",)


def test_stock_is_one_setting_away(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDERS", "pollinations,openverse")
    assert images.providers() == ("pollinations", "openverse")


def test_unknown_provider_is_named_not_ignored(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDERS", "pollinations,midjourney")
    with pytest.raises(ConfigError, match="midjourney"):
        images.providers()


def test_fetching_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("IMAGE_FETCH", "0")
    assert not images.enabled()
    monkeypatch.setenv("IMAGE_FETCH", "1")
    assert images.enabled()


def test_seed_is_stable_per_scene(manifest):
    first = images.seed_for(manifest.id, 3)
    assert first == images.seed_for(manifest.id, 3)
    assert first != images.seed_for(manifest.id, 4)
    assert first != images.seed_for("other", 3)


def test_prompt_carries_the_scene_and_the_style(manifest, monkeypatch):
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    scene = manifest.scenes[0]
    scene.image_prompt = "a boy counting coins at dusk"
    prompt = images.prompt_for(scene)
    assert "counting coins" in prompt
    assert "cinematic still" in prompt


def test_a_scene_with_no_prompt_says_which_scene(manifest):
    with pytest.raises(StageError, match="scene 1"):
        images.prompt_for(manifest.scenes[0])


def test_search_keywords_drop_the_style_words():
    keywords = images._keywords(
        "a boy counting coins at dusk. cinematic still, natural light, "
        "muted colour, no text")
    assert "boy" in keywords and "coins" in keywords
    assert "cinematic" not in keywords and "light" not in keywords


# ---- validation -------------------------------------------------------


def test_a_real_picture_passes():
    assert images.validate(_png(1024, 1024)) == (1024, 1024)


def test_a_solid_colour_is_refused():
    with pytest.raises(StageError, match="flat"):
        images.validate(_png(1024, 1024, flat=True))


def test_a_picture_softer_than_the_frame_is_refused():
    with pytest.raises(StageError, match="minimum"):
        images.validate(_png(320, 320))


def test_html_served_as_an_image_is_refused():
    with pytest.raises(StageError, match="could not be decoded"):
        images.validate(b"<html>rate limited</html>")


# ---- providers over the wire ------------------------------------------


def test_pollinations_returns_a_generated_credit(local, tmp_path):
    open_url, routes = local
    routes.routes["/prompt/a%20boy%20counting%20coins"] = (200, PNG, _png())
    body, credit = images.from_pollinations(
        "a boy counting coins", 1080, 1920, 42, open_url)
    images.validate(body)
    assert credit["provider"] == "pollinations"
    assert credit["license"] == "generated"
    # The shape it has to fill, and the seed, must reach the endpoint or
    # the picture comes back the wrong shape and changes between runs.
    # The size is the free ceiling, not the frame -- see the cap test.
    asked = routes.seen[-1]
    assert "width=576" in asked and "height=1024" in asked
    assert "seed=42" in asked


def test_the_request_is_capped_to_what_the_free_tier_draws(local):
    """Asking for the frame size got every scene refused with 402, which
    is how a whole episode came back as title cards."""
    open_url, routes = local
    routes.routes["/prompt/a%20village"] = (200, PNG, _png())
    images.from_pollinations("a village", 1408, 2496, 1, open_url)
    asked = routes.seen[-1]
    assert "width=576" in asked and "height=1024" in asked


def test_the_cap_keeps_the_shape_of_the_frame():
    cap = images.FREE_PIXEL_CAP["pollinations"]
    for frame in ((1408, 2496), (2496, 1408), (1024, 1024)):
        width, height = images.fit_within(*frame, cap)
        assert width * height <= cap
        # A vertical frame asked for as a square comes back stretched,
        # and that bug has already cost one round of samples.
        assert abs((width / height) - (frame[0] / frame[1])) < 0.05


def test_a_size_under_the_cap_is_asked_for_as_it_is(local):
    open_url, routes = local
    routes.routes["/prompt/a%20village"] = (200, PNG, _png())
    images.from_pollinations("a village", 512, 512, 1, open_url)
    assert "width=512" in routes.seen[-1]


def test_the_cap_can_be_raised_for_a_paid_key(local, monkeypatch):
    open_url, routes = local
    monkeypatch.setenv("POLLINATIONS_MAX_PIXELS", "9999999")
    routes.routes["/prompt/a%20village"] = (200, PNG, _png())
    images.from_pollinations("a village", 1080, 1920, 1, open_url)
    assert "width=1080" in routes.seen[-1]


def test_a_text_error_at_status_200_is_refused(local):
    open_url, routes = local
    routes.routes["/prompt/anything"] = (200, PNG, b"queue full, try later")
    with pytest.raises(StageError, match="did not return an image"):
        images.from_pollinations("anything", 1024, 1024, 1, open_url)


def test_a_prompt_with_a_slash_does_not_break_the_url(local):
    open_url, routes = local
    # Unescaped, "1/2 of the village" would make the path a different
    # route entirely.
    routes.routes["/prompt/1%2F2%20of%20the%20village"] = (200, PNG, _png())
    body, _credit = images.from_pollinations(
        "1/2 of the village", 1024, 1024, 1, open_url)
    assert images.validate(body) == (SERVED, SERVED)


def _openverse_results(*items) -> bytes:
    return json.dumps({"results": list(items)}).encode()


def test_openverse_keeps_the_licence_and_the_creator(local):
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/photo.png", "license": "by",
         "license_version": "4.0", "creator": "A. Photographer",
         "foreign_landing_url": "https://example.test/page"}))
    routes.routes["/photo.png"] = (200, PNG, _png())
    body, credit = images.from_openverse("a village at dusk", 1024, 1024, 0,
                                         open_url)
    images.validate(body)
    assert credit["license"] == "by-4.0"
    assert credit["creator"] == "A. Photographer"
    assert credit["source_url"] == "https://example.test/page"


def test_openverse_only_asks_for_commercially_reusable_pictures(local):
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json",
                                    _openverse_results())
    with pytest.raises(StageError, match="nothing for"):
        images.from_openverse("a village", 1024, 1024, 0, open_url)
    asked = routes.seen[0]
    # A non-commercial or no-derivatives picture cannot go in a monetised
    # video, and cropping one for a Ken Burns move is a derivative.
    assert "license_type=commercial%2Cmodification" in asked


def test_the_stock_search_widens_until_something_comes_back(local):
    """A generation prompt is specific by design, and a tag-matching
    library has nothing that specific -- which returned nothing for all
    fourteen scenes of a real episode."""
    open_url, routes = local
    narrow = _openverse_results()
    wide = _openverse_results(
        {"url": "https://example.test/photo.png", "license": "cc0",
         "creator": "Stock"})
    calls = {"n": 0}

    # The first query (six words) finds nothing; the narrowed one does.
    def respond(handler):
        calls["n"] += 1
        body = narrow if calls["n"] == 1 else wide
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    routes.routes["/photo.png"] = (200, PNG, _png())
    routes.routes["/v1/images/"] = (200, "application/json", narrow)
    # Two misses then a hit, driven by the handler above.
    original = routes._respond

    def patched(handler):
        if handler.path.startswith("/v1/images/"):
            return respond(handler)
        return original(handler)

    routes._respond = patched
    routes.do_GET = patched
    try:
        _body, credit = images.from_openverse(
            "a small grey langur monkey with a black face and a long tail",
            1024, 1024, 0, open_url)
    finally:
        routes._respond = original
        routes.do_GET = original
    assert credit["creator"] == "Stock"
    assert calls["n"] >= 2, "the query was never widened"


def test_openverse_skips_a_dead_link_and_takes_the_next(local):
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/gone.png", "license": "by"},
        {"url": "https://example.test/live.png", "license": "cc0",
         "creator": "Second"}))
    routes.routes["/live.png"] = (200, PNG, _png())
    _body, credit = images.from_openverse("a village", 1024, 1024, 0, open_url)
    assert credit["creator"] == "Second"


def test_a_server_error_names_the_host(local):
    open_url, routes = local
    routes.routes["/v1/images/"] = (500, "text/plain", b"down")
    with pytest.raises(StageError, match="500"):
        images.from_openverse("a village", 1024, 1024, 0, open_url)


def test_local_diffusion_posts_the_prompt_and_decodes_base64(
        local, monkeypatch):
    import base64

    open_url, routes = local
    monkeypatch.setenv("IMAGE_LOCAL_URL", "http://unused.test")
    payload = json.dumps(
        {"images": [base64.b64encode(_png()).decode()]}).encode()
    routes.routes["/sdapi/v1/txt2img"] = (200, "application/json", payload)
    body, credit = images.from_local("a village", 1024, 1024, 3, open_url)
    assert images.validate(body) == (SERVED, SERVED)
    assert credit["provider"] == "local"


def test_local_diffusion_without_a_url_is_a_clear_refusal(monkeypatch):
    monkeypatch.delenv("IMAGE_LOCAL_URL", raising=False)
    with pytest.raises(StageError, match="IMAGE_LOCAL_URL"):
        images.from_local("a village", 1024, 1024, 1)


# ---- the chain --------------------------------------------------------


def test_a_dead_generator_falls_through_to_stock(local, tmp_path):
    open_url, routes = local
    routes.routes["/prompt/a%20village%20at%20dusk.%20plain"] = (
        503, "text/plain", b"overloaded")
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/photo.png", "license": "cc0",
         "creator": "Stock"}))
    routes.routes["/photo.png"] = (200, PNG, _png())
    out = tmp_path / "scene_01.png"
    credit = images.fetch_one("a village at dusk. plain", out, 1024, 1024, 0,
                              chain=("pollinations", "openverse"),
                              open_url=open_url)
    assert out.exists()
    assert credit.provider == "openverse"


def test_every_provider_failing_says_what_was_tried(local, tmp_path):
    open_url, _routes = local          # nothing registered: all 404
    with pytest.raises(StageError, match="pollinations"):
        images.fetch_one("a village", tmp_path / "x.png", 1024, 1024, 0,
                         chain=("pollinations", "openverse"),
                         open_url=open_url)


def test_a_flat_placeholder_is_rejected_and_the_next_provider_used(
        local, tmp_path):
    open_url, routes = local
    routes.routes["/prompt/a%20village.%20plain"] = (
        200, PNG, _png(1024, 1024, flat=True))
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/photo.png", "license": "cc0"}))
    routes.routes["/photo.png"] = (200, PNG, _png())
    out = tmp_path / "scene_01.png"
    credit = images.fetch_one("a village. plain", out, 1024, 1024, 0,
                              chain=("pollinations", "openverse"),
                              open_url=open_url)
    assert credit.provider == "openverse"


# ---- a whole episode --------------------------------------------------


@pytest.fixture
def story(manifest):
    for scene in manifest.scenes:
        scene.image_prompt = f"scene {scene.index} of a village"
    return manifest


def _serve_generated(routes, manifest) -> None:
    """Register the pollinations route for every scene in the episode."""
    import urllib.parse
    for scene in manifest.scenes:
        prompt = images.prompt_for(scene)
        routes.routes["/prompt/" + urllib.parse.quote(prompt, safe="")] = (
            200, PNG, _png(768, 768))


def test_run_for_fills_every_scene_and_records_the_credits(
        local, episode, story, monkeypatch):
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    _serve_generated(routes, story)
    result = images.run_for(episode, story, SHORTS, open_url=open_url)
    assert len(result["made"]) == 3
    assert not result["failed"]
    for scene in story.scenes:
        assert (episode.dir / "sources" / f"{scene.slug}.png").exists()
    credits = images.load_credits(episode)
    assert [c.scene for c in credits] == [1, 2, 3]
    assert all(c.provider == "pollinations" for c in credits)


def test_an_existing_picture_is_kept_unless_forced(
        local, episode, story, monkeypatch):
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    _serve_generated(routes, story)
    sources = episode.dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    mine = sources / "scene_01.png"
    mine.write_bytes(_png(900, 900))
    before = mine.read_bytes()

    kept = images.run_for(episode, story, SHORTS, open_url=open_url)
    assert "scene_01.png" in kept["kept"]
    assert mine.read_bytes() == before, "a hand-placed picture was overwritten"

    forced = images.run_for(episode, story, SHORTS, force=True,
                            open_url=open_url)
    assert "scene_01.png" in forced["made"]
    assert mine.read_bytes() != before


def test_one_unavailable_scene_does_not_lose_the_episode(
        local, episode, story, monkeypatch):
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    _serve_generated(routes, story)
    # Drop scene 2's route: every provider will 404 for it.
    import urllib.parse
    gone = "/prompt/" + urllib.parse.quote(
        images.prompt_for(story.scenes[1]), safe="")
    del routes.routes[gone]

    result = images.run_for(episode, story, SHORTS, open_url=open_url)
    assert len(result["made"]) == 2
    assert "scene_02" in result["failed"]


def test_sources_are_asked_for_larger_than_the_frame(
        local, episode, story, monkeypatch):
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    _serve_generated(routes, story)
    result = images.run_for(episode, story, SHORTS, open_url=open_url)
    width, height = (int(n) for n in result["size"].split("x"))
    # The move crops into the picture; a source at frame size goes soft
    # the moment it zooms.
    assert width > SHORTS.width and height > SHORTS.height


# ---- attribution ------------------------------------------------------


def test_creative_commons_pictures_are_credited(episode):
    images.save_credits(episode, [
        images.Credit(1, "openverse", "by-4.0", "https://example.test/p",
                      "A. Photographer"),
        images.Credit(2, "pollinations", "generated"),
    ])
    lines = images.attribution_lines(episode)
    assert any("A. Photographer" in line for line in lines)
    assert not any("pollinations" in line for line in lines)


def test_an_all_generated_episode_adds_no_credits(episode):
    images.save_credits(episode, [images.Credit(1, "pollinations",
                                                "generated")])
    assert images.attribution_lines(episode) == []


def test_a_credit_appears_in_the_youtube_description(episode, manifest):
    """The licence term is only met if the credit reaches the platform."""
    import profiles
    from pipeline.stages import metadata

    images.save_credits(episode, [
        images.Credit(1, "openverse", "by-4.0", "https://example.test/p",
                      "A. Photographer")])
    manifest.pass_gate_1()
    metadata.run(episode, manifest, profiles.load(manifest.profile))
    written = json.loads((episode.dir / "metadata.json").read_text())
    assert "A. Photographer" in written["youtube"]["description"]


# ---- the stage --------------------------------------------------------


def test_a_chart_profile_skips_rather_than_fails(episode, manifest):
    import profiles
    from pipeline.stages import images as stage

    result = stage.run(episode, manifest, profiles.load("trading"))
    assert result == {"skipped": True}
    assert manifest.is_done("images")


def test_turning_fetching_off_skips_the_stage(episode, story, monkeypatch):
    import profiles
    from pipeline.stages import images as stage

    monkeypatch.setenv("IMAGE_FETCH", "off")
    profile = profiles.load("storyboard_shorts")
    result = stage.run(episode, story, profile)
    assert result == {"skipped": True}


def test_the_stage_fills_sources_for_a_storyboard(
        local, episode, story, monkeypatch):
    import profiles
    from pipeline.stages import images as stage

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    monkeypatch.setenv("IMAGE_FETCH", "1")
    profile = profiles.load("storyboard_shorts")
    _serve_generated(routes, story)
    monkeypatch.setattr(images, "_open", open_url)
    result = stage.run(episode, story, profile)
    assert len(result["made"]) == 3
    assert story.stage("images").data["providers"] == ["pollinations"]


def test_a_vertical_profile_asks_for_a_vertical_picture():
    import profiles
    from pipeline.config import SHORTS
    from pipeline.stages import images as stage

    assert stage.spec_for(profiles.load("storyboard_shorts")) == SHORTS


# ---- waiting out a busy endpoint --------------------------------------


def test_a_402_is_retried_not_believed(local, monkeypatch):
    """The free tier answers 402 when it is busy as well as when it is
    out of quota. Believing it the first time cost eleven of fourteen
    scenes on a real episode."""
    open_url, routes = local
    monkeypatch.setattr(images, "BACKOFF_SEC", (0.01, 0.01))
    attempts = {"n": 0}

    def flaky(url, data=None, headers=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise images.Unavailable("image.pollinations.ai returned 402", 402)
        return open_url(url, data, headers)

    routes.routes["/prompt/a%20village"] = (200, PNG, _png())
    body, _credit = images.from_pollinations("a village", 512, 512, 1, flaky)
    assert images.validate(body) == (SERVED, SERVED)
    assert attempts["n"] == 3


def test_a_404_is_not_retried(monkeypatch):
    """A missing route will still be missing in twelve seconds."""
    monkeypatch.setattr(images, "BACKOFF_SEC", (0.01, 0.01))
    attempts = {"n": 0}

    def gone(url, data=None, headers=None):
        attempts["n"] += 1
        raise images.Unavailable("example.test returned 404", 404)

    with pytest.raises(StageError, match="404"):
        images.from_pollinations("a village", 512, 512, 1, gone)
    assert attempts["n"] == 1


def test_an_svg_candidate_is_skipped_for_the_next_one(local):
    """An SVG has an image content type and no pixels."""
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/diagram.svg", "license": "by"},
        {"url": "https://example.test/photo.png", "license": "cc0",
         "creator": "Second"}))
    routes.routes["/diagram.svg"] = (200, "image/svg+xml", b"<svg/>")
    routes.routes["/photo.png"] = (200, PNG, _png())
    _body, credit = images.from_openverse("a village", 1024, 1024, 0, open_url)
    assert credit["creator"] == "Second"


def test_one_undecodable_candidate_does_not_lose_the_provider(local):
    """Letting it reach fetch_one discards the whole provider, and with
    it the other results that were fine."""
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/broken.png", "license": "by"},
        {"url": "https://example.test/photo.png", "license": "cc0",
         "creator": "Second"}))
    routes.routes["/broken.png"] = (200, PNG, b"\x89PNG truncated")
    routes.routes["/photo.png"] = (200, PNG, _png())
    _body, credit = images.from_openverse("a village", 1024, 1024, 0, open_url)
    assert credit["creator"] == "Second"


def test_a_tiny_stock_photo_is_skipped(local):
    open_url, routes = local
    routes.routes["/v1/images/"] = (200, "application/json", _openverse_results(
        {"url": "https://example.test/thumb.png", "license": "by"},
        {"url": "https://example.test/photo.png", "license": "cc0",
         "creator": "Second"}))
    routes.routes["/thumb.png"] = (200, PNG, _png(200, 200))
    routes.routes["/photo.png"] = (200, PNG, _png())
    _body, credit = images.from_openverse("a village", 1024, 1024, 0, open_url)
    assert credit["creator"] == "Second"


def test_scenes_are_paced_apart(episode, story, local, monkeypatch):
    """Sending fourteen requests in four seconds is how you get refused."""
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    monkeypatch.setenv("IMAGE_PACE_SEC", "0.05")
    _serve_generated(routes, story)
    waits = []
    monkeypatch.setattr(images.time, "sleep", waits.append)
    images.run_for(episode, story, SHORTS, open_url=open_url)
    # Three scenes, so two gaps: paced between, not before the first.
    assert waits == [0.05, 0.05]


def test_the_pace_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("IMAGE_PACE_SEC", "0")
    assert images.pace_sec() == 0.0
    monkeypatch.setenv("IMAGE_PACE_SEC", "not a number")
    assert images.pace_sec() == images.DEFAULT_PACE_SEC


def test_a_failed_redraw_keeps_the_old_picture_and_says_so(
        local, episode, story, monkeypatch):
    """Eight scenes of a real episode stayed on pictures from a provider
    that had since been removed from the chain, and nothing said so."""
    from pipeline.config import SHORTS

    open_url, _routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    sources = episode.dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    for scene in story.scenes:
        (sources / f"{scene.slug}.png").write_bytes(_png())

    # Nothing registered, so every redraw fails.
    result = images.run_for(episode, story, SHORTS, force=True,
                            open_url=open_url)
    assert len(result["failed"]) == 3
    assert sorted(result["stale"]) == ["scene_01.png", "scene_02.png",
                                       "scene_03.png"]
    for scene in story.scenes:
        assert (sources / f"{scene.slug}.png").exists()


def test_a_400_from_the_generator_is_retried(monkeypatch):
    """A well-behaved API means "your request is wrong" by 400. This one
    answers 400, 402 and 503 interchangeably when busy -- the same short,
    uncontroversial prompt got all three within a minute."""
    monkeypatch.setattr(images, "BACKOFF_SEC", (0.0, 0.0))
    attempts = {"n": 0}

    def flaky(url, data=None, headers=None):
        attempts["n"] += 1
        raise images.Unavailable("image.pollinations.ai returned 400", 400)

    with pytest.raises(StageError, match="400"):
        images.from_pollinations("a village", 512, 512, 1, flaky)
    assert attempts["n"] == 3, "a 400 from this endpoint was believed"


def test_a_400_from_the_stock_library_is_not_retried(monkeypatch):
    """The exception is the generator's alone."""
    monkeypatch.setattr(images, "BACKOFF_SEC", (0.0, 0.0))
    attempts = {"n": 0}

    def flaky(url, data=None, headers=None):
        attempts["n"] += 1
        raise images.Unavailable("api.openverse.org returned 400", 400)

    with pytest.raises(StageError, match="400"):
        images.from_openverse("a village", 512, 512, 1, flaky)
    assert attempts["n"] == 1


# ---- reusing a picture instead of drawing another ---------------------


def test_a_close_enough_prompt_reuses_the_picture(tmp_path, monkeypatch):
    """A story returns to the same subject. Four slightly different men
    on four slightly different doorsteps is worse than one man seen four
    ways."""
    from pipeline import image_library as lib

    monkeypatch.setenv("IMAGE_REUSE", "1")
    picture = tmp_path / "drawn.png"
    picture.write_bytes(_png())
    lib.add("a small grey langur monkey on a dusty village lane at dawn",
            picture, {"provider": "pollinations", "license": "generated"})

    found = lib.find(
        "a small grey langur monkey on a dusty village lane carrying a coin")
    assert found is not None
    entry, score = found
    assert score >= lib.threshold()
    assert entry.provider == "pollinations"


def test_a_different_subject_is_not_reused(tmp_path, monkeypatch):
    from pipeline import image_library as lib

    monkeypatch.setenv("IMAGE_REUSE", "1")
    picture = tmp_path / "drawn.png"
    picture.write_bytes(_png())
    lib.add("a small grey langur monkey on a dusty village lane", picture)
    assert lib.find("a frail woman asleep on a rope cot indoors") is None


def test_shot_words_do_not_make_two_prompts_the_same():
    """Two prompts are not the same picture because both say "medium
    shot", and not different because one does."""
    from pipeline import image_library as lib

    assert lib.words("a man on a doorstep, medium shot") == {"man", "doorstep"}


def test_reuse_can_be_turned_off(tmp_path, monkeypatch):
    from pipeline import image_library as lib

    picture = tmp_path / "drawn.png"
    picture.write_bytes(_png())
    lib.add("a village lane at dawn", picture)
    monkeypatch.setenv("IMAGE_REUSE", "0")
    assert lib.find("a village lane at dawn") is None


def test_an_index_entry_whose_file_is_gone_is_skipped(tmp_path, monkeypatch):
    """The library is a cache; emptying the directory must not break."""
    from pipeline import image_library as lib

    picture = tmp_path / "drawn.png"
    picture.write_bytes(_png())
    entry = lib.add("a village lane at dawn", picture)
    entry.path.unlink()
    assert lib.load() == []


def test_the_budget_caps_how_many_pictures_are_drawn(
        local, episode, story, monkeypatch):
    """"I do not need a new image for every scene", expressed as a
    number rather than as a similarity score that cannot tell "the same
    man, calmer" from "the same man, broken"."""
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    monkeypatch.setenv("IMAGE_REUSE", "1")
    monkeypatch.setenv("IMAGE_REUSE_THRESHOLD", "1.0")   # only the budget
    monkeypatch.setenv("IMAGE_MAX_NEW", "1")
    _serve_generated(routes, story)

    result = images.run_for(episode, story, SHORTS, open_url=open_url)
    assert len(result["made"]) == 1, "the budget was ignored"
    assert len(result["reused"]) == 2
    # Every scene still has a picture: a spent budget reuses, it does
    # not leave title cards.
    for scene in story.scenes:
        assert (episode.dir / "sources" / f"{scene.slug}.png").exists()


def test_no_budget_means_every_scene_is_drawn(
        local, episode, story, monkeypatch):
    from pipeline.config import SHORTS

    open_url, routes = local
    monkeypatch.delenv("IMAGE_STYLE", raising=False)
    monkeypatch.setenv("IMAGE_REUSE", "1")
    monkeypatch.setenv("IMAGE_MAX_NEW", "0")
    # Prompts with nothing in common, so nothing is reused for being
    # similar and only the absent budget is under test.
    for scene, prompt in zip(story.scenes,
                             ("a monkey on a wall", "a woman asleep indoors",
                              "a harbour at night"), strict=True):
        scene.image_prompt = prompt
    _serve_generated(routes, story)
    result = images.run_for(episode, story, SHORTS, open_url=open_url)
    assert len(result["made"]) == 3
    assert result["reused"] == []
