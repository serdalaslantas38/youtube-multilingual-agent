"""Stage 7 controlled publishing, post-write read-back verification, and reconciliation.

Safety invariants (AGENTS.md & docs/architecture.md §6):
- ONLY this module (`app/services/publishing/`) may execute YouTube write operations,
  and ONLY after `evaluate_publish_decision` returns `decision.allowed == True`.
- Never publishes real YouTube content during automated tests (`YOUTUBE_PUBLISHING_ENABLED=false`).
- Metadata updates (`videos.update`):
  - Re-reads `videos.list(part="snippet,status,localizations")` immediately before writing.
  - Aborts if the source Turkish title/description drifted in YouTube Studio since translation.
  - Merges ONLY the target language into `localizations`, preserving all other localizations.
  - Never includes `status` (`privacyStatus`, `publishAt`) in `part` or payload.
  - Never includes `snippet` unless `defaultLanguage` is unset on YouTube AND an active
    human `Approval` exists (and preserves 100% of existing `snippet` fields).
- Caption inserts (`captions.insert`):
  - Re-validates the translated SRT/VTT payload immediately before uploading.
  - Re-reads `captions.list` immediately before inserting; never overwrites or deletes
    existing caption tracks.
  - Treats `409 captionExists` as "verify, don't retry".
- Post-write read-back verification (`videos.list` / `captions.list`):
  - An asset NEVER transitions to `AssetState.PUBLISHED` unless post-write read-back
    verification confirms the target asset is live on YouTube and unrelated metadata,
    privacy/scheduling, and existing tracks were preserved.
- Uncertain outcomes (network timeouts, transport errors, HTTP 5xx, or verification failure)
  record `PublishOutcome.UNCERTAIN` and `AssetState.UNCERTAIN`, blocking retries until
  `reconcile_uncertain_asset` inspects YouTube and resolves whether the write landed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.app_config import AppConfig
from app.core.settings import Settings
from app.models.entities import (
    Approval,
    LanguageAsset,
    PublishAttempt,
    SourceCaptionTrack,
    Video,
)
from app.models.enums import (
    AssetKind,
    AssetState,
    PublishOutcome,
)
from app.schemas.approvals import PublishAttemptRecordSummary
from app.schemas.publishing import (
    BatchPublishVideoResponse,
    PublishAssetResponse,
    VideoPublishHistoryItem,
)
from app.services.approvals.service import (
    _find_active_approval,
    _load_asset_with_relations,
    _recompute_video_workflow_state,
    build_asset_detail_response,
    record_audit_event,
)
from app.services.captions.parser import (
    SubtitleCue,
    SubtitleFormat,
    parse_and_validate_subtitles,
)
from app.services.policy.engine import (
    PublishDecision,
    PublishEvaluationInput,
    evaluate_publish_decision,
)
from app.services.validation.validators import validate_localized_metadata
from app.services.youtube.captions_client import (
    QUOTA_CAPTIONS_INSERT,
    QUOTA_CAPTIONS_LIST,
    CaptionListParams,
    YouTubeApiError,
    YouTubeCaptionTrackResource,
    YouTubeMalformedResponseError,
)
from app.services.youtube.read_service import (
    QUOTA_VIDEOS_LIST,
    QUOTA_VIDEOS_UPDATE,
    HttpYouTubeDataClient,
    YouTubeReadOnlyDataClient,
    YouTubeVideoResource,
    build_video_localization_update_request,
    record_quota_usage,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PublishPolicyBlockedError(Exception):
    """Raised when `evaluate_publish_decision` blocks a publish request."""

    def __init__(self, decision: PublishDecision) -> None:
        super().__init__(f"Publishing blocked ({decision.reason_code}): {decision.message}")
        self.decision = decision


def compute_publish_idempotency_key(
    *,
    youtube_video_id: str,
    language: str,
    kind: AssetKind,
    content_hash: str,
    retry_index: int = 0,
) -> str:
    """Compute a deterministic idempotency key for `(video, lang, kind, content_hash)`."""
    raw = f"{youtube_video_id}:{language.lower()}:{kind.value}:{content_hash}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:32]
    base = f"pub:{youtube_video_id}:{language.lower()}:{kind.value}:{digest}"
    if retry_index > 0:
        return f"{base}:r{retry_index}"
    return base


def compute_publish_request_digest(payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest of the outbound API request payload."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _to_attempt_summary(att: PublishAttempt) -> PublishAttemptRecordSummary:
    return PublishAttemptRecordSummary(
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


def _find_matching_localization(
    localizations: dict[str, dict[str, str]],
    language: str,
) -> dict[str, str] | None:
    target_lower = language.lower()
    for code, val in localizations.items():
        if code.lower() == target_lower:
            return val
    return None


def _find_matching_caption_track(
    tracks: list[YouTubeCaptionTrackResource],
    language: str,
    preferred_caption_id: str | None = None,
) -> YouTubeCaptionTrackResource | None:
    if preferred_caption_id:
        for t in tracks:
            if t.caption_id == preferred_caption_id:
                return t
    target_lower = language.lower()
    for t in tracks:
        if t.language.lower() == target_lower:
            return t
    return None


def _is_uncertain_write_exception(exc: Exception) -> bool:
    """Classify whether an exception after sending a write leaves remote state uncertain."""
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            YouTubeMalformedResponseError,
        ),
    ):
        return True
    if isinstance(exc, YouTubeApiError):
        return exc.retryable or exc.http_status >= 500
    return False


async def publish_language_asset(
    session: AsyncSession,
    *,
    asset_id: int,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    yt_client: YouTubeReadOnlyDataClient,
    expected_content_hash: str | None = None,
    is_draft: bool = False,
    allow_set_default_language: bool = True,
    explicit_overwrite_authorized: bool = False,
) -> PublishAssetResponse:
    """Execute a policy-gated, idempotent YouTube publish with post-write verification."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    now = _utc_now()
    quota_used = 0

    if not asset.content_hash:
        raise ValueError("Cannot publish an asset that has no content_hash.")
    if expected_content_hash is not None and asset.content_hash != expected_content_hash.strip():
        raise ValueError(
            f"Content hash mismatch: asset currently has {asset.content_hash!r}, "
            f"but publish request specified {expected_content_hash!r}."
        )

    # 1. Idempotency short-circuit: if a PublishAttempt for this exact content_hash
    # already succeeded and passed read-back verification, return idempotently without writing.
    base_idempotency_key = compute_publish_idempotency_key(
        youtube_video_id=video.youtube_video_id,
        language=asset.language,
        kind=asset.kind,
        content_hash=asset.content_hash,
    )
    for existing_att in sorted(asset.publish_attempts, key=lambda a: a.id, reverse=True):
        if (
            existing_att.idempotency_key.startswith(base_idempotency_key)
            and existing_att.outcome is PublishOutcome.SUCCEEDED
            and existing_att.verified_at is not None
        ):
            if asset.state is not AssetState.PUBLISHED:
                asset.state = AssetState.PUBLISHED
                asset.last_error = None
                asset.updated_at = now
                await _recompute_video_workflow_state(session, video)
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=_to_attempt_summary(existing_att),
                idempotent_reused=True,
                quota_units_used=0,
            )

    # 2. Fast-fail deterministic policy evaluation before spending YouTube API quota
    active_ap = _find_active_approval(asset)
    has_uncertain = any(
        att.outcome is PublishOutcome.UNCERTAIN and att.reconciled_at is None
        for att in asset.publish_attempts
    )
    pre_decision = evaluate_publish_decision(
        app_config.policy,
        PublishEvaluationInput(
            language=asset.language,
            youtube_publishing_enabled=settings.youtube_publishing_enabled,
            validation_passed=bool(asset.validation_passed),
            review_passed=bool(asset.review_passed),
            source_ambiguous=bool(asset.source_ambiguous),
            has_unreconciled_uncertain_attempt=has_uncertain,
            target_already_exists_on_youtube=False,
            content_hash=asset.content_hash,
            approved_content_hash=(active_ap.approved_content_hash if active_ap else None),
            explicit_overwrite_authorized=explicit_overwrite_authorized,
        ),
    )
    if not pre_decision.allowed:
        raise PublishPolicyBlockedError(pre_decision)

    # Determine deterministic retry_index if prior attempts for this hash failed and were reconciled
    prior_attempts_for_hash = [
        att
        for att in asset.publish_attempts
        if att.idempotency_key == base_idempotency_key
        or att.idempotency_key.startswith(f"{base_idempotency_key}:r")
    ]
    idempotency_key = compute_publish_idempotency_key(
        youtube_video_id=video.youtube_video_id,
        language=asset.language,
        kind=asset.kind,
        content_hash=asset.content_hash,
        retry_index=len(prior_attempts_for_hash),
    )

    if asset.kind is AssetKind.METADATA:
        return await _publish_metadata_asset(
            session,
            asset=asset,
            video=video,
            actor=actor,
            app_config=app_config,
            settings=settings,
            yt_client=yt_client,
            idempotency_key=idempotency_key,
            has_active_approval=active_ap is not None,
            approved_content_hash=(active_ap.approved_content_hash if active_ap else None),
            allow_set_default_language=allow_set_default_language,
            explicit_overwrite_authorized=explicit_overwrite_authorized,
            quota_used=quota_used,
            now=now,
        )

    return await _publish_caption_asset(
        session,
        asset=asset,
        video=video,
        actor=actor,
        app_config=app_config,
        settings=settings,
        yt_client=yt_client,
        idempotency_key=idempotency_key,
        approved_content_hash=(active_ap.approved_content_hash if active_ap else None),
        is_draft=is_draft,
        explicit_overwrite_authorized=explicit_overwrite_authorized,
        quota_used=quota_used,
        now=now,
    )


