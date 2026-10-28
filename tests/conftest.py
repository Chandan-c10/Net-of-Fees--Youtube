"""Shared fixtures.

No test touches a real API, the network, or a database. A suite that needs
credentials is a suite that stops being run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.manifest import Episode, Manifest, Scene  # noqa: E402
from pipeline.platform_support import ffmpeg_available  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="FFmpeg not on PATH"
)


@pytest.fixture
def episode_dir(tmp_path: Path) -> Path:
    return tmp_path / "episodes"


@pytest.fixture
def fixture_snapshot() -> dict:
    """A three-scene episode's data. Committed, deterministic, tiny."""
    from pipeline.recipes import get
    data = get("fee_drag").export()
    data["title"] = "Fixture episode"
    return data


@pytest.fixture
def manifest() -> Manifest:
    m = Manifest.new("999", recipe="fee_drag", profile="trading",
                     title="Fixture episode", voice="chandan_en")
    m.scenes = [
        Scene(index=1, text="Charges took 41 percent of a small win.",
              visual_spec={"data_key": "breakeven"}),
        Scene(index=2, text="Here is every charge on one round trip, 62 rupees.",
              visual_spec={"data_key": "fee_stack"}),
        Scene(index=3, text="Gross was 750 rupees and you keep 688.",
              visual_spec={"data_key": "gross_vs_net"}),
    ]
    return m


@pytest.fixture
def episode(episode_dir: Path, manifest: Manifest, fixture_snapshot: dict) -> Episode:
    import json
    ep = Episode(manifest.id, episode_dir)
    ep.ensure_dirs()
    ep.snapshot.write_text(json.dumps(fixture_snapshot, indent=2))
    manifest.mark_done("export", sha256="fixture")
    manifest.mark_done("script", scenes=len(manifest.scenes))
    manifest.mark_done("scrub", findings=0)
    ep.save_manifest(manifest)
    return ep
