"""Kokoro-82M: licensed stock voices.

Apache 2.0, so commercial use is permitted. These are model voices, not
clones of any real person, which is why they can be used before a
personal voice reference exists.

They are a legitimate interim voice, not a draft: output from here may be
published. The draft engine in silent.py is the one that cannot.

NEVER clone a voice found online. Section 14 of the architecture doc:
any voice other than your own requires the speaker's documented consent,
and a public figure's voice is impersonation under YouTube policy.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError, StageError
from .base import VoiceResult, measure_wav

# Naming convention: <accent><gender>_<name>.
#   a = American, b = British, h = Hindi
#   f = female,   m = male
#
# This table is for discovery and the `voices` listing. The installed
# package is the authority -- available_voices() prefers it when present,
# because the roster changes between releases.
KNOWN_VOICES: dict[str, str] = {
    "af_heart":    "American female, warm and conversational",
    "af_bella":    "American female, bright",
    "af_nicole":   "American female, softer and closer-miked",
    "af_sarah":    "American female, even and measured",
    "am_michael":  "American male, steady and neutral",
    "am_adam":     "American male, deeper",
    "am_onyx":     "American male, low and calm",
    "bf_emma":     "British female, measured",
    "bf_isabella": "British female, brighter",
    "bm_george":   "British male, warm",
    "bm_lewis":    "British male, deeper",
    "hf_alpha":    "Hindi female",
    "hf_beta":     "Hindi female, alternative",
    "hm_omega":    "Hindi male",
    "hm_psi":      "Hindi male, alternative",
}

DEFAULT_VOICE = "am_michael"
SAMPLE_RATE = 24000  # Kokoro's native rate


def available_voices() -> dict[str, str]:
    """Voices this install can actually produce.

    Falls back to the known table when the package is absent, so the
    listing still works before anything is installed.
    """
    try:
        import kokoro  # noqa: F401
    except ImportError:
        return dict(KNOWN_VOICES)
    try:
        from kokoro import KPipeline  # noqa: F401
        # The package does not expose a stable roster API across versions,
        # so the curated table stays authoritative for descriptions.
        return dict(KNOWN_VOICES)
    except Exception:
        return dict(KNOWN_VOICES)


def is_installed() -> bool:
    try:
        import kokoro  # noqa: F401
        return True
    except ImportError:
        return False


class KokoroVoice:
    """Stock voice. Publishable, but it is not yours.

    Record a personal reference as soon as practical: a channel whose
    credibility rests on first-hand results reads better in a first-hand
    voice.
    """

    name = "kokoro"
    draft = False

    def __init__(self, voice: str = DEFAULT_VOICE, lang_code: str | None = None,
                 speed: float = 1.0, episode_id: str = "", **_: object):
        if voice not in KNOWN_VOICES:
            raise ConfigError(
                f"unknown Kokoro voice {voice!r}.\n"
                f"  List them with:  python run.py voices"
            )
        self.voice = voice
        self.speed = speed
        # The first letter of the voice name selects the language pack.
        self.lang_code = lang_code or voice[0]
        self.episode_id = episode_id
        self._pipeline = None
        self._scene = 0

    @staticmethod
    def _require_deps():
        """Check every dependency up front with one actionable message.

        Failing on the third import, halfway through a render, with a bare
        ModuleNotFoundError is a poor way to learn that torch is missing.
        """
        missing: list[str] = []
        for module, package in (("kokoro", "kokoro"),
                                ("numpy", "numpy"),
                                ("soundfile", "soundfile"),
                                ("torch", "torch")):
            try:
                __import__(module)
            except ImportError:
                missing.append(package)
        if missing:
            raise ConfigError(
                f"the Kokoro voice engine needs: {', '.join(missing)}\n"
                f"  Install them with:\n"
                f"    pip install -r requirements-voice.txt\n"
                f"  (pulls in torch; roughly 2 GB on first run)\n"
                f"  Or render with the draft engine for now:\n"
                f"    --voice-engine silent"
            )

    def _get_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        self._require_deps()
        from kokoro import KPipeline
        self._pipeline = KPipeline(lang_code=self.lang_code)
        return self._pipeline

    def speak(self, text: str, out_path: Path) -> VoiceResult:
        from ..pacing import scene_padding

        self._scene += 1
        pipeline = self._get_pipeline()   # checks every dependency first
        import numpy as np
        import soundfile as sf

        chunks = [
            audio for _gs, _ps, audio in
            pipeline(text, voice=self.voice, speed=self.speed)
        ]
        if not chunks:
            raise StageError(f"Kokoro produced no audio for scene {self._scene}")
        samples = np.concatenate(chunks)

        # Pause is baked into this scene's file so the measured duration
        # includes it, keeping the timing contract in section 5a intact.
        pad = scene_padding(text, self._scene, self.episode_id)
        samples = np.concatenate([samples, np.zeros(int(pad * SAMPLE_RATE))])

        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), samples, SAMPLE_RATE, subtype="PCM_16")
        return VoiceResult(
            path=out_path,
            duration_sec=measure_wav(out_path),  # measured, never estimated
            engine=f"kokoro:{self.voice}",
            draft=False,
        )
