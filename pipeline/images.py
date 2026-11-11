"""Getting a picture for a scene that already said what it wants.

`Scene.image_prompt` is written with the script, so every scene names
its own frame. Until now nothing read it: `sources/scene_NN.png` was
filled by hand, and an episode run without that hand-filling showed
typographic cards.

This is the thing that fills it.

**Why generated rather than stock.** A sixty-second sample made from
stock photographs had every character grinning through a story about
debt and illness. That is not a synchronisation bug and no amount of
timing work fixes it: a photograph has one expression and the script
needs several. A picture made for the line can be sad when the line is
sad, which is the whole argument for generation over search.

Stock is kept as a fallback anyway, because a generator that is down
should not stop an episode -- and because some scenes genuinely want a
real photograph of a real thing.

**Licensing is part of the result, not an afterthought.** Every picture
comes back with a credit: what made it, under what licence, and who to
name. Creative Commons images need attribution in the description, so
the credit is written to disk next to the pictures and the metadata
stage reads it. A picture whose licence is unknown is not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from . import image_library
from .config import env_int
from .errors import ConfigError, StageError
from .logging_setup import get

TIMEOUT_SEC = 180          # flux on a free endpoint is not quick
MAX_BYTES = 25 * 1024 * 1024
USER_AGENT = "net-of-fees/1.0 (+https://github.com/Chandan-c10)"

# Below this there is not enough picture to work with at all. It is
# lower than the frame on purpose: the free generators cap what they
# will draw, so a native source is routinely smaller than the video and
# is upscaled to fit. Rejecting those would mean rejecting every free
# provider.
MIN_SIDE = 512

# A picture whose pixels barely vary is an error page, a placeholder, or
# a solid colour. The same threshold checks.py uses on rendered frames.
FLAT_STDDEV = 3.0

# Providers that brand what they draw, and how much of the bottom edge
# the mark occupies.
#
# `nologo=true` is sent and ignored -- it is a paid-tier feature now --
# so the mark arrives on every picture and burns into the render
# permanently. Measured on real output at 637x924: the mark sits inside
# the bottom 6%. Seven is taken, because a mark half-cropped is worse
# than one left alone.
WATERMARK_STRIP = {"pollinations": 0.07}


# Dimensions are asked for in multiples of eight because every diffusion
# model quietly rounds to that anyway, and capped because a free
# endpoint will time out long before it refuses.
DIM_STEP = 8
MAX_SIDE = 2048

# What each provider will actually draw for nothing.
#
# Measured, not documented: pollinations answers 402 Payment Required
# above 589,824 pixels, which is 768x768 or 576x1024 -- exactly one
# ceiling in pixels rather than per side. Asking for the frame size gets
# every scene refused, which is how a whole episode came back as title
# cards.
FREE_PIXEL_CAP = {"pollinations": 589_824}

# Generation only, by default.
#
# Stock was the fallback until a real episode came back with a Hubble
# photograph in a village story and the same langur in three different
# scenes. A keyword search cannot know what a scene is about; it matches
# words. For narrative work a missing picture -- which shows a title
# card, and which gate 1 and `run.py check` both report -- is better
# than a confidently wrong one, because a wrong picture looks
# deliberate.
#
# Stock remains one setting away for anyone who wants it:
#   IMAGE_PROVIDERS=pollinations,openverse
# Everything that can draw, in the order worth trying. Each refuses
# immediately and cheaply when its credentials are absent, so an
# unconfigured provider costs a function call rather than a request --
# which means listing them all by default is free, and configuring one
# later needs no change here.
DEFAULT_PROVIDERS = ("cloudflare", "together", "huggingface", "pollinations")

# Appended to every prompt so a single episode's pictures look like they
# came from one hand. Overridable per profile and per run.
DEFAULT_STYLE = ("cinematic still, natural light, shallow depth of "
                 "field, muted colour, photographic")

# Added to every prompt, whatever the style.
#
# Cloudflare's flux-1-schnell takes a prompt and a step count and
# nothing else -- no negative prompt -- so everything that would have
# been a negative has to be said positively here instead. These are the
# mistakes that actually came back: lettering across the frame, and a
# second animal the prompt never asked for.
GUARDS = ("clean illustration with no text, no letters, no words, "
          "no watermark, no signature, no border")

# Asked for in the negative. "No text" in the positive prompt is widely
# ignored; a caption burnt into a generated picture is unfixable.
NEGATIVE = "text, watermark, caption, letters, logo, signature, frame, border"


@dataclass(frozen=True)
class Credit:
    """Where one picture came from, and what naming it requires."""

    scene: int
    provider: str
    license: str
    source_url: str = ""
    creator: str = ""
    # What the provider actually drew, which is not what was asked for:
    # the free tier caps the pixels, so the frame is filled by scaling
    # up. Recorded because a soft scene is otherwise a mystery.
    native: str = ""

    @property
    def needs_attribution(self) -> bool:
        """Whether this picture must be credited where it is published.

        Generated pictures do not need it. Creative Commons ones do, and
        the difference is a licence term rather than a courtesy.
        """
        return self.license.lower().startswith(("cc", "by"))

    def line(self) -> str:
        who = self.creator or "unknown"
        where = f" ({self.source_url})" if self.source_url else ""
        return f'"{who}", {self.license.upper()}{where}'


# ---- configuration ----------------------------------------------------


def providers() -> tuple[str, ...]:
    """Which sources to try, in order.

    A list rather than one name so a generator being down degrades to
    stock instead of to a title card.
    """
    raw = os.getenv("IMAGE_PROVIDERS", "").strip()
    if not raw:
        return DEFAULT_PROVIDERS
    chosen = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    unknown = [p for p in chosen if p not in PROVIDERS]
    if unknown:
        raise ConfigError(
            f"unknown image provider(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(PROVIDERS))}"
        )
    return chosen or DEFAULT_PROVIDERS


def style_suffix(profile=None) -> str:
    env = os.getenv("IMAGE_STYLE", "").strip()
    if env:
        return env
    return getattr(profile, "IMAGE_STYLE", DEFAULT_STYLE)


def workers() -> int:
    """How many scenes to draw at the same time."""
    try:
        return max(1, min(8, int(os.getenv("IMAGE_WORKERS",
                                           str(DEFAULT_WORKERS)))))
    except ValueError:
        return DEFAULT_WORKERS


def pace_sec() -> float:
    """How long to wait between scenes."""
    try:
        return max(0.0, float(os.getenv("IMAGE_PACE_SEC",
                                        str(DEFAULT_PACE_SEC))))
    except ValueError:
        return DEFAULT_PACE_SEC


def enabled() -> bool:
    """Whether to fetch pictures at all.

    Off is a legitimate choice: a profile drawing charts has no use for
    this, and someone curating pictures by hand does not want them
    overwritten.
    """
    return os.getenv("IMAGE_FETCH", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _dim(value: float) -> int:
    return max(DIM_STEP, min(MAX_SIDE, round(value / DIM_STEP) * DIM_STEP))


def fit_within(width: int, height: int, pixels: int) -> tuple[int, int]:
    """The largest size of this shape that fits in a pixel budget.

    The shape is kept, not the size: a vertical frame asked for as a
    square comes back stretched, and that bug has already cost one round
    of samples.
    """
    if width * height <= pixels:
        return width, height
    scale = (pixels / (width * height)) ** 0.5
    return _dim(width * scale), _dim(height * scale)


def budget_for(provider: str) -> int | None:
    """The pixel ceiling for this provider, if it has one."""
    override = os.getenv(f"{provider.upper()}_MAX_PIXELS", "").strip()
    if override:
        try:
            return max(DIM_STEP * DIM_STEP, int(override))
        except ValueError:
            pass
    return FREE_PIXEL_CAP.get(provider)


def seed_for(episode_id: str, index: int) -> int:
    """A stable seed, so re-running a stage reproduces the picture.

    Derived from the episode and scene rather than random: a re-render
    after a caption fix should not silently change what is on screen.
    """
    digest = hashlib.sha256(f"{episode_id}:{index}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def prompt_for(scene, profile=None) -> str:
    """The full prompt for one scene."""
    base = (getattr(scene, "image_prompt", "") or "").strip()
    if not base:
        raise StageError(
            f"scene {getattr(scene, 'index', '?')} has no image_prompt. "
            "The script stage writes one per scene; re-run it, or fill "
            "sources/ by hand."
        )
    return f"{base}. {style_suffix(profile)}. {GUARDS}".strip()


# ---- transport --------------------------------------------------------


class Unavailable(StageError):
    """An HTTP failure that may not happen again.

    Carries the status so a caller can tell "come back later" from "this
    will never work". The free generators answer 402 Payment Required
    under load as well as over quota, which reads like a permanent
    refusal and is not one.
    """

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


# Worth waiting out. 402 is in the list because the free tier returns it
# when it is busy, which a burst of fourteen scenes reliably causes.
RETRY_STATUSES = frozenset({402, 408, 425, 429, 500, 502, 503, 504})

# Measured against the live endpoint: about half of all requests come
# back 402 whatever the gap before them, because the free capacity is
# shared and busy rather than rate-limited per caller. Half a chance per
# attempt makes five attempts a 97% chance, and a failed attempt costs a
# second, so patience is nearly free. This matters because the pipeline
# runs unattended -- two minutes on a picture nobody is waiting for is
# not a cost worth optimising away.
BACKOFF_SEC = (5.0, 15.0, 30.0, 60.0)

# A gap between scenes, for the same reason. Being the caller who sends
# fourteen requests in four seconds is how you get refused.
DEFAULT_PACE_SEC = 8.0

# How many scenes to draw at once.
#
# One at a time, a sixteen-scene episode spends twenty minutes waiting
# on a server rather than working. Several at once is not a way to get
# more out of one busy endpoint -- it is a way to use several at the
# same time, which is why it arrived with the providers rather than
# before them. Kept low on purpose: the point is to overlap the waiting,
# not to flood anyone.
DEFAULT_WORKERS = 3


def _open(url: str, data: bytes | None = None,
          headers: dict[str, str] | None = None) -> tuple[bytes, str]:
    """Fetch bytes. Returns the body and its content type."""
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise StageError(f"{url} returned more than "
                                 f"{MAX_BYTES / 1e6:.0f} MB")
            return body, (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        raise Unavailable(f"{urllib.parse.urlparse(url).netloc} returned "
                          f"{exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unavailable(f"could not reach "
                          f"{urllib.parse.urlparse(url).netloc}: {exc}"
                          ) from exc


def _open_patiently(open_url, url: str, data: bytes | None = None,
                    headers: dict[str, str] | None = None,
                    log=None,
                    also_retry: frozenset[int] = frozenset()
                    ) -> tuple[bytes, str]:
    """Fetch, waiting out a busy server.

    Eleven of fourteen scenes came back 402 from a free endpoint that
    had answered the same request a minute earlier. Retrying is the
    difference between a full episode and a handful of title cards.
    """
    opener = open_url or _open
    for attempt, pause in enumerate((*BACKOFF_SEC, None)):
        try:
            return opener(url, data, headers)
        except Unavailable as exc:
            if pause is None or exc.code not in (RETRY_STATUSES | also_retry):
                raise
            if log:
                log.debug(f"{exc}; waiting {pause:.0f}s "
                          f"(attempt {attempt + 1})")
            time.sleep(pause)
    raise Unavailable("unreachable")       # pragma: no cover


# ---- providers --------------------------------------------------------
#
# Each takes the prompt and the frame it has to fill, and returns bytes
# plus a credit. Raising StageError moves on to the next provider; it is
# not fatal on its own.


def from_pollinations(prompt: str, width: int, height: int, seed: int,
                      open_url=None) -> tuple[bytes, dict]:
    """A free hosted Flux endpoint. No key, so nothing to leak.

    The prompt travels in the path, which means it must be escaped
    rather than trusted: a scene describing "1/2 of the village" would
    otherwise split the URL.
    """
    cap = budget_for("pollinations")
    if cap:
        width, height = fit_within(width, height, cap)
    query = urllib.parse.urlencode({
        "width": width, "height": height, "seed": seed,
        "model": os.getenv("POLLINATIONS_MODEL", "flux"),
        "negative": NEGATIVE, "nologo": "true", "safe": "true",
    })
    url = ("https://image.pollinations.ai/prompt/"
           f"{urllib.parse.quote(prompt, safe='')}?{query}")
    # 400 is retryable here and nowhere else. A well-behaved API means
    # "your request is wrong" by it, and retrying is pointless. This one
    # answers 400, 402 and 503 interchangeably when it is busy: the same
    # short, uncontroversial prompt got all three within a minute. So
    # the status says nothing about the request and only patience helps.
    body, kind = _open_patiently(open_url, url,
                                 also_retry=frozenset({400}))
    # The bytes decide, not the content type. Under load this endpoint
    # answers with a text error at HTTP 200 and still labels it
    # image/jpeg, so believing the header means saving an error message
    # as a scene.
    if not body.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
        raise StageError(f"pollinations did not return an image "
                         f"({kind or 'no content type'}, "
                         f"{len(body)} bytes)")
    return body, {"provider": "pollinations",
                  "license": "generated",
                  "source_url": ""}


def _keywords(prompt: str, limit: int = 6) -> str:
    """The searchable part of a prompt.

    A stock search engine matches tags, not sentences: handing it the
    style suffix returns pictures of the words "shallow depth of field".
    """
    stop = {"the", "and", "with", "into", "that", "this", "from", "over",
            "a", "an", "of", "in", "on", "at", "his", "her", "their",
            "cinematic", "still", "natural", "light", "shallow", "depth",
            "field", "muted", "colour", "color", "photographic", "text",
            "no", "shot", "close", "up", "wide"}
    words = [w for w in re.findall(r"[A-Za-z]{3,}", prompt.lower())
             if w not in stop]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return " ".join(seen[:limit])


def _search_openverse(query: str, open_url) -> list[dict]:
    search = ("https://api.openverse.org/v1/images/?"
              + urllib.parse.urlencode({
                  "q": query,
                  "license_type": "commercial,modification",
                  "size": "large", "page_size": 8,
                  "mature": "false",
              }))
    body, _kind = (open_url or _open)(search)
    try:
        return json.loads(body).get("results") or []
    except ValueError as exc:
        raise StageError("openverse returned something that is not JSON"
                         ) from exc


def from_openverse(prompt: str, width: int, height: int, seed: int,
                   open_url=None) -> tuple[bytes, dict]:
    """Creative Commons stock, filtered to what may be used commercially.

    `license_type=commercial,modification` is not a preference. A
    non-commercial or no-derivatives picture cannot go in a monetised
    video, and cropping one for a Ken Burns move is a derivative.

    The query is widened until something comes back. A generation prompt
    is specific by design -- "small grey langur monkey with a black
    face" -- and a tag-matching stock library has nothing that specific,
    so searching it verbatim returns nothing for every scene. Dropping
    words from the end keeps the subject and loses the detail, which is
    the right thing to lose.
    """
    results: list[dict] = []
    for terms in (6, 3, 2):
        query = _keywords(prompt, terms)
        if not query:
            break
        results = _search_openverse(query, open_url)
        if results:
            break
    if not results:
        raise StageError(f"openverse has nothing for {_keywords(prompt)!r}")

    # The seed picks among the candidates, so two scenes with similar
    # prompts do not land on the same photograph.
    order = sorted(range(len(results)),
                   key=lambda i: (i + seed) % len(results))
    last: StageError | None = None
    for i in order:
        item = results[i]
        url = item.get("url")
        licence = (item.get("license") or "").strip()
        if not url or not licence:
            continue
        try:
            picture, kind = (open_url or _open)(url)
        except StageError as exc:          # a dead link among live ones
            last = exc
            continue
        if "image" not in kind or "svg" in kind:
            # An SVG has an image content type and no pixels. Found when
            # three scenes lost their picture to a diagram served as one.
            continue
        try:
            validate(picture)
        except StageError as exc:
            # Checked here rather than left to the caller so one bad
            # candidate costs one candidate. Letting it reach fetch_one
            # discards the whole provider, and with it the other seven
            # results that were fine.
            last = exc
            continue
        return picture, {
            "provider": "openverse",
            "license": licence + (f"-{item['license_version']}"
                                  if item.get("license_version") else ""),
            "source_url": item.get("foreign_landing_url") or url,
            "creator": item.get("creator") or "",
        }
    raise StageError(f"no usable openverse result: {last or 'all unreadable'}")


def from_local(prompt: str, width: int, height: int, seed: int,
               open_url=None) -> tuple[bytes, dict]:
    """A diffusion server on this machine or the local network.

    The AUTOMATIC1111 API shape, because that is what the common local
    installs expose. Nothing leaves the network, which is the point.
    """
    import base64

    base = os.getenv("IMAGE_LOCAL_URL", "").strip().rstrip("/")
    if not base:
        raise StageError("IMAGE_LOCAL_URL is not set")
    payload = json.dumps({
        "prompt": prompt, "negative_prompt": NEGATIVE,
        "width": width, "height": height, "seed": seed,
        "steps": env_int("IMAGE_LOCAL_STEPS", 28),
        "sampler_name": os.getenv("IMAGE_LOCAL_SAMPLER", "DPM++ 2M"),
    }).encode()
    body, _kind = (open_url or _open)(
        f"{base}/sdapi/v1/txt2img", payload,
        {"Content-Type": "application/json"})
    try:
        encoded = (json.loads(body).get("images") or [None])[0]
    except ValueError as exc:
        raise StageError("the local server returned something that is not "
                         "JSON") from exc
    if not encoded:
        raise StageError("the local server returned no image")
    return base64.b64decode(encoded), {
        "provider": "local", "license": "generated", "source_url": ""}


def from_cloudflare(prompt: str, width: int, height: int, seed: int,
                    open_url=None) -> tuple[bytes, dict]:
    """Workers AI, on the account you already have.

    Ten thousand neurons a day at no charge, and flux-1-schnell costs a
    few each: hundreds of pictures a day rather than the handful a free
    public endpoint will spare. No watermark, and the credentials are
    an API token from the same dashboard as the storage.

    Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_TOKEN. Absent either,
    it says so and the chain moves on.
    """
    import base64

    account = (os.getenv("CLOUDFLARE_ACCOUNT_ID")
               or os.getenv("R2_ACCOUNT_ID") or "").strip()
    token = os.getenv("CLOUDFLARE_AI_TOKEN", "").strip()
    if not account or not token:
        raise StageError(
            "cloudflare needs CLOUDFLARE_AI_TOKEN (and an account id; "
            "R2_ACCOUNT_ID is used if it is the same account)"
        )
    model = os.getenv("CLOUDFLARE_IMAGE_MODEL",
                      "@cf/black-forest-labs/flux-1-schnell")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           f"/ai/run/{model}")
    # Prompt and steps only. flux-1-schnell refuses anything else
    # outright -- "Additional or unevaluated properties '/seed' not
    # allowed" -- so a seed cannot be passed and the picture is not
    # reproducible on this provider. The library is what makes a scene
    # stable across runs; this is where that stops being a nicety.
    payload = json.dumps({
        "prompt": prompt,
        "steps": env_int("CLOUDFLARE_IMAGE_STEPS", 4),
    }).encode()
    body, _kind = _open_patiently(
        open_url, url, payload,
        {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json"})
    try:
        answer = json.loads(body)
    except ValueError as exc:
        raise StageError("cloudflare returned something that is not JSON"
                         ) from exc
    if not answer.get("success", True):
        errors = answer.get("errors") or [{}]
        raise StageError(f"cloudflare refused: "
                         f"{errors[0].get('message', 'no reason given')}")
    # flux-1-schnell answers with base64 in `result.image`; some models
    # answer with the bytes directly.
    encoded = (answer.get("result") or {}).get("image")
    if not encoded:
        raise StageError("cloudflare returned no image")
    return base64.b64decode(encoded), {
        "provider": "cloudflare", "license": "generated", "source_url": ""}


def from_huggingface(prompt: str, width: int, height: int, seed: int,
                     open_url=None) -> tuple[bytes, dict]:
    """The Inference API, on a free account token.

    Rate limited rather than refused, which makes it a good second
    source: it is slow when busy instead of saying no.
    """
    token = os.getenv("HUGGINGFACE_TOKEN", "").strip()
    if not token:
        raise StageError("huggingface needs HUGGINGFACE_TOKEN")
    model = os.getenv("HUGGINGFACE_IMAGE_MODEL",
                      "stabilityai/stable-diffusion-3-medium-diffusers")
    payload = json.dumps({
        "inputs": prompt,
        "parameters": {"width": _dim(width), "height": _dim(height),
                       "negative_prompt": NEGATIVE, "seed": seed % 2147483647},
    }).encode()
    # router.huggingface.co, not api-inference: the old host stopped
    # resolving when Inference Providers replaced the serverless API,
    # and a DNS failure is an unhelpful way to learn that.
    body, kind = _open_patiently(
        open_url,
        f"https://router.huggingface.co/hf-inference/models/{model}",
        payload, {"Authorization": f"Bearer {token}",
                  "Content-Type": "application/json"})
    if not body.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
        raise StageError(f"huggingface returned {kind or 'no content type'} "
                         "rather than an image")
    return body, {"provider": "huggingface", "license": "generated",
                  "source_url": ""}


def from_together(prompt: str, width: int, height: int, seed: int,
                  open_url=None) -> tuple[bytes, dict]:
    """Together.ai, which has a free FLUX-schnell tier."""
    import base64

    token = os.getenv("TOGETHER_API_KEY", "").strip()
    if not token:
        raise StageError("together needs TOGETHER_API_KEY")
    model = os.getenv("TOGETHER_IMAGE_MODEL",
                      "black-forest-labs/FLUX.1-schnell-Free")
    payload = json.dumps({
        "model": model, "prompt": prompt,
        "width": _dim(width), "height": _dim(height),
        "steps": 4, "n": 1, "response_format": "b64_json",
    }).encode()
    body, _kind = _open_patiently(
        open_url, "https://api.together.xyz/v1/images/generations", payload,
        {"Authorization": f"Bearer {token}",
         "Content-Type": "application/json"})
    try:
        rows = json.loads(body).get("data") or []
    except ValueError as exc:
        raise StageError("together returned something that is not JSON"
                         ) from exc
    encoded = rows[0].get("b64_json") if rows else None
    if not encoded:
        raise StageError("together returned no image")
    return base64.b64decode(encoded), {
        "provider": "together", "license": "generated", "source_url": ""}


PROVIDERS = {
    "cloudflare": from_cloudflare,
    "huggingface": from_huggingface,
    "together": from_together,
    "pollinations": from_pollinations,
    "openverse": from_openverse,
    "local": from_local,
}


# ---- validation and writing -------------------------------------------


def validate(body: bytes) -> tuple[int, int]:
    """Refuse a picture that would look broken on screen.

    Every provider here will, under load, hand back a placeholder or an
    error page with an image content type. Checking the pixels is the
    only way to tell.
    """
    import io

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(body)) as im:
            im.load()
            width, height = im.size
            grey = im.convert("L")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise StageError(f"the picture could not be decoded: {exc}") from exc

    if min(width, height) < MIN_SIDE:
        raise StageError(f"{width}x{height} is smaller than the "
                         f"{MIN_SIDE}px minimum")
    try:
        import numpy as np
        spread = float(np.asarray(grey, dtype=float).std())
    except ImportError:                     # pragma: no cover
        return width, height
    if spread < FLAT_STDDEV:
        raise StageError(f"the picture is almost flat (variation {spread:.1f})")
    return width, height


def strip_watermark(body: bytes, provider: str) -> bytes:
    """Cut off the bottom edge where a provider brands its output.

    Done before the picture is saved anywhere, so nothing marked reaches
    the library, the render, or the backup. A watermark burned into a
    finished video cannot be removed afterwards without re-rendering,
    which makes every episode made with one wasted work.
    """
    import io

    from PIL import Image

    share = WATERMARK_STRIP.get(provider, 0.0)
    if share <= 0:
        return body
    with Image.open(io.BytesIO(body)) as im:
        width, height = im.size
        keep = int(height * (1 - share))
        if keep < MIN_SIDE // 2:        # nothing worth keeping
            return body
        cropped = im.convert("RGB").crop((0, 0, width, keep))
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    return buffer.getvalue()


def write(body: bytes, out: Path) -> Path:
    """Save as RGB PNG, which is what the visuals stage expects."""
    import io

    from PIL import Image

    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(body)) as im:
        im.convert("RGB").save(out, format="PNG")
    return out


def fetch_one(prompt: str, out: Path, width: int, height: int, seed: int,
              chain: tuple[str, ...] | None = None,
              open_url=None, log=None) -> Credit:
    """One picture, trying each provider until one gives a usable file.

    `open_url` is resolved here rather than bound as a default, so a
    caller that replaces the transport -- a test against a loopback
    server, or a run behind a proxy -- is honoured by the providers too.
    """
    log = log or get(None, "images")
    opener = open_url or _open
    tried: list[str] = []
    for name in (chain or providers()):
        provider = PROVIDERS[name]
        try:
            body, credit = provider(prompt, width, height, seed, opener)
            native_w, native_h = validate(body)
            # Before anything stores it. The library, the episode and the
            # backup should never hold a marked picture.
            body = strip_watermark(body, credit.get("provider", name))
        except StageError as exc:
            tried.append(f"{name}: {exc}")
            log.warning(f"{name} could not supply this scene: {exc}")
            continue
        write(body, out)
        return Credit(scene=0, native=f"{native_w}x{native_h}", **credit)
    raise StageError(
        "no provider produced a usable picture. Tried " + "; ".join(tried)
    )


# ---- the whole episode ------------------------------------------------


def credits_path(ep) -> Path:
    return ep.dir / "sources" / "credits.json"


def load_credits(ep) -> list[Credit]:
    path = credits_path(ep)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Credit(**row) for row in raw if isinstance(row, dict)]


def save_credits(ep, rows: list[Credit]) -> Path:
    path = credits_path(ep)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in rows], indent=2),
                    encoding="utf-8")
    return path


def attribution_lines(ep) -> list[str]:
    """Credits that have to appear where the episode is published.

    Only the ones a licence actually requires. Padding a description
    with "generated by flux" per scene helps nobody.
    """
    rows = [c for c in load_credits(ep) if c.needs_attribution]
    if not rows:
        return []
    seen, lines = set(), ["Images:"]
    for row in rows:
        line = row.line()
        if line not in seen:
            seen.add(line)
            lines.append(f"- {line}")
    return lines


def run_for(ep, m, spec, profile=None, force: bool = False,
            open_url=None, overscan: float = 1.3) -> dict:
    """Fill `sources/` for every scene. Returns what happened.

    Asks for more pixels than the frame because the move crops into the
    picture; a source rendered at frame size goes soft the moment it
    zooms.
    """
    log = get(m.id, "images")
    sources = ep.dir / "sources"
    width = _dim(spec.width * overscan)
    height = _dim(spec.height * overscan)

    existing = {c.scene: c for c in load_credits(ep)}
    rows: list[Credit] = []
    made, kept, failed, stale, reused = [], [], {}, [], []
    substituted: list[str] = []
    # Scenes no provider could draw. Resolved after the drawing
    # pass, when this episode's own pictures exist to borrow from.
    unfilled: list[tuple] = []
    pace = pace_sec()
    sent = 0
    # Read once and extended in memory, so a picture drawn for scene 2
    # is available to scene 5 in the same run without re-reading the
    # index fourteen times.
    shelf = image_library.load()
    budget = image_library.max_new()
    # Pictures drawn for this episode. The budget falls back to these
    # rather than to the whole library, so a scene that runs out of
    # budget still gets a picture from its own story.
    mine: list[image_library.Entry] = []
    todo: list[tuple] = []
    spill: list[tuple] = []
    for scene in m.scenes:
        out = sources / f"scene_{scene.index:02d}.png"
        if out.exists() and not force:
            kept.append(out.name)
            rows.append(existing.get(scene.index) or Credit(
                scene.index, "supplied", "unknown"))
            continue

        # A picture already drawn for a close enough prompt. Most of a
        # story returns to the same few subjects, and four slightly
        # different men on four slightly different doorsteps is worse
        # than one man seen four ways.
        raw = (getattr(scene, "image_prompt", "") or "").strip()
        match = image_library.find(raw, shelf) if raw else None
        if not match and raw and budget and len(todo) >= budget:
            # The budget is spent. Held back rather than resolved now:
            # nothing has been drawn yet, so there is nothing of this
            # episode's own to reuse until the pass below has run.
            spill.append((scene, out, raw))
            continue
        if match:
            entry, score = match
            image_library.reuse(entry, out)
            reused.append(out.name)
            rows.append(Credit(scene.index, entry.provider or "library",
                               entry.license or "generated",
                               entry.source_url, entry.creator,
                               entry.native))
            log.info(f"{scene.slug} <- library ({score:.0%} match)")
            continue

        todo.append((scene, out, raw))

    # Everything left needs drawing. Done several at a time, because
    # each one is almost entirely waiting: the pace between requests is
    # kept per worker so the politeness survives the parallelism.
    if todo:
        import concurrent.futures as cf

        lock = threading.Lock()
        pool = workers()

        def draw(job):
            scene, out, raw = job
            with lock:
                nonlocal sent
                wait = pace if sent else 0.0
                sent += 1
            if wait:
                time.sleep(wait * (1.0 / max(1, pool)))
            try:
                credit = fetch_one(
                    prompt_for(scene, profile), out, width, height,
                    seed_for(m.id, scene.index), open_url=open_url, log=log)
            except StageError as exc:
                return scene, out, raw, None, exc
            return scene, out, raw, credit, None

        with cf.ThreadPoolExecutor(max_workers=pool) as runner:
            results = list(runner.map(draw, todo))

        for scene, out, raw, credit, exc in results:
            if exc is not None:
                failed[scene.slug] = str(exc)
                log.warning(f"{scene.slug}: {exc}")
                if not out.exists():
                    # Borrow a picture below rather than print a card.
                    unfilled.append((scene, out, raw))
                if out.exists():
                    # A redraw that failed leaves the previous picture
                    # in place, which is worth keeping and dangerous to
                    # be quiet about.
                    stale.append(out.name)
                    rows.append(existing.get(scene.index) or Credit(
                        scene.index, "supplied", "unknown"))
                    log.warning(f"{scene.slug} still shows the previous "
                                f"picture; delete {out.name} to force a card")
                continue
            rows.append(Credit(scene.index, credit.provider, credit.license,
                               credit.source_url, credit.creator,
                               credit.native))
            made.append(out.name)
            drawn = credit.native.split("x")
            if drawn and drawn[0].isdigit() and int(drawn[0]) < spec.width:
                log.info(f"{scene.slug} <- {credit.provider} at "
                         f"{credit.native}, scaled up to fill the frame")
            else:
                log.info(f"{scene.slug} <- {credit.provider} "
                         f"({credit.native})")
            if raw and image_library.enabled():
                entry = image_library.add(raw, out, asdict(credit),
                                          credit.native)
                shelf.append(entry)
                mine.append(entry)

    # Scenes held back by the budget, now that this episode has
    # pictures of its own to reuse.
    for scene, out, raw in spill:
        match = image_library.closest(raw, mine) if mine else None
        if not match:
            failed[scene.slug] = ("held back by IMAGE_MAX_NEW, and nothing "
                                  "was drawn to reuse")
            continue
        entry, score = match
        image_library.reuse(entry, out)
        reused.append(out.name)
        rows.append(Credit(scene.index, entry.provider or "library",
                           entry.license or "generated",
                           entry.source_url, entry.creator, entry.native))
        log.info(f"{scene.slug} <- library ({score:.0%} match, budget spent)")

    # Scenes no provider would draw. A free generator runs out -- every
    # one of them returned 402 or 429 on the day this was written -- and
    # the renderer's answer to a missing picture is a card of plain
    # text. Fifteen of those in a nineteen-scene story is not a story
    # anybody watches, and the quota is not going to come back before
    # the slot does.
    #
    # So borrow: this episode's own pictures first, because a scene from
    # the same story is the closest thing to the one that was wanted,
    # then the whole library. A repeated illustration is a far smaller
    # loss than a wall of words, and it is what reuse was built for.
    #
    # Never the same picture twice while an unused one is left. The
    # first version took the best match for each scene independently
    # and handed fourteen of nineteen scenes the identical drawing,
    # which is not an improvement on the text it replaced: a story is
    # a sequence of different moments.
    taken: set[str] = {e.file for e in mine}
    for scene, out, raw in unfilled:
        if not raw or not image_library.enabled():
            continue
        pool = [e for e in (mine + shelf) if e.file not in taken]
        if not pool:
            pool = mine + shelf          # everything is spoken for
        match = image_library.closest(raw, pool) if pool else None
        if not match:
            continue
        entry, score = match
        taken.add(entry.file)
        image_library.reuse(entry, out)
        substituted.append(out.name)
        failed.pop(scene.slug, None)
        rows.append(Credit(scene.index, entry.provider or "library",
                           entry.license or "generated",
                           entry.source_url, entry.creator, entry.native))
        log.warning(f"{scene.slug} <- library ({score:.0%} match): no "
                    f"provider would draw it, so it borrows a picture "
                    f"rather than showing a card of text")

    if rows:
        save_credits(ep, rows)
    return {
        "made": made, "kept": kept, "reused": reused,
        "substituted": substituted,
        "failed": failed, "stale": stale,
        "size": f"{width}x{height}",
        "max_new": budget,
        "providers": list(providers()),
        "attribution": bool(attribution_lines(ep)),
    }
