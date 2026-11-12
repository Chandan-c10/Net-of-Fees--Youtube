"""Keeping a copy of a finished episode somewhere durable.

A render exists in two places: the laptop that made it, and YouTube once
it is published. That is usually enough. It stops being enough when the
render happened on a cloud runner, because an Actions artifact is
deleted after seven days and the episode has to be made again.

So: an optional upload to S3-compatible storage after a successful
render. Cloudflare R2 is the default target because its egress is free,
which for a bucket holding video is the difference between a free tier
and a surprise.

Three rules, in the shape of the rest of this project.

* Unconfigured is a valid state. No keys means no upload and no error,
  because archiving is insurance and insurance must not fail a render.
* It never blocks publishing. An upload that fails is logged and the
  episode carries on; the video is already on disk.
* The URL goes in the manifest, the credentials do not. Same rule as
  everywhere else: secrets live in the environment.

Signing is done here rather than with boto3 so the core install stays at
matplotlib and pillow. SigV4 is a page of hashing and the alternative is
a dependency on a render path that already works.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import ConfigError, StageError
from .logging_setup import get

TIMEOUT_SEC = 600
# R2 and most S3 implementations accept a single PUT up to 5 GB. An
# episode is single-figure megabytes, so multipart is not worth carrying.
MAX_BYTES = 5 * 1024 * 1024 * 1024

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
# R2 ignores the region but requires one in the signature.
DEFAULT_REGION = "auto"

# R2 gives 10 GB free and then bills $0.015 per GB per month without
# asking. The default stops short of that, because a storage bill is a
# bad way to find out an archive grew -- and nothing here is worth
# paying for when the local copy and YouTube both still have it.
DEFAULT_CAP_GB = 9.5

# How long an archived episode is kept. The cap stops the bucket
# growing; this stops it filling in the first place. A week is the
# default because the thing the archive protects against -- a cloud
# render's artifact expiring after seven days -- is over by then, and
# by that point the episode is either published or abandoned.
DEFAULT_RETAIN_DAYS = 7
# Pictures go sooner than videos. A picture has done its job once
# the video is rendered; the video has to survive until it is
# posted and seen.
DEFAULT_IMAGE_RETAIN_DAYS = 1

# Listing costs a Class B operation, of which there are 10 million free
# a month. Cached for a run so a batch does not list once per episode.
_USAGE_CACHE: dict[str, tuple[float, int]] = {}
CACHE_SECONDS = 300


def _unreachable(target: Target, reason) -> StageError:
    """A network error, with the setting that probably caused it.

    "Temporary failure in name resolution" is a true description of
    what the resolver did and tells the reader nothing about what to
    change. If a setting cannot be right, say so here: the hostname in
    the error is one nobody typed, and nobody will connect it to a
    secret pasted with a trailing newline.
    """
    said = [f"could not reach the bucket at {target.endpoint}: {reason}"]
    said.extend(target.complaints())
    if len(said) == 1 and "resolution" in str(reason).lower():
        said.append(
            "The host does not resolve. Check R2_ACCOUNT_ID (and "
            "R2_ENDPOINT, if set) for stray spaces or a newline -- a "
            "value pasted into a web form often carries one, and it "
            "builds a hostname that cannot exist."
        )
    return StageError("\n  ".join(said))


# A Cloudflare account id as the dashboard gives it.
_ACCOUNT_SHAPE = re.compile(r"[0-9a-f]{32}")


def _setting(name: str) -> str:
    """One environment value, with the whitespace taken off."""
    return (os.getenv(name) or "").strip()


class Target:
    """Where archives go, assembled from the environment."""

    def __init__(self) -> None:
        # Stripped, every one. A secret pasted into a web form arrives
        # with a trailing newline often enough that it is the first
        # thing to rule out -- and an account id with a newline in it
        # builds a hostname that cannot resolve, so the error that comes
        # back talks about DNS and never mentions the setting that
        # caused it. Three alerts in a day said "Temporary failure in
        # name resolution" while the same code worked locally.
        self.account = _setting("R2_ACCOUNT_ID")
        self.bucket = _setting("R2_BUCKET")
        self.key_id = _setting("R2_ACCESS_KEY_ID")
        self.secret = _setting("R2_SECRET_ACCESS_KEY")
        self.region = _setting("R2_REGION") or DEFAULT_REGION
        # Set when the bucket is served on a domain of your own.
        self.public_base = _setting("R2_PUBLIC_BASE_URL").rstrip("/")
        # Any S3-compatible endpoint; R2's is derived from the account.
        self.endpoint = _setting("R2_ENDPOINT") or (
            f"https://{self.account}.r2.cloudflarestorage.com"
            if self.account else "")

    def complaints(self) -> list[str]:
        """Settings that are present but cannot be right.

        Checked before a request rather than after, because the failure
        a bad value produces is a network error that names a hostname
        nobody typed.
        """
        out = []
        if self.account and not _ACCOUNT_SHAPE.fullmatch(self.account):
            looks_like_url = "//" in self.account or "." in self.account
            out.append(
                f"R2_ACCOUNT_ID is {self.account[:12]!r}..., which is not an "
                f"account id"
                + (" -- it looks like a URL. The id is the 32-character "
                   "string in the dashboard, not the endpoint; set "
                   "R2_ENDPOINT if you mean to give a whole address."
                   if looks_like_url else
                   ". It is 32 hex characters in the Cloudflare dashboard.")
            )
        if self.bucket and ("/" in self.bucket or " " in self.bucket):
            out.append(f"R2_BUCKET is {self.bucket!r}, which is a name, "
                       f"not a path")
        return out

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.key_id
                    and self.secret)

    def missing(self) -> list[str]:
        names = {
            "R2_ACCOUNT_ID or R2_ENDPOINT": self.endpoint,
            "R2_BUCKET": self.bucket,
            "R2_ACCESS_KEY_ID": self.key_id,
            "R2_SECRET_ACCESS_KEY": self.secret,
        }
        return [k for k, v in names.items() if not v]

    def url_for(self, key: str) -> str:
        """Where the object will be readable.

        A bucket is private until you publish it, so without a public
        base this is the storage URL rather than a working link.
        """
        if self.public_base:
            return f"{self.public_base}/{urllib.parse.quote(key)}"
        return f"{self.endpoint}/{self.bucket}/{urllib.parse.quote(key)}"


def cap_bytes() -> int:
    """The ceiling, in bytes. Zero disables the check."""
    raw = os.getenv("R2_STORAGE_CAP_GB", str(DEFAULT_CAP_GB)).strip()
    try:
        gigabytes = max(0.0, float(raw))
    except ValueError:
        gigabytes = DEFAULT_CAP_GB
    return int(gigabytes * 1_000_000_000)


def _list_page(target: Target, token: str | None) -> tuple[list[int], str | None]:
    """One page of object sizes, and the continuation token."""
    import xml.etree.ElementTree as ET

    query = {"list-type": "2", "max-keys": "1000"}
    if token:
        query["continuation-token"] = token
    url, headers = signed_get(target, "", query=query)
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
        body = resp.read()

    # The namespace is on every element, so it is stripped rather than
    # matched -- S3 implementations differ on which URI they declare.
    root = ET.fromstring(body)
    sizes, following = [], None
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "Size" and element.text:
            sizes.append(int(element.text))
        elif tag == "NextContinuationToken" and element.text:
            following = element.text
    return sizes, following


def bucket_usage(target: Target | None = None,
                 use_cache: bool = True) -> int:
    """Total bytes stored. Raises if it cannot be determined.

    Deliberately not guessed at: a cap enforced against an unknown
    figure is not a cap.
    """
    import time
    target = target or Target()
    if not target.configured:
        raise ConfigError("archive storage is not configured")

    key = f"{target.endpoint}/{target.bucket}"
    if use_cache and key in _USAGE_CACHE:
        when, value = _USAGE_CACHE[key]
        if time.time() - when < CACHE_SECONDS:
            return value

    total, token = 0, None
    while True:
        try:
            sizes, token = _list_page(target, token)
        except urllib.error.HTTPError as exc:
            raise StageError(
                f"could not list the bucket to check its size: {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise _unreachable(target, exc.reason) from exc
        total += sum(sizes)
        if not token:
            break
    _USAGE_CACHE[key] = (time.time(), total)
    return total


def budget(incoming: int, target: Target | None = None) -> dict:
    """Whether storing `incoming` more bytes stays inside the cap."""
    target = target or Target()
    limit = cap_bytes()
    if not limit:
        return {"capped": False, "allowed": True, "used": 0, "limit": 0}
    used = bucket_usage(target)
    allowed = used + incoming <= limit
    return {
        "capped": True,
        "allowed": allowed,
        "used": used,
        "limit": limit,
        "incoming": incoming,
        "free": max(0, limit - used),
        "percent": round(100 * used / limit, 1) if limit else 0.0,
    }


def retain_days() -> float:
    """How long to keep an archived object. 0 keeps everything."""
    raw = os.getenv("R2_RETAIN_DAYS", str(DEFAULT_RETAIN_DAYS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_RETAIN_DAYS


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def image_retain_days() -> float:
    """How long to keep an archived picture. 0 keeps them for good."""
    raw = os.getenv("R2_IMAGE_RETAIN_DAYS",
                    str(DEFAULT_IMAGE_RETAIN_DAYS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_IMAGE_RETAIN_DAYS


def is_image(key: str) -> bool:
    return key.lower().endswith(IMAGE_SUFFIXES)


def _list_objects(target: Target) -> list[tuple[str, int, str]]:
    """Every object as (key, bytes, last-modified)."""
    import xml.etree.ElementTree as ET

    out: list[tuple[str, int, str]] = []
    token = None
    while True:
        query = {"list-type": "2", "max-keys": "1000"}
        if token:
            query["continuation-token"] = token
        url, headers = signed_get(target, "", query=query)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise StageError(f"could not list the bucket: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise _unreachable(target, exc.reason) from exc

        root = ET.fromstring(body)
        token = None
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "Contents":
                fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "")
                          for child in element}
                out.append((fields.get("Key", ""),
                            int(fields.get("Size") or 0),
                            fields.get("LastModified", "")))
            elif tag == "NextContinuationToken" and element.text:
                token = element.text
        if not token:
            return out


def signed_delete(target: Target, key: str,
                  now: dt.datetime | None = None
                  ) -> tuple[str, dict[str, str]]:
    """A signed DELETE for one object."""
    now = now or dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(target.endpoint).netloc
    empty = hashlib.sha256(b"").hexdigest()
    path = f"/{target.bucket}/{urllib.parse.quote(key)}"

    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{empty}\n"
                         f"x-amz-date:{amz_date}\n")
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["DELETE", path, "", canonical_headers,
                            signed, empty])
    scope = f"{stamp}/{target.region}/{SERVICE}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                          hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(_signing_key(target.secret, stamp, target.region),
                         to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{target.endpoint}{path}", {
        "x-amz-content-sha256": empty,
        "x-amz-date": amz_date,
        "Authorization": (f"{ALGORITHM} Credential={target.key_id}/{scope}, "
                          f"SignedHeaders={signed}, Signature={signature}"),
    }


def _age_days(last_modified: str) -> float | None:
    if not last_modified:
        return None
    try:
        when = dt.datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.UTC) - when).total_seconds() / 86400


def read_object(key: str, target: Target | None = None) -> bytes:
    """Read one archived object into memory."""
    target = target or Target()
    url, headers = signed_get(target, key)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            return resp.read(MAX_RECORD_BYTES)
    except urllib.error.HTTPError as exc:
        raise StageError(f"{key} could not be read: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise StageError(f"{key} could not be read: {exc.reason}") from exc


def was_posted(record: dict) -> bool:
    """Whether this episode has actually gone out somewhere.

    Either platform counts. Once it is public anywhere, the archive has
    done the job it existed for; before that, the archived video may be
    the only copy of work nobody has judged yet.
    """
    if record.get("published_at"):
        return True
    for row in record.get("platform_posts") or []:
        if isinstance(row, dict) and row.get("status") == "posted":
            return True
    return False


def episode_was_posted(episode_id: str, target: Target,
                       seen: dict[str, bool] | None = None) -> bool:
    """Ask the archived record whether this episode was published.

    Unknown means no. A record that is missing, unreadable or not yet
    uploaded is not evidence that the video is safe to delete, and the
    whole point of the rule is that an unpublished video is the only
    copy of something.
    """
    if seen is not None and episode_id in seen:
        return seen[episode_id]
    answer = False
    try:
        raw = read_object(record_key(episode_id, "manifest.json"), target)
        answer = was_posted(json.loads(raw.decode("utf-8")))
    except (StageError, ValueError, UnicodeDecodeError):
        answer = False
    if seen is not None:
        seen[episode_id] = answer
    return answer


def expire(target: Target | None = None, dry_run: bool = False,
           days: float | None = None) -> dict:
    """Delete archived objects that have gone out and are past their age.

    Age alone is not enough. It was, while the archive was a third copy
    -- the local file and the published video both predated it. It stops
    being enough the moment this is the backup, because then an
    unpublished video past its retention is work nobody has judged being
    deleted on a timer.

    So a video is removed only once the episode has actually been
    posted. Everything else is held, and said so, because a retention
    setting that quietly keeps things is one you would rather know
    about.
    """
    log = get(None, "archive")
    target = target or Target()
    if not target.configured:
        return {"enabled": False, "note": "archive storage is not configured"}
    keep = retain_days() if days is None else days
    # Pictures keep their own, shorter clock, unless `days` was given --
    # an explicit age on the command line means that age for everything.
    keep_images = image_retain_days() if days is None else days
    if not keep and not keep_images:
        return {"enabled": False,
                "note": "R2_RETAIN_DAYS and R2_IMAGE_RETAIN_DAYS are both 0; "
                        "nothing expires"}

    removed, freed, kept = [], 0, 0
    held: list[dict] = []
    posted: dict[str, bool] = {}
    for key, size, modified in _list_objects(target):
        if is_permanent(key):
            # The backup does not expire. Everything under these
            # prefixes is the only copy of something.
            kept += 1
            continue
        limit = keep_images if is_image(key) else keep
        age = _age_days(modified)
        if not limit or age is None or age < limit:
            kept += 1
            continue
        parts = key.split("/", 2)
        if parts[0] == "episodes" and len(parts) == 3:
            if not episode_was_posted(parts[1], target, posted):
                kept += 1
                held.append({"key": key, "mb": round(size / 1e6, 2),
                             "age_days": round(age, 1),
                             "why": "not posted yet"})
                continue
        if not dry_run:
            url, headers = signed_delete(target, key)
            request = urllib.request.Request(url, headers=headers,
                                             method="DELETE")
            try:
                urllib.request.urlopen(request, timeout=TIMEOUT_SEC)
            except urllib.error.HTTPError as exc:
                log.warning(f"could not delete {key}: {exc.code}")
                continue
            except urllib.error.URLError as exc:
                log.warning(f"could not delete {key}: {exc.reason}")
                continue
        removed.append({"key": key, "mb": round(size / 1e6, 2),
                        "age_days": round(age, 1)})
        freed += size

    _USAGE_CACHE.clear()
    if removed and not dry_run:
        log.info(f"expired {len(removed)} objects, freeing "
                 f"{freed / 1e6:.0f} MB")
    if held:
        log.info(f"{len(held)} objects held: not posted yet")
    return {
        "enabled": True, "dry_run": dry_run, "retain_days": keep,
        "image_retain_days": keep_images,
        "removed": removed, "kept": kept, "held": held,
        "freed_mb": round(freed / 1e6, 1),
    }


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, stamp: str, region: str) -> bytes:
    key = _sign(f"AWS4{secret}".encode(), stamp)
    key = _sign(key, region)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def signed_headers(target: Target, key: str, payload: bytes,
                   content_type: str, now: dt.datetime | None = None
                   ) -> dict[str, str]:
    """SigV4 headers for a single PUT.

    Separated from the request so it can be tested without a network:
    a signature that is wrong is wrong deterministically.
    """
    now = now or dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(target.endpoint).netloc
    digest = hashlib.sha256(payload).hexdigest()
    path = f"/{target.bucket}/{urllib.parse.quote(key)}"

    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{digest}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(
        ["PUT", path, "", canonical_headers, signed, digest])

    scope = f"{stamp}/{target.region}/{SERVICE}/aws4_request"
    to_sign = "\n".join([
        ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(target.secret, stamp, target.region),
        to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "Content-Type": content_type,
        "x-amz-content-sha256": digest,
        "x-amz-date": amz_date,
        "Authorization": (
            f"{ALGORITHM} Credential={target.key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }


def signed_get(target: Target, key: str,
               now: dt.datetime | None = None,
               query: dict[str, str] | None = None
               ) -> tuple[str, dict[str, str]]:
    """A signed GET for a private object.

    Lets the bucket stay private. A voice reference served from a public
    URL is a voice anyone can clone, so the credentials already present
    for archiving are reused to read it instead.
    """
    now = now or dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(target.endpoint).netloc
    empty = hashlib.sha256(b"").hexdigest()
    path = f"/{target.bucket}/{urllib.parse.quote(key)}" if key else \
        f"/{target.bucket}"
    # Query parameters are signed, and must be in sorted order.
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted((query or {}).items()))

    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{empty}\n"
                         f"x-amz-date:{amz_date}\n")
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(
        ["GET", path, canonical_query, canonical_headers, signed, empty])

    scope = f"{stamp}/{target.region}/{SERVICE}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                          hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(_signing_key(target.secret, stamp, target.region),
                         to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "x-amz-content-sha256": empty,
        "x-amz-date": amz_date,
        "Authorization": (f"{ALGORITHM} Credential={target.key_id}/{scope}, "
                          f"SignedHeaders={signed}, Signature={signature}"),
    }
    suffix = f"?{canonical_query}" if canonical_query else ""
    return f"{target.endpoint}{path}{suffix}", headers


# A manifest is a few kilobytes; anything far larger is not one.
MAX_RECORD_BYTES = 4 * 1024 * 1024

# Prefixes that expiry never touches.
#
# The rest of the bucket is a third copy: the local file and the
# published video both predate it, so deleting by age loses nothing.
# These are not. They are the backup -- the only copy if the laptop
# goes -- and expiring them would mean the safety net deleting itself
# on a schedule.
# `library/` was here too, until the owner set a one-day clock on
# pictures. The record stays: it is a few kilobytes, it is the only
# account of what an episode was, and a video can be re-rendered from
# it while it cannot be recovered from a video.
PERMANENT = ("record/",)

# Small, and the whole account of what an episode is. A video can be
# re-rendered from these; they cannot be recovered from a video.
RECORD_FILES = ("manifest.json", "script.md", "metadata.json",
                "cues.json", "captions.srt")

CONTENT_TYPES = {
    ".json": "application/json", ".md": "text/markdown",
    ".srt": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
    ".mp4": "video/mp4", ".wav": "audio/wav",
}


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def is_permanent(key: str) -> bool:
    return key.startswith(PERMANENT)


def key_for(episode_id: str, name: str) -> str:
    """Where an episode's file lives in the bucket."""
    return f"episodes/{episode_id}/{name}"


