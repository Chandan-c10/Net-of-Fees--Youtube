"""Freeing disk, and the rule that makes it safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import prune
from pipeline.manifest import Episode, Manifest, Scene


def make(root, eid, *, rendered_days_ago=None, published=False,
         archived=False):
    ep = Episode(eid, root)
    ep.ensure_dirs()
    m = Manifest.new(eid, recipe="r", profile="trading", voice="v",
                     title=eid)
    m.scenes = [Scene(index=1, text="x.", visual_spec={}, duration_sec=1.0)]
    if rendered_days_ago is not None:
        m.mark_done("assemble")
        when = datetime.now(UTC) - timedelta(days=rendered_days_ago)
        m.stage("assemble").at = when.isoformat()
        for name in ("audio", "clips", "visuals"):
            d = ep.dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "big.bin").write_bytes(b"\x00" * 50_000)
        ep.video.parent.mkdir(parents=True, exist_ok=True)
        ep.video.write_bytes(b"\x00" * 80_000)
    if published:
        m.published_at = (datetime.now(UTC)
                          - timedelta(days=rendered_days_ago or 0)).isoformat()
    if archived:
        m.stage("assemble").data["archive_url"] = "https://x/y.mp4"
    ep.save_manifest(m)
    return ep, m


# ---- off by default ---------------------------------------------------


def test_retention_is_off_unless_set(monkeypatch, tmp_path):
    """Deleting by default is how someone loses work they did not know
    was scheduled for removal."""
    monkeypatch.delenv("RETAIN_WORKING_DAYS", raising=False)
    monkeypatch.delenv("RETAIN_VIDEO_DAYS", raising=False)
    assert prune.working_days() == 0 and prune.video_days() == 0
    make(tmp_path, "a", rendered_days_ago=30)
    result = prune.sweep(tmp_path)
    assert result["enabled"] is False
    assert (tmp_path / "a" / "audio").exists()


def test_a_bad_setting_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "soon")
    assert prune.working_days() == prune.DEFAULT_WORKING_DAYS


# ---- working files ----------------------------------------------------


def test_working_files_go_once_old_enough(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "1")
    monkeypatch.setenv("RETAIN_VIDEO_DAYS", "0")
    ep, _ = make(tmp_path, "a", rendered_days_ago=3)
    prune.sweep(tmp_path)
    assert not (ep.dir / "audio").exists()
    assert not (ep.dir / "clips").exists()
    assert ep.video.exists(), "the video is not a working file"


def test_working_files_stay_while_young(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "7")
    ep, _ = make(tmp_path, "a", rendered_days_ago=1)
    prune.sweep(tmp_path)
    assert (ep.dir / "audio").exists()


def test_the_manifest_is_never_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "1")
    monkeypatch.setenv("RETAIN_VIDEO_DAYS", "1")
    ep, _ = make(tmp_path, "a", rendered_days_ago=9, published=True)
    prune.sweep(tmp_path)
    assert ep.manifest_path.exists()
    assert json.loads(ep.manifest_path.read_text())["id"] == "a"


# ---- the rule that matters --------------------------------------------


def test_an_unpublished_video_is_never_deleted(monkeypatch, tmp_path):
    """It is the only copy of work you have not judged yet, and no
    retention setting is worth losing that."""
    monkeypatch.setenv("RETAIN_VIDEO_DAYS", "1")
    ep, _ = make(tmp_path, "a", rendered_days_ago=90)
    result = prune.sweep(tmp_path)
    assert ep.video.exists()
    assert result["kept"] and "not published" in result["kept"][0]["why"]


def test_a_published_video_can_go(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_VIDEO_DAYS", "1")
    ep, _ = make(tmp_path, "a", rendered_days_ago=5, published=True)
    prune.sweep(tmp_path)
    assert not ep.video.exists()


def test_an_archived_video_can_go_even_unpublished(monkeypatch, tmp_path):
    """The bucket holds a copy, so the local one is not the only one."""
    monkeypatch.setenv("RETAIN_VIDEO_DAYS", "1")
    ep, _ = make(tmp_path, "a", rendered_days_ago=5, archived=True)
    prune.sweep(tmp_path)
    assert not ep.video.exists()


@pytest.mark.parametrize("published,archived,expected", [
    (True, False, True), (False, True, True), (False, False, False),
])
def test_the_safety_rule_directly(tmp_path, published, archived, expected):
    _, m = make(tmp_path, "a", rendered_days_ago=1,
                published=published, archived=archived)
    assert prune.safe_to_remove_video(m)[0] is expected


# ---- reporting --------------------------------------------------------


def test_a_dry_run_deletes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "1")
    ep, _ = make(tmp_path, "a", rendered_days_ago=5)
    result = prune.sweep(tmp_path, dry_run=True)
    assert result["freed_mb"] > 0
    assert (ep.dir / "audio").exists(), "a preview must not delete"


def test_usage_counts_only_real_episodes(tmp_path):
    make(tmp_path, "a", rendered_days_ago=1)
    (tmp_path / "notanepisode").mkdir()
    got = prune.usage(tmp_path)
    assert got["episodes"] == 1 and got["mb"] > 0


def test_a_broken_manifest_does_not_stop_the_sweep(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAIN_WORKING_DAYS", "1")
    make(tmp_path, "good", rendered_days_ago=5)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not json")
    result = prune.sweep(tmp_path)
    assert [r["episode"] for r in result["episodes"]] == ["good"]
