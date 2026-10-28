"""Episode state.

The manifest is the only state a render stage reads. A stage never infers
state from the filesystem, so a crash at stage 7 does not discard 1-6.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ManifestError

# Declared order. `run.py` executes stages in this sequence and refuses to
# run one whose predecessors are incomplete.
STAGES: tuple[str, ...] = (
    "export",
    "script",
    "scrub",
    "voice",
    "charts",
    "captions",
    "assemble",
    "metadata",
    "thumbnail",
    "shorts",
    "upload",
)

# Stages that may not run until the named gate has been cleared.
GATE_1_STAGES = ("voice", "charts", "captions", "assemble",
                 "metadata", "thumbnail")
GATE_2_STAGES = ("shorts", "upload")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StageState:
    done: bool = False
    at: str | None = None
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scene:
    """One scene. The index is the only glue between audio, visuals and rows."""

    index: int
    text: str
    visual_spec: dict[str, Any] = field(default_factory=dict)
    duration_sec: float | None = None
    voice: str | None = None  # overrides the profile default when set
    flagged: bool = False

    @property
    def slug(self) -> str:
        """Zero-padded two-digit index. Every module agrees on this form."""
        return f"scene_{self.index:02d}"


@dataclass
class Manifest:
    id: str
    topic: str = ""
    title: str = ""
    profile: str = "trading"
    series: str = ""
    series_episode: int | None = None
    tier: str = "A"
    recipe: str = ""
    voice: str = "chandan_en"
    scenes: list[Scene] = field(default_factory=list)
    stages: dict[str, StageState] = field(default_factory=dict)
    gate_1_passed_at: str | None = None
    gate_2_passed_at: str | None = None
    approved_at: str | None = None
    published_at: str | None = None
    snapshot_sha256: str | None = None
    platform_posts: list[dict[str, Any]] = field(default_factory=list)

    # ---- construction -------------------------------------------------

    @classmethod
    def new(cls, episode_id: str, **kwargs: Any) -> Manifest:
        m = cls(id=episode_id, **kwargs)
        m.stages = {name: StageState() for name in STAGES}
        return m

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.exists():
            raise ManifestError(f"no manifest at {path}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ManifestError(f"corrupt manifest at {path}: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        if "id" not in raw:
            raise ManifestError("manifest has no id")
        scenes = [Scene(**s) for s in raw.get("scenes", [])]
        stages = {
            name: StageState(**raw.get("stages", {}).get(name, {}))
            for name in STAGES
        }
        known = {f for f in cls.__dataclass_fields__ if f not in ("scenes", "stages")}
        body = {k: v for k, v in raw.items() if k in known}
        m = cls(**body)
        m.scenes = scenes
        m.stages = stages
        m.validate()
        return m

    # ---- persistence --------------------------------------------------

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, sort_keys=False))
        tmp.replace(path)  # atomic: a crash mid-write never truncates state

    def validate(self) -> None:
        """Validated on every read and write. A corrupt manifest blocks resume."""
        if not self.id:
            raise ManifestError("manifest id is empty")
        indices = [s.index for s in self.scenes]
        if len(indices) != len(set(indices)):
            raise ManifestError(f"duplicate scene indices in episode {self.id}")
        if indices and sorted(indices) != list(range(1, len(indices) + 1)):
            raise ManifestError(
                f"scene indices must be 1..n with no gaps, got {sorted(indices)}"
            )
        unknown = set(self.stages) - set(STAGES)
        if unknown:
            raise ManifestError(f"unknown stages: {sorted(unknown)}")

    # ---- stage bookkeeping --------------------------------------------

    def stage(self, name: str) -> StageState:
        if name not in STAGES:
            raise ManifestError(f"unknown stage {name!r}")
        return self.stages.setdefault(name, StageState())

    def is_done(self, name: str) -> bool:
        return self.stage(name).done

    def mark_done(self, name: str, **data: Any) -> None:
        st = self.stage(name)
        st.done = True
        st.at = _utcnow()
        st.error = None
        st.data.update(data)

    def mark_failed(self, name: str, error: str) -> None:
        st = self.stage(name)
        st.done = False
        st.at = _utcnow()
        st.error = error

    def reset(self, name: str) -> None:
        self.stages[name] = StageState()

    def pending(self) -> list[str]:
        return [s for s in STAGES if not self.is_done(s)]

    def unmet_prerequisites(self, name: str) -> list[str]:
        """Stages before `name` that are not done."""
        if name not in STAGES:
            raise ManifestError(f"unknown stage {name!r}")
        return [s for s in STAGES[: STAGES.index(name)] if not self.is_done(s)]

    # ---- gates ---------------------------------------------------------

    def gate_blocking(self, name: str) -> str | None:
        """Which gate, if any, blocks this stage. None means clear to run."""
        if name in GATE_1_STAGES and not self.gate_1_passed_at:
            return "gate_1"
        if name in GATE_2_STAGES and not self.gate_2_passed_at:
            return "gate_2"
        return None

    def pass_gate_1(self) -> None:
        self.gate_1_passed_at = _utcnow()

    def pass_gate_2(self) -> None:
        now = _utcnow()
        self.gate_2_passed_at = now
        self.approved_at = now

    # ---- scenes --------------------------------------------------------

    def scene(self, index: int) -> Scene:
        for s in self.scenes:
            if s.index == index:
                return s
        raise ManifestError(f"episode {self.id} has no scene {index}")

    def total_duration(self) -> float:
        if any(s.duration_sec is None for s in self.scenes):
            missing = [s.index for s in self.scenes if s.duration_sec is None]
            raise ManifestError(f"scenes without a measured duration: {missing}")
        return sum(s.duration_sec for s in self.scenes)  # type: ignore[misc]


# ---- episode directory layout -----------------------------------------


class Episode:
    """Paths for one episode. Every stage resolves files through this."""

    def __init__(self, episode_id: str, base: Path):
        self.id = episode_id
        self.dir = base / episode_id

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    @property
    def input_dir(self) -> Path:
        return self.dir / "input"

    @property
    def snapshot(self) -> Path:
        return self.input_dir / "data.json"

    @property
    def script(self) -> Path:
        return self.dir / "script.md"

    @property
    def audio_dir(self) -> Path:
        return self.dir / "audio"

    @property
    def voice_dir(self) -> Path:
        return self.dir / "voice"

    @property
    def visuals_dir(self) -> Path:
        return self.dir / "visuals"

    @property
    def out_dir(self) -> Path:
        return self.dir / "out"

    @property
    def captions(self) -> Path:
        return self.dir / "captions.srt"

    @property
    def final_audio(self) -> Path:
        return self.audio_dir / "final.wav"

    @property
    def video(self) -> Path:
        return self.out_dir / "video.mp4"

    @property
    def shorts_dir(self) -> Path:
        return self.out_dir / "shorts"

    @property
    def log(self) -> Path:
        return self.dir / "run.log"

    def scene_audio(self, scene: Scene) -> Path:
        return self.audio_dir / f"{scene.slug}.wav"

    def scene_visual(self, scene: Scene) -> Path:
        return self.visuals_dir / f"{scene.slug}.png"

    def thumbnail(self, n: int) -> Path:
        return self.out_dir / f"thumb_{n}.png"

    def ensure_dirs(self) -> None:
        for d in (
            self.dir, self.input_dir, self.audio_dir, self.voice_dir,
            self.visuals_dir, self.out_dir, self.shorts_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> Manifest:
        return Manifest.load(self.manifest_path)

    def save_manifest(self, m: Manifest) -> None:
        m.save(self.manifest_path)
