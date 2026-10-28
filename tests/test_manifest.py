"""Manifest: state integrity and resumability."""

from __future__ import annotations

import json

import pytest

from pipeline.errors import ManifestError
from pipeline.manifest import STAGES, Episode, Manifest, Scene


def test_round_trip_preserves_everything(manifest, tmp_path):
    manifest.pass_gate_1()
    manifest.mark_done("voice", total_sec=42.5, draft=False)
    path = tmp_path / "manifest.json"
    manifest.save(path)
    back = Manifest.load(path)
    assert back.id == manifest.id
    assert len(back.scenes) == 3
    assert back.gate_1_passed_at == manifest.gate_1_passed_at
    assert back.stage("voice").data["total_sec"] == 42.5


def test_corrupt_manifest_is_rejected_not_guessed(tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text("{not json")
    with pytest.raises(ManifestError, match="corrupt"):
        Manifest.load(bad)


def test_missing_manifest_names_the_path(tmp_path):
    with pytest.raises(ManifestError, match="no manifest"):
        Manifest.load(tmp_path / "absent.json")


def test_scene_indices_must_be_contiguous(manifest):
    manifest.scenes.append(Scene(index=9, text="orphan"))
    with pytest.raises(ManifestError, match="no gaps"):
        manifest.validate()


def test_duplicate_scene_indices_are_rejected(manifest):
    manifest.scenes.append(Scene(index=1, text="dupe"))
    with pytest.raises(ManifestError, match="duplicate"):
        manifest.validate()


def test_unmet_prerequisites_are_listed(manifest):
    assert manifest.unmet_prerequisites("assemble")
    for s in STAGES[:STAGES.index("assemble")]:
        manifest.mark_done(s)
    assert manifest.unmet_prerequisites("assemble") == []


def test_total_duration_refuses_unmeasured_scenes(manifest):
    with pytest.raises(ManifestError, match="without a measured duration"):
        manifest.total_duration()
    for s in manifest.scenes:
        s.duration_sec = 2.0
    assert manifest.total_duration() == 6.0


def test_save_is_atomic(manifest, tmp_path):
    """A crash mid-write must not leave a truncated manifest."""
    path = tmp_path / "manifest.json"
    manifest.save(path)
    manifest.save(path)
    assert json.loads(path.read_text())["id"] == manifest.id
    assert not list(tmp_path.glob("*.tmp"))


def test_episode_paths_are_platform_neutral(tmp_path):
    ep = Episode("001", tmp_path)
    scene = Scene(index=7, text="x")
    assert ep.scene_audio(scene).name == "scene_07.wav"
    assert ep.scene_visual(scene).name == "scene_07.png"
