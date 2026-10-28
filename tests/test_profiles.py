"""Structural guarantees every profile must meet.

The engineering profile shipped without a vertical sibling, so Shorts
failed at render time with a missing-module error rather than at test
time. These assertions close that class of gap for every future profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import profiles
from pipeline.config import LONGFORM, SHORTS
from pipeline.manifest import Scene

ROOT = Path(__file__).resolve().parent.parent
ALL_PROFILES = sorted(
    d.name for d in (ROOT / "profiles").iterdir()
    if d.is_dir() and (d / "__init__.py").exists()
)


def test_profiles_are_discovered():
    assert ALL_PROFILES, "no profiles found"
    assert "trading" in ALL_PROFILES
    assert "engineering" in ALL_PROFILES


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_profile_has_the_required_interface(name):
    p = profiles.load(name)
    for attr in ("PROFILE_NAME", "DEFAULT_VOICE", "style", "visuals"):
        assert hasattr(p, attr), f"{name} is missing {attr}"
    for fn in ("build_visual", "supports", "available_keys"):
        assert callable(getattr(p.visuals, fn)), f"{name}.visuals lacks {fn}"
    assert p.visuals.available_keys(), f"{name} implements no charts"


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_profile_supports_exactly_one_aspect(name):
    p = profiles.load(name)
    supported = [a for a in ("16:9", "9:16") if p.visuals.supports(a)]
    assert len(supported) == 1, (
        f"{name} claims {supported}; a profile renders one aspect so that "
        f"type scale and data density can be tuned for it"
    )


@pytest.mark.parametrize("name", [n for n in ALL_PROFILES if not n.endswith("_shorts")])
def test_every_longform_profile_has_a_vertical_sibling(name):
    """Shorts are cut from long-form, so every long-form profile needs one.

    Without this, Shorts fail at render time with a missing-module error.
    """
    p = profiles.load(name)
    if p.visuals.supports("9:16"):
        return
    sibling = f"{name}_shorts"
    assert sibling in ALL_PROFILES, (
        f"{name} renders 16:9 but profiles/{sibling}/ does not exist, "
        f"so `run.py <id> --stage shorts` cannot work for it"
    )
    assert profiles.load(sibling).visuals.supports("9:16")


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_profile_declares_hashtags(name):
    p = profiles.load(name)
    tags = getattr(p, "HASHTAGS", ())
    assert tags, f"{name} declares no hashtags"
    for t in tags:
        assert t.startswith("#"), f"{name}: {t!r} is missing its hash"
        assert " " not in t, f"{name}: {t!r} contains a space"


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_declared_chart_actually_renders(tmp_path, name):
    """A chart in available_keys() that cannot draw is a dead button."""
    from pipeline.recipes import get

    p = profiles.load(name)
    spec = SHORTS if p.visuals.supports("9:16") else LONGFORM
    recipe = "incident_log" if "engineering" in name else "fee_drag"
    data = get(recipe).export()
    data["title"] = "Fixture"

    for key in p.visuals.available_keys():
        scene = Scene(index=1, text="A number, 42 of them.",
                      visual_spec={"data_key": key})
        out = tmp_path / f"{name}_{key}.png"
        p.visuals.build_visual(scene, data, spec, out)
        assert out.exists() and out.stat().st_size > 1000, (
            f"{name}.{key} produced nothing usable"
        )


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_style_exposes_what_stages_use(name):
    p = profiles.load(name)
    for attr in ("BACKGROUND", "INK", "INK_DIM", "ACCENT", "CHANNEL",
                 "DISCLAIMER", "DPI"):
        assert hasattr(p.style, attr), f"{name}.style lacks {attr}"
    assert callable(p.style.money)
    assert callable(p.style.rc_params)


# ---- a bad profile name must fail at creation, not three stages later --


def test_available_lists_every_profile_on_disk():
    assert set(profiles.available()) == set(ALL_PROFILES)


def test_an_unknown_profile_names_the_ones_that_exist():
    from pipeline.errors import ConfigError

    with pytest.raises(ConfigError) as exc:
        profiles.load("nosuchprofile")
    message = str(exc.value)
    assert "trading" in message and "engineering" in message


# ---- every profile must declare how YouTube should file its videos ----
# The category was hardcoded in upload.py, so switching topic left the
# video filed under the previous topic's category.

VALID_CATEGORIES = {"22", "24", "26", "27", "28"}


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_profile_declares_a_youtube_category(name):
    profile = profiles.load(name)
    category = getattr(profile, "YOUTUBE_CATEGORY_ID", None)
    assert category, f"{name} declares no YOUTUBE_CATEGORY_ID"
    assert category in VALID_CATEGORIES, f"{name}: {category} is not a id we use"


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_profile_declares_a_language(name):
    assert getattr(profiles.load(name), "LANGUAGE", None)
