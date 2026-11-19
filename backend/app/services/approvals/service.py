"""Stage 6 service for hash-bound approvals, asset editing, DB policy overrides, and auditing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.metadata_localizer import compute_metadata_content_hash
from app.core.app_config import (
    AppConfig,
    GlobalPolicy,
    LanguagesConfig,
    PublishingPolicyConfig,
    PublishMode,
    TargetLanguage,
)
from app.core.settings import Settings
from app.core.tenancy import current_organization_id
from app.models.entities import (
    Approval,
    AuditEvent,
    LanguageAsset,
    SourceCaptionTrack,
    Video,
)
from app.models.enums import (
    AssetKind,
    AssetState,
    PublishOutcome,
    SourceCaptionStatus,
    VideoWorkflowState,
)
from app.schemas.approvals import (
    AlignedCuePair,
    ApprovalRecordSummary,
    AssetDetailResponse,
    LanguagePolicyItem,
    PolicySettingsResponse,
    PublishAttemptRecordSummary,
    PublishDecisionPreview,
)
from app.services.captions.parser import (
    SubtitleCue,
    SubtitleFormat,
    parse_and_validate_subtitles,
)
from app.services.policy.engine import PublishEvaluationInput, evaluate_publish_decision
from app.services.validation.validators import validate_localized_metadata


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def record_audit_event(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
    now: datetime | None = None,
) -> AuditEvent:
    """Append an immutable entry to the `audit_events` table."""
    event = AuditEvent(
        organization_id=current_organization_id(),
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        created_at=now or _utc_now(),
    )
    session.add(event)
    await session.flush()
    return event


async def resolve_effective_app_config(
    session: AsyncSession,
    seed_config: AppConfig,
) -> AppConfig:
    """Overlay database policy and language overrides from `audit_events` onto `seed_config`."""
    stmt = (
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == current_organization_id(),
            AuditEvent.target_type.in_(("language_policy", "global_policy")),
        )
        .order_by(AuditEvent.id.asc())
    )
    events = list((await session.execute(stmt)).scalars().all())
    if not events:
        return seed_config

    target_by_code: dict[str, TargetLanguage] = {
        t.code: t for t in seed_config.languages.target_languages
    }
    ordered_codes: list[str] = [t.code for t in seed_config.languages.target_languages]
    mode_by_code: dict[str, PublishMode] = dict(seed_config.policy.languages)
    auto_publish = seed_config.policy.global_.auto_publish_enabled

    for ev in events:
        if ev.target_type == "global_policy" and ev.action == "policy.global_updated":
            if "auto_publish_enabled" in ev.details:
                auto_publish = bool(ev.details["auto_publish_enabled"])
        elif ev.target_type == "language_policy" and ev.action == "policy.language_updated":
            code = ev.target_id
            existing = target_by_code.get(code)
            if existing is not None:
                enabled = bool(ev.details.get("enabled", existing.enabled))
                name = str(ev.details.get("name", existing.name))
                native_name = str(ev.details.get("native_name", existing.native_name))
                caption_tlang = str(ev.details.get("caption_tlang", existing.caption_tlang))
                rtl = bool(ev.details.get("rtl", existing.rtl))
                target_by_code[code] = TargetLanguage(
                    code=code,
                    name=name,
                    native_name=native_name,
                    caption_tlang=caption_tlang,
                    enabled=enabled,
                    rtl=rtl,
                    popular=existing.popular,
                )
            elif all(k in ev.details for k in ("name", "native_name", "caption_tlang")):
                target_by_code[code] = TargetLanguage(
                    code=code,
                    name=str(ev.details["name"]),
                    native_name=str(ev.details["native_name"]),
                    caption_tlang=str(ev.details["caption_tlang"]),
                    enabled=bool(ev.details.get("enabled", True)),
                    rtl=bool(ev.details.get("rtl", False)),
                )
                ordered_codes.append(code)

            if "mode" in ev.details and ev.details["mode"] is not None:
                mode_by_code[code] = PublishMode(str(ev.details["mode"]))

    new_languages = LanguagesConfig(
        version=1,
        source_language=seed_config.languages.source_language,
        target_languages=[target_by_code[c] for c in ordered_codes],
        protected_glossary=seed_config.languages.protected_glossary,
    )
    new_policy = PublishingPolicyConfig(
        version=1,
        **{"global": GlobalPolicy(auto_publish_enabled=auto_publish)},
        default_mode=seed_config.policy.default_mode,
        languages=mode_by_code,
        captions=seed_config.policy.captions,
        metadata=seed_config.policy.metadata,
        llm=seed_config.policy.llm,
    )
    return AppConfig(languages=new_languages, policy=new_policy)


def build_policy_settings_response(
    app_config: AppConfig,
    settings: Settings,
) -> PolicySettingsResponse:
    """Format effective `AppConfig` and `Settings` for the dashboard settings screen."""
    items: list[LanguagePolicyItem] = []
    for lang in app_config.languages.target_languages:
        items.append(
            LanguagePolicyItem(
                code=lang.code,
                name=lang.name,
                native_name=lang.native_name,
                caption_tlang=lang.caption_tlang,
                rtl=lang.rtl,
                enabled=lang.enabled,
                popular=lang.popular,
                configured_mode=app_config.policy.configured_mode(lang.code),
                effective_mode=app_config.policy.effective_mode(lang.code),
            )
        )
    return PolicySettingsResponse(
        source_language_code=app_config.languages.source_language.code,
        source_language_name=app_config.languages.source_language.name,
        youtube_publishing_enabled=settings.youtube_publishing_enabled,
        global_auto_publish_enabled=app_config.policy.global_.auto_publish_enabled,
        default_mode=app_config.policy.default_mode,
        protected_glossary=list(app_config.languages.protected_glossary),
        translation_sources=list(app_config.policy.captions.translation_sources),
        languages=items,
    )


async def update_language_policy_in_db(
    session: AsyncSession,
    *,
    seed_config: AppConfig,
    actor: str,
    language_code: str,
    mode: PublishMode | None = None,
    enabled: bool | None = None,
    name: str | None = None,
    native_name: str | None = None,
    caption_tlang: str | None = None,
    rtl: bool | None = None,
) -> AppConfig:
    """Persist an audited per-language policy/enabled override and return updated `AppConfig`."""
    current_cfg = await resolve_effective_app_config(session, seed_config)
    code = language_code.strip()
    existing_lang = current_cfg.languages.get(code)

    if existing_lang is None:
        if not name or not native_name or not caption_tlang:
            raise ValueError(
                f"Language {code!r} is not yet configured; provide name, native_name, "
                "and caption_tlang to add it."
            )
        # Validate via TargetLanguage model
        candidate = TargetLanguage(
            code=code,
            name=name.strip(),
            native_name=native_name.strip(),
            caption_tlang=caption_tlang.strip(),
            enabled=enabled if enabled is not None else True,
            rtl=bool(rtl),
        )
        prev_state: dict[str, Any] = {"existed": False}
        new_details: dict[str, Any] = {
            "name": candidate.name,
            "native_name": candidate.native_name,
            "caption_tlang": candidate.caption_tlang,
            "enabled": candidate.enabled,
            "rtl": candidate.rtl,
            "mode": (mode or current_cfg.policy.default_mode).value,
            "previous": prev_state,
        }
    else:
        prev_mode = current_cfg.policy.configured_mode(code)
        new_mode = mode if mode is not None else prev_mode
        new_enabled = enabled if enabled is not None else existing_lang.enabled
        new_details = {
            "name": (name.strip() if name else existing_lang.name),
            "native_name": (native_name.strip() if native_name else existing_lang.native_name),
            "caption_tlang": (
                caption_tlang.strip() if caption_tlang else existing_lang.caption_tlang
            ),
            "enabled": new_enabled,
            "rtl": rtl if rtl is not None else existing_lang.rtl,
            "mode": new_mode.value,
            "previous": {
                "existed": True,
                "enabled": existing_lang.enabled,
                "mode": prev_mode.value,
            },
        }

    await record_audit_event(
        session,
        actor=actor,
        action="policy.language_updated",
        target_type="language_policy",
        target_id=code,
        details=new_details,
    )
    return await resolve_effective_app_config(session, seed_config)


async def update_global_policy_in_db(
    session: AsyncSession,
    *,
    seed_config: AppConfig,
    actor: str,
    auto_publish_enabled: bool,
    reason: str,
) -> AppConfig:
    """Persist an audited change to `global.auto_publish_enabled`."""
    current_cfg = await resolve_effective_app_config(session, seed_config)
    prev_val = current_cfg.policy.global_.auto_publish_enabled
    await record_audit_event(
        session,
        actor=actor,
        action="policy.global_updated",
        target_type="global_policy",
        target_id="global",
        details={
            "auto_publish_enabled": auto_publish_enabled,
            "previous_auto_publish_enabled": prev_val,
            "reason": reason.strip(),
        },
    )
    return await resolve_effective_app_config(session, seed_config)


async def _load_asset_with_relations(
    session: AsyncSession,
    asset_id: int,
) -> LanguageAsset:
    stmt = (
        select(LanguageAsset)
        .options(
            selectinload(LanguageAsset.video).selectinload(Video.source_captions),
            selectinload(LanguageAsset.approvals),
            selectinload(LanguageAsset.publish_attempts),
        )
        .where(
            LanguageAsset.id == asset_id,
            LanguageAsset.video.has(Video.organization_id == current_organization_id()),
        )
    )
    asset = (await session.execute(stmt)).scalar_one_or_none()
    if asset is None:
        raise LookupError(f"LanguageAsset id={asset_id} not found.")
    return asset


async def _recompute_video_workflow_state(
    session: AsyncSession,
    video: Video,
) -> None:
    stmt = select(LanguageAsset.state).where(LanguageAsset.video_id == video.id)
    states = set((await session.execute(stmt)).scalars().all())
    if AssetState.NEEDS_ATTENTION in states or AssetState.FAILED in states:
        video.workflow_state = VideoWorkflowState.NEEDS_ATTENTION
    elif AssetState.AWAITING_SOURCE in states:
        video.workflow_state = VideoWorkflowState.AWAITING_SOURCE
    elif states and all(s in (AssetState.PUBLISHED, AssetState.SKIPPED) for s in states):
        video.workflow_state = VideoWorkflowState.COMPLETED
    else:
        video.workflow_state = VideoWorkflowState.SOURCE_READY
    video.updated_at = _utc_now()


def _find_active_approval(asset: LanguageAsset) -> Approval | None:
    if not asset.content_hash:
        return None
    valid = [
        ap
        for ap in asset.approvals
        if ap.invalidated_at is None and ap.approved_content_hash == asset.content_hash
    ]
    if not valid:
        return None
    valid.sort(key=lambda a: a.id, reverse=True)
    return valid[0]


async def build_asset_detail_response(
    session: AsyncSession,
    *,
    asset_id: int,
    app_config: AppConfig,
    settings: Settings,
) -> AssetDetailResponse:
    """Build full side-by-side asset detail with cue alignment and publish decision preview."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    target_lang = app_config.languages.get(asset.language)
    lang_name = target_lang.name if target_lang else asset.language
    native_name = target_lang.native_name if target_lang else asset.language
    is_rtl = target_lang.rtl if target_lang else False

    source_caption_content: str | None = None
    source_cues: tuple[SubtitleCue, ...] = ()
    if asset.source_caption_track_id is not None:
        track = next(
            (t for t in video.source_captions if t.id == asset.source_caption_track_id),
            None,
        )
        if track is not None and track.raw_content:
            source_caption_content = track.raw_content
            parsed_src = parse_and_validate_subtitles(track.raw_content, SubtitleFormat.SRT)
            source_cues = parsed_src.cues

    aligned_cues: list[AlignedCuePair] = []
    if asset.kind is AssetKind.CAPTION and asset.caption_content:
        parsed_tgt = parse_and_validate_subtitles(asset.caption_content, SubtitleFormat.SRT)
        tgt_by_idx = {c.index: c for c in parsed_tgt.cues}
        if source_cues:
            for src_c in source_cues:
                tgt_c = tgt_by_idx.get(src_c.index)
                aligned_cues.append(
                    AlignedCuePair(
                        index=src_c.index,
                        start_ms=src_c.start_ms,
                        end_ms=src_c.end_ms,
                        source_text=src_c.text,
                        translated_text=tgt_c.text if tgt_c else "",
                    )
                )
        else:
            for tgt_c in parsed_tgt.cues:
                aligned_cues.append(
                    AlignedCuePair(
                        index=tgt_c.index,
                        start_ms=tgt_c.start_ms,
                        end_ms=tgt_c.end_ms,
                        source_text="",
                        translated_text=tgt_c.text,
                    )
                )

    active_ap = _find_active_approval(asset)
    sorted_approvals = sorted(asset.approvals, key=lambda a: a.id, reverse=True)
    history = [
        ApprovalRecordSummary(
            id=ap.id,
            approved_content_hash=ap.approved_content_hash,
            approved_by=ap.approved_by,
            notes=ap.notes,
            approved_at=ap.approved_at,
            invalidated_at=ap.invalidated_at,
            invalidation_reason=ap.invalidation_reason,
            is_valid_for_current_hash=(
                ap.invalidated_at is None
                and asset.content_hash is not None
                and ap.approved_content_hash == asset.content_hash
            ),
        )
        for ap in sorted_approvals
    ]

    has_uncertain = any(
        att.outcome is PublishOutcome.UNCERTAIN and att.reconciled_at is None
        for att in asset.publish_attempts
    )
    if asset.kind is AssetKind.METADATA:
        target_exists = asset.language.lower() in {
            k.lower() for k in (video.existing_localizations or {})
        }
    else:
        target_exists = any(
            t.language.lower() == asset.language.lower()
            and t.processing_status is SourceCaptionStatus.SERVING
            for t in video.source_captions
        )

    decision = evaluate_publish_decision(
        app_config.policy,
        PublishEvaluationInput(
            language=asset.language,
            youtube_publishing_enabled=settings.youtube_publishing_enabled,
            validation_passed=bool(asset.validation_passed),
            review_passed=bool(asset.review_passed),
            source_ambiguous=bool(asset.source_ambiguous),
            has_unreconciled_uncertain_attempt=has_uncertain,
            target_already_exists_on_youtube=target_exists,
            content_hash=asset.content_hash or "",
            approved_content_hash=(active_ap.approved_content_hash if active_ap else None),
        ),
    )

    sorted_attempts = sorted(asset.publish_attempts, key=lambda p: p.id, reverse=True)
    attempt_summaries = [
        PublishAttemptRecordSummary(
            id=att.id,
            asset_id=att.asset_id,
            idempotency_key=att.idempotency_key,
            operation=att.operation,
            request_digest=att.request_digest,
            outcome=att.outcome,
            response_summary=att.response_summary,
            error_message=att.error_message,
            created_at=att.created_at,
            verified_at=att.verified_at,
            reconciled_at=att.reconciled_at,
        )
        for att in sorted_attempts
    ]

    return AssetDetailResponse(
        id=asset.id,
        youtube_video_id=video.youtube_video_id,
        language=asset.language,
        language_name=lang_name,
        native_name=native_name,
        is_rtl=is_rtl,
        kind=asset.kind,
        state=asset.state,
        provider_used=asset.provider_used,
        prompt_version=asset.prompt_version,
        source_title=video.source_title,
        source_description=video.source_description,
        source_caption_content=source_caption_content,
        translated_title=asset.translated_title,
        translated_description=asset.translated_description,
        caption_content=asset.caption_content,
        caption_format=asset.caption_format,
        youtube_published_caption_id=asset.youtube_published_caption_id,
        aligned_cues=aligned_cues,
        content_hash=asset.content_hash,
        validation_passed=asset.validation_passed,
        validation_report=asset.validation_report,
        review_passed=asset.review_passed,
        source_ambiguous=asset.source_ambiguous,
        review_report=asset.review_report,
        active_approval=(history[0] if active_ap and history else None),
        approval_history=history,
        publish_decision=PublishDecisionPreview(
            allowed=decision.allowed,
            effective_mode=decision.effective_mode,
            reason_code=decision.reason_code,
            message=decision.message,
        ),
        publish_attempts=attempt_summaries,
        last_error=asset.last_error,
        updated_at=asset.updated_at,
    )


