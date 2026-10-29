"""Stage 3a contract & proof-of-concept tests for YouTube's native caption APIs.

Covers:
- `captions.list` request parameters and response parsing
- `captions.download` for original track (`tfmt=srt` / `vtt`)
- `captions.download` with `tlang` for machine translation across target languages
- `captions.insert` request construction (`part=snippet`, `videoId`, `language`, `name`, `isDraft`)
  without executing any real insertion
- HTTP 403 (`forbidden`, `insufficientPermissions`, `quotaExceeded`) and
  HTTP 404 (`captionNotFound`, `videoNotFound`)
- HTTP 409 (`captionExists` conflict)
- `couldNotConvert`, `invalidValue`, `contentRequired`, and non-JSON media download error formats
- Unavailable (`missing_source_caption`) and processing (`syncing`) source tracks
- Unsupported or invalid language codes
- Empty, malformed, or unexpected API responses
- Retryable (`500`, `503`, `429 rateLimitExceeded`) vs
  non-retryable (`400`, `403`, `404`, `409`, `403 quotaExceeded`) failures
- Proof that publishing remains disabled and outbound network calls are blocked
- Sanitized CLI diagnostic report output (no credentials or subtitle text leaked)
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core.app_config import load_app_config
from app.core.settings import get_settings
from app.models.enums import SourceCaptionStatus, SourceCaptionTrackKind
from app.services.captions import (
    SubtitleFormat,
    parse_and_validate_subtitles,
    run_caption_diagnostics_probe,
)
from app.services.youtube import (
    QUOTA_CAPTIONS_DOWNLOAD,
    QUOTA_CAPTIONS_LIST,
    CaptionDownloadFormat,
    CaptionDownloadParams,
    CaptionListParams,
    MockYouTubeCaptionClient,
    YouTubeMalformedResponseError,
    build_caption_insert_request,
    parse_caption_list_response,
    parse_youtube_api_error,
)
from scripts.poc.caption_diagnostics import build_demo_mock_client, run_cli

pytestmark = pytest.mark.contract


async def test_captions_list_parses_standard_and_asr_tracks() -> None:
    client = MockYouTubeCaptionClient(
        list_responses={
            "vid_123": {
                "kind": "youtube#captionListResponse",
                "items": [
                    {
                        "kind": "youtube#caption",
                        "id": "cap_tr_asr",
                        "snippet": {
                            "videoId": "vid_123",
                            "lastUpdated": "2026-10-27T10:00:00.000Z",
                            "trackKind": "ASR",
                            "language": "tr",
                            "name": "",
                            "audioTrackType": "primary",
                            "isCC": False,
                            "isDraft": False,
                            "isAutoSynced": True,
                            "status": "serving",
                        },
                    },
                    {
                        "kind": "youtube#caption",
                        "id": "cap_tr_std",
                        "snippet": {
                            "videoId": "vid_123",
                            "lastUpdated": "2026-10-27T11:00:00.000Z",
                            "trackKind": "standard",
                            "language": "tr",
                            "name": "Türkçe",
                            "audioTrackType": "primary",
                            "isCC": False,
                            "isDraft": False,
                            "isAutoSynced": False,
                            "status": "serving",
                        },
                    },
                ],
            }
        }
    )

    params = CaptionListParams(video_id="vid_123")
    assert params.to_query_params() == {"part": "snippet", "videoId": "vid_123"}

    tracks = await client.list_captions(params)
    assert len(tracks) == 2
    assert tracks[0].track_kind is SourceCaptionTrackKind.ASR
    assert tracks[0].is_auto_synced is True
    assert tracks[1].track_kind is SourceCaptionTrackKind.STANDARD
    assert tracks[1].status is SourceCaptionStatus.SERVING
    assert client.quota_units_used == QUOTA_CAPTIONS_LIST


async def test_captions_download_original_and_tlang_machine_translation() -> None:
    client = build_demo_mock_client("vid_tr_01", track_kind="standard")

    # 1. Original Turkish track download (no tlang)
    src_params = CaptionDownloadParams(
        caption_id="cap_vid_tr_01_tr",
        tfmt=CaptionDownloadFormat.SRT,
    )
    assert src_params.to_query_params() == {"tfmt": "srt"}
    raw_tr = await client.download_caption(src_params)
    parsed_tr = parse_and_validate_subtitles(raw_tr, SubtitleFormat.SRT)
    assert parsed_tr.is_valid is True
    assert len(parsed_tr.cues) == 2

    # 2. Machine-translated download with tlang=ar (Arabic RTL) and tlang=en
    for tlang in ("en", "ar", "ro", "ru"):
        tlang_params = CaptionDownloadParams(
            caption_id="cap_vid_tr_01_tr",
            tfmt=CaptionDownloadFormat.SRT,
            tlang=tlang,
        )
        assert tlang_params.to_query_params() == {"tfmt": "srt", "tlang": tlang}
        raw_translated = await client.download_caption(tlang_params)
        parsed_translated = parse_and_validate_subtitles(
            raw_translated,
            SubtitleFormat.SRT,
            reference_cues=parsed_tr.cues,
        )
        assert parsed_translated.is_valid is True
        assert len(parsed_translated.cues) == 2

    assert client.quota_units_used == 5 * QUOTA_CAPTIONS_DOWNLOAD


def test_captions_insert_request_construction_without_executing_write() -> None:
    client = build_demo_mock_client("vid_tr_01")
    srt_text = "1\n00:00:01,000 --> 00:00:03,000\nHello world!\n"

    req = client.prepare_caption_insert(
        video_id="vid_tr_01",
        language="en",
        caption_content=srt_text,
        name="English",
        is_draft=True,
    )
    api_payload = req.to_api_payload()
    assert api_payload["endpoint"] == "https://www.googleapis.com/upload/youtube/v3/captions"
    assert api_payload["query"] == {"part": "snippet"}
    assert api_payload["body"] == {
        "snippet": {
            "videoId": "vid_tr_01",
            "language": "en",
            "name": "English",
            "isDraft": True,
        }
    }
    # Deprecated `sync` parameter must never be emitted
    assert "sync" not in api_payload["query"]
    assert "sync" not in api_payload["body"]["snippet"]
    # Prepare must not consume write quota or call captions.insert
    assert client.quota_units_used == 0
    assert all(c["method"] != "captions.insert" for c in client.recorded_calls)

    # Empty media body raises contentRequired validation error
    with pytest.raises(ValidationError, match="contentRequired"):
        build_caption_insert_request(
            video_id="vid_tr_01",
            language="en",
            caption_content=b"   ",
        )


@pytest.mark.parametrize(
    ("http_status", "raw_payload", "expected_reason", "expected_retryable", "expected_conflict"),
    [
        # 403 forbidden on ASR / unowned caption download
        (
            403,
            {
                "error": {
                    "code": 403,
                    "message": (
                        "The caller does not have permission to download "
                        "the specified caption track."
                    ),
                    "errors": [
                        {
                            "domain": "youtube.caption",
                            "reason": "forbidden",
                            "message": "The caller does not have permission.",
                        }
                    ],
                }
            },
            "forbidden",
            False,
            False,
        ),
        # 403 insufficientPermissions (wrong OAuth scope)
        (
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Insufficient Permission",
                    "errors": [
                        {
                            "domain": "global",
                            "reason": "insufficientPermissions",
                            "message": "Insufficient Permission",
                        }
                    ],
                }
            },
            "insufficientPermissions",
            False,
            False,
        ),
        # 403 quotaExceeded (must NOT be retried immediately)
        (
            403,
            {
                "error": {
                    "code": 403,
                    "message": (
                        "The request cannot be completed because you have exceeded your quota."
                    ),
                    "errors": [
                        {
                            "domain": "youtube.quota",
                            "reason": "quotaExceeded",
                            "message": "Quota exceeded.",
                        }
                    ],
                }
            },
            "quotaExceeded",
            False,
            False,
        ),
        # 404 captionNotFound
        (
            404,
            {
                "error": {
                    "code": 404,
                    "message": "Caption track not found.",
                    "errors": [
                        {
                            "domain": "youtube.caption",
                            "reason": "captionNotFound",
                            "locationType": "parameter",
                            "location": "id",
                        }
                    ],
                }
            },
            "captionNotFound",
            False,
            False,
        ),
        # 409 captionExists conflict on insert
        (
            409,
            {
                "error": {
                    "code": 409,
                    "message": "The caption track already exists.",
                    "errors": [
                        {
                            "domain": "youtube.caption",
                            "reason": "captionExists",
                            "message": "The caption track already exists.",
                        }
                    ],
                }
            },
            "captionExists",
            False,
            True,
        ),
        # 400 couldNotConvert on captions.download with unsupported tlang/tfmt conversion
        (
            400,
            {
                "error": {
                    "code": 400,
                    "message": (
                        "The requested caption track cannot be converted to the "
                        "specified format or language."
                    ),
                    "errors": [
                        {
                            "domain": "youtube.caption",
                            "reason": "couldNotConvert",
                            "message": "Cannot convert caption track.",
                        }
                    ],
                }
            },
            "couldNotConvert",
            False,
            False,
        ),
        # Non-JSON plain-text error body from media download endpoint
        (
            403,
            b"Forbidden: caller is not authorized to download this track",
            "forbidden",
            False,
            False,
        ),
        # 429 rateLimitExceeded (retryable)
        (
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Too Many Requests",
                    "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}],
                }
            },
            "rateLimitExceeded",
            True,
            False,
        ),
        # 503 backendError (retryable)
        (
            503,
            {
                "error": {
                    "code": 503,
                    "message": "Backend Error",
                    "errors": [{"domain": "global", "reason": "backendError"}],
                }
            },
            "backendError",
            True,
            False,
        ),
    ],
)
def test_error_parser_handles_documented_and_non_json_formats(
    http_status: int,
    raw_payload: bytes | dict[str, object],
    expected_reason: str,
    expected_retryable: bool,
    expected_conflict: bool,
) -> None:
    err = parse_youtube_api_error(http_status, raw_payload)
    assert err.http_status == http_status
    assert err.reason == expected_reason
    assert err.retryable is expected_retryable
    assert err.conflict is expected_conflict


async def test_diagnostics_probe_handles_missing_and_syncing_source_tracks(
    config_dir: Path,
) -> None:
    languages_cfg = load_app_config(config_dir).languages

    # 1. Missing Turkish caption track -> retryable awaiting_source
    empty_client = MockYouTubeCaptionClient(
        list_responses={"vid_no_subs": {"kind": "youtube#captionListResponse", "items": []}}
    )
    missing_report = await run_caption_diagnostics_probe(
        empty_client,
        video_id="vid_no_subs",
        languages_cfg=languages_cfg,
    )
    assert missing_report.source_ready is False
    assert missing_report.retry_later is True
    assert missing_report.diagnosis_reason_code == "missing_source_caption"

    # 2. Processing / syncing Turkish caption track -> retryable source_caption_syncing
    syncing_client = MockYouTubeCaptionClient(
        list_responses={
            "vid_syncing": {
                "kind": "youtube#captionListResponse",
                "items": [
                    {
                        "id": "cap_tr_sync",
                        "snippet": {
                            "videoId": "vid_syncing",
                            "language": "tr",
                            "trackKind": "standard",
                            "status": "syncing",
                            "isDraft": False,
                        },
                    }
                ],
            }
        }
    )
    syncing_report = await run_caption_diagnostics_probe(
        syncing_client,
        video_id="vid_syncing",
        languages_cfg=languages_cfg,
    )
    assert syncing_report.source_ready is False
    assert syncing_report.retry_later is True
    assert syncing_report.diagnosis_reason_code == "source_caption_syncing"


async def test_diagnostics_probe_handles_could_not_convert_403_and_unsupported_languages(
    config_dir: Path,
) -> None:
    languages_cfg = load_app_config(config_dir).languages
    client = build_demo_mock_client("vid_partial", track_kind="asr")

    # Simulate `couldNotConvert` on Romanian and `403 forbidden` on Arabic for ASR tlang
    client.download_responses[("cap_vid_partial_tr", "srt", "ro")] = parse_youtube_api_error(
        400,
        {
            "error": {
                "code": 400,
                "errors": [{"domain": "youtube.caption", "reason": "couldNotConvert"}],
            }
        },
    )
    client.download_responses[("cap_vid_partial_tr", "srt", "ar")] = parse_youtube_api_error(
        403,
        {
            "error": {
                "code": 403,
                "errors": [{"domain": "youtube.caption", "reason": "forbidden"}],
            }
        },
    )

    report = await run_caption_diagnostics_probe(
        client,
        video_id="vid_partial",
        languages_cfg=languages_cfg,
        target_language_codes=["en", "ro", "ar", "sw", "INVALID!"],
    )
    assert report.source_ready is True
    assert report.selected_track_kind is SourceCaptionTrackKind.ASR

    by_lang = {p.language: p for p in report.target_probes}
    assert by_lang["en"].download_succeeded is True
    assert by_lang["en"].validation_passed is True
    assert by_lang["en"].insert_request_valid is True

    assert by_lang["ro"].download_succeeded is False
    assert by_lang["ro"].error_reason == "couldNotConvert"
    assert by_lang["ro"].retryable is False

    assert by_lang["ar"].download_succeeded is False
    assert by_lang["ar"].http_status == 403
    assert by_lang["ar"].error_reason == "forbidden"
    assert by_lang["ar"].retryable is False

    assert by_lang["sw"].download_succeeded is False
    assert by_lang["sw"].error_reason == "unsupported_target_language"

    assert by_lang["INVALID!"].download_succeeded is False
    assert by_lang["INVALID!"].error_reason == "invalid_language_code"


def test_tlang_parameter_rejects_non_iso639_1_codes() -> None:
    with pytest.raises(ValidationError, match="ISO 639-1"):
        CaptionDownloadParams(caption_id="cap_1", tlang="eng")
    with pytest.raises(ValidationError, match="ISO 639-1"):
        CaptionDownloadParams(caption_id="cap_1", tlang="en-US")


@pytest.mark.parametrize(
    "bad_payload",
    [
        None,
        "not a dict",
        {},  # missing 'items'
        {"items": "not a list"},
        {"items": [123]},
        {"items": [{"id": "", "snippet": {}}]},
        {"items": [{"id": "cap_1"}]},  # missing snippet
        {"items": [{"id": "cap_1", "snippet": {"language": "tr"}}]},  # missing videoId
        {"items": [{"id": "cap_1", "snippet": {"videoId": "v1"}}]},  # missing language
    ],
)
def test_malformed_or_unexpected_api_responses_raise_structured_error(
    bad_payload: object,
) -> None:
    with pytest.raises(YouTubeMalformedResponseError):
        parse_caption_list_response(bad_payload)


async def test_empty_download_stream_raises_malformed_response_error() -> None:
    client = MockYouTubeCaptionClient(download_responses={("cap_empty", "srt", None): b""})
    with pytest.raises(YouTubeMalformedResponseError, match="empty byte stream"):
        await client.download_caption(CaptionDownloadParams(caption_id="cap_empty"))


async def test_publishing_remains_disabled_and_outbound_network_is_blocked() -> None:
    # 1. Settings enforce YOUTUBE_PUBLISHING_ENABLED=False
    settings = get_settings()
    assert settings.youtube_publishing_enabled is False

    # 2. Attempting insert_caption with publishing_enabled=False raises PermissionError
    client = build_demo_mock_client("vid_safe")
    req = client.prepare_caption_insert(
        video_id="vid_safe",
        language="en",
        caption_content="1\n00:00:01,000 --> 00:00:02,000\nSafe\n",
    )
    with pytest.raises(PermissionError, match="YOUTUBE_PUBLISHING_ENABLED is false"):
        await client.insert_caption(req, publishing_enabled=settings.youtube_publishing_enabled)

    # 3. Attempting any real outbound network connection (socket or httpx) is blocked by conftest
    with pytest.raises(RuntimeError, match=r"Outbound network connection .* is forbidden"):
        socket.create_connection(("www.googleapis.com", 443), timeout=1)

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(Exception) as exc_info:
            await http_client.get("https://www.googleapis.com/youtube/v3/captions")
        assert "forbidden during automated tests" in str(exc_info.value)


async def test_diagnostic_cli_reports_sanitized_summary_without_subtitle_text() -> None:
    exit_code, output = await run_cli(
        ["--mode", "mock", "--video-id", "dev_tr_video_01", "--languages", "en,ar"]
    )
    assert exit_code == 0
    serialized = json.dumps(output, ensure_ascii=False)

    assert output["selected_language"] == "tr"
    assert output["selected_track_kind"] == "standard"
    assert output["selected_status"] == "serving"
    assert output["source_download_succeeded"] is True

    # Subtitle contents must NEVER appear in the diagnostic output
    assert "Merhaba arkadaşlar" not in serialized
    assert "mercimek çorbası" not in serialized
    assert "Hello friends" not in serialized
    assert "مرحباً" not in serialized

    # Live mode refuses to run without explicit confirmation flag
    code_refused, refused_out = await run_cli(
        ["--mode", "live-readonly", "--video-id", "dev_tr_video_01"]
    )
    assert code_refused == 2
    assert refused_out["error"] == "missing_dev_video_confirmation"
