"""Stage 1: freeze a data snapshot.

The snapshot is written to a file and hashed, so the episode is
reproducible a year later, a re-render never re-queries a source, and a
sleeping database cannot break a render.
"""

from __future__ import annotations

import hashlib
import json

from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..recipes import get as get_recipe
from ..scrub import scan_snapshot, strip_sensitive


def run(ep: Episode, m: Manifest, params: dict | None = None) -> None:
    log = get(m.id, "export")
    recipe = get_recipe(m.recipe or "fee_drag")

    data = recipe.export(**(params or {}))
    if not isinstance(data, dict) or not data:
        raise StageError(f"recipe {recipe.name!r} returned no data")

    # Strip never-narrated fields before anything touches the episode dir,
    # so the sensitive value never reaches disk here at all.
    data = strip_sensitive(data)
    data["title"] = m.title or recipe.title_template

    findings = scan_snapshot(data)
    if findings:
        raise StageError(
            f"snapshot contains {len(findings)} secret finding(s) after stripping: "
            + "; ".join(str(f) for f in findings)
        )

    ep.ensure_dirs()
    payload = json.dumps(data, indent=2, sort_keys=True)
    ep.snapshot.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    m.tier = recipe.tier
    m.title = data["title"]
    m.snapshot_sha256 = digest
    m.mark_done("export", sha256=digest, keys=sorted(data), recipe=recipe.name)
    log.info(f"snapshot {len(payload)} bytes, sha256 {digest[:12]}")


def load_snapshot(ep: Episode) -> dict:
    if not ep.snapshot.exists():
        raise StageError(f"no snapshot at {ep.snapshot}; run the export stage first")
    return json.loads(ep.snapshot.read_text(encoding="utf-8"))


def verify_snapshot(ep: Episode, m: Manifest) -> None:
    """Confirm the snapshot on disk is the one the manifest recorded."""
    if not m.snapshot_sha256:
        return
    actual = hashlib.sha256(ep.snapshot.read_bytes()).hexdigest()
    if actual != m.snapshot_sha256:
        raise StageError(
            f"snapshot changed since export: manifest records "
            f"{m.snapshot_sha256[:12]} but file hashes {actual[:12]}. "
            f"Re-run export, or restore the original snapshot."
        )
