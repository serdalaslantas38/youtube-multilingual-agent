"""Unit tests for SRT and WebVTT subtitle parsing and deterministic validation."""

from __future__ import annotations

import pytest

from app.services.captions.parser import (
    SubtitleFormat,
    normalize_source_srt,
    parse_and_validate_subtitles,
)

VALID_TURKISH_SRT = """\
1
00:00:01,000 --> 00:00:04,250
Merhaba arkadaşlar, mutfağıma hoş geldiniz!
Bugün çok lezzetli bir tarif paylaşıyorum.

2
00:00:04,500 --> 00:00:08,000
Malzemeler: 2 su bardağı un, 1 çay kaşığı tuz ve ılık su.
"""

VALID_ARABIC_SRT = """\
1
00:00:01,000 --> 00:00:04,250
مرحباً أصدقائي، أهلاً بكم في مطبخي!
اليوم أشارككم وصفة لذيذة جداً.

2
00:00:04,500 --> 00:00:08,000
المكونات: ٢ كوب دقيق، ١ ملعقة صغيرة ملح وماء دافئ.
"""

VALID_WEBVTT = """\
WEBVTT - Multivariate Multilingual Subtitles

NOTE
This is a comment block that should be ignored.

cue-1
00:00:01.000 --> 00:00:04.250 align:start position:0%
Hello friends, welcome to my kitchen!

00:04.500 --> 00:08.000
Ingredients: 2 cups flour, 1 tsp salt, and warm water.
"""


def test_valid_turkish_unicode_srt_parses_cleanly() -> None:
    doc = parse_and_validate_subtitles(VALID_TURKISH_SRT, SubtitleFormat.SRT)
    assert doc.is_valid is True
    assert len(doc.cues) == 2
    assert doc.cues[0].start_ms == 1000
    assert doc.cues[0].end_ms == 4250
    assert "mutfağıma hoş geldiniz" in doc.cues[0].text
    assert "çay kaşığı" in doc.cues[1].text
    assert doc.content_hash.startswith("sha256:")


def test_valid_arabic_rtl_srt_and_reference_parity() -> None:
    src_doc = parse_and_validate_subtitles(VALID_TURKISH_SRT, SubtitleFormat.SRT)
    ar_doc = parse_and_validate_subtitles(
        VALID_ARABIC_SRT.encode("utf-8"),
        SubtitleFormat.SRT,
        reference_cues=src_doc.cues,
    )
    assert ar_doc.is_valid is True
    assert len(ar_doc.cues) == 2
    assert "مرحباً أصدقائي" in ar_doc.cues[0].text
    assert "المكونات" in ar_doc.cues[1].text


def test_valid_webvtt_with_notes_and_cue_settings() -> None:
    doc = parse_and_validate_subtitles(VALID_WEBVTT, SubtitleFormat.VTT)
    assert doc.is_valid is True
    assert len(doc.cues) == 2
    assert doc.cues[0].start_ms == 1000
    assert doc.cues[1].end_ms == 8000


@pytest.mark.parametrize(
    "empty_payload",
    ["", "   \n\r\n  ", b""],
)
def test_empty_subtitle_content_fails(empty_payload: str | bytes) -> None:
    doc = parse_and_validate_subtitles(empty_payload, SubtitleFormat.SRT)
    assert doc.is_valid is False
    assert any(i.code == "empty_subtitle_content" for i in doc.report.issues)


def test_webvtt_missing_header_fails() -> None:
    no_header = "00:00:01.000 --> 00:00:03.000\nMissing WEBVTT header\n"
    doc = parse_and_validate_subtitles(no_header, SubtitleFormat.VTT)
    assert doc.is_valid is False
    assert any(i.code == "missing_webvtt_header" for i in doc.report.issues)


@pytest.mark.parametrize(
    ("bad_srt", "expected_code"),
    [
        # Dot instead of comma in SRT timestamp
        ("1\n00:00:01.000 --> 00:00:03,000\nHello\n", "invalid_timestamp_format"),
        # Invalid seconds (65)
        ("1\n00:00:65,000 --> 00:01:10,000\nHello\n", "invalid_timestamp_format"),
        # End time equals start time
        ("1\n00:00:05,000 --> 00:00:05,000\nHello\n", "non_positive_cue_duration"),
        # End time before start time
        ("1\n00:00:05,000 --> 00:00:03,000\nHello\n", "non_positive_cue_duration"),
        # Non-numeric cue index
        ("one\n00:00:01,000 --> 00:00:03,000\nHello\n", "invalid_cue_index"),
        # Non-sequential cue index
        (
            "1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n3\n00:00:03,000 --> 00:00:04,000\nThird\n",
            "non_sequential_cue_index",
        ),
        # Missing cue text (blank cue)
        ("1\n00:00:01,000 --> 00:00:02,000\n   \n", "empty_cue_text"),
        # Malformed single-line block
        ("1\n", "malformed_cue_block"),
    ],
)
def test_malformed_srt_files_are_rejected(bad_srt: str, expected_code: str) -> None:
    doc = parse_and_validate_subtitles(bad_srt, SubtitleFormat.SRT)
    assert doc.is_valid is False
    assert any(i.code == expected_code for i in doc.report.issues)