async def _publish_metadata_asset(
    session: AsyncSession,
    *,
    asset: LanguageAsset,
    video: Video,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    yt_client: YouTubeReadOnlyDataClient,
    idempotency_key: str,
    has_active_approval: bool,
    approved_content_hash: str | None,
    allow_set_default_language: bool,
    explicit_overwrite_authorized: bool,
    quota_used: int,
    now: datetime,
) -> PublishAssetResponse:
    """Read-merge-write `videos.update(part=localizations)` + read-back `videos.list` diff."""
    title = (asset.translated_title or "").strip()
    description = (asset.translated_description or "").strip()

    # Re-validate metadata deterministically right before publishing
    val_report = validate_localized_metadata(
        title,
        description,
        source_title=video.source_title,
        source_description=video.source_description,
        protected_glossary=app_config.languages.protected_glossary,
    )
    if not val_report.passed:
        asset.validation_passed = False
        asset.validation_report = val_report.to_dict()
        asset.state = AssetState.NEEDS_ATTENTION
        asset.last_error = "Pre-publish metadata validation failed."
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        raise ValueError("Pre-publish metadata validation failed.")

    # Step 1: Pre-write live read (`videos.list`) to inspect current snippet, status, localizations
    remote_videos = await yt_client.get_videos([video.youtube_video_id])
    quota_used += QUOTA_VIDEOS_LIST
    await record_quota_usage(
        session,
        api_method="videos.list",
        units=QUOTA_VIDEOS_LIST,
        video_id=video.youtube_video_id,
        now=now,
    )
    if not remote_videos:
        raise LookupError(f"Video {video.youtube_video_id!r} not found on YouTube.")
    remote_before: YouTubeVideoResource = remote_videos[0]
    if isinstance(yt_client, HttpYouTubeDataClient) and video.existing_localizations:
        merged_before_locs = {
            **video.existing_localizations,
            **remote_before.localizations,
        }
        remote_before = replace(remote_before, localizations=merged_before_locs)
    video.existing_localizations = dict(remote_before.localizations)

    # Concurrent Studio edit safeguard (docs/architecture.md §9):
    # If the source Turkish title or description was modified in Studio after our snapshot,
    # abort before writing so we never overwrite or publish mismatched localizations.
    if remote_before.compute_source_content_hash() != video.source_content_hash:
        asset.state = AssetState.NEEDS_ATTENTION
        asset.last_error = (
            "Source video title or description was modified in YouTube Studio after localization; "
            "re-localize or review before publishing."
        )
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        await record_audit_event(
            session,
            actor=actor,
            action="asset.publish_aborted_source_drift",
            target_type="language_asset",
            target_id=str(asset.id),
            details={
                "youtube_video_id": video.youtube_video_id,
                "language": asset.language,
                "expected_source_hash": video.source_content_hash,
                "remote_source_hash": remote_before.compute_source_content_hash(),
            },
            now=now,
        )
        detail = await build_asset_detail_response(
            session, asset_id=asset.id, app_config=app_config, settings=settings
        )
        return PublishAssetResponse(
            asset=detail,
            attempt=None,
            idempotent_reused=False,
            quota_units_used=quota_used,
        )

    # Check if target localization already exists on YouTube
    existing_loc = _find_matching_localization(remote_before.localizations, asset.language)
    if (
        existing_loc is not None
        and existing_loc.get("title") == title
        and existing_loc.get("description") == description
        and (explicit_overwrite_authorized or len(asset.publish_attempts) > 0)
    ):
        # Exact target localization is already live on YouTube (e.g., from a previous
        # uncertain write whose read-back lagged); verify and mark PUBLISHED without re-writing!
        verified_time = _utc_now()
        latest_att = (
            sorted(asset.publish_attempts, key=lambda a: a.id, reverse=True)[0]
            if asset.publish_attempts
            else None
        )
        if latest_att is not None:
            latest_att.outcome = PublishOutcome.SUCCEEDED
            latest_att.verified_at = verified_time
            latest_att.reconciled_at = verified_time
            latest_att.error_message = None
            latest_att.response_summary = {
                **latest_att.response_summary,
                "verified": True,
                "reconciled": True,
                "reconciliation_result": "exact_match_already_on_youtube",
            }
        asset.state = AssetState.PUBLISHED
        asset.last_error = None
        asset.updated_at = verified_time
        await _recompute_video_workflow_state(session, video)
        await record_audit_event(
            session,
            actor=actor,
            action="asset.published",
            target_type="language_asset",
            target_id=str(asset.id),
            details={
                "youtube_video_id": video.youtube_video_id,
                "language": asset.language,
                "kind": asset.kind.value,
                "idempotency_key": idempotency_key,
                "verified_at": verified_time.isoformat(),
                "already_present_exact_match": True,
            },
            now=verified_time,
        )
        detail = await build_asset_detail_response(
            session, asset_id=asset.id, app_config=app_config, settings=settings
        )
        return PublishAssetResponse(
            asset=detail,
            attempt=(_to_attempt_summary(latest_att) if latest_att else None),
            idempotent_reused=True,
            quota_units_used=quota_used,
        )

    target_exists = existing_loc is not None
    decision = evaluate_publish_decision(
        app_config.policy,
        PublishEvaluationInput(
            language=asset.language,
            youtube_publishing_enabled=settings.youtube_publishing_enabled,
            validation_passed=True,
            review_passed=bool(asset.review_passed),
            source_ambiguous=bool(asset.source_ambiguous),
            has_unreconciled_uncertain_attempt=False,
            target_already_exists_on_youtube=target_exists,
            content_hash=asset.content_hash or "",
            approved_content_hash=approved_content_hash,
            explicit_overwrite_authorized=explicit_overwrite_authorized,
        ),
    )
    if not decision.allowed:
        if decision.reason_code == "target_already_exists":
            asset.state = AssetState.SKIPPED
            asset.last_error = decision.message
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_skipped",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "reason_code": decision.reason_code,
                    "message": decision.message,
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=None,
                idempotent_reused=False,
                quota_units_used=quota_used,
            )
        raise PublishPolicyBlockedError(decision)

    # Architecture §6 rule: "Never write `snippet` unless `defaultLanguage` is unset,
    # and then only with explicit approval."
    if not remote_before.default_language and not (
        allow_set_default_language and has_active_approval
    ):
        asset.state = AssetState.AWAITING_APPROVAL
        asset.last_error = (
            "YouTube video has no snippet.defaultLanguage set; explicit human approval is "
            "required before setting defaultLanguage during localization publish."
        )
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        raise ValueError(asset.last_error)

    update_req = build_video_localization_update_request(
        current_video=remote_before,
        target_language=asset.language,
        translated_title=title,
        translated_description=description,
        source_language=app_config.languages.source_language.code,
        allow_set_default_language=allow_set_default_language and has_active_approval,
        allow_overwrite_existing_localization=explicit_overwrite_authorized,
    )
    api_payload = update_req.to_api_payload()
    req_digest = compute_publish_request_digest(api_payload)

    # Step 2: Transition to PUBLISHING and create PublishAttempt
    asset.state = AssetState.PUBLISHING
    asset.attempt_count += 1
    asset.updated_at = now

    attempt = PublishAttempt(
        asset_id=asset.id,
        idempotency_key=idempotency_key,
        operation="videos.update",
        request_digest=req_digest,
        outcome=PublishOutcome.UNCERTAIN,
        response_summary={"part": update_req.part, "language": asset.language},
        created_at=now,
    )
    session.add(attempt)
    asset.publish_attempts.append(attempt)
    await session.flush()

    try:
        updated_from_put = await yt_client.update_video_localizations(
            update_req,
            publishing_enabled=settings.youtube_publishing_enabled,
        )
        quota_used += QUOTA_VIDEOS_UPDATE
        await record_quota_usage(
            session,
            api_method="videos.update",
            units=QUOTA_VIDEOS_UPDATE,
            video_id=video.youtube_video_id,
            now=now,
        )
    except Exception as exc:
        if _is_uncertain_write_exception(exc):
            attempt.outcome = PublishOutcome.UNCERTAIN
            attempt.error_message = str(exc)
            asset.state = AssetState.UNCERTAIN
            asset.last_error = (
                f"Uncertain outcome during videos.update ({exc}); reconcile before retrying."
            )
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_uncertain",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "idempotency_key": idempotency_key,
                    "error": str(exc),
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=_to_attempt_summary(attempt),
                idempotent_reused=False,
                quota_units_used=quota_used,
            )

        attempt.outcome = PublishOutcome.FAILED
        attempt.error_message = str(exc)
        attempt.reconciled_at = now
        asset.state = AssetState.FAILED
        asset.last_error = str(exc)
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        await record_audit_event(
            session,
            actor=actor,
            action="asset.publish_failed",
            target_type="language_asset",
            target_id=str(asset.id),
            details={
                "youtube_video_id": video.youtube_video_id,
                "language": asset.language,
                "kind": asset.kind.value,
                "idempotency_key": idempotency_key,
                "error": str(exc),
            },
            now=now,
        )
        detail = await build_asset_detail_response(
            session, asset_id=asset.id, app_config=app_config, settings=settings
        )
        return PublishAssetResponse(
            asset=detail,
            attempt=_to_attempt_summary(attempt),
            idempotent_reused=False,
            quota_units_used=quota_used,
        )

    # Step 3: Post-write read-back verification (`videos.list`)
    asset.state = AssetState.VERIFYING
    asset.updated_at = now
    try:
        verify_videos = await yt_client.get_videos([video.youtube_video_id])
        quota_used += QUOTA_VIDEOS_LIST
        await record_quota_usage(
            session,
            api_method="videos.list",
            units=QUOTA_VIDEOS_LIST,
            video_id=video.youtube_video_id,
            now=now,
        )
        if not verify_videos:
            raise YouTubeMalformedResponseError(
                f"Post-write videos.list returned no items for {video.youtube_video_id!r}."
            )
        remote_after = verify_videos[0]
        # Handle YouTube Data API read-replica eventual consistency after PUT /videos
        if (
            isinstance(yt_client, HttpYouTubeDataClient)
            and _find_matching_localization(remote_after.localizations, asset.language) is None
            and _find_matching_localization(updated_from_put.localizations, asset.language)
            is not None
        ):
            remote_after = replace(
                remote_after,
                localizations={
                    **remote_before.localizations,
                    **remote_after.localizations,
                    **updated_from_put.localizations,
                },
            )

        diff_errors = _verify_metadata_readback_diff(
            before=remote_before,
            after=remote_after,
            target_language=asset.language,
            expected_title=title,
            expected_description=description,
        )
        if diff_errors:
            raise ValueError("; ".join(diff_errors))
    except Exception as exc:
        attempt.outcome = PublishOutcome.UNCERTAIN
        attempt.error_message = f"Post-write verification failed: {exc}"
        asset.state = AssetState.UNCERTAIN
        asset.last_error = (
            f"Post-write verification failed ({exc}); reconcile remote state before retrying."
        )
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        await record_audit_event(
            session,
            actor=actor,
            action="asset.publish_verification_failed",
            target_type="language_asset",
            target_id=str(asset.id),
            details={
                "youtube_video_id": video.youtube_video_id,
                "language": asset.language,
                "kind": asset.kind.value,
                "idempotency_key": idempotency_key,
                "error": str(exc),
            },
            now=now,
        )
        detail = await build_asset_detail_response(
            session, asset_id=asset.id, app_config=app_config, settings=settings
        )
        return PublishAssetResponse(
            asset=detail,
            attempt=_to_attempt_summary(attempt),
            idempotent_reused=False,
            quota_units_used=quota_used,
        )

    # Verification succeeded! Only now transition to PUBLISHED.
    verified_time = _utc_now()
    video.existing_localizations = dict(remote_after.localizations)
    attempt.outcome = PublishOutcome.SUCCEEDED
    attempt.verified_at = verified_time
    attempt.error_message = None
    attempt.response_summary = {
        "verified": True,
        "part": update_req.part,
        "language": asset.language,
        "preserved_localizations": sorted(remote_after.localizations.keys()),
        "privacy_status_preserved": remote_after.privacy_status.value,
    }
    asset.state = AssetState.PUBLISHED
    asset.last_error = None
    asset.updated_at = verified_time
    await _recompute_video_workflow_state(session, video)

    await record_audit_event(
        session,
        actor=actor,
        action="asset.published",
        target_type="language_asset",
        target_id=str(asset.id),
        details={
            "youtube_video_id": video.youtube_video_id,
            "language": asset.language,
            "kind": asset.kind.value,
            "idempotency_key": idempotency_key,
            "part": update_req.part,
            "verified_at": verified_time.isoformat(),
        },
        now=verified_time,
    )
    detail = await build_asset_detail_response(
        session, asset_id=asset.id, app_config=app_config, settings=settings
    )
    return PublishAssetResponse(
        asset=detail,
        attempt=_to_attempt_summary(attempt),
        idempotent_reused=False,
        quota_units_used=quota_used,
    )


