"""Which account an episode publishes to.

One install, several content streams. A profile already made the topic
swappable; these tests pin the destination being swappable too, and pin
the fallback that keeps a single-channel setup working untouched.
"""

from __future__ import annotations

import pytest

from pipeline import channels
from pipeline.errors import ConfigError
from pipeline.manifest import Manifest


def manifest_for(profile: str) -> Manifest:
    return Manifest.new("x", recipe="r", profile=profile, voice="v")


# ---- which channel ----------------------------------------------------


def test_each_profile_gets_its_own_channel():
    assert channels.key_for(manifest_for("engineering")) == "engineering"
    assert channels.key_for(manifest_for("trading")) == "trading"


def test_a_shorts_profile_shares_its_parent_channel():
    """The vertical sibling exists to post the same content; it is not a
    separate account."""
    assert channels.key_for(manifest_for("engineering_shorts")) == "engineering"
    assert channels.key_for(manifest_for("trading_shorts")) == "trading"


def test_a_profile_may_declare_a_shared_channel(monkeypatch):
    import profiles
    real = profiles.load

    def fake(name):
        mod = real("engineering")
        if name == "engineering":
            monkeypatch.setattr(mod, "CHANNEL", "main", raising=False)
        return mod

    monkeypatch.setattr(profiles, "load", fake)
    assert channels.key_for(manifest_for("engineering")) == "main"


def test_an_unknown_profile_falls_back_to_its_name():
    """Upload runs after the render, so a profile problem has surfaced
    already; it must not become a crash here."""
    assert channels.key_for(manifest_for("nosuchprofile")) == "nosuchprofile"


@pytest.mark.parametrize("bad", ["Has Caps", "a/b", "../x", "x" * 40, ""])
def test_a_bad_channel_key_is_refused(bad, monkeypatch):
    m = manifest_for("engineering")
    m.profile = bad
    import profiles
    monkeypatch.setattr(profiles, "load",
                        lambda n: (_ for _ in ()).throw(ConfigError("no")))
    if bad == "":
        assert channels.key_for(m) == "default"
        return
    with pytest.raises(ConfigError, match="bad channel key"):
        channels.key_for(m)


def test_known_lists_one_entry_per_account():
    found = channels.known()
    assert "engineering" in found and "trading" in found
    # The _shorts siblings must not appear as separate accounts.
    assert not any(k.endswith("_shorts") for k in found)


# ---- youtube credentials ----------------------------------------------


def test_a_per_channel_token_file_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "SECRETS_DIR", tmp_path)
    (tmp_path / "youtube_token_trading.json").write_text("{}")
    assert channels.youtube_token_path("trading").name == \
        "youtube_token_trading.json"


def test_it_falls_back_to_the_single_channel_file(tmp_path, monkeypatch):
    """An existing one-channel install keeps working with no changes."""
    monkeypatch.setattr(channels, "SECRETS_DIR", tmp_path)
    monkeypatch.delenv("YOUTUBE_TOKEN_FILE", raising=False)
    assert channels.youtube_token_path("trading").name == "youtube_token.json"


def test_an_explicit_per_channel_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE_TRADING", "/tmp/t.json")
    assert str(channels.youtube_token_path("trading")) == "/tmp/t.json"


def test_the_global_override_still_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "SECRETS_DIR", tmp_path)
    monkeypatch.delenv("YOUTUBE_TOKEN_FILE_TRADING", raising=False)
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", "/tmp/global.json")
    assert str(channels.youtube_token_path("trading")) == "/tmp/global.json"


# ---- instagram credentials --------------------------------------------


def test_per_channel_instagram_credentials_win(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "global")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "globaltok")
    monkeypatch.setenv("IG_USER_ID_TRADING", "trade")
    monkeypatch.setenv("IG_ACCESS_TOKEN_TRADING", "tradetok")
    assert channels.instagram_credentials("trading") == ("trade", "tradetok")
    assert channels.instagram_credentials("engineering") == ("global", "globaltok")


def test_instagram_falls_back_to_the_unsuffixed_names(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "only")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "onlytok")
    monkeypatch.delenv("IG_USER_ID_TRADING", raising=False)
    monkeypatch.delenv("IG_ACCESS_TOKEN_TRADING", raising=False)
    assert channels.instagram_credentials("trading") == ("only", "onlytok")


def test_the_instagram_error_names_the_channel_variable(monkeypatch):
    from pipeline.stages import instagram
    for name in ("IG_USER_ID", "IG_ACCESS_TOKEN",
                 "IG_USER_ID_TRADING", "IG_ACCESS_TOKEN_TRADING"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as exc:
        instagram._credentials("trading")
    assert "IG_USER_ID_TRADING" in str(exc.value)
    assert "trading" in str(exc.value)


# ---- never leaks a value ----------------------------------------------


def test_describe_reports_presence_not_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(channels, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("IG_USER_ID_TRADING", "17841400000000000")
    monkeypatch.setenv("IG_ACCESS_TOKEN_TRADING", "EAA-super-secret-value")
    described = channels.describe("trading")
    blob = " ".join(described.values())
    assert "EAA-super-secret-value" not in blob
    assert "17841400000000000" not in blob
    assert described["instagram"] == "configured"


def test_the_auth_tool_writes_a_per_channel_file():
    import sys
    sys.path.insert(0, "tools")
    from youtube_auth import token_path
    assert token_path(None).name == "youtube_token.json"
    assert token_path("trading").name == "youtube_token_trading.json"
    assert token_path("TRADING").name == "youtube_token_trading.json"
