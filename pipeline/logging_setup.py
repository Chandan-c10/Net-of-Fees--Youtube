"""Structured logging. Every line carries episode_id and stage.

Debugging a 2am render failure without those two fields is guesswork
-- architecture doc, section 18.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "episode_id": getattr(record, "episode_id", None),
            "stage": getattr(record, "stage", None),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ep = getattr(record, "episode_id", None) or "-"
        stage = getattr(record, "stage", None) or "-"
        return f"[{ep}/{stage}] {record.levelname:<7} {record.getMessage()}"


def configure(logfile: Path | None = None, verbose: bool = False) -> None:
    root = logging.getLogger("nof")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(HumanFormatter())
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console)

    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)


def get(episode_id: str | None = None, stage: str | None = None) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(
        logging.getLogger("nof"),
        {"episode_id": episode_id, "stage": stage},
    )
