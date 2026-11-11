"""What a runner hands over, and what a runner can actually read.

Both failures here come from the same autopilot run: the first unattended
end-to-end attempt. It died on its first command, and then died a second
time inside the error reporter, which replaced the real message with a
traceback about an SMTP port.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import pipeline
from pipeline import config, notify
from pipeline.recipes import available, get

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


# ---- an unset repository variable arrives as "" ----------------------
#
# GitHub Actions writes the whole `env:` block whether the secrets behind
# it exist or not, so `SMTP_PORT: ${{ secrets.SMTP_PORT }}` with no such
# secret becomes SMTP_PORT="". int("") raises.


def test_blank_variables_are_forgotten(monkeypatch):
    monkeypatch.setenv("NOF_BLANK", "")
    monkeypatch.setenv("NOF_SPACES", "   ")
    monkeypatch.setenv("NOF_REAL", "kept")
    dropped = pipeline.drop_blank_env()
    assert "NOF_BLANK" in dropped and "NOF_SPACES" in dropped
    assert "NOF_REAL" not in dropped
    assert os.getenv("NOF_REAL") == "kept"
    assert os.getenv("NOF_BLANK") is None


def test_blank_variable_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "")
    pipeline.drop_blank_env()
    assert notify.Channel().port == 587


def test_channel_survives_a_nonsense_port(monkeypatch):
    """The reporter must never raise while being built to report."""
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    assert notify.Channel().port == 587


@pytest.mark.parametrize("raw", ["", "  ", "eight", "8.5"])
def test_env_int_falls_back(monkeypatch, raw):
    monkeypatch.setenv("NOF_NUMBER", raw)
    assert config.env_int("NOF_NUMBER", 42) == 42


def test_env_int_reads_a_real_value(monkeypatch):
    monkeypatch.setenv("NOF_NUMBER", " 8 ")
    assert config.env_int("NOF_NUMBER", 42) == 8


@pytest.mark.parametrize("raw", ["", "fast"])
def test_env_float_falls_back(monkeypatch, raw):
    monkeypatch.setenv("NOF_RATE", raw)
    assert config.env_float("NOF_RATE", 1.5) == 1.5


# ---- a runner has the repository and nothing else --------------------


def test_every_recipe_declares_whether_it_needs_local_data():
    for name in available():
        assert isinstance(get(name).needs_local_data, bool), name


def test_the_story_recipe_reads_nothing():
    data = get("story").export()
    assert data and data["kind"] == "story"
    assert not get("story").needs_local_data


def _default_recipes() -> dict[str, str]:
    """Each workflow's default recipe, as the YAML spells it."""
    found = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"--recipe\s+(?:\"?\$\{\{[^}]*\|\|\s*'([a-z_]+)'"
                                 r"[^}]*\}\}\"?|([a-z_]+))", text):
            found[path.name] = match.group(1) or match.group(2)
    return found


def test_a_workflow_defaults_to_a_recipe_a_fresh_clone_can_run():
    """The bug this file is named for.

    `autopilot.yml` named `incident_log`, whose data file is gitignored,
    so the very first command of every unattended run raised before the
    story was written. Nothing caught it because nothing asked whether a
    runner could read what the recipe reads.
    """
    defaults = _default_recipes()
    assert defaults, "no workflow names a recipe; the pattern must have changed"
    for workflow, recipe in defaults.items():
        assert recipe in available(), f"{workflow}: unknown recipe {recipe!r}"
        assert not get(recipe).needs_local_data, (
            f"{workflow} defaults to {recipe!r}, which reads a file that is "
            f"not in git. A runner checks out the repository and nothing "
            f"else, so that run fails before it writes a word."
        )