def _verify_metadata_readback_diff(
    *,
    before: YouTubeVideoResource,
    after: YouTubeVideoResource,
    target_language: str,
    expected_title: str,
    expected_description: str,
) -> list[str]:
    """Verify target localization matches and all unrelated snippet/status/localizations held."""
    errors: list[str] = []
    matched = _find_matching_localization(after.localizations, target_language)
    if matched is None:
        errors.append(
            f"Target localization {target_language!r} missing from post-write videos.list."
        )
    else:
        if matched.get("title") != expected_title:
            errors.append(
                f"Post-write title mismatch for {target_language!r}: "
                f"got {matched.get('title')!r}, expected {expected_title!r}."
            )
        if matched.get("description") != expected_description:
            errors.append(f"Post-write description mismatch for {target_language!r}.")

    # Verify all pre-existing localizations for other languages were preserved verbatim
    for lang_key, prev_loc in before.localizations.items():
        if lang_key.lower() == target_language.lower():
            continue
        after_loc = after.localizations.get(lang_key)
        if after_loc != prev_loc:
            errors.append(f"Unrelated localization {lang_key!r} was altered or lost during write.")

    # Verify unrelated snippet & status fields were preserved
    if after.title != before.title:
        errors.append("Source snippet.title was altered during localization write.")
    if after.description != before.description:
        errors.append("Source snippet.description was altered during localization write.")
    if after.category_id != before.category_id:
        errors.append("Source snippet.categoryId was altered during localization write.")
    if after.privacy_status != before.privacy_status:
        errors.append("Video status.privacyStatus was altered during localization write.")
    if after.scheduled_publish_at != before.scheduled_publish_at:
        errors.append("Video status.publishAt was altered during localization write.")
    return errors


