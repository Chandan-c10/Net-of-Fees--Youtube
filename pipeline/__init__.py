"""Net of Fees — automated video pipeline.

The render path is deliberately offline: once a snapshot and script exist,
no stage between scrub and assemble touches the network.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path | None = None) -> list[str]:
    """Put `.env` into the environment. Returns the names it set.

    Every module reads its settings with `os.getenv`, and nothing was
    putting `.env` there -- so the panel wrote a file that nothing read,
    and saving a credential did nothing at all. Found during setup, when
    four correct R2 values still reported "not configured".

    Loaded here, in the package, so it happens however the pipeline is
    entered: the CLI, the panel, a test, or a one-line import.

    **A real environment variable always wins.** A runner is configured
    by its secrets, and a stale `.env` that happened to be checked out
    beside the code must never quietly override them.
    """
    path = path or ENV_FILE
    if not path.exists():
        return []
    applied: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or name in os.environ:
            continue
        value = value.strip()
        # Quotes are how you write a value with spaces in it; they are
        # not part of the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[name] = value
        applied.append(name)
    return applied


load_env()
