"""Fetching a voice reference that lives somewhere else.

`voices/<name>/reference/` is gitignored, correctly: recordings of your
own voice do not belong in a repository. The cost of that is that a
cloud runner has no reference to clone from, so your own voice has only
ever worked on a machine that already had the files.

Pointing at a URL removes that. The reference is downloaded once per
run, verified, and used exactly as a local one would be.

Two things to be clear about.

**A link to your voice reference is a link to your voice.** Anyone who
has it can clone you with the same tools you are using. It is a
credential, not a file path: it belongs in `.env` or a repository
secret, never in a commit, and a link-shareable Drive URL is readable by
anyone who finds it. Prefer a short-lived or restricted link.

**Nothing is cached between runs by default.** A downloaded reference
lands in the same gitignored directory and is deleted afterwards unless
asked otherwise, so a shared runner does not keep your voice on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ..errors import ConfigError, StageError
from ..logging_setup import get

TIMEOUT_SEC = 300
MAX_BYTES = 500 * 1024 * 1024        # a long session, not a mistake
AUDIO_SUFFIXES = (".wav", ".flac", ".mp3", ".m4a", ".aiff", ".aif")

# Google Drive share links point at a viewer page, not the bytes. The
# file id is what the download endpoint wants.
_DRIVE_ID = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)


def direct_url(url: str) -> str:
    """Turn a share link into something that returns bytes.

    Only Drive needs this today. Anything else is assumed to be a direct
    link already, which a signed S3 or release-asset URL is.
    """
    host = urllib.parse.urlparse(url).netloc.lower()
    if "drive.google.com" not in host and "docs.google.com" not in host:
        return url
    for pattern in _DRIVE_ID:
        found = pattern.search(url)
        if found:
            return ("https://drive.google.com/uc?export=download"
                    f"&id={found.group(1)}")
    raise ConfigError(
        f"could not find a file id in {url}. Use the 'Share' link to a "
        "single file, not a folder."
    )


def reference_url(voice: str) -> str | None:
    """The URL for this voice, if one is configured.

    Per-voice first, so two voices can live in different places:
      VOICE_REFERENCE_URL_CHANDAN_EN, then VOICE_REFERENCE_URL.
    """
    key = re.sub(r"[^A-Za-z0-9]", "_", voice).upper()
    return (os.getenv(f"VOICE_REFERENCE_URL_{key}")
            or os.getenv("VOICE_REFERENCE_URL") or None)


def object_key(voice: str) -> str | None:
    """A key in the archive bucket, if the reference lives there.

    Preferred over a URL: the bucket stays private and the credentials
    already configured for archiving are used to read it, so there is no
    link that can clone your voice if it leaks.
    """
    key = re.sub(r"[^A-Za-z0-9]", "_", voice).upper()
    return (os.getenv(f"VOICE_REFERENCE_KEY_{key}")
            or os.getenv("VOICE_REFERENCE_KEY") or None)


def _download(url: str, out: Path, log,
              headers: dict[str, str] | None = None) -> Path:
    request = urllib.request.Request(
        url, headers={"User-Agent": "net-of-fees/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > MAX_BYTES:
                raise StageError(
                    f"the reference is {declared / 1e6:.0f} MB, over the "
                    f"{MAX_BYTES / 1e6:.0f} MB limit"
                )
            written = 0
            with out.open("wb") as handle:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise StageError("the reference exceeded the size limit")
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise StageError(
            f"the reference URL returned {exc.code}. A Drive link must be "
            "shared as 'anyone with the link', and must point at a file "
            "rather than a folder."
        ) from exc
    except urllib.error.URLError as exc:
        raise StageError(f"could not reach the reference URL: {exc.reason}"
                         ) from exc
    log.info(f"downloaded {written / 1e6:.1f} MB of reference audio")
    return out


def _looks_like_html(path: Path) -> bool:
    """Drive serves a confirmation page for some files instead of bytes."""
    head = path.open("rb").read(200).lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def fetch(voice: str, into: Path, url: str | None = None,
          keep: bool = False) -> list[Path]:
    """Put this voice's reference audio in `into`. Returns what landed.

    A zip is unpacked; a single audio file is used as-is. Anything else
    is refused rather than guessed at.
    """
    log = get(None, "voice-fetch")
    # A private object in the archive bucket is preferred to a URL.
    key = None if url else object_key(voice)
    if key:
        from .. import archive
        target = archive.Target()
        if not target.configured:
            raise ConfigError(
                f"VOICE_REFERENCE_KEY is set for {voice!r} but the archive "
                "storage is not configured: "
                + ", ".join(target.missing()) + " not set"
            )
    else:
        url = url or reference_url(voice)
        if not url:
            return []

    into.mkdir(parents=True, exist_ok=True)
    existing = sorted(p for p in into.iterdir()
                      if p.suffix.lower() in AUDIO_SUFFIXES)
    if existing and keep:
        log.debug(f"{len(existing)} reference files already present")
        return existing

    work = Path(tempfile.mkdtemp(prefix="voiceref-"))
    try:
        if key:
            from .. import archive
            signed_url, headers = archive.signed_get(archive.Target(), key)
            blob = _download(signed_url, work / "reference.bin", log, headers)
            source_name = key
        else:
            blob = _download(direct_url(url), work / "reference.bin", log)
            source_name = url
        if _looks_like_html(blob):
            raise StageError(
                "the URL returned a web page rather than audio. For Drive, "
                "the file must be shared as 'anyone with the link'; large "
                "files also show a virus-scan page that cannot be bypassed "
                "here, so host those elsewhere."
            )

        landed: list[Path] = []
        if zipfile.is_zipfile(blob):
            # Not named `archive`: that is the storage module, imported
            # in this same function for the signed-GET path.
            with zipfile.ZipFile(blob) as bundle:
                for member in bundle.namelist():
                    name = Path(member).name
                    if not name or Path(member).suffix.lower() not in AUDIO_SUFFIXES:
                        continue
                    # Flattened by name: a zip entry must not choose where
                    # it lands on disk.
                    target = into / name
                    with bundle.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    landed.append(target)
        else:
            suffix = Path(urllib.parse.urlparse(source_name).path).suffix.lower()
            if suffix not in AUDIO_SUFFIXES:
                suffix = ".wav"
            target = into / f"reference{suffix}"
            shutil.copy2(blob, target)
            landed.append(target)

        if not landed:
            raise StageError(
                "the reference contained no audio. Expected a single "
                f"recording or a zip of them ({', '.join(AUDIO_SUFFIXES)})."
            )
        log.info(f"{len(landed)} reference file(s) ready for {voice}")
        return sorted(landed)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def discard(into: Path) -> None:
    """Remove a downloaded reference.

    Called after a cloud render so a shared runner does not keep your
    voice on disk. A local reference is never touched by this -- the
    caller only calls it for what it downloaded.
    """
    for path in list(into.glob("*")):
        if path.suffix.lower() in AUDIO_SUFFIXES:
            path.unlink(missing_ok=True)
