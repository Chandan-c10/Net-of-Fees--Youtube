"""The two human gates, in one place.

These checks used to live in run.py, which was fine while the CLI was the
only caller. The dashboard is a second caller, and a gate implemented
twice is a gate that will eventually disagree with itself -- the same
reason the upload stage re-checks `approved_at` instead of trusting the
frontend (architecture doc, section 13).

So: one implementation, no `force` parameter, and deliberately no way to
pass a gate without satisfying it.
"""

from __future__ import annotations

import json
import os

from .errors import GateError
from .logging_setup import get
from .manifest import Episode, Manifest

GATE_2_REQUIRES = ("assemble", "metadata", "thumbnail")

# CI events that are nobody's decision. A workflow can be written to
# approve only on workflow_dispatch, but a workflow is a file someone can
# edit; this is the same check in the place that actually clears the gate,
# for the same reason the upload stage re-checks approved_at rather than
# trusting a frontend.
UNATTENDED_EVENTS = ("schedule", "repository_dispatch")


def _unattended() -> str | None:
    """The CI event in progress, when it is one no human triggered."""
    event = os.getenv("GITHUB_EVENT_NAME", "")
    return event if event in UNATTENDED_EVENTS else None


def assert_a_human_is_asking() -> None:
    """Refuse to clear a gate on behalf of a timer.

    A human running `--approve-script` in a terminal, in the dashboard, or
    by pressing "Run workflow" has decided something. A cron firing at
    02:00 has not, and must not be able to reach this even if a workflow
    file is edited to try.
    """
    event = _unattended()
    if event:
        raise GateError(
            f"refusing to clear a gate during a {event!r} run: a gate is a "
            "human decision. Trigger the workflow manually instead "
            "(workflow_dispatch), which records that you asked."
        )


def gate_1_blockers(ep: Episode, m: Manifest) -> list[str]:
    """Every reason Gate 1 cannot be cleared. Empty means it can.

    Separate from `approve_gate_1` so a UI can show what is outstanding
    without attempting the approval.
    """
    out: list[str] = []
    if not m.is_done("script"):
        out.append("the script stage is not complete")
    if not m.is_done("scrub"):
        out.append("the scrub stage has not passed")
    flagged = [s.index for s in m.scenes if s.flagged]
    if flagged:
        out.append(f"scenes still flagged: {flagged}")
    if not ep.snapshot.exists():
        out.append("the data snapshot is missing")
        return out

    # The script must still match the numbers it was written from.
    try:
        import profiles

        from .script_format import validate_against_snapshot
        data = json.loads(ep.snapshot.read_text(encoding="utf-8"))
        validate_against_snapshot(m.scenes, data, profiles.load(m.profile))
    except Exception as exc:
        out.append(f"script does not match the data: {exc}")
    return out


def gate_2_blockers(m: Manifest) -> list[str]:
    """Every reason Gate 2 cannot be cleared. Empty means it can."""
    out: list[str] = []
    for required in GATE_2_REQUIRES:
        if not m.is_done(required):
            out.append(f"{required} is not complete")
    if not m.gate_1_passed_at:
        out.append("gate 1 was never cleared")
    if m.stage("scrub").data.get("findings"):
        out.append("scrub reported findings")
    if m.stage("voice").data.get("draft"):
        out.append(
            "this episode was narrated with the draft voice engine. "
            "Re-run `--stage voice --force` with a real engine before approving."
        )
    return out


def approve_gate_1(ep: Episode, m: Manifest) -> None:
    """Clear Gate 1, or raise with the first reason it cannot be cleared."""
    assert_a_human_is_asking()
    blockers = gate_1_blockers(ep, m)
    if blockers:
        raise GateError("; ".join(blockers))
    m.pass_gate_1()
    ep.save_manifest(m)
    get(m.id, "gate").info("gate 1 cleared")


def approve_gate_2(ep: Episode, m: Manifest) -> None:
    """Clear Gate 2, or raise with the first reason it cannot be cleared."""
    assert_a_human_is_asking()
    blockers = gate_2_blockers(m)
    if blockers:
        raise GateError("; ".join(blockers))
    m.pass_gate_2()
    ep.save_manifest(m)
    get(m.id, "gate").info("gate 2 cleared")