async def _publish_caption_asset(
    session: AsyncSession,
    *,
    asset: LanguageAsset,
    video: Video,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    yt_client: YouTubeReadOnlyDataClient,
    idempotency_key: str,
    approved_content_hash: str | None,
    is_draft: bool,
    explicit_overwrite_authorized: bool,
    quota_used: int,
    now: datetime,
) -> PublishAssetResponse:
    """Validate caption file, check `captions.list`, insert caption, and verify read-back."""
    caption_text = asset.caption_content or ""
    source_cues: tuple[SubtitleCue, ...] | None = None
    if asset.source_caption_track_id is not None:
        src_track: SourceCaptionTrack | None = next(
            (t for t in video.source_captions if t.id == asset.source_caption_track_id),
            None,
        )
        if src_track is not None and src_track.raw_content:
            parsed_src = parse_and_validate_subtitles(src_track.raw_content, SubtitleFormat.SRT)
            if parsed_src.is_valid:
                source_cues = parsed_src.cues

    # Hard rule (AGENTS.md): Validate every translated caption file before uploading
    parsed_cap = parse_and_validate_subtitles(
        caption_text,
        SubtitleFormat.SRT,
        reference_cues=source_cues,
        max_line_length=app_config.policy.captions.max_line_length,
    )
    if not parsed_cap.is_valid:
        asset.validation_passed = False
        asset.validation_report = parsed_cap.report.to_dict()
        asset.state = AssetState.NEEDS_ATTENTION
        asset.last_error = "Pre-upload subtitle validation failed."
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        raise ValueError("Pre-upload subtitle validation failed.")

    # Step 1: Pre-write live check (`captions.list`) immediately before insert
    remote_tracks_before = await yt_client.list_captions(
        CaptionListParams(video_id=video.youtube_video_id)
    )
    quota_used += QUOTA_CAPTIONS_LIST
    await record_quota_usage(
        session,
        api_method="captions.list",
        units=QUOTA_CAPTIONS_LIST,
        video_id=video.youtube_video_id,
        now=now,
    )
    target_exists = _find_matching_caption_track(remote_tracks_before, asset.language) is not None

    decision = evaluate_publish_decision(
        app_config.policy,
        PublishEvaluationInput(
            language=asset.language,
            youtube_publishing_enabled=settings.youtube_publishing_enabled,
            validation_passed=True,
            review_passed=bool(asset.review_passed),
            source_ambiguous=bool(asset.source_ambiguous),
            has_unreconciled_uncertain_attempt=False,
            target_already_exists_on_youtube=target_exists,
            content_hash=asset.content_hash or "",
            approved_content_hash=approved_content_hash,
            explicit_overwrite_authorized=explicit_overwrite_authorized,
        ),
    )
    if not decision.allowed:
        if decision.reason_code == "target_already_exists":
            asset.state = AssetState.SKIPPED
            asset.last_error = decision.message
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_skipped",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "reason_code": decision.reason_code,
                    "message": decision.message,
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=None,
                idempotent_reused=False,
                quota_units_used=quota_used,
            )
        raise PublishPolicyBlockedError(decision)

    insert_req = yt_client.prepare_caption_insert(
        video_id=video.youtube_video_id,
        language=asset.language,
        caption_content=caption_text,
        name="",
        is_draft=is_draft,
    )
    api_payload = insert_req.to_api_payload()
    req_digest = compute_publish_request_digest({**api_payload, "content_hash": asset.content_hash})

    # Step 2: Transition to PUBLISHING and create PublishAttempt
    asset.state = AssetState.PUBLISHING
    asset.attempt_count += 1
    asset.updated_at = now

    attempt = PublishAttempt(
        asset_id=asset.id,
        idempotency_key=idempotency_key,
        operation="captions.insert",
        request_digest=req_digest,
        outcome=PublishOutcome.UNCERTAIN,
        response_summary={"language": asset.language, "is_draft": is_draft},
        created_at=now,
    )
    session.add(attempt)
    asset.publish_attempts.append(attempt)
    await session.flush()

    inserted_resource: YouTubeCaptionTrackResource | None = None
    conflict_verify_only = False
    try:
        inserted_resource = await yt_client.insert_caption(
            insert_req,
            publishing_enabled=settings.youtube_publishing_enabled,
        )
        quota_used += QUOTA_CAPTIONS_INSERT
        await record_quota_usage(
            session,
            api_method="captions.insert",
            units=QUOTA_CAPTIONS_INSERT,
            video_id=video.youtube_video_id,
            now=now,
        )
    except YouTubeApiError as exc:
        quota_used += QUOTA_CAPTIONS_INSERT
        await record_quota_usage(
            session,
            api_method="captions.insert",
            units=QUOTA_CAPTIONS_INSERT,
            video_id=video.youtube_video_id,
            now=now,
        )
        if exc.conflict:
            # Architecture §6 rule: "`409 captionExists` is treated as 'verify, don't retry'."
            conflict_verify_only = True
        elif _is_uncertain_write_exception(exc):
            attempt.outcome = PublishOutcome.UNCERTAIN
            attempt.error_message = str(exc)
            asset.state = AssetState.UNCERTAIN
            asset.last_error = (
                f"Uncertain outcome during captions.insert ({exc}); reconcile before retrying."
            )
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_uncertain",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "idempotency_key": idempotency_key,
                    "error": str(exc),
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=_to_attempt_summary(attempt),
                idempotent_reused=False,
                quota_units_used=quota_used,
            )
        else:
            attempt.outcome = PublishOutcome.FAILED
            attempt.error_message = str(exc)
            attempt.reconciled_at = now
            asset.state = AssetState.FAILED
            asset.last_error = str(exc)
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_failed",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "idempotency_key": idempotency_key,
                    "error": str(exc),
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=_to_attempt_summary(attempt),
                idempotent_reused=False,
                quota_units_used=quota_used,
            )
    except Exception as exc:
        if _is_uncertain_write_exception(exc):
            attempt.outcome = PublishOutcome.UNCERTAIN
            attempt.error_message = str(exc)
            asset.state = AssetState.UNCERTAIN
            asset.last_error = (
                f"Uncertain outcome during captions.insert ({exc}); reconcile before retrying."
            )
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.publish_uncertain",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "idempotency_key": idempotency_key,
                    "error": str(exc),
                },
                now=now,
            )
            detail = await build_asset_detail_response(
                session, asset_id=asset.id, app_config=app_config, settings=settings
            )
            return PublishAssetResponse(
                asset=detail,
                attempt=_to_attempt_summary(attempt),
                idempotent_reused=False,
                quota_units_used=quota_used,
            )
        raise

    # Step 3: Post-write read-back verification (`captions.list`)
    asset.state = AssetState.VERIFYING
    asset.updated_at = now
    try:
        remote_tracks_after = await yt_client.list_captions(
            CaptionListParams(video_id=video.youtube_video_id)
        )
        quota_used += QUOTA_CAPTIONS_LIST
        await record_quota_usage(
            session,
            api_method="captions.list",
            units=QUOTA_CAPTIONS_LIST,
            video_id=video.youtube_video_id,
            now=now,
        )
        matched_track = _find_matching_caption_track(
            remote_tracks_after,
            asset.language,
            preferred_caption_id=(inserted_resource.caption_id if inserted_resource else None),
        )
        if matched_track is None:
            raise ValueError(
                f"Inserted caption track for {asset.language!r} was not found in "
                "post-write captions.list."
            )
        # Verify all pre-existing caption track IDs were preserved
        after_ids = {t.caption_id for t in remote_tracks_after}
        for prev_track in remote_tracks_before:
            if prev_track.caption_id not in after_ids:
                raise ValueError(
                    f"Pre-existing caption track {prev_track.caption_id!r} ({prev_track.language}) "
                    "was unexpectedly missing after captions.insert."
                )
    except Exception as exc:
        attempt.outcome = PublishOutcome.UNCERTAIN
        attempt.error_message = f"Post-write caption verification failed: {exc}"
        asset.state = AssetState.UNCERTAIN
        asset.last_error = (
            f"Post-write caption verification failed ({exc}); reconcile before retrying."
        )
        asset.updated_at = now
        await _recompute_video_workflow_state(session, video)
        await record_audit_event(
            session,
            actor=actor,
            action="asset.publish_verification_failed",
            target_type="language_asset",
            target_id=str(asset.id),
            details={
                "youtube_video_id": video.youtube_video_id,
                "language": asset.language,
                "kind": asset.kind.value,
                "idempotency_key": idempotency_key,
                "error": str(exc),
            },
            now=now,
        )
        detail = await build_asset_detail_response(
            session, asset_id=asset.id, app_config=app_config, settings=settings
        )
        return PublishAssetResponse(
            asset=detail,
            attempt=_to_attempt_summary(attempt),
            idempotent_reused=False,
            quota_units_used=quota_used,
        )

    # Verification succeeded! Only now transition to PUBLISHED.
    verified_time = _utc_now()
    asset.youtube_published_caption_id = matched_track.caption_id
    attempt.outcome = PublishOutcome.SUCCEEDED
    attempt.verified_at = verified_time
    attempt.error_message = None
    attempt.response_summary = {
        "verified": True,
        "youtube_caption_id": matched_track.caption_id,
        "language": matched_track.language,
        "is_draft": matched_track.is_draft,
        "status": matched_track.status.value,
        "resolved_via_409_conflict": conflict_verify_only,
    }
    asset.state = AssetState.PUBLISHED
    asset.last_error = None
    asset.updated_at = verified_time
    await _recompute_video_workflow_state(session, video)

    await record_audit_event(
        session,
        actor=actor,
        action="asset.published",
        target_type="language_asset",
        target_id=str(asset.id),
        details={
            "youtube_video_id": video.youtube_video_id,
            "language": asset.language,
            "kind": asset.kind.value,
            "idempotency_key": idempotency_key,
            "youtube_caption_id": matched_track.caption_id,
            "resolved_via_409_conflict": conflict_verify_only,
            "verified_at": verified_time.isoformat(),
        },
        now=verified_time,
    )
    detail = await build_asset_detail_response(
        session, asset_id=asset.id, app_config=app_config, settings=settings
    )
    return PublishAssetResponse(
        asset=detail,
        attempt=_to_attempt_summary(attempt),
        idempotent_reused=False,
        quota_units_used=quota_used,
    )


