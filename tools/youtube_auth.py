#!/usr/bin/env python3
"""One-time YouTube OAuth. Produces secrets/youtube_token.json.

Prerequisites, done once in the Google Cloud console:
  1. Create a project
  2. Enable "YouTube Data API v3"
  3. Create an OAuth client ID of type "Desktop app"
  4. Download it as secrets/client_secret.json

Note: until the Google Cloud project passes API audit, uploads are
force-locked to private whatever privacy status is requested. Apply for
the audit early -- it has the longest lead time of anything in setup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Playlists need a wider scope than uploading. youtube.upload can insert
# a video and nothing else; adding it to a playlist is a write against
# the channel, which needs the full youtube scope. That widening is
# visible on the OAuth consent screen and in the API audit, so it is
# opt-in: set YOUTUBE_PLAYLISTS=1 before authorising, and leave it off if
# you would rather request the narrower scope.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
PLAYLIST_SCOPE = "https://www.googleapis.com/auth/youtube"


def wanted_scopes() -> list[str]:
    import os
    if os.getenv("YOUTUBE_PLAYLISTS", "").strip() in ("1", "true", "yes"):
        return [UPLOAD_SCOPE, PLAYLIST_SCOPE]
    return [UPLOAD_SCOPE]


SCOPES = wanted_scopes()
ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = ROOT / "secrets" / "client_secret.json"


def token_path(channel: str | None) -> Path:
    """Where to save. A channel gets its own file so one install can
    publish to several accounts -- see README, 'Several channels'."""
    if not channel:
        return ROOT / "secrets" / "youtube_token.json"
    return ROOT / "secrets" / f"youtube_token_{channel.lower()}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="authorise a second channel, e.g. "
                                          "--channel trading")
    args = parser.parse_args()
    TOKEN = token_path(args.channel)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the publishing extras first:\n"
              "  pip install -r requirements-publish.txt", file=sys.stderr)
        return 1

    if not CLIENT_SECRET.exists():
        print(f"No client secret at {CLIENT_SECRET}\n\n{__doc__}", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    TOKEN.chmod(0o600)  # the refresh token is precious; keep it off other users
    print(f"\nSaved {TOKEN}")
    if args.channel:
        print(f"Profiles whose channel is {args.channel!r} will use it.")
    print("This file is a credential. It is gitignored. Back it up securely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