def record_key(episode_id: str, name: str) -> str:
    """Where an episode's record lives. Never expired."""
    return f"record/{episode_id}/{name}"


def library_key(name: str) -> str:
    """Where a reusable picture lives. Never expired."""
    return f"library/{name}"


def put(path: Path, key: str, target: Target | None = None,
        content_type: str = "video/mp4") -> str:
    """Upload one file. Returns its URL. Raises on failure."""
    target = target or Target()
    if not target.configured:
        raise ConfigError(
            "archive storage is not configured: "
            + ", ".join(target.missing()) + " not set"
        )
    if not path.exists():
        raise StageError(f"nothing to archive at {path}")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise StageError(
            f"{path.name} is {size / 1e9:.1f} GB, over the single-request "
            "limit; multipart upload is not implemented"
        )

    # The cap is checked before the bytes go, not after. R2 bills past
    # 10 GB without asking, and a storage bill is a poor way to learn
    # that an archive grew.
    plan = budget(size, target)
    if plan["capped"] and not plan["allowed"]:
        raise StageError(
            f"refusing to archive {path.name}: the bucket holds "
            f"{plan['used'] / 1e9:.2f} GB of a {plan['limit'] / 1e9:.2f} GB "
            f"cap and this would pass it. Free space with "
            f"`run.py archive --prune`, raise R2_STORAGE_CAP_GB, or set it "
            f"to 0 to stop checking. The video is still on disk."
        )

    payload = path.read_bytes()
    headers = signed_headers(target, key, payload, content_type)
    url = f"{target.endpoint}/{target.bucket}/{urllib.parse.quote(key)}"
    request = urllib.request.Request(url, data=payload, method="PUT",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            if resp.status not in (200, 201):
                raise StageError(f"storage returned {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise StageError(
            f"could not archive {path.name}: {exc.code} {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise StageError(
            f"could not reach the archive storage: {exc.reason}") from exc
    return target.url_for(key)


def archive_episode(ep, m, log=None) -> str | None:
    """Store a finished episode, if storage is configured.

    Returns the URL, or None when there is nothing to do. Never raises:
    archiving is insurance, and insurance that fails the render it was
    protecting is worse than none.
    """
    log = log or get(m.id, "archive")
    target = Target()
    if not target.configured:
        log.debug("archive storage not configured; skipping")
        return None
    if not ep.video.exists():
        log.debug("no video to archive")
        return None
    try:
        url = put(ep.video, key_for(m.id, "video.mp4"), target)
    except (ConfigError, StageError) as exc:
        log.warning(f"could not archive: {exc}")
        # A cap refusal is worth a mail: it means archiving has silently
        # stopped, and the operator would otherwise find out when they
        # went looking for a file that was never stored.
        if "cap" in str(exc):
            try:
                from . import notify
                notify.failure(m.id, "archive", exc)
            except Exception:
                pass
        return None

    # Warn while there is still room to act.
    try:
        plan = budget(0, target)
        if plan["capped"] and plan["percent"] >= 80:
            log.warning(
                f"the archive bucket is {plan['percent']:.0f}% full "
                f"({plan['used'] / 1e9:.2f} of {plan['limit'] / 1e9:.2f} GB)")
    except (ConfigError, StageError):
        pass
    m.stage("assemble").data["archive_url"] = url
    # The video is the big thing; the record is the important one.
    archive_record(ep, m, target, log)
    log.info(f"archived to {url}")
    return url


def archive_record(ep, m, target: Target | None = None, log=None) -> list[str]:
    """Store the account of what this episode is.

    A few kilobytes against tens of megabytes of video, and worth more:
    a video can be re-rendered from the manifest and the script, and
    neither can be recovered from a video. Never expired.
    """
    log = log or get(m.id, "archive")
    target = target or Target()
    directory = getattr(ep, "dir", None)
    if not target.configured or directory is None:
        # Archiving is insurance, and insurance that fails the render it
        # was protecting is worse than none. That applies to a caller
        # that hands over something which is not quite an episode too.
        return []
    stored = []
    for name in RECORD_FILES:
        path = directory / name
        if not path.exists():
            continue
        try:
            put(path, record_key(m.id, name), target,
                content_type_for(path))
            stored.append(name)
        except (ConfigError, StageError) as exc:
            log.warning(f"could not store {name}: {exc}")
    if stored:
        log.info(f"record stored: {', '.join(stored)}")
    return stored


def archive_library(target: Target | None = None, log=None) -> dict:
    """Store the reusable pictures, skipping what is already there.

    Each picture is named after the prompt that drew it, so the same
    picture never uploads twice. Losing the library costs nothing but
    time -- and on a free generator that half-refuses, time is the
    expensive part.
    """
    from . import image_library

    log = log or get(None, "archive")
    target = target or Target()
    if not target.configured:
        return {"enabled": False, "note": "archive storage is not configured"}

    have = {key for key, _size, _mod in _list_objects(target)
            if key.startswith("library/")}
    entries = image_library.load()
    sent, skipped = [], 0
    for entry in entries:
        key = library_key(entry.file)
        if key in have:
            skipped += 1
            continue
        try:
            put(entry.path, key, target, "image/png")
            sent.append(entry.file)
        except (ConfigError, StageError) as exc:
            log.warning(f"could not store {entry.file}: {exc}")
    index = image_library.index_path()
    if index.exists():
        try:
            put(index, library_key("index.json"), target, "application/json")
        except (ConfigError, StageError) as exc:
            log.warning(f"could not store the library index: {exc}")
    log.info(f"library: {len(sent)} stored, {skipped} already there")
    return {"enabled": True, "stored": sent, "skipped": skipped,
            "pictures": len(entries)}


def fetch(key: str, out: Path, target: Target | None = None) -> Path:
    """Download one archived object to a local path."""
    target = target or Target()
    url, headers = signed_get(target, key)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("wb") as handle:
                shutil.copyfileobj(resp, handle, 1 << 20)
    except urllib.error.HTTPError as exc:
        raise StageError(f"{key} could not be fetched: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise StageError(f"{key} could not be fetched: {exc.reason}") from exc
    return out


def archived_episodes(target: Target | None = None) -> list[dict]:
    """Every episode the bucket holds, newest first.

    Keyed on the record rather than the video, because the record is
    what never expires: an episode whose video has aged out is still
    restorable, and still worth showing.
    """
    target = target or Target()
    if not target.configured:
        return []
    found: dict[str, dict] = {}
    for key, size, modified in _list_objects(target):
        parts = key.split("/", 2)
        if len(parts) < 3 or parts[0] not in ("record", "episodes"):
            continue
        episode_id = parts[1]
        row = found.setdefault(episode_id, {
            "id": episode_id, "bytes": 0, "video": False,
            "files": [], "modified": modified,
        })
        row["bytes"] += size
        row["files"].append(parts[2])
        if parts[2] == "video.mp4":
            row["video"] = True
        if modified > row["modified"]:
            row["modified"] = modified
    return sorted(found.values(), key=lambda r: r["modified"], reverse=True)


def restore_episode(episode_id: str, into: Path,
                    target: Target | None = None, log=None) -> list[str]:
    """Bring one episode back from the bucket.

    The record first, because it is what makes the directory an episode
    rather than a loose video.
    """
    log = log or get(episode_id, "archive")
    target = target or Target()
    if not target.configured:
        raise ConfigError("archive storage is not configured: "
                          + ", ".join(target.missing()))
    keys = [key for key, _size, _mod in _list_objects(target)
            if key.startswith((f"record/{episode_id}/",
                               f"episodes/{episode_id}/"))]
    if not keys:
        raise StageError(f"nothing archived for {episode_id!r}")
    landed = []
    for key in sorted(keys):
        name = key.split("/", 2)[-1]
        out = into / episode_id / ("out/video.mp4" if name == "video.mp4"
                                   else name)
        fetch(key, out, target)
        landed.append(name)
    log.info(f"restored {len(landed)} files for {episode_id}")
    return landed


def restore_library(target: Target | None = None, log=None) -> int:
    """Bring the reusable pictures back."""
    from . import image_library

    log = log or get(None, "archive")
    target = target or Target()
    if not target.configured:
        raise ConfigError("archive storage is not configured: "
                          + ", ".join(target.missing()))
    directory = image_library.library_dir()
    count = 0
    for key, _size, _mod in _list_objects(target):
        if not key.startswith("library/"):
            continue
        fetch(key, directory / key.split("/", 1)[1], target)
        count += 1
    log.info(f"restored {count} library files")
    return count


def test_target() -> tuple[bool, str]:
    """Prove the credentials work, before a render depends on them."""
    import tempfile
    target = Target()
    if not target.configured:
        return False, "not configured: " + ", ".join(target.missing())
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
        handle.write(b"net-of-fees archive check\n")
        probe = Path(handle.name)
    try:
        url = put(probe, "_check/hello.txt", target, "text/plain")
    except (ConfigError, StageError) as exc:
        return False, str(exc)
    finally:
        probe.unlink(missing_ok=True)
    return True, f"wrote {url}"
