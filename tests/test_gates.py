"""Both gates, enforced in code rather than displayed in a UI."""

from __future__ import annotations

import pytest

import run
from pipeline import gates
from pipeline.errors import ConfigError, GateError
from pipeline.manifest import GATE_1_STAGES, GATE_2_STAGES
from pipeline.stages.upload import assert_publishable


def test_gate_1_blocks_every_render_stage(manifest):
    for stage in GATE_1_STAGES:
        assert manifest.gate_blocking(stage) == "gate_1"


def test_gate_1_cleared_unblocks_render(manifest):
    manifest.pass_gate_1()
    for stage in GATE_1_STAGES:
        assert manifest.gate_blocking(stage) is None


def test_gate_2_blocks_shorts_and_upload(manifest):
    manifest.pass_gate_1()
    for stage in GATE_2_STAGES:
        assert manifest.gate_blocking(stage) == "gate_2"


def test_upload_refuses_null_approved_at(manifest):
    manifest.pass_gate_1()
    with pytest.raises(GateError, match="approved_at"):
        assert_publishable(manifest)


def test_upload_refuses_draft_voice(manifest):
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=True)
    with pytest.raises(GateError, match="draft"):
        assert_publishable(manifest)


def test_upload_refuses_scrub_findings(manifest):
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=2)
    with pytest.raises(GateError, match="scrub"):
        assert_publishable(manifest)


def test_upload_allows_a_fully_approved_episode(manifest):
    manifest.pass_gate_1()
    manifest.pass_gate_2()
    manifest.mark_done("assemble")
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    assert_publishable(manifest)  # must not raise


def test_there_is_no_gate_skip_flag():
    """A --skip-gates flag would violate the architecture doc, section 13."""
    import run
    parser_src = __import__("inspect").getsource(run.main)
    assert "skip-gate" not in parser_src
    assert "skip_gate" not in parser_src


# ---- episode id validation -------------------------------------------
# A scheduled run supplies the id from a workflow input, so it reaches a
# filesystem path from outside the repo.

@pytest.mark.parametrize("bad", [
    "../etc", "a/b", "a\\b", "with space", "x$y", "", "." , "..",
    "x" * 33, "-leading-dash",
])
def test_a_dangerous_episode_id_is_refused(bad):
    with pytest.raises(ConfigError):
        run._episode(bad)


@pytest.mark.parametrize("good", ["001", "20261028-am", "ep_7", "A1"])
def test_a_reasonable_episode_id_is_accepted(good):
    assert run._episode(good).dir.name == good


# ---- a timer is not a person ------------------------------------------
# The cloud workflows clear gates, so the code must know the difference
# between a human pressing "Run workflow" and a cron firing at 02:00.


@pytest.mark.parametrize("event", ["schedule", "repository_dispatch"])
def test_an_unattended_ci_event_cannot_clear_a_gate(event, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    with pytest.raises(GateError, match="human decision"):
        gates.assert_a_human_is_asking()


@pytest.mark.parametrize("event", ["workflow_dispatch", "push", ""])
def test_a_human_triggered_event_may_clear_a_gate(event, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    gates.assert_a_human_is_asking()


def test_no_ci_at_all_may_clear_a_gate(monkeypatch):
    """A laptop has no GITHUB_EVENT_NAME."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    gates.assert_a_human_is_asking()


def test_gate_1_refuses_on_a_schedule_even_when_otherwise_clear(
        episode, manifest, monkeypatch):
    """The check lives where the gate is cleared, not only in the workflow
    file, because a workflow file is something someone can edit."""
    import json
    episode.snapshot.write_text(json.dumps({"recipe": "fee_drag"}),
                                encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    with pytest.raises(GateError, match="human decision"):
        gates.approve_gate_1(episode, manifest)
    assert manifest.gate_1_passed_at is None


def test_gate_2_refuses_on_a_schedule(episode, manifest, monkeypatch):
    manifest.pass_gate_1()
    for stage in ("assemble", "metadata", "thumbnail"):
        manifest.mark_done(stage)
    manifest.mark_done("voice", draft=False)
    manifest.mark_done("scrub", findings=0)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    with pytest.raises(GateError, match="human decision"):
        gates.approve_gate_2(episode, manifest)
    assert manifest.gate_2_passed_at is None


def test_the_scheduled_workflow_never_runs_an_approve_command():
    """draft.yml runs on a schedule, so no step of it may approve.

    Matching executed lines, not any mention: the mail it sends quotes the
    approve command to tell the operator what to run next.
    """
    import re
    from pathlib import Path
    draft = Path(".github/workflows/draft.yml")
    if not draft.exists():
        pytest.skip("workflow not in this checkout")
        return
    executed = re.compile(r"^\s*python\s+run\.py\b.*--approve", re.MULTILINE)
    found = executed.findall(draft.read_text(encoding="utf-8"))
    assert not found, f"the scheduled workflow approves something: {found}"


@pytest.mark.parametrize("name", ["render.yml", "publish.yml"])
def test_the_gate_workflows_are_manual_only(name):
    from pathlib import Path

    import yaml
    path = Path(".github/workflows") / name
    if not path.exists():
        pytest.skip("workflow not in this checkout")
        return
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = set(spec.get(True, spec.get("on", {})))
    assert triggers == {"workflow_dispatch"}, \
        f"{name} clears a gate, so it must be manual only; got {triggers}"