def test_chronological_order_and_overlap_detection() -> None:
    out_of_order = (
        "1\n00:00:05,000 --> 00:00:07,000\nLater cue first\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nEarlier cue second\n"
    )
    doc1 = parse_and_validate_subtitles(out_of_order, SubtitleFormat.SRT)
    assert doc1.is_valid is False
    assert any(i.code == "non_chronological_timestamps" for i in doc1.report.issues)

    overlapping = (
        "1\n00:00:01,000 --> 00:00:05,000\nFirst cue\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\nOverlapping cue\n"
    )
    doc2 = parse_and_validate_subtitles(overlapping, SubtitleFormat.SRT)
    assert doc2.is_valid is False
    assert any(i.code == "overlapping_cue_timestamps" for i in doc2.report.issues)


def test_missing_segments_and_timing_drift_against_source() -> None:
    src = parse_and_validate_subtitles(VALID_TURKISH_SRT, SubtitleFormat.SRT)
    one_cue_only = "1\n00:00:01,000 --> 00:00:04,250\nOnly one segment translated\n"
    missing_seg = parse_and_validate_subtitles(
        one_cue_only, SubtitleFormat.SRT, reference_cues=src.cues
    )
    assert missing_seg.is_valid is False
    assert any(i.code == "missing_or_extra_segments" for i in missing_seg.report.issues)

    drifted = (
        "1\n00:00:01,000 --> 00:00:04,250\nFirst segment\n\n"
        "2\n00:00:10,000 --> 00:00:15,000\nShifted by more than 1s\n"
    )
    drift_doc = parse_and_validate_subtitles(drifted, SubtitleFormat.SRT, reference_cues=src.cues)
    assert drift_doc.is_valid is False
    assert any(i.code == "timestamp_drift_from_source" for i in drift_doc.report.issues)


def test_invalid_utf8_and_replacement_character_detection() -> None:
    bad_bytes = b"1\n00:00:01,000 --> 00:00:02,000\n\xff\xfeInvalid UTF-8\n"
    doc = parse_and_validate_subtitles(bad_bytes, SubtitleFormat.SRT)
    assert doc.is_valid is False
    assert any(i.code == "invalid_utf8_encoding" for i in doc.report.issues)

    mojibake = "1\n00:00:01,000 --> 00:00:02,000\nBroken char \ufffd here\n"
    doc2 = parse_and_validate_subtitles(mojibake, SubtitleFormat.SRT)
    assert doc2.is_valid is False
    assert any(i.code == "unicode_replacement_character" for i in doc2.report.issues)


def test_normalize_source_srt_repairs_overlapping_asr_cues() -> None:
    overlapping_asr_srt = (
        "1\n00:00:01,200 --> 00:00:05,400\nMerhaba arkadaşlar\n\n"
        "2\n00:00:03,100 --> 00:00:07,200\nBugün harika bir ipucu var\n\n"
        "3\n00:00:05,400 --> 00:00:09,000\nHemen başlayalım\n"
    )
    raw_doc = parse_and_validate_subtitles(overlapping_asr_srt, SubtitleFormat.SRT)
    assert raw_doc.is_valid is False
    assert any(i.code == "overlapping_cue_timestamps" for i in raw_doc.report.issues)

    repaired = normalize_source_srt(overlapping_asr_srt)
    repaired_doc = parse_and_validate_subtitles(repaired, SubtitleFormat.SRT)
    assert repaired_doc.is_valid is True
    assert len(repaired_doc.cues) == 3
    assert repaired_doc.cues[0].start_ms == 1200
    assert repaired_doc.cues[0].end_ms == 3100
    assert repaired_doc.cues[1].start_ms == 3100
    assert repaired_doc.cues[1].end_ms == 5400
    assert repaired_doc.cues[2].start_ms == 5400
    assert repaired_doc.cues[2].end_ms == 9000
