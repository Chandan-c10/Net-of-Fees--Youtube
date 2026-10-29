"""Motion for photographic stills.

A still held for the length of a scene reads as a slide deck. `motion.py`
solves that for charts by drawing them in; a photograph or a generated
image cannot draw itself in, so it gets a slow move across it instead --
the thing a camera operator would have done.

Four decisions worth stating, because each one is the difference between
this looking authored and looking cheap:

* The move is small. Zoom travels about eight percent over a whole scene.
  A big push reads as a screensaver; a small one reads as a camera that
  happened to be moving.
* It eases. A linear zoom is the single clearest sign of an automated
  pan. This uses the same cubic ease-out the chart builds use.
* The direction varies, seeded from the episode and scene, so consecutive
  scenes do not all drift the same way. Mechanical repetition is what the
  eye notices.
* The source is upscaled before the crop, never after. Cropping into a
  1920-wide image and scaling the crop back up is how you get a soft
  frame; starting wider keeps every output pixel real.

Duration is the part that must not drift. `zoompan` counts in frames, so
the clip is built as an exact frame count -- duration x fps, rounded once
-- and the result is measured and checked against what was asked for.
That keeps the contract in section 5a: the audio decides the length, and
every visual is cut to it.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import VideoSpec
from ..errors import StageError
from ..logging_setup import get
from ..platform_support import ffmpeg, ffprobe

# How far the zoom travels across a scene. Eight percent is enough to
# feel alive and little enough not to announce itself.
ZOOM_TRAVEL = 0.08

# The source is scaled to this multiple of the output width before
# cropping, so the tightest crop still has real pixels behind it.
OVERSCAN = 1.25

# A clip whose length is out by more than this is a bug, not a rounding
# artifact; assemble's own tolerance is 50ms for the whole episode.
DURATION_TOLERANCE_SEC = 0.04

# Shortest clip worth moving across. Below this the move is a twitch.
MIN_MOVE_SEC = 1.2

# Where to take the crop from when the source is taller than the target.
# Centre is the obvious choice and the wrong one: faces sit above the
# middle of almost every frame, so a centred crop takes the chin and
# leaves the hair. 0.35 keeps heads.
VERTICAL_CROP_BIAS = 0.35


@dataclass(frozen=True)
class Move:
    """Where the frame starts and ends, in normalised terms."""

    name: str
    zoom_in: bool
    # Pan target as a fraction of the available slack, -1.0 to 1.0.
    pan_x: float
    pan_y: float


# A small set, each a move a person would actually choose. Push-in on a
# subject, pull-out to reveal, and gentle drifts for everything else.
MOVES: tuple[Move, ...] = (
    Move("push_in", True, 0.0, 0.0),
    Move("pull_out", False, 0.0, 0.0),
    Move("drift_left", True, -1.0, 0.0),
    Move("drift_right", True, 1.0, 0.0),
    Move("rise", True, 0.0, -1.0),
    Move("settle", True, 0.0, 1.0),
    Move("reveal_left", False, -1.0, 0.0),
    Move("reveal_right", False, 1.0, 0.0),
)


# How many recent moves a new one must differ from. One is not enough:
# excluding only the last move lets the choice ping-pong between two, and
# an A-B-A-B pattern reads as mechanical just as plainly as A-A-A.
MOVE_MEMORY = 3


def choose_move(scene_index: int, episode_id: str = "",
                avoid: str | tuple[str, ...] | None = None) -> Move:
    """Pick a move for this scene, the same way on every re-render.

    `avoid` is a name or names this scene must not use.
    """
    if avoid is None:
        excluded: tuple[str, ...] = ()
    elif isinstance(avoid, str):
        excluded = (avoid,)
    else:
        excluded = tuple(avoid)
    seed = f"{episode_id}:{scene_index}:kenburns"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    options = [m for m in MOVES if m.name not in excluded] or list(MOVES)
    return options[digest[0] % len(options)]


def plan_moves(count: int, episode_id: str = "") -> list[Move]:
    """A move per scene, none repeating within MOVE_MEMORY of itself.

    `choose_move` takes an `avoid`, but a caller picking scene by scene
    has nothing to pass it, and repetition is exactly what the eye reads
    as automated.
    """
    moves: list[Move] = []
    for index in range(1, count + 1):
        recent = tuple(m.name for m in moves[-MOVE_MEMORY:])
        moves.append(choose_move(index, episode_id, avoid=recent))
    return moves


def frame_count(duration_sec: float, fps: int) -> int:
    """Frames for one clip, rounded independently.

    Use `plan_frames` for a whole episode. Rounding each scene on its own
    loses up to half a frame every time, and at 30fps seven scenes can
    end up 115ms short -- which is more than assemble's whole 50ms
    budget for the episode.
    """
    if duration_sec <= 0:
        raise StageError(f"cannot render a clip of {duration_sec}s")
    return max(2, int(round(duration_sec * fps)))


def plan_frames(durations: list[float], fps: int) -> list[int]:
    """Frame counts for a whole episode, carrying the rounding error.

    Each clip takes the frames its own duration asks for, plus whatever
    the previous clips lost or gained. The total therefore matches the
    episode exactly even though no individual scene lands on a whole
    frame, which is what keeps the 50ms contract from being spent on
    arithmetic.
    """
    if any(d <= 0 for d in durations):
        raise StageError(f"cannot render clips of {durations}")
    counts, elapsed, emitted = [], 0.0, 0
    for duration in durations:
        elapsed += duration
        target = int(round(elapsed * fps))
        count = max(2, target - emitted)
        counts.append(count)
        emitted += count
    return counts


def _ease(progress: str) -> str:
    """Cubic ease-out as a zoompan expression. Matches motion.ease_out."""
    return f"(1-pow(1-{progress},3))"


def filter_chain(move: Move, frames: int, spec: VideoSpec) -> str:
    """The zoompan expression for this move.

    Built as a string rather than composed, because zoompan's own option
    parser treats a comma as a filter separator; every comma inside an
    expression has to be escaped, and doing that in one place is safer
    than doing it at each call site.
    """
    target = int(spec.width * OVERSCAN)
    progress = f"on/{max(1, frames - 1)}"
    eased = _ease(progress)

    if move.zoom_in:
        zoom = f"1+{ZOOM_TRAVEL}*{eased}"
    else:
        zoom = f"{1 + ZOOM_TRAVEL}-{ZOOM_TRAVEL}*{eased}"

    # Slack is whatever the zoomed frame leaves over; panning moves the
    # crop centre across it, eased on the same curve.
    centre_x = "iw/2-(iw/zoom/2)"
    centre_y = "ih/2-(ih/zoom/2)"
    x = centre_x if not move.pan_x else \
        f"{centre_x}+({move.pan_x})*(iw-iw/zoom)/2*{eased}"
    y = centre_y if not move.pan_y else \
        f"{centre_y}+({move.pan_y})*(ih-ih/zoom)/2*{eased}"

    # Crop to the output's aspect BEFORE zoompan. zoompan scales its crop
    # region to `s`, so feeding it a square and asking for 1080x1920
    # stretches the picture instead of framing it -- a circle came out at
    # ratio 0.562, which is exactly 9/16. Cover the target first, then
    # crop to it, then move within that.
    canvas_w = target
    canvas_h = int(round(target * spec.height / spec.width))
    chain = (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase"
        f":flags=lanczos,"
        f"crop={canvas_w}:{canvas_h}:(iw-ow)/2:(ih-oh)*{VERTICAL_CROP_BIAS},"
        f"format=yuv420p,"
        f"zoompan=z='{zoom}':x='{x}':y='{y}'"
        f":d={frames}:s={spec.width}x{spec.height}"
    )
    # Every comma inside an expression must be escaped or zoompan reads it
    # as the start of the next filter. pow(a,b) is the only one here.
    return _escape_expression_commas(chain)


def _escape_expression_commas(chain: str) -> str:
    """Escape commas that sit inside a quoted zoompan expression."""
    out, in_expr = [], False
    for char in chain:
        if char == "'":
            in_expr = not in_expr
            out.append(char)
        elif char == "," and in_expr:
            out.append("\\,")
        else:
            out.append(char)
    return "".join(out)


def measure(path: Path) -> float:
    proc = subprocess.run(
        [ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise StageError(f"could not measure {path.name}: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def render(src: Path, out: Path, duration_sec: float, spec: VideoSpec,
           move: Move | None = None, scene_index: int = 0,
           episode_id: str = "", frames: int | None = None) -> Path:
    """One still, moved across for exactly `duration_sec`.

    Measures the result and refuses it on drift, for the same reason
    assemble does: a clip that is quietly the wrong length moves every
    visual after it.
    """
    if not src.exists():
        raise StageError(f"no source image at {src}")
    log = get(episode_id or None, "kenburns")

    if duration_sec < MIN_MOVE_SEC:
        move = None  # too short to move across; hold it instead
    chosen = move or choose_move(scene_index, episode_id)
    # The caller plans frames across the episode so the rounding error
    # does not accumulate; alone, this clip rounds on its own.
    if frames is None:
        frames = frame_count(duration_sec, spec.fps)

    if duration_sec < MIN_MOVE_SEC:
        chain = (f"scale={spec.width}:{spec.height}"
                 f":force_original_aspect_ratio=increase,"
                 f"crop={spec.width}:{spec.height},format=yuv420p")
    else:
        chain = filter_chain(chosen, frames, spec)

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg(), "-y", "-v", "error",
        "-loop", "1", "-i", str(src),
        "-vf", chain,
        "-frames:v", str(frames),
        "-r", str(spec.fps),
        "-c:v", "libx264", "-crf", str(spec.crf), "-preset", spec.preset,
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600, check=False)
    if proc.returncode != 0:
        raise StageError(
            f"could not render motion for {src.name}: {proc.stderr.strip()}"
        )

    got = measure(out)
    # Against the frames actually asked for, not the raw seconds: a
    # planned clip is deliberately off its own duration to keep the
    # episode total exact.
    drift = abs(got - frames / spec.fps)
    if drift > DURATION_TOLERANCE_SEC:
        raise StageError(
            f"{out.name} came out {got:.3f}s for {frames} frames at "
            f"{spec.fps}fps ({drift * 1000:.0f}ms out). The audio decides "
            f"the length."
        )
    log.debug(f"{out.name}: {chosen.name}, {frames} frames, {got:.3f}s")
    return out
