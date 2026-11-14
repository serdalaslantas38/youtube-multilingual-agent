"""Deterministic per-video, per-language localization orchestrator (Stage 5).

Coordinates:
1. Existing-localization and existing-caption-track detection (`skip_and_report`).
2. Metadata localization via `MetadataLocalizerAgent` + deterministic validation +
   `QualityReviewerAgent`.
3. Subtitle translation via `SubtitleTranslationService` (`youtube_native` -> `llm`
   fallback) + deterministic subtitle validation + `QualityReviewerAgent`.
4. Persistence of `LanguageAsset` rows with `prompt_version`, `content_hash`,
   `validation_report`, `review_report`, and explicit `AssetState`.
5. Quota accounting in `QuotaLedger` for any YouTube `captions.download` calls.

Never performs YouTube write operations (`videos.update` or `captions.insert`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.metadata_localizer import MetadataLocalizerAgent
from app.agents.quality_reviewer import QualityReviewerAgent
from app.core.app_config import AppConfig, PublishMode, TargetLanguage
from app.core.settings import Settings
from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.models.entities import LanguageAsset, SourceCaptionTrack, Video
from app.models.enums import (
    AssetKind,
    AssetState,
    SourceCaptionStatus,
    VideoWorkflowState,
)
from app.schemas.localization import LanguageAssetSummary, VideoLocalizationResponse
from app.services.captions.parser import (
    SubtitleCue,
    SubtitleFormat,
    normalize_source_srt,
    parse_and_validate_subtitles,
)
from app.services.captions.translator import SubtitleTranslationService
from app.services.translation.providers import (
    CaptionTranslationError,
    GoogleMetadataTranslator,
    LLMStructuredClient,
    create_llm_structured_client,
)
from app.services.validation.validators import (
    SourceCaptionCandidate,
    ValidationIssue,
    ValidationReport,
    evaluate_source_caption_tracks,
    validate_existing_metadata_localization,
    validate_existing_target_track,
    validate_language_code,
)
from app.services.youtube.captions_client import (
    CaptionDownloadFormat,
    CaptionDownloadParams,
    YouTubeApiError,
    YouTubeCaptionClient,
)
from app.services.youtube.read_service import record_quota_usage


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _determine_post_review_state(
    *,
    app_config: AppConfig,
    language_code: str,
    validation_passed: bool,
    review_passed: bool,
    source_ambiguous: bool,
) -> AssetState:
    if not validation_passed or not review_passed or source_ambiguous:
        return AssetState.NEEDS_ATTENTION
    effective = app_config.policy.effective_mode(language_code)
    if effective is PublishMode.AUTO_AFTER_VALIDATION:
        return AssetState.READY_TO_PUBLISH
    return AssetState.AWAITING_APPROVAL


async def _get_or_create_language_asset(
    session: AsyncSession,
    *,
    video_id: int,
    language: str,
    kind: AssetKind,
) -> LanguageAsset:
    now = _utc_now()
    stmt = select(LanguageAsset).where(
        LanguageAsset.video.has(Video.organization_id == LEGACY_ORGANIZATION_ID),
        LanguageAsset.video_id == video_id,
        LanguageAsset.language == language,
        LanguageAsset.kind == kind,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    asset = LanguageAsset(
        video_id=video_id,
        language=language,
        kind=kind,
        state=AssetState.PENDING,
        validation_report={},
        review_report={},
        attempt_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(asset)
    await session.flush()
    return asset


async def _ensure_source_caption_cues(
    session: AsyncSession,
    *,
    track_row: SourceCaptionTrack,
    caption_client: YouTubeCaptionClient | None,
) -> tuple[tuple[SubtitleCue, ...], int, str | None]:
    """Return parsed source cues, downloading the source SRT if not yet cached."""
    quota_used = 0
    raw_srt = track_row.raw_content
    if not raw_srt:
        if caption_client is None:
            return (), 0, "No YouTubeCaptionClient provided to download source caption track."
        try:
            raw_bytes = await caption_client.download_caption(
                CaptionDownloadParams(
                    caption_id=track_row.youtube_caption_id,
                    tfmt=CaptionDownloadFormat.SRT,
                )
            )
            quota_used += 200
            await record_quota_usage(
                session,
                api_method="captions.download",
                units=200,
            )
        except YouTubeApiError as exc:
            track_row.is_downloadable = False
            track_row.download_error = f"{exc.reason} (HTTP {exc.http_status})"
            return (), quota_used, track_row.download_error

        decoded_text = raw_bytes.decode("utf-8-sig")
        parsed_src = parse_and_validate_subtitles(decoded_text, SubtitleFormat.SRT)
        if not parsed_src.is_valid:
            repaired_text = normalize_source_srt(raw_bytes)
            repaired_doc = parse_and_validate_subtitles(repaired_text, SubtitleFormat.SRT)
            if repaired_doc.is_valid:
                decoded_text = repaired_text
                parsed_src = repaired_doc
            else:
                codes = [i.code for i in parsed_src.report.issues]
                track_row.is_downloadable = False
                track_row.download_error = f"Source subtitle validation failed: {codes}"
                return (), quota_used, track_row.download_error

        track_row.raw_content = decoded_text
        track_row.content_format = "srt"
        track_row.content_hash = parsed_src.content_hash
        track_row.cue_count = len(parsed_src.cues)
        track_row.is_downloadable = True
        track_row.download_error = None
        return parsed_src.cues, quota_used, None

    parsed_cached = parse_and_validate_subtitles(raw_srt, SubtitleFormat.SRT)
    if not parsed_cached.is_valid:
        repaired_cached = normalize_source_srt(raw_srt)
        repaired_cached_doc = parse_and_validate_subtitles(repaired_cached, SubtitleFormat.SRT)
        if repaired_cached_doc.is_valid:
            track_row.raw_content = repaired_cached
            track_row.content_hash = repaired_cached_doc.content_hash
            track_row.cue_count = len(repaired_cached_doc.cues)
            track_row.is_downloadable = True
            track_row.download_error = None
            return repaired_cached_doc.cues, 0, None
        codes = [i.code for i in parsed_cached.report.issues]
        return (), 0, f"Cached source subtitle failed validation: {codes}"
    return parsed_cached.cues, 0, None


def _asset_to_summary(asset: LanguageAsset, youtube_video_id: str) -> LanguageAssetSummary:
    cue_count: int | None = None
    if asset.kind is AssetKind.CAPTION and asset.caption_content:
        cue_count = asset.validation_report.get("cue_count")
        if cue_count is None:
            doc = parse_and_validate_subtitles(asset.caption_content, SubtitleFormat.SRT)
            cue_count = len(doc.cues)

    return LanguageAssetSummary(
        id=asset.id,
        youtube_video_id=youtube_video_id,
        language=asset.language,
        kind=asset.kind,
        state=asset.state,
        provider_used=asset.provider_used,
        prompt_version=asset.prompt_version,
        translated_title=asset.translated_title,
        translated_description=asset.translated_description,
        caption_format=asset.caption_format,
        caption_cue_count=cue_count,
        content_hash=asset.content_hash,
        validation_passed=asset.validation_passed,
        validation_report=asset.validation_report,
        review_passed=asset.review_passed,
        source_ambiguous=asset.source_ambiguous,
        review_report=asset.review_report,
        attempt_count=asset.attempt_count,
        last_error=asset.last_error,
        updated_at=asset.updated_at,
    )


async def localize_video_assets(
    session: AsyncSession,
    *,
    youtube_video_id: str,
    app_config: AppConfig,
    settings: Settings,
    llm_client: LLMStructuredClient | None = None,
    caption_client: YouTubeCaptionClient | None = None,
    target_language_codes: Sequence[str] | None = None,
    include_metadata: bool = True,
    include_captions: bool = True,
    force_regenerate: bool = False,
) -> VideoLocalizationResponse:
    """Run Stage 5 localization, fallback translation, validation, and review for a video."""
    stmt = (
        select(Video)
        .options(selectinload(Video.source_captions), selectinload(Video.language_assets))
        .where(
            Video.organization_id == LEGACY_ORGANIZATION_ID,
            Video.youtube_video_id == youtube_video_id,
        )
    )
    video = (await session.execute(stmt)).scalar_one_or_none()
    if video is None:
        raise LookupError(f"Video {youtube_video_id!r} not found in database; discover it first.")

    # Resolve target languages
    if target_language_codes is not None:
        resolved_targets: list[TargetLanguage] = []
        for code in target_language_codes:
            code_report = validate_language_code(code, app_config.languages)
            if not code_report.passed:
                msg = "; ".join(i.message for i in code_report.issues)
                raise ValueError(msg)
            target_obj = app_config.languages.get(code.strip())
            if target_obj is not None:
                resolved_targets.append(target_obj)
    else:
        resolved_targets = [t for t in app_config.languages.target_languages if t.enabled]

    model_name = settings.gemini_model_name or app_config.policy.llm.model_name
    active_llm_client = llm_client or create_llm_structured_client(
        app_config.policy.llm.provider,
        api_key=settings.gemini_api_key.get_secret_value(),
        use_vertex=settings.gemini_use_vertex,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )

    google_translator = (
        GoogleMetadataTranslator(
            max_title_length=app_config.policy.metadata.max_title_length,
        )
        if active_llm_client.provider_name != "fake"
        else None
    )
    metadata_agent = MetadataLocalizerAgent(
        llm_client=active_llm_client,
        model_name=model_name,
        prompt_name=app_config.policy.llm.metadata_prompt_name,
        prompts_dir=settings.prompts_dir,
        max_retries=app_config.policy.llm.max_output_retries,
        translation_sources=app_config.policy.metadata.translation_sources,
        google_translator=google_translator,
        max_title_length=app_config.policy.metadata.max_title_length,
    )
    reviewer_agent = QualityReviewerAgent(
        llm_client=active_llm_client,
        model_name=model_name,
        prompt_name=app_config.policy.llm.review_prompt_name,
        prompts_dir=settings.prompts_dir,
        max_retries=app_config.policy.llm.max_output_retries,
    )
    subtitle_service = SubtitleTranslationService(
        translation_sources=app_config.policy.captions.translation_sources,
        llm_client=active_llm_client,
        model_name=model_name,
        caption_client=caption_client,
        caption_prompt_name=app_config.policy.llm.caption_prompt_name,
        prompts_dir=settings.prompts_dir,
        max_line_length=app_config.policy.captions.max_line_length,
        llm_batch_size=app_config.policy.captions.llm_batch_size,
        max_output_retries=app_config.policy.llm.max_output_retries,
    )

    now = _utc_now()
    video.workflow_state = VideoWorkflowState.PROCESSING
    video.updated_at = now
    quota_used_total = 0

    existing_loc_langs = {k.lower() for k in (video.existing_localizations or {})}
    existing_caption_langs = {
        t.language.lower()
        for t in video.source_captions
        if t.processing_status is SourceCaptionStatus.SERVING
        and t.language.lower() != app_config.languages.source_language.code.lower()
    }

    # Evaluate source Turkish caption track availability
    candidates = [
        SourceCaptionCandidate(
            youtube_caption_id=t.youtube_caption_id,
            language=t.language,
            track_kind=t.track_kind,
            processing_status=t.processing_status,
            is_draft=t.is_draft,
            failure_reason=t.failure_reason,
        )
        for t in video.source_captions
    ]
    diagnosis = evaluate_source_caption_tracks(
        candidates, source_language=app_config.languages.source_language.code
    )
    selected_track_row: SourceCaptionTrack | None = None
    source_cues: tuple[SubtitleCue, ...] = ()
    source_download_error: str | None = None

    if include_captions and diagnosis.ready and diagnosis.selected_track is not None:
        selected_id = diagnosis.selected_track.youtube_caption_id
        selected_track_row = next(
            (t for t in video.source_captions if t.youtube_caption_id == selected_id),
            None,
        )
        if selected_track_row is not None:
            source_cues, dl_quota, source_download_error = await _ensure_source_caption_cues(
                session,
                track_row=selected_track_row,
                caption_client=caption_client,
            )
            quota_used_total += dl_quota

    # Process each target language
    for target_lang in resolved_targets:
        lang_code = target_lang.code
        if app_config.policy.configured_mode(lang_code) is PublishMode.DISABLED:
            continue

        # 1. Metadata Localization
        if include_metadata:
            meta_asset = await _get_or_create_language_asset(
                session,
                video_id=video.id,
                language=lang_code,
                kind=AssetKind.METADATA,
            )
            existing_meta_check = validate_existing_metadata_localization(
                lang_code,
                existing_loc_langs,
                explicit_overwrite_authorized=force_regenerate,
            )
            if not existing_meta_check.passed:
                meta_asset.state = AssetState.SKIPPED
                meta_asset.validation_passed = False
                meta_asset.validation_report = existing_meta_check.to_dict()
                meta_asset.last_error = existing_meta_check.issues[0].message
                meta_asset.updated_at = _utc_now()
            else:
                meta_asset.state = AssetState.TRANSLATING
                meta_res = await metadata_agent.localize(
                    source_title=video.source_title,
                    source_description=video.source_description,
                    source_language=app_config.languages.source_language,
                    target_language=target_lang,
                    protected_glossary=app_config.languages.protected_glossary,
                )
                meta_asset.provider_used = meta_res.provider_used
                meta_asset.prompt_version = meta_res.prompt_version
                meta_asset.translated_title = meta_res.title
                meta_asset.translated_description = meta_res.description
                meta_asset.content_hash = meta_res.content_hash
                meta_asset.validation_passed = meta_res.validation_report.passed
                meta_asset.validation_report = meta_res.validation_report.to_dict()
                meta_asset.attempt_count += meta_res.attempt_count

                if meta_res.validation_report.passed and not meta_res.source_ambiguous:
                    meta_asset.state = AssetState.REVIEWING
                    if meta_res.provider_used == "google_translate":
                        rev_res = reviewer_agent.review_metadata_deterministic(
                            source_title=video.source_title,
                            source_description=video.source_description,
                            translated_title=meta_res.title,
                            translated_description=meta_res.description,
                        )
                    else:
                        rev_res = await reviewer_agent.review_metadata(
                            source_title=video.source_title,
                            source_description=video.source_description,
                            translated_title=meta_res.title,
                            translated_description=meta_res.description,
                            source_language=app_config.languages.source_language,
                            target_language=target_lang,
                            protected_glossary=app_config.languages.protected_glossary,
                        )
                    meta_asset.review_passed = rev_res.passed
                    meta_asset.source_ambiguous = rev_res.source_ambiguous
                    meta_asset.review_report = rev_res.to_dict()
                    meta_asset.state = _determine_post_review_state(
                        app_config=app_config,
                        language_code=lang_code,
                        validation_passed=True,
                        review_passed=rev_res.passed,
                        source_ambiguous=rev_res.source_ambiguous,
                    )
                    meta_asset.last_error = (
                        None
                        if meta_asset.state is not AssetState.NEEDS_ATTENTION
                        else rev_res.summary
                    )
                else:
                    meta_asset.review_passed = False
                    meta_asset.source_ambiguous = meta_res.source_ambiguous
                    meta_asset.review_report = {
                        "passed": False,
                        "source_ambiguous": meta_res.source_ambiguous,
                        "summary": meta_res.ambiguity_notes
                        or meta_res.last_error
                        or "Skipped review because deterministic validation failed.",
                        "issues": [],
                    }
                    meta_asset.state = AssetState.NEEDS_ATTENTION
                    meta_asset.last_error = meta_res.last_error or meta_res.ambiguity_notes
                meta_asset.updated_at = _utc_now()

        # 2. Caption Translation
        if include_captions:
            cap_asset = await _get_or_create_language_asset(
                session,
                video_id=video.id,
                language=lang_code,
                kind=AssetKind.CAPTION,
            )
            existing_cap_check = validate_existing_target_track(
                lang_code,
                existing_caption_langs,
                explicit_overwrite_authorized=force_regenerate,
            )
            if not existing_cap_check.passed:
                cap_asset.state = AssetState.SKIPPED
                cap_asset.validation_passed = False
                cap_asset.validation_report = existing_cap_check.to_dict()
                cap_asset.last_error = existing_cap_check.issues[0].message
                cap_asset.updated_at = _utc_now()
            elif not diagnosis.ready or selected_track_row is None:
                cap_asset.state = (
                    AssetState.AWAITING_SOURCE
                    if diagnosis.retry_later
                    else AssetState.NEEDS_ATTENTION
                )
                cap_asset.validation_passed = False
                cap_asset.validation_report = ValidationReport(
                    passed=False,
                    issues=(
                        ValidationIssue(
                            code=diagnosis.reason_code,
                            message=diagnosis.message,
                        ),
                    ),
                ).to_dict()
                cap_asset.last_error = diagnosis.message
                cap_asset.updated_at = _utc_now()
            elif not source_cues:
                cap_asset.state = AssetState.NEEDS_ATTENTION
                cap_asset.validation_passed = False
                cap_asset.validation_report = ValidationReport(
                    passed=False,
                    issues=(
                        ValidationIssue(
                            code="source_caption_download_failed",
                            message=source_download_error or "Source caption cues unavailable.",
                        ),
                    ),
                ).to_dict()
                cap_asset.last_error = source_download_error
                cap_asset.updated_at = _utc_now()
            else:
                cap_asset.state = AssetState.TRANSLATING
                cap_asset.source_caption_track_id = selected_track_row.id
                try:
                    cap_res = await subtitle_service.translate(
                        source_caption_id=selected_track_row.youtube_caption_id,
                        source_cues=source_cues,
                        target_language=target_lang,
                        source_language=app_config.languages.source_language,
                        protected_glossary=app_config.languages.protected_glossary,
                    )
                    if cap_res.quota_units_used > 0:
                        quota_used_total += cap_res.quota_units_used
                        await record_quota_usage(
                            session,
                            api_method="captions.download",
                            units=cap_res.quota_units_used,
                            video_id=youtube_video_id,
                        )

                    cap_asset.provider_used = cap_res.provider_used
                    cap_asset.prompt_version = cap_res.prompt_version or "youtube_native.tlang.v1"
                    cap_asset.caption_content = cap_res.srt_content
                    cap_asset.caption_format = cap_res.parsed_doc.format.value
                    cap_asset.content_hash = cap_res.parsed_doc.content_hash
                    cap_asset.validation_passed = cap_res.parsed_doc.is_valid
                    val_dict = cap_res.parsed_doc.report.to_dict()
                    val_dict["cue_count"] = len(cap_res.parsed_doc.cues)
                    cap_asset.validation_report = val_dict
                    cap_asset.attempt_count += cap_res.attempt_count

                    if cap_res.parsed_doc.is_valid and not cap_res.source_ambiguous:
                        cap_asset.state = AssetState.REVIEWING
                        if cap_res.provider_used == "google_translate" or (
                            active_llm_client.provider_name != "fake"
                            and cap_res.provider_used.startswith("youtube_native")
                        ):
                            rev_res = reviewer_agent.review_captions_deterministic(
                                source_cues=source_cues,
                                translated_cues=cap_res.parsed_doc.cues,
                            )
                        else:
                            rev_res = await reviewer_agent.review_captions(
                                source_cues=source_cues,
                                translated_cues=cap_res.parsed_doc.cues,
                                source_language=app_config.languages.source_language,
                                target_language=target_lang,
                                protected_glossary=app_config.languages.protected_glossary,
                            )
                        cap_asset.review_passed = rev_res.passed
                        cap_asset.source_ambiguous = rev_res.source_ambiguous
                        cap_asset.review_report = rev_res.to_dict()
                        cap_asset.state = _determine_post_review_state(
                            app_config=app_config,
                            language_code=lang_code,
                            validation_passed=True,
                            review_passed=rev_res.passed,
                            source_ambiguous=rev_res.source_ambiguous,
                        )
                        cap_asset.last_error = (
                            None
                            if cap_asset.state is not AssetState.NEEDS_ATTENTION
                            else rev_res.summary
                        )
                    else:
                        cap_asset.review_passed = False
                        cap_asset.source_ambiguous = cap_res.source_ambiguous
                        cap_asset.state = AssetState.NEEDS_ATTENTION
                        cap_asset.last_error = (
                            cap_res.ambiguity_notes or "Subtitle validation failed."
                        )
                except CaptionTranslationError as exc:
                    if exc.quota_units_used > 0:
                        quota_used_total += exc.quota_units_used
                        await record_quota_usage(
                            session,
                            api_method="captions.download",
                            units=exc.quota_units_used,
                            video_id=youtube_video_id,
                        )
                    cap_asset.state = AssetState.NEEDS_ATTENTION
                    cap_asset.validation_passed = False
                    cap_asset.validation_report = ValidationReport(
                        passed=False,
                        issues=(
                            ValidationIssue(
                                code="caption_translation_failed",
                                message=str(exc),
                            ),
                        ),
                    ).to_dict()
                    cap_asset.attempt_count += 1
                    cap_asset.last_error = str(exc)
                cap_asset.updated_at = _utc_now()

    await session.flush()

    # Refresh assets and compute overall video workflow state
    assets_stmt = (
        select(LanguageAsset)
        .where(LanguageAsset.video.has(Video.organization_id == LEGACY_ORGANIZATION_ID))
        .where(LanguageAsset.video_id == video.id)
        .order_by(LanguageAsset.language.asc(), LanguageAsset.kind.asc())
    )
    all_assets = list((await session.execute(assets_stmt)).scalars().all())
    asset_states = {a.state for a in all_assets}

    if AssetState.NEEDS_ATTENTION in asset_states or AssetState.FAILED in asset_states:
        video.workflow_state = VideoWorkflowState.NEEDS_ATTENTION
    elif AssetState.AWAITING_SOURCE in asset_states:
        video.workflow_state = VideoWorkflowState.AWAITING_SOURCE
    else:
        video.workflow_state = VideoWorkflowState.SOURCE_READY
    video.updated_at = _utc_now()
    await session.flush()

    return VideoLocalizationResponse(
        youtube_video_id=video.youtube_video_id,
        source_title=video.source_title,
        workflow_state=video.workflow_state,
        source_caption_ready=diagnosis.ready,
        selected_source_caption_id=(
            diagnosis.selected_track.youtube_caption_id if diagnosis.selected_track else None
        ),
        selected_source_track_kind=(
            diagnosis.selected_track.track_kind.value if diagnosis.selected_track else None
        ),
        assets=[_asset_to_summary(a, video.youtube_video_id) for a in all_assets],
        quota_units_used_this_run=quota_used_total,
    )
