"""Stage 10b: publish the vertical cut to Instagram Reels.

`metadata.py` has been writing an Instagram caption since it was built,
and until now nothing read it. This stage consumes it.

Reels take the 9:16 cut, not the long-form, so this publishes a file
from `out/shorts/` rather than `out/video.mp4`.

Two upload paths exist in the Graph API. The resumable one sends the
file straight from disk, which is the default here because the
alternative -- `video_url` -- requires the episode to be sitting on a
public HTTPS host first, and standing up a bucket to publish a 12 MB
file is a poor trade. Set `IG_VIDEO_URL` to use that path instead.

Instagram has no idempotency key either, so this follows the same
protocol as the YouTube stage (architecture doc, section 23): record the
attempt first, and reconcile against recent media before re-uploading.
The marker is a `ref:` line in the caption, the only field that
round-trips.

Needs, one time: a Business or Creator account linked to a Facebook
Page, a Meta app with `instagram_content_publish`, and a long-lived
token. README, 'Instagram setup'.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import platform_support
from ..errors import ConfigError, StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from .upload import assert_publishable

DEFAULT_API_HOST = "https://graph.facebook.com"
DEFAULT_UPLOAD_HOST = "https://rupload.facebook.com/ig-api-upload"
DEFAULT_VERSION = "v21.0"


def _api_host() -> str:
    """Overridable so the protocol can be exercised against a local
    server, and because Graph has regional endpoints."""
    return os.getenv("IG_API_HOST", DEFAULT_API_HOST).rstrip("/")


def _upload_host() -> str:
    return os.getenv("IG_UPLOAD_HOST", DEFAULT_UPLOAD_HOST).rstrip("/")

# Instagram's own limits, not ours.
CAPTION_MAX = 2200
HASHTAG_MAX = 30
DAILY_POST_LIMIT = 50

# Instagram's published Reels requirements. Checked locally, because the
# alternative is learning about a bad aspect ratio after a full upload
# and a minute of transcoding, from a status code that says only "ERROR".
VIDEO_CODECS = ("h264", "hevc")
AUDIO_CODECS = ("aac",)
MIN_DURATION_SEC = 3.0
MAX_DURATION_SEC = 900.0
MAX_FILE_BYTES = 1_000_000_000
TARGET_ASPECT = 9 / 16
ASPECT_TOLERANCE = 0.01

# Transcoding a 60s reel is usually under a minute; the ceiling is a
# guard against polling a container that will never finish.
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 300
HTTP_TIMEOUT_SEC = 60


def _version() -> str:
    return os.getenv("IG_API_VERSION", DEFAULT_VERSION)


def _credentials() -> tuple[str, str]:
    user_id = os.getenv("IG_USER_ID")
    token = os.getenv("IG_ACCESS_TOKEN")
    missing = [n for n, v in (("IG_USER_ID", user_id),
                              ("IG_ACCESS_TOKEN", token)) if not v]
    if missing:
        raise ConfigError(
            f"Instagram is not configured: {', '.join(missing)} not set.\n"
            "  See README, 'Instagram setup'. Skip this stage with "
            "--no-instagram."
        )
    return user_id, token


def _request(url: str, *, data: bytes | None = None,
             headers: dict[str, str] | None = None,
             method: str | None = None) -> dict:
    """One Graph call. Turns an API error body into a readable message."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(detail).get("error", {})
            detail = f"{err.get('type', 'error')}: {err.get('message', detail)}"
        except ValueError:
            pass
        raise StageError(f"Instagram API returned {exc.code}. {detail}") from exc
    except urllib.error.URLError as exc:
        raise StageError(f"cannot reach the Instagram API: {exc.reason}") from exc
    return json.loads(body) if body else {}


def _post(path: str, params: dict[str, str]) -> dict:
    url = f"{_api_host()}/{_version()}/{path}"
    return _request(url, data=urllib.parse.urlencode(params).encode(),
                    method="POST")


def _get(path: str, params: dict[str, str]) -> dict:
    url = f"{_api_host()}/{_version()}/{path}?{urllib.parse.urlencode(params)}"
    return _request(url, method="GET")


