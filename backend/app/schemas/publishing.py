"""Pydantic schemas for Stage 7 controlled publishing, verification, and reconciliation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AssetKind, PublishOutcome, VideoWorkflowState
from app.schemas.approvals import AssetDetailResponse, PublishAttemptRecordSummary


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublishAssetRequest(_StrictRequest):
    """Request body for publishing a single localized asset to YouTube."""

    expected_content_hash: str | None = Field(
        default=None,
        description=(
            "Optional optimistic concurrency guard: if provided, must match "
            "the asset's current content_hash."
        ),
    )
    is_draft: bool = Field(
        default=False,
        description="For caption tracks, whether to upload as draft on YouTube.",
    )
    allow_set_default_language: bool = Field(
        default=True,
        description=(
            "When publishing metadata and snippet.defaultLanguage is unset on YouTube, "
            "allow setting snippet.defaultLanguage to the configured source language "
            "while preserving all existing snippet fields."
        ),
    )
    explicit_overwrite_authorized: bool = Field(
        default=False,
        description=(
            "Default False: never overwrite existing localizations or caption tracks "
            "on YouTube unless explicitly authorized."
        ),
    )


class ReconcileAssetRequest(_StrictRequest):
    """Request body for reconciling an asset with an uncertain publish outcome."""

    notes: str | None = Field(default=None, max_length=1000)


class PublishAssetResponse(BaseModel):
    """Response from publishing or reconciling a single asset."""

    asset: AssetDetailResponse
    attempt: PublishAttemptRecordSummary | None = None
    idempotent_reused: bool = False
    quota_units_used: int = 0


class BatchPublishVideoRequest(_StrictRequest):
    """Request body for publishing all eligible (`ready_to_publish`) assets on a video."""

    languages: list[str] | None = None
    include_metadata: bool = True
    include_captions: bool = True
    is_draft: bool = False
    allow_set_default_language: bool = True
    force: bool = Field(
        default=False,
        description=(
            "When True, skips manual reconciliation for uncertain attempts, "
            "auto-approves all validated localized assets bound to their content_hash, "
            "and publishes them immediately."
        ),
    )


class BatchPublishVideoResponse(BaseModel):
    """Summary response from batch-publishing eligible assets for a video."""

    youtube_video_id: str
    workflow_state: VideoWorkflowState
    published_asset_ids: list[int] = Field(default_factory=list)
    skipped_asset_ids: list[int] = Field(default_factory=list)
    uncertain_asset_ids: list[int] = Field(default_factory=list)
    failed_asset_ids: list[int] = Field(default_factory=list)
    blocked_reasons: dict[int, str] = Field(default_factory=dict)
    quota_units_used: int = 0


class VideoPublishHistoryItem(BaseModel):
    """Publish attempt history entry across all assets of a video."""

    id: int
    asset_id: int
    youtube_video_id: str
    language: str
    kind: AssetKind
    idempotency_key: str
    operation: str
    request_digest: str
    outcome: PublishOutcome
    response_summary: dict[str, Any]
    error_message: str | None = None
    created_at: datetime
    verified_at: datetime | None = None
    reconciled_at: datetime | None = None