async def edit_language_asset(
    session: AsyncSession,
    *,
    asset_id: int,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    translated_title: str | None = None,
    translated_description: str | None = None,
    caption_content: str | None = None,
) -> AssetDetailResponse:
    """Update asset content, re-run deterministic validation, and invalidate prior approvals."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    now = _utc_now()
    old_hash = asset.content_hash

    if asset.kind is AssetKind.METADATA:
        if translated_title is None and translated_description is None:
            raise ValueError(
                "Editing a metadata asset requires translated_title and/or translated_description."
            )
        new_title = (
            translated_title.strip()
            if translated_title is not None
            else (asset.translated_title or "")
        )
        new_desc = (
            translated_description.strip()
            if translated_description is not None
            else (asset.translated_description or "")
        )
        report = validate_localized_metadata(
            new_title,
            new_desc,
            source_title=video.source_title,
            source_description=video.source_description,
            protected_glossary=app_config.languages.protected_glossary,
        )
        new_hash = compute_metadata_content_hash(new_title, new_desc)
        asset.translated_title = new_title
        asset.translated_description = new_desc
        asset.content_hash = new_hash
        asset.validation_passed = report.passed
        asset.validation_report = report.to_dict()
    else:
        if caption_content is None:
            raise ValueError("Editing a caption asset requires caption_content.")
        source_cues: tuple[SubtitleCue, ...] | None = None
        if asset.source_caption_track_id is not None:
            track: SourceCaptionTrack | None = next(
                (t for t in video.source_captions if t.id == asset.source_caption_track_id),
                None,
            )
            if track is not None and track.raw_content:
                parsed_src = parse_and_validate_subtitles(track.raw_content, SubtitleFormat.SRT)
                if parsed_src.is_valid:
                    source_cues = parsed_src.cues

        parsed_cap = parse_and_validate_subtitles(
            caption_content,
            SubtitleFormat.SRT,
            reference_cues=source_cues,
            max_line_length=app_config.policy.captions.max_line_length,
        )
        asset.caption_content = caption_content
        asset.caption_format = SubtitleFormat.SRT.value
        asset.content_hash = parsed_cap.content_hash
        asset.validation_passed = parsed_cap.is_valid
        val_dict = parsed_cap.report.to_dict()
        val_dict["cue_count"] = len(parsed_cap.cues)
        asset.validation_report = val_dict
        new_hash = parsed_cap.content_hash

    # Invalidate any active approvals if the content hash changed (ADR-005 hard rule)
    invalidated_count = 0
    if new_hash != old_hash:
        for ap in asset.approvals:
            if ap.invalidated_at is None:
                ap.invalidated_at = now
                ap.invalidation_reason = "content_edited_after_approval"
                invalidated_count += 1

    # Human edit clears prior LLM ambiguity block so the reviewer can approve once valid
    if asset.validation_passed:
        asset.review_passed = True
        asset.source_ambiguous = False
        asset.state = AssetState.AWAITING_APPROVAL
        asset.last_error = None
    else:
        asset.state = AssetState.NEEDS_ATTENTION
        issues_list = asset.validation_report.get("issues", [])
        asset.last_error = (
            "; ".join(i.get("message", "") for i in issues_list) or "Validation failed after edit."
        )

    asset.updated_at = now
    await _recompute_video_workflow_state(session, video)

    await record_audit_event(
        session,
        actor=actor,
        action="asset.edited",
        target_type="language_asset",
        target_id=str(asset.id),
        details={
            "youtube_video_id": video.youtube_video_id,
            "language": asset.language,
            "kind": asset.kind.value,
            "previous_content_hash": old_hash,
            "new_content_hash": new_hash,
            "validation_passed": asset.validation_passed,
            "approvals_invalidated": invalidated_count,
        },
        now=now,
    )
    return await build_asset_detail_response(
        session,
        asset_id=asset.id,
        app_config=app_config,
        settings=settings,
    )


async def approve_language_asset(
    session: AsyncSession,
    *,
    asset_id: int,
    actor: str,
    expected_content_hash: str,
    notes: str | None,
    override_review_issues: bool,
    app_config: AppConfig,
    settings: Settings,
) -> AssetDetailResponse:
    """Record a hash-bound human approval and transition the asset to `ready_to_publish`."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    now = _utc_now()

    if not asset.content_hash:
        raise ValueError("Cannot approve an asset that has no generated content hash.")
    if asset.content_hash != expected_content_hash.strip():
        raise ValueError(
            f"Content hash mismatch: asset currently has {asset.content_hash!r}, "
            f"but approval request specified {expected_content_hash!r}. Reload before approving."
        )
    if not asset.validation_passed:
        raise ValueError(
            "Cannot approve an asset that failed deterministic validation; "
            "fix validation issues first."
        )
    if (not asset.review_passed or asset.source_ambiguous) and not override_review_issues:
        raise ValueError(
            "Quality review flagged issues on this asset; review/edit the content or set "
            "override_review_issues=true to confirm human sign-off."
        )

    if override_review_issues and (not asset.review_passed or asset.source_ambiguous):
        asset.review_passed = True
        asset.source_ambiguous = False

    # Invalidate any older un-invalidated approval rows before adding the new one
    for ap in asset.approvals:
        if ap.invalidated_at is None:
            ap.invalidated_at = now
            ap.invalidation_reason = "superseded_by_new_approval"

    new_approval = Approval(
        asset_id=asset.id,
        approved_content_hash=asset.content_hash,
        approved_by=actor,
        notes=notes.strip() if notes else None,
        approved_at=now,
    )
    session.add(new_approval)
    asset.approvals.append(new_approval)
    asset.state = AssetState.READY_TO_PUBLISH
    asset.last_error = None
    asset.updated_at = now

    await _recompute_video_workflow_state(session, video)
    await record_audit_event(
        session,
        actor=actor,
        action="asset.approved",
        target_type="language_asset",
        target_id=str(asset.id),
        details={
            "youtube_video_id": video.youtube_video_id,
            "language": asset.language,
            "kind": asset.kind.value,
            "approved_content_hash": asset.content_hash,
            "override_review_issues": override_review_issues,
            "notes": notes,
        },
        now=now,
    )
    return await build_asset_detail_response(
        session,
        asset_id=asset.id,
        app_config=app_config,
        settings=settings,
    )


