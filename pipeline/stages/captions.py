"""Stage 6: caption timing.

Two paths. `whisper` transcribes the rendered narration for true
word-level timing. `derive` distributes the known script text across each
scene's measured duration -- exact at scene boundaries, approximate
within, and it needs no model at all.

Derive is the default so the pipeline runs with zero ML dependencies;
whisper is used once a real voice engine is in place.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import ConfigError, StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest, Scene

METHODS = ("derive", "whisper")
MAX_CHARS_PER_CUE = 42  # readable at 360p on a phone


def _ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    mnt, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{mnt:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, limit: int = MAX_CHARS_PER_CUE) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > limit:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [text]


def _derive_cues(scenes: list[Scene]) -> list[tuple[int, float, float, str]]:
    """Split each scene into cues proportional to word count.

    Cue boundaries never cross a scene boundary, so a caption can never be
    on screen while the wrong chart is showing.
    """
    cues: list[tuple[int, float, float, str]] = []
    clock = 0.0
    for scene in scenes:
        if scene.duration_sec is None:
            raise StageError(
                f"scene {scene.index} has no measured duration; run voice first"
            )
        text = " ".join(scene.text.split())
        chunks = _wrap(text)
        total_words = sum(len(c.split()) for c in chunks) or 1
        start = clock
        for chunk in chunks:
            share = len(chunk.split()) / total_words
            length = scene.duration_sec * share
            cues.append((scene.index, start, start + length, chunk))
            start += length
        clock += scene.duration_sec
    return cues


def _whisper_cues(ep: Episode, scenes: list[Scene], model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ConfigError(
            "faster-whisper is not installed. "
            "pip install faster-whisper, or use --captions derive"
        ) from exc
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    cues, clock = [], 0.0
    for scene in scenes:
        path = ep.scene_audio(scene)
        if not path.exists():
            raise StageError(f"no audio for {scene.slug}; run voice first")
        segments, _ = model.transcribe(str(path), word_timestamps=True)
        for seg in segments:
            cues.append((scene.index, clock + seg.start, clock + seg.end,
                         seg.text.strip()))
        clock += scene.duration_sec or 0.0
    return cues


def write_srt(cues: list[tuple[int, float, float, str]], path: Path) -> None:
    lines = []
    for i, (_scene, start, end, text) in enumerate(cues, start=1):
        if end <= start:
            end = start + 0.4  # a zero-length cue never renders
        lines += [str(i), f"{_ts(start)} --> {_ts(end)}", text, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run(ep: Episode, m: Manifest, method: str = "derive",
        model_size: str = "base") -> None:
    log = get(m.id, "captions")
    if method not in METHODS:
        raise ConfigError(f"unknown caption method {method!r}; expected {METHODS}")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")

    if method == "whisper":
        cues = _whisper_cues(ep, m.scenes, model_size)
    else:
        cues = _derive_cues(m.scenes)

    if not cues:
        raise StageError("caption generation produced no cues")
    write_srt(cues, ep.captions)
    write_cues(cues, cues_path(ep))
    m.mark_done("captions", method=method, cues=len(cues))
    log.info(f"{len(cues)} cues via {method}")


def cues_path(ep: Episode) -> Path:
    return ep.dir / "cues.json"


def write_cues(cues: list[tuple[int, float, float, str]], path: Path) -> None:
    payload = [
        {"scene": sc, "start": round(st, 3), "end": round(en, 3), "text": tx}
        for sc, st, en, tx in cues
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_cues(ep: Episode) -> list[dict]:
    path = cues_path(ep)
    if not path.exists():
        raise StageError(f"no cues at {path}; run the captions stage first")
    return json.loads(path.read_text(encoding="utf-8"))
