"""Both gates, enforced in code rather than displayed in a UI."""

from __future__ import annotations

import pytest

from pipeline.errors import GateError
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
