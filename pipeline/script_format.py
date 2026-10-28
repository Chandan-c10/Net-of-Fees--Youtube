"""The script.md format: the contract between script_gen and every stage after it.

A scene header looks like:

    [SCENE 1 | chart: fee_stack | voice: chandan_en]

`chart` names a visual spec key the profile must implement. `voice`
overrides the episode default. Both are optional; a scene with no chart
renders the profile's title card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ValidationError
from .manifest import Scene

HEADER = re.compile(r"^\[SCENE\s+(\d+)\s*(?:\|(?P<attrs>[^\]]*))?\]\s*$", re.IGNORECASE)
_ATTR = re.compile(r"(?P<key>[a-z_]+)\s*:\s*(?P<value>[^|]+)", re.IGNORECASE)

KNOWN_ATTRS = frozenset({"chart", "voice", "type", "annotate"})


@dataclass
class ParsedScene:
    index: int
    text: str
    attrs: dict[str, str]


def parse_attrs(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    attrs: dict[str, str] = {}
    for m in _ATTR.finditer(raw):
        key = m.group("key").strip().lower()
        value = m.group("value").strip()
        if key not in KNOWN_ATTRS:
            raise ValidationError(
                f"unknown scene attribute {key!r}; known: {sorted(KNOWN_ATTRS)}"
            )
        attrs[key] = value
    return attrs


def parse(text: str) -> list[ParsedScene]:
    """Split a script into scenes. Raises on anything ambiguous."""
    scenes: list[ParsedScene] = []
    current: ParsedScene | None = None
    body: list[str] = []
    preamble: list[str] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        m = HEADER.match(line.strip())
        if m:
            if current is not None:
                current.text = "\n".join(body).strip()
                scenes.append(current)
            try:
                attrs = parse_attrs(m.group("attrs"))
            except ValidationError as exc:
                raise ValidationError(f"line {lineno}: {exc}") from exc
            current = ParsedScene(index=int(m.group(1)), text="", attrs=attrs)
            body = []
        elif current is None:
            if line.strip():
                preamble.append(line)
        else:
            body.append(line)

    if current is not None:
        current.text = "\n".join(body).strip()
        scenes.append(current)

    if not scenes:
        raise ValidationError(
            "script contains no [SCENE n] headers. "
            "Every narrated line must sit under a scene header."
        )

    indices = [s.index for s in scenes]
    if indices != sorted(indices):
        raise ValidationError(f"scene headers are out of order: {indices}")
    if len(indices) != len(set(indices)):
        dupes = [i for i in indices if indices.count(i) > 1]
        raise ValidationError(f"duplicate scene numbers: {sorted(set(dupes))}")
    if indices != list(range(1, len(indices) + 1)):
        raise ValidationError(
            f"scene numbers must run 1..{len(indices)} with no gaps, got {indices}"
        )
    for s in scenes:
        if not s.text.strip():
            raise ValidationError(f"scene {s.index} has no narration text")

    return scenes


def to_scenes(parsed: list[ParsedScene], default_voice: str | None = None) -> list[Scene]:
    out: list[Scene] = []
    for p in parsed:
        spec: dict[str, object] = {}
        if "chart" in p.attrs:
            spec["data_key"] = p.attrs["chart"]
        if "type" in p.attrs:
            spec["type"] = p.attrs["type"]
        if "annotate" in p.attrs:
            spec["annotate"] = [a.strip() for a in p.attrs["annotate"].split(",")]
        out.append(
            Scene(
                index=p.index,
                text=p.text,
                visual_spec=spec,
                voice=p.attrs.get("voice", default_voice),
            )
        )
    return out


def render(scenes: list[Scene], title: str) -> str:
    """Write scenes back out as script.md, so a round trip is lossless."""
    lines = [f"# {title}", ""]
    for s in scenes:
        attrs = []
        if s.visual_spec.get("data_key"):
            attrs.append(f"chart: {s.visual_spec['data_key']}")
        if s.visual_spec.get("type"):
            attrs.append(f"type: {s.visual_spec['type']}")
        if s.visual_spec.get("annotate"):
            attrs.append("annotate: " + ", ".join(s.visual_spec["annotate"]))
        if s.voice:
            attrs.append(f"voice: {s.voice}")
        header = f"[SCENE {s.index}"
        if attrs:
            header += " | " + " | ".join(attrs)
        header += "]"
        lines += [header, "", s.text, ""]
    return "\n".join(lines).rstrip() + "\n"


def validate_against_snapshot(scenes: list[Scene], snapshot: dict) -> None:
    """Every data_key a scene references must exist in the snapshot.

    This is the check that catches a scene the model invented. It returns
    the equivalent of HTTP 422 at Gate 1, before any render -- architecture
    doc, section 21.
    """
    missing: list[str] = []
    for s in scenes:
        key = s.visual_spec.get("data_key")
        if key and key not in snapshot and key not in _derived_keys(snapshot):
            missing.append(f"scene {s.index} -> {key!r}")
    if missing:
        available = sorted(set(snapshot) | _derived_keys(snapshot))
        raise ValidationError(
            "scenes reference data keys absent from the snapshot: "
            + "; ".join(missing)
            + f"\n  available keys: {available}"
        )


def _derived_keys(snapshot: dict) -> set[str]:
    """Named views a profile may render even though they are not top-level keys."""
    derived = set()
    if "charges" in snapshot:
        derived |= {"fee_stack", "gross_vs_net"}
    if "ladder" in snapshot:
        derived |= {"drag_ladder", "breakeven"}
    return derived
