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
import os
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


class Target:
    """Where archives go, assembled from the environment."""

    def __init__(self) -> None:
        self.account = os.getenv("R2_ACCOUNT_ID")
        self.bucket = os.getenv("R2_BUCKET")
        self.key_id = os.getenv("R2_ACCESS_KEY_ID")
        self.secret = os.getenv("R2_SECRET_ACCESS_KEY")
        self.region = os.getenv("R2_REGION", DEFAULT_REGION)
        # Set when the bucket is served on a domain of your own.
        self.public_base = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
        # Any S3-compatible endpoint; R2's is derived from the account.
        self.endpoint = os.getenv("R2_ENDPOINT") or (
            f"https://{self.account}.r2.cloudflarestorage.com"
            if self.account else "")

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
               now: dt.datetime | None = None) -> tuple[str, dict[str, str]]:
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
    path = f"/{target.bucket}/{urllib.parse.quote(key)}"

    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{empty}\n"
                         f"x-amz-date:{amz_date}\n")
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["GET", path, "", canonical_headers, signed, empty])

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
    return f"{target.endpoint}{path}", headers


def key_for(episode_id: str, name: str) -> str:
    """Where an episode's file lives in the bucket."""
    return f"episodes/{episode_id}/{name}"


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
        return None
    m.stage("assemble").data["archive_url"] = url
    log.info(f"archived to {url}")
    return url


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
