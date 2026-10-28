"""End to end: topic row to a rendered video, with no network."""

from __future__ import annotations

import pytest
from conftest import needs_ffmpeg

import profiles
from pipeline.errors import StageError
from pipeline.manifest import Episode, Manifest
from pipeline.stages import assemble, captions, charts, export, script_gen, shorts, thumbnail, voice


@needs_ffmpeg
def test_full_run_offline(tmp_path):
    ep = Episode("e2e", tmp_path)
    ep.ensure_dirs()
    m = Manifest.new("e2e", recipe="fee_drag", profile="trading",
                     voice="chandan_en", title="E2E episode")
    profile = profiles.load("trading")

    export.run(ep, m, {})
    assert m.snapshot_sha256, "snapshot was not hashed"

    script_gen.run(ep, m, engine="template", profile=profile)
    assert len(m.scenes) >= 5

    from pipeline.scrub import assert_clean
    assert_clean([ep.snapshot, ep.script])
    m.mark_done("scrub", findings=0)

    m.pass_gate_1()
    voice.run(ep, m, engine_name="silent")
    charts.run(ep, m, profile)
    captions.run(ep, m, method="derive")
    video = assemble.run(ep, m)
    thumbnail.run(ep, m, profile)

    assert video.exists() and video.stat().st_size > 10_000
    assert m.stage("assemble").data["drift_sec"] <= 0.05
    assert m.stage("thumbnail").data["count"] == 3

    # Gate 2 is not cleared, so Shorts must refuse.
    with pytest.raises(StageError, match="gate 2"):
        shorts.run(ep, m, profile)

    m.pass_gate_2()
    shorts.run(ep, m, profile)
    clips = list(ep.shorts_dir.glob("short_*.mp4"))
    assert clips, "no Shorts produced"
    for c in clips:
        assert c.stat().st_size > 5_000


def test_hook_rule_rejects_a_preview_opening(tmp_path):
    """Scene 1 must state the finding, not preview it."""
    from pipeline.script_format import parse, to_scenes
    from pipeline.stages.script_gen import _check_hook

    bad = to_scenes(parse("[SCENE 1]\nIn this video we will look at 5 charges."))
    with pytest.raises(StageError, match="hook"):
        _check_hook(bad)

    good = to_scenes(parse("[SCENE 1]\nCharges took 41 percent of a small win."))
    _check_hook(good)


def test_hook_rule_requires_a_number(tmp_path):
    from pipeline.script_format import parse, to_scenes
    from pipeline.stages.script_gen import _check_hook

    with pytest.raises(StageError, match="no number"):
        _check_hook(to_scenes(parse("[SCENE 1]\nCharges take most of your profit.")))


def test_voice_consent_is_enforced(tmp_path):
    """A voice directory with no consent.md fails validation."""
    from pipeline.voices.base import assert_consented

    unconsented = tmp_path / "someone_else"
    (unconsented / "reference").mkdir(parents=True)
    with pytest.raises(StageError, match="consent"):
        assert_consented(unconsented)

    (unconsented / "consent.md").write_text("Granted by the speaker, 2026-10-28.")
    assert_consented(unconsented)


def test_stock_voice_is_publishable_but_draft_is_not():
    """A licensed stock voice may publish; the draft engine may never."""
    from pipeline.voices import get_engine

    draft = get_engine("silent")
    assert draft.draft is True, "silent engine must be marked draft"

    from pipeline.voices.kokoro import KokoroVoice
    assert KokoroVoice.draft is False, "a licensed stock voice is publishable"


def test_unknown_stock_voice_is_rejected():
    from pipeline.errors import ConfigError
    from pipeline.voices.kokoro import KokoroVoice

    with pytest.raises(ConfigError, match="unknown Kokoro voice"):
        KokoroVoice(voice="someone_from_youtube")


def test_stock_voice_records_it_is_not_a_clone(tmp_path):
    """Provenance must state plainly that no real person was copied."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    d = root / "voices" / "stock_am_michael"
    if not d.exists():
        pytest.skip("stock voice not set up in this checkout")

    meta = json.loads((d / "meta.json").read_text())
    assert meta["is_clone_of_real_person"] is False
    assert meta["licence"] == "Apache-2.0"

    consent = (d / "consent.md").read_text().lower()
    assert "not a clone of any real person" in consent
    assert "impersonation" in consent, "the rule it does not bend must be stated"