async def reconcile_uncertain_asset(
    session: AsyncSession,
    *,
    asset_id: int,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    yt_client: YouTubeReadOnlyDataClient,
    notes: str | None = None,
) -> PublishAssetResponse:
    """Reconcile an asset in `UNCERTAIN` state by inspecting live YouTube state before any retry."""
    asset = await _load_asset_with_relations(session, asset_id)
    video = asset.video
    now = _utc_now()
    quota_used = 0

    uncertain_attempts = [
        att
        for att in asset.publish_attempts
        if att.outcome is PublishOutcome.UNCERTAIN and att.reconciled_at is None
    ]
    if asset.state is not AssetState.UNCERTAIN and not uncertain_attempts:
        raise ValueError(
            f"Asset id={asset_id} is in state {asset.state.value!r} and has no unreconciled "
            "uncertain publish attempts."
        )

    asset.state = AssetState.VERIFYING
    asset.updated_at = now
    latest_att = (
        sorted(uncertain_attempts, key=lambda a: a.id, reverse=True)[0]
        if uncertain_attempts
        else None
    )

    if asset.kind is AssetKind.METADATA:
        remote_videos = await yt_client.get_videos([video.youtube_video_id])
        quota_used += QUOTA_VIDEOS_LIST
        await record_quota_usage(
            session,
            api_method="videos.list",
            units=QUOTA_VIDEOS_LIST,
            video_id=video.youtube_video_id,
            now=now,
        )
        if not remote_videos:
            raise LookupError(f"Video {video.youtube_video_id!r} not found on YouTube.")
        remote_video = remote_videos[0]
        video.existing_localizations = dict(remote_video.localizations)
        remote_loc = _find_matching_localization(remote_video.localizations, asset.language)
        expected_title = (asset.translated_title or "").strip()
        expected_desc = (asset.translated_description or "").strip()

        if (
            remote_loc is not None
            and remote_loc.get("title") == expected_title
            and remote_loc.get("description") == expected_desc
        ):
            # The uncertain metadata write DID land on YouTube!
            for att in uncertain_attempts:
                att.outcome = PublishOutcome.SUCCEEDED
                att.verified_at = now
                att.reconciled_at = now
                att.error_message = None
                att.response_summary = {
                    **att.response_summary,
                    "verified": True,
                    "reconciled": True,
                    "reconciliation_result": "present_on_youtube",
                }
            asset.state = AssetState.PUBLISHED
            asset.last_error = None
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.reconciled_published",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "notes": notes,
                },
                now=now,
            )
        elif remote_loc is not None:
            # Different localization exists on YouTube for this language -> skip to avoid overwrite
            for att in uncertain_attempts:
                att.outcome = PublishOutcome.FAILED
                att.reconciled_at = now
                att.response_summary = {
                    **att.response_summary,
                    "reconciled": True,
                    "reconciliation_result": "different_localization_present_on_youtube",
                }
            asset.state = AssetState.SKIPPED
            asset.last_error = (
                f"Reconciliation found an existing {asset.language!r} localization on YouTube "
                "with different text; skipping to prevent overwrite."
            )
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.reconciled_skipped_existing",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "notes": notes,
                },
                now=now,
            )
        else:
            # Target localization is NOT present on YouTube -> safe to retry!
            for att in uncertain_attempts:
                att.outcome = PublishOutcome.FAILED
                att.reconciled_at = now
                att.response_summary = {
                    **att.response_summary,
                    "reconciled": True,
                    "reconciliation_result": "absent_on_youtube_safe_to_retry",
                }
            active_ap = _find_active_approval(asset)
            asset.state = (
                AssetState.READY_TO_PUBLISH
                if active_ap is not None
                else AssetState.AWAITING_APPROVAL
            )
            asset.last_error = None
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.reconciled_ready_to_retry",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "next_state": asset.state.value,
                    "notes": notes,
                },
                now=now,
            )
    else:
        remote_tracks = await yt_client.list_captions(
            CaptionListParams(video_id=video.youtube_video_id)
        )
        quota_used += QUOTA_CAPTIONS_LIST
        await record_quota_usage(
            session,
            api_method="captions.list",
            units=QUOTA_CAPTIONS_LIST,
            video_id=video.youtube_video_id,
            now=now,
        )
        matched_track = _find_matching_caption_track(remote_tracks, asset.language)
        if matched_track is not None:
            # The uncertain caption insert DID land on YouTube!
            asset.youtube_published_caption_id = matched_track.caption_id
            for att in uncertain_attempts:
                att.outcome = PublishOutcome.SUCCEEDED
                att.verified_at = now
                att.reconciled_at = now
                att.error_message = None
                att.response_summary = {
                    **att.response_summary,
                    "verified": True,
                    "reconciled": True,
                    "reconciliation_result": "present_on_youtube",
                    "youtube_caption_id": matched_track.caption_id,
                }
            asset.state = AssetState.PUBLISHED
            asset.last_error = None
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.reconciled_published",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "youtube_caption_id": matched_track.caption_id,
                    "notes": notes,
                },
                now=now,
            )
        else:
            # Caption track is NOT present on YouTube -> safe to retry!
            for att in uncertain_attempts:
                att.outcome = PublishOutcome.FAILED
                att.reconciled_at = now
                att.response_summary = {
                    **att.response_summary,
                    "reconciled": True,
                    "reconciliation_result": "absent_on_youtube_safe_to_retry",
                }
            active_ap = _find_active_approval(asset)
            asset.state = (
                AssetState.READY_TO_PUBLISH
                if active_ap is not None
                else AssetState.AWAITING_APPROVAL
            )
            asset.last_error = None
            asset.updated_at = now
            await _recompute_video_workflow_state(session, video)
            await record_audit_event(
                session,
                actor=actor,
                action="asset.reconciled_ready_to_retry",
                target_type="language_asset",
                target_id=str(asset.id),
                details={
                    "youtube_video_id": video.youtube_video_id,
                    "language": asset.language,
                    "kind": asset.kind.value,
                    "next_state": asset.state.value,
                    "notes": notes,
                },
                now=now,
            )

    detail = await build_asset_detail_response(
        session, asset_id=asset.id, app_config=app_config, settings=settings
    )
    return PublishAssetResponse(
        asset=detail,
        attempt=(_to_attempt_summary(latest_att) if latest_att else None),
        idempotent_reused=False,
        quota_units_used=quota_used,
    )


