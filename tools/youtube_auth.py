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

import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = ROOT / "secrets" / "client_secret.json"
TOKEN = ROOT / "secrets" / "youtube_token.json"


def main() -> int:
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
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)  # the refresh token is precious; keep it off other users
    print(f"\nSaved {TOKEN}")
    print("This file is a credential. It is gitignored. Back it up securely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
