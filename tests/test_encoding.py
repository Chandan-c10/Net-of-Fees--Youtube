"""Text that is not ASCII must survive a round trip on either platform.

Windows defaults text IO to the locale codepage rather than UTF-8 until
Python 3.15, so a path that omits `encoding=` behaves differently on the
two machines this project targets. These tests pin the behaviour by
writing bytes that cp1252 cannot represent.
"""

import json

import pytest

from pipeline import scrub
from pipeline.manifest import Episode, Manifest, Scene

# A rupee sign has no cp1252 mapping at all; Devanagari has none either.
RUPEE = "₹"
DEVANAGARI = "चंदन"
EMOJI = "\U0001f4c9"


@pytest.mark.parametrize("text", [RUPEE, DEVANAGARI, EMOJI, "em—dash"])
def test_these_characters_really_are_the_hard_case(text):
    """Guard the premise: if cp1252 could encode these, the tests below
    would prove nothing."""
    text.encode("utf-8")
    if text != "em—dash":
        with pytest.raises(UnicodeEncodeError):
            text.encode("cp1252")


def test_a_manifest_round_trips_non_latin_text(tmp_path):
    ep = Episode("enc", tmp_path)
    ep.ensure_dirs()
    m = Manifest.new("enc", recipe="fee_drag", profile="trading",
                     title=f"{RUPEE}62 a trip {EMOJI}", voice="v")
    m.scenes = [Scene(index=1, text=f"{DEVANAGARI} paid {RUPEE}62.",
                      visual_spec={"data_key": "breakeven"})]
    ep.save_manifest(m)

    again = ep.load_manifest()
    assert again.title == m.title
    assert again.scenes[0].text == m.scenes[0].text


def test_a_snapshot_round_trips_non_latin_text(tmp_path):
    ep = Episode("enc", tmp_path)
    ep.ensure_dirs()
    payload = {"note": f"{DEVANAGARI} {RUPEE}62 {EMOJI}"}
    ep.snapshot.write_text(json.dumps(payload), encoding="utf-8")
    assert json.loads(ep.snapshot.read_text(encoding="utf-8")) == payload


def test_the_scanner_reads_a_utf8_file_as_utf8(tmp_path):
    """A mis-decoded file is a mis-scanned file, in the one place that
    must not miss anything."""
    path = tmp_path / "script.md"
    path.write_text(f"{DEVANAGARI} spent {RUPEE}62 — and "
                    "SMTP_APP_PASSWORD=hunter2", encoding="utf-8")
    found = scrub.scan_file(path)
    assert found, "the planted credential was not found"
    assert found[0].pattern == "password_label"


def test_a_binary_file_is_skipped_not_crashed(tmp_path):
    path = tmp_path / "frame.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    assert scrub.scan_file(path) == []
