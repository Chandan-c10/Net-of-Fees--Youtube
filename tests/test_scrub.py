"""Scrub: a planted secret must fail the episode."""

from __future__ import annotations

import json

import pytest

from pipeline.errors import ScrubError
from pipeline.scrub import assert_clean, scan_text, strip_sensitive


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
