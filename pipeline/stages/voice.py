"""Stage 4: narrate each scene and measure its duration.

Duration is measured from the rendered file, never estimated. That
measurement drives every visual's on-screen time, so an estimate here
becomes visible drift in the finished video.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import VOICES_DIR
from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..voices import apply_lexicon, get_engine, load_lexicon
from ..voices.base import assert_consented


def run(ep: Episode, m: Manifest, engine_name: str = "silent",
        allow_unconsented_draft: bool = True) -> None:
    log = get(m.id, "voice")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    kwargs: dict[str, object] = {"episode_id": m.id}
    if engine_name == "kokoro":
        # A stock voice directory records which model voice it maps to.
        meta = VOICES_DIR / m.voice / "meta.json"
        if meta.exists():
            import json
            kwargs["voice"] = json.loads(
                meta.read_text(encoding="utf-8")).get("voice", "am_michael")
    engine = get_engine(engine_name, **kwargs)
    ep.audio_dir.mkdir(parents=True, exist_ok=True)

    # Consent is enforced, not trusted. The draft engine is exempt because
    # it produces silence and can never be published.
    voices_used = {s.voice or m.voice for s in m.scenes}
    if not getattr(engine, "draft", False) or not allow_unconsented_draft:
        for v in voices_used:
            assert_consented(VOICES_DIR / v)

    # A cloning engine needs reference audio, which is gitignored and so
    # absent on a fresh runner. A configured URL is fetched here, used,
    # and discarded afterwards so a shared machine keeps no copy.
    fetched: list[Path] = []
    if getattr(engine, "needs_reference", False):
        from ..voices import fetch as voice_fetch
        for v in voices_used:
            into = VOICES_DIR / v / "reference"
            got = voice_fetch.fetch(v, into, keep=True)
            if got:
                fetched.append(into)
                log.info(f"reference for {v} fetched from its URL")

    try:
        _narrate(ep, m, engine, voices_used, log)
    finally:
        if fetched and not os.getenv("KEEP_VOICE_REFERENCE"):
            from ..voices import fetch as voice_fetch
            for into in fetched:
                voice_fetch.discard(into)
                log.debug(f"discarded the downloaded reference in {into}")
    return


def _narrate_continuously(ep: Episode, m: Manifest, engine, lexicons,
                          log) -> bool:
    """One take for the whole script, with the lines timed inside it.

    Returns False when the engine cannot do it, so the caller falls back
    to per-scene narration rather than failing.
    """
    from ..voices import continuous
    if not hasattr(engine, "render_text"):
        return False

    spoken = []
    for scene in m.scenes:
        voice = scene.voice or m.voice
        spoken.append(apply_lexicon(scene.text, lexicons.get(voice, {})))

    class _Lines:
        def __init__(self, texts):
            self.items = [type("L", (), {"text": t})() for t in texts]

    spans = continuous.narrate(
        engine, _Lines(spoken).items, ep.final_audio, m.id, log)

    # Per-scene files from an earlier per-scene run are stale the moment
    # a take exists, and a stale file is not inert: a silent scene_01.wav
    # left by the draft engine made `run.py check` report fourteen silent
    # scenes over an episode whose audio was fine.
    for scene in m.scenes:
        ep.scene_audio(scene).unlink(missing_ok=True)

    for scene, span in zip(m.scenes, spans, strict=True):
        scene.duration_sec = round(span.duration, 3)
    total = sum(s.duration_sec for s in m.scenes)
    m.mark_done("voice", engine=engine.name, draft=False,
                total_sec=round(total, 3), take="continuous",
                scenes=len(m.scenes))
    log.info(f"continuous take, {total:.2f}s across {len(m.scenes)} scenes")
    return True


def _narrate(ep: Episode, m: Manifest, engine, voices_used, log) -> None:
    lexicons = {v: load_lexicon(VOICES_DIR / v) for v in voices_used}

    # One take by default. Per-scene narration pauses at every boundary
    # while the frame changes, which is the opposite of how narration
    # works -- the read runs on and the pictures cut underneath it.
    if (os.getenv("VOICE_TAKE", "continuous").strip() != "per_scene"
            and len(voices_used) == 1
            and not getattr(engine, "draft", False)):
        try:
            if _narrate_continuously(ep, m, engine, lexicons, log):
                return
        except Exception as exc:
            log.warning(f"continuous take failed ({exc}); narrating per scene")

    total = 0.0
    for scene in m.scenes:
        voice = scene.voice or m.voice
        spoken = apply_lexicon(scene.text, lexicons.get(voice, {}))
        result = engine.speak(spoken, ep.scene_audio(scene))
        scene.duration_sec = round(result.duration_sec, 3)
        total += scene.duration_sec
        log.debug(f"{scene.slug} {scene.duration_sec:.2f}s voice={voice}")

    from ..pacing import describe
    pacing = describe(m.scenes)
    m.mark_done(
        "voice",
        engine=engine.name,
        pacing=pacing,
        draft=bool(getattr(engine, "draft", False)),
        total_sec=round(total, 3),
        voices=sorted(voices_used),
    )
    log.info(
        f"{len(m.scenes)} scenes, {total:.1f}s total, engine={engine.name}, "
        f"scene length varies {pacing.get('variation_pct', 0)}%"
        + (" [DRAFT]" if getattr(engine, "draft", False) else "")
    )