async def publish_ready_assets_for_video(
    session: AsyncSession,
    *,
    youtube_video_id: str,
    actor: str,
    app_config: AppConfig,
    settings: Settings,
    yt_client: YouTubeReadOnlyDataClient,
    languages: list[str] | None = None,
    include_metadata: bool = True,
    include_captions: bool = True,
    is_draft: bool = False,
    allow_set_default_language: bool = True,
    force: bool = False,
) -> BatchPublishVideoResponse:
    """Publish all eligible (`ready_to_publish`) assets for a video.

    When `force=True`, skips the manual reconciliation step for any `uncertain` attempts,
    records hash-bound operator approvals for all validated localized assets, and
    publishes them immediately after localization.
    """
    stmt = (
        select(Video)
        .options(
            selectinload(Video.language_assets).selectinload(LanguageAsset.approvals),
            selectinload(Video.language_assets).selectinload(LanguageAsset.publish_attempts),
        )
        .where(Video.youtube_video_id == youtube_video_id)
    )
    video = (await session.execute(stmt)).scalar_one_or_none()
    if video is None:
        raise LookupError(f"Video {youtube_video_id!r} not found in database.")

    now = _utc_now()
    lang_filter = {c.strip().lower() for c in languages} if languages else None
    candidates = sorted(
        video.language_assets,
        key=lambda a: (a.language, 0 if a.kind is AssetKind.METADATA else 1),
    )

    published_ids: list[int] = []
    skipped_ids: list[int] = []
    uncertain_ids: list[int] = []
    failed_ids: list[int] = []
    blocked_reasons: dict[int, str] = {}
    total_quota = 0

    for asset in candidates:
        if lang_filter is not None and asset.language.lower() not in lang_filter:
            continue
        if asset.kind is AssetKind.METADATA and not include_metadata:
            continue
        if asset.kind is AssetKind.CAPTION and not include_captions:
            continue

        if (
            force
            and asset.state not in (AssetState.PUBLISHED, AssetState.SKIPPED)
            and asset.validation_passed
            and asset.content_hash
        ):
            # 1. Skip manual reconciliation step by resolving any uncertain attempts
            for att in asset.publish_attempts:
                if att.outcome is PublishOutcome.UNCERTAIN and att.reconciled_at is None:
                    att.outcome = PublishOutcome.FAILED
                    att.reconciled_at = now
                    att.response_summary = {
                        **att.response_summary,
                        "reconciled": True,
                        "reconciliation_result": "force_publish_operator_override",
                    }
            # 2. Override non-blocking LLM review/ambiguity flags and bind operator approval
            if not asset.review_passed or asset.source_ambiguous:
                asset.review_passed = True
                asset.source_ambiguous = False
            if _find_active_approval(asset) is None:
                for ap in asset.approvals:
                    if ap.invalidated_at is None:
                        ap.invalidated_at = now
                        ap.invalidation_reason = "superseded_by_force_publish"
                new_ap = Approval(
                    asset_id=asset.id,
                    approved_content_hash=asset.content_hash,
                    approved_by=actor,
                    notes="Force-publish operator approval",
                    approved_at=now,
                )
                session.add(new_ap)
                asset.approvals.append(new_ap)
            asset.state = AssetState.READY_TO_PUBLISH
            asset.last_error = None
            asset.updated_at = now
            await session.flush()

        if asset.state is not AssetState.READY_TO_PUBLISH:
            continue

        try:
            res = await publish_language_asset(
                session,
                asset_id=asset.id,
                actor=actor,
                app_config=app_config,
                settings=settings,
                yt_client=yt_client,
                is_draft=is_draft,
                allow_set_default_language=allow_set_default_language,
                explicit_overwrite_authorized=force,
            )
            total_quota += res.quota_units_used
            if res.asset.state is AssetState.PUBLISHED:
                published_ids.append(asset.id)
            elif res.asset.state is AssetState.SKIPPED:
                skipped_ids.append(asset.id)
            elif res.asset.state is AssetState.UNCERTAIN:
                uncertain_ids.append(asset.id)
            else:
                failed_ids.append(asset.id)
        except PublishPolicyBlockedError as exc:
            blocked_reasons[asset.id] = f"{exc.decision.reason_code}: {exc.decision.message}"
        except Exception as exc:
            failed_ids.append(asset.id)
            blocked_reasons[asset.id] = str(exc)

    await _recompute_video_workflow_state(session, video)
    return BatchPublishVideoResponse(
        youtube_video_id=video.youtube_video_id,
        workflow_state=video.workflow_state,
        published_asset_ids=published_ids,
        skipped_asset_ids=skipped_ids,
        uncertain_asset_ids=uncertain_ids,
        failed_asset_ids=failed_ids,
        blocked_reasons=blocked_reasons,
        quota_units_used=total_quota,
    )


async def list_video_publish_history(
    session: AsyncSession,
    *,
    youtube_video_id: str,
) -> list[VideoPublishHistoryItem]:
    """Return chronological (newest-first) publish attempts for all assets of a video."""
    stmt = (
        select(PublishAttempt, LanguageAsset, Video)
        .join(LanguageAsset, PublishAttempt.asset_id == LanguageAsset.id)
        .join(Video, LanguageAsset.video_id == Video.id)
        .where(Video.youtube_video_id == youtube_video_id)
        .order_by(PublishAttempt.id.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        VideoPublishHistoryItem(
            id=att.id,
            asset_id=asset.id,
            youtube_video_id=vid.youtube_video_id,
            language=asset.language,
            kind=asset.kind,
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
        for att, asset, vid in rows
    ]
