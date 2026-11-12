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


# ---- a setting put in the other list must still work -----------------
#
# The gates were created as repository *secrets* while the workflows read
# `vars.*`. Nothing failed: the runner simply received an empty string,
# the gate stayed shut, and no video was published for a reason nobody
# could see from the log.


def test_every_variable_falls_back_to_a_secret():
    bare = re.compile(r"\$\{\{\s*vars\.([A-Z0-9_]+)\s*\}\}")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for match in bare.finditer(path.read_text(encoding="utf-8")):
            raise AssertionError(
                f"{path.name} reads vars.{match.group(1)} with no fallback. "
                f"Write `${{{{ vars.X || secrets.X }}}}`, so the setting "
                f"works from whichever list it was put in."
            )


def test_every_secret_falls_back_to_a_variable():
    """The converse, for the same reason.

    SMTP_HOST is not a credential and belongs in the variables list,
    where a workflow reading only `secrets.SMTP_HOST` would never see
    it -- and the symptom is an unattended channel that cannot tell
    anybody anything.
    """
    bare = re.compile(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for match in bare.finditer(path.read_text(encoding="utf-8")):
            raise AssertionError(
                f"{path.name} reads secrets.{match.group(1)} with no "
                f"fallback. Write `${{{{ secrets.X || vars.X }}}}`."
            )


def test_no_step_condition_reads_secrets():
    """GitHub does not give a step `if` the `secrets` context.

    Such a condition is not an error -- it is silently false forever,
    which for the publish step means a channel that never posts.
    """
    pattern = re.compile(r"^\s*if:.*secrets\.", re.MULTILINE)
    for path in sorted(WORKFLOWS.glob("*.yml")):
        found = pattern.findall(path.read_text(encoding="utf-8"))
        assert not found, (
            f"{path.name}: a step `if` cannot see `secrets`; it would be "
            f"false forever. Read the job's `env` instead: {found}"
        )


# ---- a step allowed to fail must be noticed --------------------------
#
# `continue-on-error` is right for a step whose failure should not kill
# the run, and it also paints the step green. The picture library failed
# to download on the first run that got that far; the step reported
# success, the video came out as walls of text, and nothing said why.


def test_a_step_allowed_to_fail_is_watched():
    import yaml

    workflow = yaml.safe_load(
        (WORKFLOWS / "autopilot.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["make"]["steps"]
    body = "\n".join(str(s.get("run", "")) for s in steps)

    for step in steps:
        if not step.get("continue-on-error"):
            continue
        name = step.get("name", "?")
        # The reporting step is the thing doing the watching; it cannot
        # watch itself, and its failure is already visible as no mail.
        if name == "Say what happened":
            continue
        assert step.get("id"), (
            f"{name!r} may fail silently and has no id, so nothing can "
            f"read its outcome"
        )
        assert f"steps.{step['id']}.outcome" in body, (
            f"{name!r} may fail silently and no step reads "
            f"steps.{step['id']}.outcome; a green step that failed is a "
            f"lie told to the only person reading"
        )


# ---- the model needs longer than the client's default ----------------


def test_gemini_is_given_a_deadline_it_can_meet(monkeypatch):
    """Two scheduled runs died on "504 Deadline Exceeded".

    The retry around the script stage reissued the same 60-second
    request three times, so waiting longer was never actually tried.
    """
    import sys
    import types

    from pipeline.stages import script_gen

    seen = {}

    class FakeModel:
        def __init__(self, name):
            seen["model"] = name

        def generate_content(self, prompt, **kwargs):
            seen["kwargs"] = kwargs
            return types.SimpleNamespace(text="# A story\n")

    fake = types.ModuleType("google.generativeai")
    fake.configure = lambda **k: None
    fake.GenerativeModel = FakeModel
    google = types.ModuleType("google")
    google.generativeai = fake
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("GEMINI_TIMEOUT_SEC", raising=False)

    script_gen._ask_gemini("write a story")
    assert seen["kwargs"]["request_options"]["timeout"] == 300, (
        "the call went out on the client's own 60-second deadline"
    )


def test_the_gemini_deadline_is_settable(monkeypatch):
    import sys
    import types

    from pipeline.stages import script_gen

    seen = {}

    class FakeModel:
        def __init__(self, name):
            pass

        def generate_content(self, prompt, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(text="# A story\n")

    fake = types.ModuleType("google.generativeai")
    fake.configure = lambda **k: None
    fake.GenerativeModel = FakeModel
    monkeypatch.setitem(sys.modules, "google.generativeai", fake)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_TIMEOUT_SEC", "90")
    script_gen._ask_gemini("write a story")
    assert seen["request_options"]["timeout"] == 90