async def reject_language_asset(
    session: AsyncSession,
    *,
    asset_id: int,
    actor: str,
    reason: str,
    app_config: AppConfig,
    settings: Settings,
) -> AssetDetailResponse:
    """Reject an asset, invalidate any active approvals, and mark it `needs_attention`."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    now = _utc_now()

    invalidated_count = 0
    for ap in asset.approvals:
        if ap.invalidated_at is None:
            ap.invalidated_at = now
            ap.invalidation_reason = f"rejected_by_reviewer: {reason.strip()}"
            invalidated_count += 1

    asset.state = AssetState.NEEDS_ATTENTION
    asset.last_error = f"Rejected by {actor}: {reason.strip()}"
    asset.updated_at = now

    await _recompute_video_workflow_state(session, video)
    await record_audit_event(
        session,
        actor=actor,
        action="asset.rejected",
        target_type="language_asset",
        target_id=str(asset.id),
        details={
            "youtube_video_id": video.youtube_video_id,
            "language": asset.language,
            "kind": asset.kind.value,
            "content_hash": asset.content_hash,
            "reason": reason.strip(),
            "approvals_invalidated": invalidated_count,
        },
        now=now,
    )
    return await build_asset_detail_response(
        session,
        asset_id=asset.id,
        app_config=app_config,
        settings=settings,
    )
