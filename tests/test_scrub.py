"""Scrub: a planted secret must fail the episode."""

from __future__ import annotations

import json
import time

import pytest

from pipeline.errors import ScrubError
from pipeline.scrub import assert_clean, censor, scan_text, strip_sensitive


@pytest.mark.parametrize("secret", [
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyB1234567890abcdefghijklmnopqrstuv",
    "postgres://user:pw@host:5432/db",
    "client_code: AB1234",
    "api_secret = s3cr3tvalue",
    "ABCDE1234F",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_secret_patterns_are_caught(secret):
    assert scan_text(f"narration {secret} more text"), f"missed: {secret}"


def test_planted_key_in_snapshot_halts_the_episode(tmp_path):
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"note": "key AKIAIOSFODNN7EXAMPLE"}))
    with pytest.raises(ScrubError):
        assert_clean([f])


def test_clean_material_passes(tmp_path):
    f = tmp_path / "script.md"
    f.write_text("Charges took 41 percent of a small win. Nothing secret here.")
    assert_clean([f])  # must not raise


def test_sensitive_keys_are_stripped_before_disk():
    stripped = strip_sensitive({
        "client_code": "AB1234",
        "capital": 500000,
        "gross_pnl": 750,
        "nested": {"api_key": "x", "keep": 1},
    })
    assert "client_code" not in stripped
    assert "capital" not in stripped
    assert stripped["gross_pnl"] == 750
    assert "api_key" not in stripped["nested"]
    assert stripped["nested"]["keep"] == 1


def test_findings_do_not_echo_the_whole_secret():
    findings = scan_text("token AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in str(findings[0])


# ---- credential names as this project actually spells them ------------
# The label patterns originally anchored with \b, which does not match
# between two word characters. `\bpassword` therefore missed
# SMTP_APP_PASSWORD -- every credential name in .env.example.

@pytest.mark.parametrize("line", [
    "GEMINI_API_KEY=AIzaSyFAKEKEY",
    "IG_ACCESS_TOKEN=EAAxyz123456",
    "SMTP_APP_PASSWORD=hunter2",
    "YOUTUBE_CLIENT_SECRET=s3cret",
    "ZERODHA_API_SECRET=abc123",
    "MY_REFRESH_TOKEN: abc123",
    "app_secret=abc123",
    "password=x",
    "api_key=y",
])
def test_a_prefixed_credential_name_is_still_caught(line):
    assert scan_text(line), f"{line} was not flagged"


@pytest.mark.parametrize("line", [
    "SMTP_USER=me@example.com",
    "ALERT_EMAIL=me@example.com",
    "IG_USER_ID=17841400000000000",
    "VOICE_ENGINE=kokoro",
    "The fee was 62 rupees on a round trip.",
])
def test_an_ordinary_setting_is_not_flagged(line):
    """A noisy scanner gets ignored, so non-secrets must stay quiet."""
    assert not scan_text(line), f"{line} was flagged"


@pytest.mark.parametrize("token,expected", [
    ("EAAGm0PX4ZCpsBA" + "x" * 120, "meta_access_token"),
    ("1//0gLm4Xq" + "A" * 40, "google_oauth_refresh"),
    ("GOCSPX-abcdefghijklmnopqrstuvwx", "google_client_secret"),
    ("ghp_" + "A" * 36, "github_token"),
])
def test_a_bare_token_is_caught_without_a_label(token, expected):
    """A token pasted into a log has no label to match on."""
    found = scan_text(f"the call failed with {token} today")
    assert found and found[0].pattern == expected


def test_the_scanner_cannot_be_stalled_by_its_input():
    """ENV_PREFIX was once nested quantifiers and took 1.8s on 8 KB.
    scan_text runs over text the pipeline did not write."""
    hostile = "A_" * 20_000 + "password"
    start = time.perf_counter()
    scan_text(hostile)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"scan took {elapsed:.1f}s on {len(hostile)} chars"


def test_censor_removes_a_prefixed_credential():
    out = censor("env had SMTP_APP_PASSWORD=hunter2 set")
    assert "hunter2" not in out
