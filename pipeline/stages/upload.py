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
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

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


def build_description(m: Manifest, data: dict | None = None) -> str:
    """Description with the disclaimer. Section 14 requires it on every video."""
    parts = [m.title, ""]
    if data and data.get("verify_before_publish"):
        parts.append("Rates used are the published schedules at time of recording.")
        parts.append("")
    parts += [DISCLAIMER, "", f"Episode {m.id}"]
    return "\n".join(parts)


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


def _client():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ConfigError(
            "YouTube libraries are not installed.\n"
            "  pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    token_path = Path(os.getenv("YOUTUBE_TOKEN_FILE", "secrets/youtube_token.json"))
    if not token_path.exists():
        raise ConfigError(
            f"no YouTube credentials at {token_path}.\n"
            f"  Run: python tools/youtube_auth.py\n"
            f"  See README, 'YouTube setup'."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def run(ep: Episode, m: Manifest, dry_run: bool = False,
        privacy: str = "private") -> None:
    """Upload the long-form. Shorts publish through the same path, per slot."""
    log = get(m.id, "upload")
    assert_publishable(m)

    if not ep.video.exists():
        raise StageError(f"no video at {ep.video}")

    data = (json.loads(ep.snapshot.read_text(encoding="utf-8"))
            if ep.snapshot.exists() else None)
    row = _record_attempt(m, "youtube")
    description = build_description(m, data) + f"\n\nref: {row['idempotency_key']}"

    if dry_run:
        log.info("dry run: nothing uploaded")
        print("\n  DRY RUN -- would upload:")
        print(f"    file      {ep.video}  "
              f"({ep.video.stat().st_size/1_048_576:.1f} MB)")
        print(f"    title     {m.title}")
        print(f"    privacy   {privacy}")
        print(f"    quota     ~{QUOTA_COST_PER_UPLOAD} of {DAILY_QUOTA}/day")
        print(f"\n  description:\n{description}\n")
        m.mark_done("upload", dry_run=True)
        return

    from googleapiclient.http import MediaFileUpload

    youtube = _client()

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
            "description": description[:5000],
            "categoryId": "22",
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
    row.update(status="posted", external_id=video_id, posted_at=_now())
    m.published_at = row["posted_at"]
    m.mark_done("upload", video_id=video_id, privacy=privacy)
    log.info(f"published {video_id} ({privacy})")
