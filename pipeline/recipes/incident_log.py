"""Engineering recipe: what broke in a live system, and how long it hid.

Tier A. Built from the owner's own incident log, which is a record of
events rather than market data, so it carries no licensing exposure and
needs no trading-system stability -- architecture doc, section 11.

The recipe never invents an incident. It reads a log the owner maintains
and computes statistics over it. If the log is missing, the episode fails
rather than fabricating content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import StageError
from . import register

LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "incidents.json"

SEVERITIES = ("cosmetic", "degraded", "wrong_output", "money_at_risk")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class IncidentLogRecipe:
    name: str = "incident_log"
    tier: str = "A"
    title_template: str = "8 bugs. My tests caught one."
    scenarios: list = field(default_factory=list)

    def export(self, **params: object) -> dict:
        path = Path(str(params.get("log", LOG_PATH)))
        if not path.exists():
            raise StageError(
                f"no incident log at {path}.\n"
                f"  This recipe reports real incidents and will not invent any.\n"
                f"  Copy data/incidents.example.json to {path.name} and fill it in."
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StageError(f"incident log is not valid JSON: {exc}") from exc

        incidents = raw.get("incidents") or []
        if len(incidents) < 3:
            raise StageError(
                f"only {len(incidents)} incidents logged. Fewer than 3 is not "
                f"a pattern worth publishing."
            )

        for i, inc in enumerate(incidents):
            for required in ("id", "title", "layer", "severity", "days_hidden"):
                if required not in inc:
                    raise StageError(f"incident {i} is missing {required!r}")
            if inc["severity"] not in SEVERITIES:
                raise StageError(
                    f"incident {inc['id']}: severity {inc['severity']!r} is not "
                    f"one of {SEVERITIES}"
                )

        by_layer: dict[str, int] = {}
        by_severity: dict[str, int] = {s: 0 for s in SEVERITIES}
        by_detection: dict[str, int] = {}
        for inc in incidents:
            by_layer[inc["layer"]] = by_layer.get(inc["layer"], 0) + 1
            by_severity[inc["severity"]] += 1
            how = inc.get("found_by", "unknown")
            by_detection[how] = by_detection.get(how, 0) + 1

        hidden = sorted(int(i["days_hidden"]) for i in incidents)
        mid = len(hidden) // 2
        median_hidden = (
            hidden[mid] if len(hidden) % 2 else (hidden[mid - 1] + hidden[mid]) / 2
        )

        serious = [i for i in incidents
                   if SEVERITY_RANK[i["severity"]] >= SEVERITY_RANK["wrong_output"]]
        caught_by_tests = sum(
            1 for i in incidents if i.get("found_by") == "test"
        )

        worst = max(incidents, key=lambda i: (SEVERITY_RANK[i["severity"]],
                                              int(i["days_hidden"])))

        return {
            "recipe": self.name,
            "series": "It Worked in Backtest",
            "tier": self.tier,
            "system": raw.get("system", "the trading system"),
            "period": raw.get("period", ""),
            "total": len(incidents),
            "serious": len(serious),
            "median_days_hidden": median_hidden,
            "max_days_hidden": hidden[-1],
            "caught_by_tests": caught_by_tests,
            "caught_by_tests_pct": round(100 * caught_by_tests / len(incidents), 1),
            "by_layer": dict(sorted(by_layer.items(), key=lambda kv: -kv[1])),
            "by_severity": by_severity,
            "by_detection": dict(sorted(by_detection.items(), key=lambda kv: -kv[1])),
            "days_hidden": hidden,
            "worst": {
                "title": worst["title"],
                "layer": worst["layer"],
                "severity": worst["severity"],
                "days_hidden": int(worst["days_hidden"]),
            },
            "incidents": incidents,
            "verify_before_publish": [
                "Confirm no incident description names a broker account or order id",
                "Confirm every claim matches the commit or log it came from",
            ],
        }


register(IncidentLogRecipe())