def caption_for(ep: Episode, m: Manifest, ref: str) -> str:
    """Read what metadata.py wrote, and append the reconciliation marker."""
    path = ep.dir / "metadata.json"
    if not path.exists():
        raise StageError(
            f"no metadata at {path}; run the metadata stage first"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    caption = payload.get("instagram", {}).get("caption", "").strip()
    if not caption:
        raise StageError("metadata.json has no instagram caption")

    tags = caption.count("#")
    if tags > HASHTAG_MAX:
        raise StageError(
            f"caption carries {tags} hashtags; Instagram rejects more than "
            f"{HASHTAG_MAX}. Trim the profile's HASHTAGS."
        )

    marker = f"\n\nref: {ref}"
    room = CAPTION_MAX - len(marker)
    if len(caption) > room:
        # Truncate the caption, never the marker: without it a retry
        # cannot tell whether the first attempt landed.
        caption = caption[:room].rstrip()
    return caption + marker


def reel_path(ep: Episode, which: int = 1):
    """The vertical cut to publish. Reels are 9:16; the long-form is not."""
    path = ep.shorts_dir / f"short_{which:02d}.mp4"
    if not path.exists():
        raise StageError(
            f"no vertical cut at {path}; run the shorts stage first"
        )
    return path


def preflight(path) -> dict:
    """Check the file against Instagram's requirements before sending it.

    Returns what ffprobe found, so the caller can log it. Raises with the
    specific violation rather than letting Instagram answer "ERROR" after
    the upload has already cost a minute.
    """
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise StageError(
            f"{path.name} is {size/1e9:.2f} GB; Instagram's limit is 1 GB"
        )

    proc = subprocess.run(
        [platform_support.ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise StageError(f"ffprobe could not read {path.name}: {proc.stderr.strip()}")
    probed = json.loads(proc.stdout)

    streams = probed.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise StageError(f"{path.name} has no video stream")
    if video.get("codec_name") not in VIDEO_CODECS:
        raise StageError(
            f"{path.name} video is {video.get('codec_name')}; Instagram "
            f"accepts {', '.join(VIDEO_CODECS)}"
        )
    # Reels are a sound-first format; a silent one is almost always a
    # render that lost its audio track.
    if audio is None:
        raise StageError(f"{path.name} has no audio stream")
    if audio.get("codec_name") not in AUDIO_CODECS:
        raise StageError(
            f"{path.name} audio is {audio.get('codec_name')}; Instagram "
            f"accepts {', '.join(AUDIO_CODECS)}"
        )

    width, height = int(video["width"]), int(video["height"])
    aspect = width / height
    if abs(aspect - TARGET_ASPECT) > ASPECT_TOLERANCE:
        raise StageError(
            f"{path.name} is {width}x{height} (aspect {aspect:.3f}); Reels "
            f"want 9:16 ({TARGET_ASPECT:.3f}). Render it from a vertical "
            "profile."
        )

    duration = float(probed.get("format", {}).get("duration", 0.0))
    if duration < MIN_DURATION_SEC:
        raise StageError(
            f"{path.name} is {duration:.1f}s; Instagram rejects anything "
            f"under {MIN_DURATION_SEC:.0f}s"
        )
    if duration > MAX_DURATION_SEC:
        raise StageError(
            f"{path.name} is {duration:.0f}s; Instagram's Reels limit is "
            f"{MAX_DURATION_SEC:.0f}s"
        )

    return {
        "width": width, "height": height,
        "duration_sec": round(duration, 3),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "size_mb": round(size / 1_048_576, 2),
    }


def _reconcile(user_id: str, token: str, ref: str) -> str | None:
    """Did a previous attempt already land? Match on the caption marker."""
    try:
        resp = _get(f"{user_id}/media",
                    {"fields": "id,caption", "limit": "25",
                     "access_token": token})
    except StageError as exc:
        raise StageError(
            f"cannot confirm whether a previous attempt landed: {exc} "
            "Check the account by hand before retrying, or this may "
            "double-post."
        ) from exc
    for item in resp.get("data", []):
        if ref in (item.get("caption") or ""):
            return item["id"]
    return None


def _create_container(user_id: str, token: str, caption: str,
                      video_url: str | None) -> tuple[str, str | None]:
    """Returns the container id, and an upload URI when resumable."""
    params = {"media_type": "REELS", "caption": caption, "access_token": token}
    if video_url:
        params["video_url"] = video_url
    else:
        params["upload_type"] = "resumable"
    resp = _post(f"{user_id}/media", params)
    container = resp.get("id")
    if not container:
        raise StageError(f"Instagram did not return a container id: {resp}")
    if video_url:
        return container, None
    return container, resp.get("uri") or f"{_upload_host()}/{_version()}/{container}"


def _upload_bytes(uri: str, token: str, path, log) -> None:
    """Send the file to the upload host in one request."""
    size = path.stat().st_size
    log.info(f"uploading {size/1_048_576:.1f} MB to Instagram")
    _request(
        uri,
        data=path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        },
    )


def _await_container(container: str, token: str, log) -> None:
    """Block until Instagram finishes transcoding, or explain why it won't."""
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    last = "unknown"
    while time.monotonic() < deadline:
        resp = _get(container, {"fields": "status_code,status",
                                "access_token": token})
        last = resp.get("status_code", "unknown")
        if last == "FINISHED":
            return
        if last in ("ERROR", "EXPIRED"):
            raise StageError(
                f"Instagram could not process the video (status {last}): "
                f"{resp.get('status', 'no detail')}. Reels want 9:16, "
                "under 90 seconds, H.264/AAC."
            )
        log.debug(f"container {container}: {last}")
        time.sleep(POLL_INTERVAL_SEC)
    raise StageError(
        f"Instagram did not finish processing within {POLL_TIMEOUT_SEC}s "
        f"(last status {last}). The container id is {container}; it stays "
        "valid for 24 hours, so a retry resumes rather than re-uploads."
    )


def run(ep: Episode, m: Manifest, dry_run: bool = False,
        which: int = 1) -> None:
    """Publish the vertical cut as a Reel.

    Gated exactly as YouTube is: `assert_publishable` is the same function,
    so a second platform cannot become a way around Gate 2.
    """
    log = get(m.id, "instagram")
    assert_publishable(m)

    path = reel_path(ep, which)

    row = None
    for existing in m.platform_posts:
        if existing["platform"] == "instagram" and existing["status"] != "failed":
            existing["attempts"] = existing.get("attempts", 1) + 1
            row = existing
            break
    if row is None:
        import uuid
        row = {
            "platform": "instagram",
            "status": "attempting",
            "idempotency_key": uuid.uuid4().hex,
            "external_id": None,
            "posted_at": None,
            "error": None,
            "attempts": 1,
        }
        m.platform_posts.append(row)

    ref = row["idempotency_key"]
    caption = caption_for(ep, m, ref)
    probed = preflight(path)

    if dry_run:
        log.info("dry run: nothing posted")
        print("\n  DRY RUN -- would post to Instagram:")
        print(f"    file     {path}  ({probed['size_mb']} MB)")
        print(f"    format   {probed['width']}x{probed['height']}, "
              f"{probed['duration_sec']}s, "
              f"{probed['video_codec']}/{probed['audio_codec']}")
        print(f"    limit    {DAILY_POST_LIMIT} posts per 24h")
        print(f"\n  caption:\n{caption}\n")
        m.mark_done("instagram", dry_run=True)
        return

    user_id, token = _credentials()
    video_url = os.getenv("IG_VIDEO_URL") or None

    if row.get("attempts", 1) > 1 and not row.get("external_id"):
        found = _reconcile(user_id, token, ref)
        if found:
            from datetime import datetime, timezone
            row.update(status="posted", external_id=found,
                       posted_at=datetime.now(timezone.utc).isoformat())
            m.mark_done("instagram", media_id=found, reconciled=True)
            log.info(f"previous attempt had already landed as {found}")
            return

    try:
        container, uri = _create_container(user_id, token, caption, video_url)
        if uri:
            _upload_bytes(uri, token, path, log)
        _await_container(container, token, log)
        resp = _post(f"{user_id}/media_publish",
                     {"creation_id": container, "access_token": token})
    except StageError as exc:
        row.update(status="failed", error=str(exc))
        raise

    media_id = resp.get("id")
    if not media_id:
        row.update(status="failed", error=f"no media id in {resp}")
        raise StageError(f"Instagram did not return a media id: {resp}")

    from datetime import datetime, timezone
    row.update(status="posted", external_id=media_id,
               posted_at=datetime.now(timezone.utc).isoformat())
    m.mark_done("instagram", media_id=media_id, file=path.name, **probed)
    log.info(f"posted reel {media_id}")
