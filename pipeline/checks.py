"""Looking at a finished episode the way a viewer would.

The gates ask whether an episode should go out. These ask whether it
came out right, which is a different question and one a person is bad
at: nobody watches twenty minutes of their own output carefully enough
to notice that scene four is silent.

Every check here measures the rendered file rather than the intention
behind it. A scene with no audio is found by reading the audio, not by
checking that the voice stage reported success -- the stage reporting
success is exactly the case where something has gone quietly wrong.

Each finding carries a severity, where it is, and what to do. A check
that says "something is wrong" without saying which scene is a check
that gets ignored.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import StageError
from .logging_setup import get
from .platform_support import ffmpeg, ffprobe

# A scene quieter than this across its whole length has no narration in
# it. Room tone sits at -62 dBFS, so this is comfortably above silence
# and comfortably below speech.
SILENT_RMS_DBFS = -50.0

# Anything at or above this is clipping. True peak should sit under -1.
CLIPPING_DBFS = -0.5

# A frame whose pixels barely vary is a blank or a solid colour.
FLAT_FRAME_STDDEV = 3.0

# A frame can be perfectly exposed and still dead: a subject cropped
# tight against empty ground, with nothing behind it. Measured on the
# upper part of the frame, where background lives. Found by watching a
# sample where five of fourteen shots were a monkey on bare dirt.
FLAT_BACKGROUND_STDDEV = 42.0

# Platform bounds, so a render that cannot be posted is caught here
# rather than by the platform.
SHORTS_MAX_SEC = 180.0
MIN_EPISODE_SEC = 5.0


@dataclass
class Finding:
    """One thing wrong, where it is, and what to do about it."""

    severity: str        # "blocker" | "warning"
    check: str
    where: str
    message: str
    fix: str

    def line(self) -> str:
        mark = "BLOCKER" if self.severity == "blocker" else "warning"
        return f"{mark:8} {self.where:12} {self.message}"


def _probe_json(path: Path, args: list[str]) -> dict:
    proc = subprocess.run(
        [ffprobe(), "-v", "error", "-print_format", "json", *args, str(path)],
        capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise StageError(f"ffprobe failed on {path.name}: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


_RMS = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_PEAK = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")


def levels(path: Path) -> tuple[float, float]:
    """Mean and peak level of an audio file, in dBFS."""
    proc = subprocess.run(
        [ffmpeg(), "-v", "info", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=300, check=False)
    text = proc.stderr
    rms = _RMS.search(text)
    peak = _PEAK.search(text)
    if not rms or not peak:
        raise StageError(f"could not measure the level of {path.name}")
    return float(rms.group(1)), float(peak.group(1))


def window_level(path: Path, start: float, length: float) -> float:
    """Mean level of one stretch of a longer file, in dBFS."""
    proc = subprocess.run(
        [ffmpeg(), "-v", "info", "-ss", f"{start:.3f}", "-t",
         f"{max(0.05, length):.3f}", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300, check=False)
    found = _RMS.search(proc.stderr)
    if not found:
        raise StageError(f"could not measure {start:.1f}s of {path.name}")
    return float(found.group(1))


def check_continuous_take(ep, m) -> list[Finding]:
    """Every scene's stretch of the one take should contain speech.

    A continuous take writes no per-scene files, so the per-scene check
    has nothing to look at -- and anything left behind by an earlier
    per-scene run is stale. Measuring the take over each scene's own
    window asks the real question: was this line actually spoken?
    """
    out: list[Finding] = []
    take = ep.final_audio
    if not take.exists():
        return [Finding("blocker", "take", "audio",
                        "the continuous take is missing",
                        f"run.py {m.id} --stage voice --force")]
    try:
        rms, _peak = levels(take)
    except StageError as exc:
        return [Finding("warning", "take", "audio", str(exc),
                        "check the file by hand")]
    if rms <= SILENT_RMS_DBFS:
        return [Finding("blocker", "take", "audio",
                        f"the whole take is silent ({rms:.0f} dBFS)",
                        f"run.py {m.id} --stage voice --force")]

    clock = 0.0
    for scene in m.scenes:
        length = scene.duration_sec or 0.0
        if length <= 0:
            continue
        try:
            level = window_level(take, clock, length)
        except StageError as exc:
            out.append(Finding("warning", "take", scene.slug, str(exc),
                               "check the take by hand"))
        else:
            if level <= SILENT_RMS_DBFS:
                out.append(Finding(
                    "blocker", "take", scene.slug,
                    f"this line is silent inside the take "
                    f"({level:.0f} dBFS)",
                    f"run.py {m.id} --stage voice --force"))
        clock += length
    return out


def check_scene_audio(ep, m) -> list[Finding]:
    """Every scene should contain speech.

    The commonest silent failure in this pipeline: a voice engine that
    returns without error and writes nothing audible.
    """
    if m.stage("voice").data.get("take") == "continuous":
        return check_continuous_take(ep, m)
    out: list[Finding] = []
    for scene in m.scenes:
        path = ep.scene_audio(scene)
        if not path.exists():
            out.append(Finding(
                "blocker", "scene_audio", scene.slug,
                "no audio file for this scene",
                f"run.py {m.id} --stage voice --force"))
            continue
        try:
            rms, _peak = levels(path)
        except StageError as exc:
            out.append(Finding("warning", "scene_audio", scene.slug,
                               str(exc), "check the file by hand"))
            continue
        if rms <= SILENT_RMS_DBFS:
            out.append(Finding(
                "blocker", "scene_audio", scene.slug,
                f"effectively silent ({rms:.0f} dBFS)",
                f"run.py {m.id} --stage voice --force"))
    return out


def check_mix(ep, m) -> list[Finding]:
    """The finished audio should be loud enough and not clipped."""
    out: list[Finding] = []
    if not ep.video.exists():
        return out
    try:
        rms, peak = levels(ep.video)
    except StageError as exc:
        return [Finding("warning", "mix", "video", str(exc),
                        "check the render")]
    if peak >= CLIPPING_DBFS:
        out.append(Finding(
            "blocker", "mix", "video",
            f"audio is clipping (peak {peak:.1f} dBFS)",
            "lower VOICE_SPEED or re-run assemble; loudnorm should "
            "prevent this, so a clip means something bypassed it"))
    if rms <= SILENT_RMS_DBFS:
        out.append(Finding(
            "blocker", "mix", "video", f"the mix is silent ({rms:.0f} dBFS)",
            f"run.py {m.id} --stage assemble --force"))
    return out


def check_frames(ep, m, samples: int = 5) -> list[Finding]:
    """Sample the video for blank frames.

    A flat frame is a render that produced a solid colour -- a missing
    picture, or a filter that emptied it.
    """
    out: list[Finding] = []
    if not ep.video.exists():
        return out
    duration = m.stage("assemble").data.get("duration_sec") or 0
    if duration < 1:
        return out
    try:
        import numpy as np
        from PIL import Image
    except ImportError:          # pragma: no cover - pillow is required
        return out

    work = ep.dir / "work" / "checks"
    work.mkdir(parents=True, exist_ok=True)
    for index in range(samples):
        at = duration * (index + 0.5) / samples
        frame = work / f"sample_{index}.png"
        subprocess.run(
            [ffmpeg(), "-v", "error", "-ss", f"{at:.2f}", "-i", str(ep.video),
             "-frames:v", "1", "-y", str(frame)],
            capture_output=True, timeout=120, check=False)
        if not frame.exists():
            continue
        pixels = np.asarray(Image.open(frame).convert("L"), dtype=float)
        spread = float(pixels.std())
        if spread < FLAT_FRAME_STDDEV:
            out.append(Finding(
                "blocker", "blank_frame", f"{at:.1f}s",
                f"the frame is almost flat (variation {spread:.1f})",
                "check the picture for this scene, then re-run charts "
                "and assemble"))
            continue
        # The upper portion, where a background would be if there were
        # one. A subject cropped tight against empty ground passes the
        # blank check and still looks dead.
        background = float(pixels[: int(pixels.shape[0] * 0.45)].std())
        if background < FLAT_BACKGROUND_STDDEV:
            out.append(Finding(
                "warning", "flat_background", f"{at:.1f}s",
                f"nothing behind the subject (variation {background:.0f})",
                "crop wider so the setting is visible, or choose a "
                "picture with a background"))
    return out


def check_duration(ep, m, vertical: bool = False) -> list[Finding]:
    """The episode should be postable on the platform it is for."""
    out: list[Finding] = []
    duration = m.stage("assemble").data.get("duration_sec") or 0
    if not duration:
        return out
    if duration < MIN_EPISODE_SEC:
        out.append(Finding(
            "blocker", "duration", "video",
            f"{duration:.1f}s is too short to post",
            "add scenes, or check the script stage produced them all"))
    if vertical and duration > SHORTS_MAX_SEC:
        out.append(Finding(
            "warning", "duration", "video",
            f"{duration:.0f}s is over the {SHORTS_MAX_SEC:.0f}s Shorts limit",
            "cut scenes, or publish it as long-form instead"))
    return out


def check_captions(ep, m) -> list[Finding]:
    """Captions should cover the narration and stay on screen long
    enough to read."""
    out: list[Finding] = []
    cues_path = ep.dir / "cues.json"
    if not cues_path.exists():
        return out
    try:
        cues = json.loads(cues_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [Finding("warning", "captions", "cues.json",
                        "could not be read", "re-run the captions stage")]
    covered = {c.get("scene") for c in cues}
    for scene in m.scenes:
        if scene.index not in covered:
            out.append(Finding(
                "warning", "captions", scene.slug,
                "no caption covers this scene",
                f"run.py {m.id} --stage captions --force"))
    for cue in cues:
        span = float(cue.get("end", 0)) - float(cue.get("start", 0))
        words = len(str(cue.get("text", "")).split())
        # Under about two words a second is unreadable on a phone.
        if words and span > 0 and words / span > 7:
            out.append(Finding(
                "warning", "captions", f"scene {cue.get('scene')}",
                f"{words} words in {span:.1f}s is too fast to read",
                "split the scene, or slow the narration with VOICE_SPEED"))
    return out


ALL_CHECKS = (
    ("scene audio", check_scene_audio),
    ("mix", check_mix),
    ("frames", check_frames),
    ("duration", check_duration),
    ("captions", check_captions),
)


def inspect(ep, m, vertical: bool = False) -> dict:
    """Run every check. Returns findings grouped by severity."""
    log = get(m.id, "checks")
    findings: list[Finding] = []
    for name, check in ALL_CHECKS:
        try:
            if check is check_duration:
                findings.extend(check(ep, m, vertical))
            else:
                findings.extend(check(ep, m))
        except Exception as exc:  # one broken check must not hide the rest
            log.warning(f"{name} could not run: {exc}")
            findings.append(Finding("warning", name, "-",
                                    f"the check itself failed: {exc}",
                                    "report this"))
    blockers = [f for f in findings if f.severity == "blocker"]
    log.info(f"{len(findings)} findings, {len(blockers)} blocking")
    return {
        "findings": [asdict(f) for f in findings],
        "blockers": len(blockers),
        "warnings": len(findings) - len(blockers),
        "clean": not findings,
    }
