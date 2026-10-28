"""Voice engines behind one interface.

`ClonedVoice` is primary and `KokoroVoice` the fallback. `SilentVoice`
exists so the pipeline can be exercised end to end with no model
installed; audio it produces is marked draft and can never be published.
"""

from __future__ import annotations

from .base import VoiceEngine, VoiceResult, apply_lexicon, load_lexicon
from .silent import SilentVoice

__all__ = [
    "ENGINES",
    "SilentVoice",
    "VoiceEngine",
    "VoiceResult",
    "apply_lexicon",
    "get_engine",
    "load_lexicon",
]

ENGINES = ("silent", "kokoro", "cloned")


def get_engine(name: str, **kwargs: object) -> VoiceEngine:
    """Resolve an engine by name, importing heavy deps only when used."""
    if name == "silent":
        return SilentVoice(**kwargs)  # type: ignore[arg-type]
    if name == "kokoro":
        from .kokoro import KokoroVoice
        return KokoroVoice(**kwargs)  # type: ignore[arg-type]
    if name == "cloned":
        from .cloned import ClonedVoice
        return ClonedVoice(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown voice engine {name!r}; expected one of {ENGINES}")
