"""Niche profiles.

The core is topic-agnostic. A profile supplies the visual language, the
script template and the brand -- architecture doc, section 10. Switching
topic costs one new visuals module and nothing else.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

from pipeline.errors import ConfigError

_DIR = Path(__file__).resolve().parent


def available() -> list[str]:
    """Every profile on disk. Discovered, not listed, so a new niche is
    usable the moment its directory exists."""
    return sorted(
        d.name for d in _DIR.iterdir()
        if d.is_dir() and (d / "__init__.py").exists()
    )


def load(name: str) -> ModuleType:
    try:
        return importlib.import_module(f"profiles.{name}")
    except ModuleNotFoundError as exc:
        raise ConfigError(
            f"no profile {name!r}; available: {available()}. A new one "
            f"needs profiles/{name}/ with visuals.py, style.py and script.md"
        ) from exc
