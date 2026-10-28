"""Titles, descriptions and hashtags.

These decide whether anyone watches at all, so they matter more to the
outcome than anything in the render path -- architecture doc, section 26.

Candidates are generated; the operator chooses. A model is least reliable
at exactly this judgment, because it depends on what the audience has
already seen this week.

Nothing here manufactures virality. What it does enforce are the rules
that correlate with clicks: lead with a specific number, open a curiosity
gap the video actually closes, and front-load the searchable words.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from .export import load_snapshot

TITLE_MAX = 60          # longer truncates in search and on mobile
IG_CAPTION_MAX = 2200
SHORTS_TITLE_MAX = 45

BANNED = (
    "you won't believe", "shocking", "secret", "nobody tells you",
    "this one trick", "gone wrong", "must watch", "insane",
)
# Advisory language is a legal boundary, not a style preference.
ADVISORY = (
    "buy ", "sell ", "should invest", "will make you", "guaranteed",
    "target", "multibagger", "tip", "call ",
)


@dataclass
class TitleCandidate:
    text: str
    why: str
    length: int = 0
    has_number: bool = False
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.length = len(self.text)
        self.has_number = any(c.isdigit() for c in self.text)
        if self.length > TITLE_MAX:
            self.warnings.append(f"{self.length} chars; truncates past {TITLE_MAX}")
        if not self.has_number:
            self.warnings.append("no number; a concrete figure earns more clicks")
        low = self.text.lower()
        for phrase in BANNED:
            if phrase in low:
                self.warnings.append(f"clickbait phrase: {phrase!r}")
        for phrase in ADVISORY:
            if phrase in low:
                self.warnings.append(f"ADVISORY LANGUAGE: {phrase!r} -- not allowed")


def _titles(data: dict, m: Manifest) -> list[TitleCandidate]:
    """Build candidates from figures in the snapshot. Never invents one."""
    out: list[TitleCandidate] = []
    recipe = data.get("recipe")

    if recipe == "incident_log":
        out += [
            TitleCandidate(
                f"{data['total']} bugs. My tests caught {data['caught_by_tests']}.",
                "the gap between what you test and what ships",
            ),
            TitleCandidate(
                f"My trading bot was wrong for {data['median_days_hidden']:.0f} days",
                "duration is more alarming than count",
            ),
            TitleCandidate(
                f"{data['serious']} of {data['total']} bugs risked real money",
                "stakes stated plainly",
            ),
        ]
    elif recipe == "fee_drag":
        out += [
            TitleCandidate(
                f"{data['breakeven_points']} points before you earn anything",
                "the counterintuitive threshold",
            ),
            TitleCandidate(
                f"Charges took {data['drag_pct']}% of this trade",
                "a share, not an amount; shares travel better",
            ),
            TitleCandidate(
                f"Why a small win is mostly fees",
                "the principle, for search traffic",
            ),
        ]
    else:
        out.append(TitleCandidate(m.title or data.get("title", ""), "fallback"))
    return out


def _description(data: dict, m: Manifest, profile, hook: str) -> str:
    parts = [hook, ""]
    if m.series:
        n = f" (episode {m.series_episode})" if m.series_episode else ""
        parts += [f"Part of the series: {m.series}{n}", ""]

    covered = [s.text.split(".")[0].strip() for s in m.scenes[1:5]]
    if covered:
        parts.append("In this episode:")
        parts += [f"- {c}" for c in covered if c]
        parts.append("")

    if data.get("verify_before_publish"):
        parts += [
            "Figures come from my own logs and the published schedules at "
            "time of recording. Your own numbers will differ.",
            "",
        ]
    parts += [profile.style.DISCLAIMER, ""]
    parts.append(" ".join(hashtags(data, profile)))
    return "\n".join(parts)


def hashtags(data: dict, profile, limit: int = 12) -> list[str]:
    """Profile tags plus any the data earns. Order matters on Instagram."""
    tags = list(getattr(profile, "HASHTAGS", ()))
    recipe = data.get("recipe")
    if recipe == "incident_log":
        tags = ["#postmortem", "#debugging"] + tags
    elif recipe == "fee_drag":
        tags = ["#tradingcosts", "#optionstrading"] + tags
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


def _instagram_caption(hook: str, data: dict, profile) -> str:
    """Links are not clickable in Reels captions, so the CTA points to bio."""
    tags = hashtags(data, profile, limit=10)
    body = [hook, "", "Full breakdown on YouTube - link in bio", "", " ".join(tags)]
    caption = "\n".join(body)
    if len(caption) > IG_CAPTION_MAX:
        caption = caption[: IG_CAPTION_MAX - 3] + "..."
    return caption


def run(ep: Episode, m: Manifest, profile) -> dict:
    log = get(m.id, "metadata")
    if not m.scenes:
        raise StageError("no scenes; run the script stage first")
    data = load_snapshot(ep)

    candidates = _titles(data, m)
    blocking = [w for c in candidates for w in c.warnings if "ADVISORY" in w]
    if blocking:
        raise StageError("title candidates contain advisory language: " + "; ".join(blocking))

    hook = m.scenes[0].text.split(".")[0].strip() + "."
    chosen = next((c for c in candidates if not c.warnings), candidates[0])

    payload = {
        "chosen_title": chosen.text,
        "title_candidates": [asdict(c) for c in candidates],
        "youtube": {
            "title": chosen.text[:100],
            "description": _description(data, m, profile, hook),
            "tags": [t.lstrip("#") for t in hashtags(data, profile)],
        },
        "shorts": {
            "title": chosen.text[:SHORTS_TITLE_MAX],
            "description": " ".join(hashtags(data, profile, limit=6)),
        },
        "instagram": {"caption": _instagram_caption(hook, data, profile)},
    }
    path = ep.dir / "metadata.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    m.title = chosen.text
    m.mark_done("metadata", title=chosen.text,
                candidates=len(candidates),
                warnings=sum(len(c.warnings) for c in candidates))
    log.info(f"title: {chosen.text!r} ({chosen.length} chars)")
    return payload
