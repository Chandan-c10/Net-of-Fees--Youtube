"""Niche profiles.

The core is topic-agnostic. A profile supplies the visual language, the
script template and the brand -- architecture doc, section 10. Switching
topic costs one new visuals module and nothing else.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from pipeline.errors import ConfigError


def load(name: str) -> ModuleType:
    try:
        return importlib.import_module(f"profiles.{name}")
    except ModuleNotFoundError as exc:
        raise ConfigError(
            f"no profile {name!r}. Expected profiles/{name}/ with "
            f"visuals.py, style.py and script.md"
        ) from exc
