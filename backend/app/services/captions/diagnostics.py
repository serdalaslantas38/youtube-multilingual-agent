"""Caption diagnostic and machine-translation probe service (Stage 3 PoC).

Operates strictly against the `YouTubeCaptionClient` interface so it can run
entirely against mocked responses in Stage 3a and later in read-only mode
against an authorized development video in Stage 3b.

Safety invariant:
- Never logs or prints OAuth tokens, secrets, or raw subtitle text.
- Never executes `captions.insert` in diagnostic mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.app_config import LanguagesConfig
from app.models.enums import SourceCaptionStatus, SourceCaptionTrackKind
from app.services.captions.parser import (
    SubtitleCue,
    SubtitleFormat,
    parse_and_validate_subtitles,
)
from app.services.validation.validators import (
    SourceCaptionCandidate,
    evaluate_source_caption_tracks,
    validate_language_code,
)
from app.services.youtube.captions_client import (
    CaptionDownloadFormat,
    CaptionDownloadParams,
    CaptionListParams,
    YouTubeApiError,
    YouTubeCaptionClient,
    YouTubeMalformedResponseError,
)


@dataclass(frozen=True, slots=True)
class TargetTranslationProbeResult:
    """Sanitized diagnostic summary for a single target language `tlang` probe."""

    language: str
    tlang: str
    download_succeeded: bool
    validation_passed: bool
    cue_count: int
    insert_request_valid: bool
    http_status: int | None = None
    error_reason: str | None = None
    retryable: bool = False
    issue_codes: tuple[str, ...] = ()

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "tlang": self.tlang,
            "download_succeeded": self.download_succeeded,
            "validation_passed": self.validation_passed,
            "cue_count": self.cue_count,
            "insert_request_valid": self.insert_request_valid,
            "http_status": self.http_status,
            "error_reason": self.error_reason,
            "retryable": self.retryable,
            "issue_codes": list(self.issue_codes),
        }


@dataclass(frozen=True, slots=True)
class CaptionTrackDiagnosticReport:
    """Sanitized diagnostic report for a video's source captions and `tlang` translations.

    Deliberately excludes raw subtitle text and credentials so it is safe to display
    in CLI output or diagnostic logs.
    """

    video_id: str
    source_language: str
    discovered_track_count: int
    source_ready: bool
    retry_later: bool
    diagnosis_reason_code: str
    selected_caption_id: str | None
    selected_language: str | None
    selected_track_kind: SourceCaptionTrackKind | None
    selected_status: SourceCaptionStatus | None
    selected_is_draft: bool | None
    source_download_succeeded: bool
    source_validation_passed: bool
    source_cue_count: int
    source_error_reason: str | None
    source_retryable: bool
    target_probes: tuple[TargetTranslationProbeResult, ...]
    quota_units_estimated: int

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "source_language": self.source_language,
            "discovered_track_count": self.discovered_track_count,
            "source_ready": self.source_ready,
            "retry_later": self.retry_later,
            "diagnosis_reason_code": self.diagnosis_reason_code,
            "selected_caption_id": self.selected_caption_id,
            "selected_language": self.selected_language,
            "selected_track_kind": (
                self.selected_track_kind.value if self.selected_track_kind else None
            ),
            "selected_status": (self.selected_status.value if self.selected_status else None),
            "selected_is_draft": self.selected_is_draft,
            "source_download_succeeded": self.source_download_succeeded,
            "source_validation_passed": self.source_validation_passed,
            "source_cue_count": self.source_cue_count,
            "source_error_reason": self.source_error_reason,
            "source_retryable": self.source_retryable,
            "target_probes": [p.to_safe_dict() for p in self.target_probes],
            "quota_units_estimated": self.quota_units_estimated,
        }


async def run_caption_diagnostics_probe(
    client: YouTubeCaptionClient,
    *,
    video_id: str,
    languages_cfg: LanguagesConfig,
    target_language_codes: list[str] | None = None,
    download_format: CaptionDownloadFormat = CaptionDownloadFormat.SRT,
) -> CaptionTrackDiagnosticReport:
    """Inspect a video's source captions and probe `tlang` downloads in read-only mode.

    1. Calls `captions.list` for `video_id`.
    2. Selects and diagnoses the Turkish source caption track without assuming `standard` vs `asr`.
    3. Downloads and validates the original source track (`captions.download`).
    4. For each requested target language, validates the language code, downloads the
       machine-translated track (`captions.download` with `tlang`), validates cues and
       timing parity against the source track, and verifies `captions.insert` request
       construction without executing any write.
    """
    source_lang = languages_cfg.source_language.code
    targets_to_check = (
        target_language_codes if target_language_codes is not None else languages_cfg.enabled_codes
    )
    subtitle_fmt = (
        SubtitleFormat.VTT if download_format is CaptionDownloadFormat.VTT else SubtitleFormat.SRT
    )

    quota_used = 50  # captions.list
    tracks = await client.list_captions(CaptionListParams(video_id=video_id))
    candidates = [
        SourceCaptionCandidate(
            youtube_caption_id=t.caption_id,
            language=t.language,
            track_kind=t.track_kind,
            processing_status=t.status,
            is_draft=t.is_draft,
            failure_reason=t.failure_reason,
        )
        for t in tracks
    ]
    diagnosis = evaluate_source_caption_tracks(candidates, source_language=source_lang)

    if not diagnosis.ready or diagnosis.selected_track is None:
        return CaptionTrackDiagnosticReport(
            video_id=video_id,
            source_language=source_lang,
            discovered_track_count=len(tracks),
            source_ready=False,
            retry_later=diagnosis.retry_later,
            diagnosis_reason_code=diagnosis.reason_code,
            selected_caption_id=None,
            selected_language=None,
            selected_track_kind=None,
            selected_status=None,
            selected_is_draft=None,
            source_download_succeeded=False,
            source_validation_passed=False,
            source_cue_count=0,
            source_error_reason=diagnosis.reason_code,
            source_retryable=diagnosis.retry_later,
            target_probes=(),
            quota_units_estimated=quota_used,
        )

    selected = diagnosis.selected_track
    quota_used += 200  # source captions.download

    source_cues: tuple[SubtitleCue, ...] = ()
    try:
        raw_source = await client.download_caption(
            CaptionDownloadParams(
                caption_id=selected.youtube_caption_id,
                tfmt=download_format,
                tlang=None,
            )
        )
        parsed_source = parse_and_validate_subtitles(raw_source, subtitle_fmt)
        source_cues = parsed_source.cues
        source_download_ok = True
        source_valid_ok = parsed_source.is_valid
        source_err = None if parsed_source.is_valid else parsed_source.report.issues[0].code
        source_retryable = False
    except YouTubeApiError as exc:
        source_download_ok = False
        source_valid_ok = False
        source_err = exc.reason
        source_retryable = exc.retryable
    except YouTubeMalformedResponseError:
        source_download_ok = False
        source_valid_ok = False
        source_err = "malformedResponse"
        source_retryable = False

    if not source_download_ok or not source_valid_ok:
        return CaptionTrackDiagnosticReport(
            video_id=video_id,
            source_language=source_lang,
            discovered_track_count=len(tracks),
            source_ready=True,
            retry_later=source_retryable,
            diagnosis_reason_code=diagnosis.reason_code,
            selected_caption_id=selected.youtube_caption_id,
            selected_language=selected.language,
            selected_track_kind=selected.track_kind,
            selected_status=selected.processing_status,
            selected_is_draft=selected.is_draft,
            source_download_succeeded=source_download_ok,
            source_validation_passed=source_valid_ok,
            source_cue_count=len(source_cues),
            source_error_reason=source_err,
            source_retryable=source_retryable,
            target_probes=(),
            quota_units_estimated=quota_used,
        )

    probe_results: list[TargetTranslationProbeResult] = []
    for lang_code in targets_to_check:
        lang_validation = validate_language_code(lang_code, languages_cfg)
        if not lang_validation.passed:
            probe_results.append(
                TargetTranslationProbeResult(
                    language=lang_code,
                    tlang="",
                    download_succeeded=False,
                    validation_passed=False,
                    cue_count=0,
                    insert_request_valid=False,
                    error_reason=lang_validation.issues[0].code,
                    retryable=False,
                    issue_codes=tuple(i.code for i in lang_validation.issues),
                )
            )
            continue

        target_cfg = languages_cfg.get(lang_code)
        if target_cfg is None:
            continue
        tlang = target_cfg.caption_tlang
        quota_used += 200  # tlang download

        try:
            translated_bytes = await client.download_caption(
                CaptionDownloadParams(
                    caption_id=selected.youtube_caption_id,
                    tfmt=download_format,
                    tlang=tlang,
                )
            )
            parsed_target = parse_and_validate_subtitles(
                translated_bytes,
                subtitle_fmt,
                reference_cues=source_cues,
            )
            insert_ok = False
            if parsed_target.is_valid:
                # Build and validate the insert payload WITHOUT executing any write
                client.prepare_caption_insert(
                    video_id=video_id,
                    language=target_cfg.code,
                    caption_content=translated_bytes,
                    name=target_cfg.native_name,
                    is_draft=True,
                )
                insert_ok = True

            probe_results.append(
                TargetTranslationProbeResult(
                    language=lang_code,
                    tlang=tlang,
                    download_succeeded=True,
                    validation_passed=parsed_target.is_valid,
                    cue_count=len(parsed_target.cues),
                    insert_request_valid=insert_ok,
                    http_status=200,
                    error_reason=(
                        None if parsed_target.is_valid else parsed_target.report.issues[0].code
                    ),
                    retryable=False,
                    issue_codes=tuple(i.code for i in parsed_target.report.issues),
                )
            )
        except YouTubeApiError as exc:
            probe_results.append(
                TargetTranslationProbeResult(
                    language=lang_code,
                    tlang=tlang,
                    download_succeeded=False,
                    validation_passed=False,
                    cue_count=0,
                    insert_request_valid=False,
                    http_status=exc.http_status,
                    error_reason=exc.reason,
                    retryable=exc.retryable,
                    issue_codes=(exc.reason,),
                )
            )
        except YouTubeMalformedResponseError:
            probe_results.append(
                TargetTranslationProbeResult(
                    language=lang_code,
                    tlang=tlang,
                    download_succeeded=False,
                    validation_passed=False,
                    cue_count=0,
                    insert_request_valid=False,
                    http_status=200,
                    error_reason="malformedResponse",
                    retryable=False,
                    issue_codes=("malformedResponse",),
                )
            )

    return CaptionTrackDiagnosticReport(
        video_id=video_id,
        source_language=source_lang,
        discovered_track_count=len(tracks),
        source_ready=True,
        retry_later=False,
        diagnosis_reason_code=diagnosis.reason_code,
        selected_caption_id=selected.youtube_caption_id,
        selected_language=selected.language,
        selected_track_kind=selected.track_kind,
        selected_status=selected.processing_status,
        selected_is_draft=selected.is_draft,
        source_download_succeeded=True,
        source_validation_passed=True,
        source_cue_count=len(source_cues),
        source_error_reason=None,
        source_retryable=False,
        target_probes=tuple(probe_results),
        quota_units_estimated=quota_used,
    )
