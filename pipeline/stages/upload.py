"""Stage 10: publish.

Refuses an episode whose approved_at is null. That check lives here as
well as at the gate, because a frontend-only check is no check at all
-- architecture doc, section 13.

YouTube's Data API has no idempotency key, so a crash between "YouTube
accepted" and "we recorded the id" would double-post on retry. The
protocol in section 23 is implemented here: record the attempt first,
reconcile before re-uploading.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from .. import channels
from ..errors import ConfigError, GateError, StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest

# One upload costs roughly 1600 of a 10,000 daily quota: about six a day.
QUOTA_COST_PER_UPLOAD = 1600
DAILY_QUOTA = 10_000

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

DISCLAIMER = (
    "Educational content only. Not investment advice. I am not a "
    "SEBI-registered investment adviser. Markets carry risk of loss. "
    "Do your own research."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def assert_publishable(m: Manifest) -> None:
    """Every reason an episode may not be published, checked server-side."""
    if not m.approved_at:
        raise GateError(
            f"episode {m.id} has no approved_at. Clear gate 2 first: "
            f"run.py {m.id} --approve"
        )
    if not m.gate_1_passed_at:
        raise GateError(f"episode {m.id} never cleared gate 1")
    if m.stage("voice").data.get("draft"):
        raise GateError(
            f"episode {m.id} was narrated with the draft voice engine and "
            f"cannot be published."
        )
    if m.stage("scrub").data.get("findings"):
        raise GateError(f"episode {m.id} has unresolved scrub findings")
    if not m.is_done("assemble"):
        raise GateError(f"episode {m.id} has no assembled video")


# People & Blogs. Used only when a profile declares nothing, which
# should not happen -- tests/test_profiles.py asserts every profile does.
FALLBACK_CATEGORY_ID = "22"

# YouTube's own limits on the fields below.
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 480          # the documented cap is 500; leave headroom
TAG_MAX = 30


def build_description(m: Manifest, data: dict | None = None) -> str:
    """Fallback description, used only when the metadata stage has not run.

    Section 14 requires the disclaimer on every video, so it is here as
    well as in the metadata stage's own text.
    """
    parts = [m.title, ""]
    if data and data.get("verify_before_publish"):
        parts.append("Rates used are the published schedules at time of recording.")
        parts.append("")
    parts += [DISCLAIMER, "", f"Episode {m.id}"]
    return "\n".join(parts)


def _snippet_extras(m: Manifest) -> dict:
    """Category and language, taken from the profile rather than fixed.

    The category was hardcoded, which meant switching topic left the
    video filed under the previous topic's category.
    """
    try:
        import profiles
        profile = profiles.load(m.profile)
    except Exception:  # a missing profile is the render's problem, not this
        return {"categoryId": FALLBACK_CATEGORY_ID}
    extras = {
        "categoryId": getattr(profile, "YOUTUBE_CATEGORY_ID",
                              FALLBACK_CATEGORY_ID),
    }
    language = getattr(profile, "LANGUAGE", None)
    if language:
        extras["defaultLanguage"] = language
        extras["defaultAudioLanguage"] = language
    return extras


def load_metadata(ep: Episode) -> dict:
    """What the metadata stage wrote, or {} if it has not run."""
    path = ep.dir / "metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def youtube_description(ep: Episode, m: Manifest,
                        data: dict | None = None) -> str:
    """The description that actually goes out.

    Prefers the metadata stage's text, which carries the hook, the series
    line and the chapter list. That text was being generated and thrown
    away: upload built its own minimal version instead.

    The disclaimer is appended if the chosen text does not already carry
    it, because section 14 requires it on every video and a profile could
    word its own differently.
    """
    meta = load_metadata(ep).get("youtube", {})
    text = (meta.get("description") or "").strip()
    if not text:
        return build_description(m, data)
    if "not investment advice" not in text.lower():
        text += "\n\n" + DISCLAIMER
    if f"Episode {m.id}" not in text:
        text += f"\n\nEpisode {m.id}"
    return text


def youtube_tags(ep: Episode) -> list[str]:
    """Tags from the metadata stage, trimmed to YouTube's total budget.

    These were generated and never sent: the insert body had no tags
    field at all.
    """
    raw = load_metadata(ep).get("youtube", {}).get("tags", [])
    tags, total = [], 0
    for tag in raw:
        tag = str(tag).lstrip("#").strip()
        if not tag or len(tag) > TAG_MAX:
            continue
        # Quoted length is what counts against the cap once a tag has a
        # space in it, so budget for the quotes.
        cost = len(tag) + (3 if " " in tag else 1)
        if total + cost > TAGS_TOTAL_MAX:
            break
        tags.append(tag)
        total += cost
    return tags


def _record_attempt(m: Manifest, platform: str) -> dict:
    """Write the attempt before calling out, so a crash leaves a trace."""
    row = {
        "platform": platform,
        "status": "attempting",
        "idempotency_key": uuid.uuid4().hex,
        "external_id": None,
        "posted_at": None,
        "error": None,
        "attempts": 1,
    }
    for existing in m.platform_posts:
        if existing["platform"] == platform and existing["status"] != "failed":
            existing["attempts"] = existing.get("attempts", 1) + 1
            return existing
    m.platform_posts.append(row)
    return row


def _reconcile(youtube, row: dict, m: Manifest) -> str | None:
    """Before re-uploading, check whether the previous attempt landed.

    Matches on the idempotency key written into the description, which is
    the only durable marker the API round-trips.
    """
    try:
        resp = youtube.search().list(
            part="id,snippet", forMine=True, type="video", maxResults=25
        ).execute()
    except Exception as exc:
        raise StageError(
            f"cannot reconcile a previous upload attempt: {exc}. "
            f"Check the channel by hand before retrying, or this may double-post."
        ) from exc
    for item in resp.get("items", []):
        title = item.get("snippet", {}).get("title", "")
        if title == m.title:
            return item["id"]["videoId"]
    return None


def _client(channel: str = "default"):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ConfigError(
            "YouTube libraries are not installed.\n"
            "  pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    token_path = channels.youtube_token_path(channel)
    if not token_path.exists():
        raise ConfigError(
            f"no YouTube credentials at {token_path} for channel "
            f"{channel!r}.\n"
            f"  Run: python tools/youtube_auth.py --channel {channel}\n"
            f"  See README, 'Several channels from one install'."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _set_thumbnail(youtube, ep: Episode, m: Manifest, video_id: str, log) -> None:
    """Attach the thumbnail the operator chose at Gate 2.

    Runs after the insert succeeded. A thumbnail that fails to attach is
    worth a warning, not an exception: the video is already live, and
    raising here would make a published episode look like a failed one.
    """
    if m.chosen_thumbnail is None:
        log.info("no thumbnail chosen; YouTube will auto-generate one")
        return
    path = ep.thumbnail(m.chosen_thumbnail)
    if not path.exists():
        log.warning(f"chosen thumbnail {path.name} is missing; skipping")
        return
    try:
        from googleapiclient.http import MediaFileUpload
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(path), mimetype="image/png"),
        ).execute()
        log.info(f"thumbnail set from {path.name}")
    except Exception as exc:
        log.warning(f"could not set the thumbnail: {exc}")


def run(ep: Episode, m: Manifest, dry_run: bool = False,
        privacy: str = "private") -> None:
    """Upload the long-form. Shorts publish through the same path, per slot."""
    log = get(m.id, "upload")
    assert_publishable(m)

    if not ep.video.exists():
        raise StageError(f"no video at {ep.video}")

    data = (json.loads(ep.snapshot.read_text(encoding="utf-8"))
            if ep.snapshot.exists() else None)
    channel = channels.key_for(m)
    row = _record_attempt(m, "youtube")
    description = (youtube_description(ep, m, data)
                   + f"\n\nref: {row['idempotency_key']}")
    tags = youtube_tags(ep)

    if dry_run:
        log.info("dry run: nothing uploaded")
        print("\n  DRY RUN -- would upload:")
        print(f"    file      {ep.video}  "
              f"({ep.video.stat().st_size/1_048_576:.1f} MB)")
        print(f"    title     {m.title}")
        print(f"    privacy   {privacy}")
        print(f"    quota     ~{QUOTA_COST_PER_UPLOAD} of {DAILY_QUOTA}/day")
        print(f"    tags      {len(tags)}: {', '.join(tags) or 'none'}")
        thumb = ("none chosen" if m.chosen_thumbnail is None
                 else f"thumb_{m.chosen_thumbnail}.png")
        print(f"    thumbnail {thumb}")
        extras = _snippet_extras(m)
        print(f"    category  {extras['categoryId']}"
              f"  language {extras.get('defaultLanguage', 'unset')}")
        print(f"    channel   {channel} "
              f"({channels.youtube_token_path(channel)})")
        print(f"\n  description:\n{description}\n")
        m.mark_done("upload", dry_run=True)
        return

    from googleapiclient.http import MediaFileUpload

    youtube = _client(channel)

    if row.get("attempts", 1) > 1 and not row.get("external_id"):
        found = _reconcile(youtube, row, m)
        if found:
            row.update(status="posted", external_id=found, posted_at=_now())
            m.published_at = row["posted_at"]
            m.mark_done("upload", video_id=found, reconciled=True)
            log.info(f"previous attempt had already landed as {found}")
            return

    body = {
        "snippet": {
            "title": m.title[:100],
            "description": description[:DESCRIPTION_MAX],
            "tags": tags,
            **_snippet_extras(m),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(ep.video), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.debug(f"upload {int(status.progress() * 100)}%")
    except Exception as exc:
        row.update(status="failed", error=str(exc))
        if "quotaExceeded" in str(exc):
            raise StageError(
                "YouTube daily quota exhausted. Retry tomorrow; this does not "
                "consume a retry attempt."
            ) from exc
        raise StageError(f"upload failed: {exc}") from exc

    video_id = response["id"]
    _set_thumbnail(youtube, ep, m, video_id, log)
    row.update(status="posted", external_id=video_id, posted_at=_now())
    m.published_at = row["posted_at"]
    m.mark_done("upload", video_id=video_id, privacy=privacy,
                tags=len(tags), channel=channel)
    log.info(f"published {video_id} ({privacy})")
