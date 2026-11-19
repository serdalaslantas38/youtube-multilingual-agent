"""Pydantic schemas for Stage 6 approvals, asset editing, policy overrides, and audit logs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.app_config import PublishMode
from app.models.enums import AssetKind, AssetState, PublishOutcome


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveAssetRequest(_StrictRequest):
    """Request body for approving a localized asset bound to its exact content hash."""

    expected_content_hash: str = Field(
        min_length=7,
        description="SHA-256 content hash currently displayed to the reviewer.",
    )
    notes: str | None = None
    override_review_issues: bool = Field(
        default=False,
        description=(
            "Allow human reviewer to approve an asset even if automated LLM review "
            "flagged issues, provided deterministic validation passed."
        ),
    )


class RejectAssetRequest(_StrictRequest):
    """Request body for rejecting a localized asset."""

    reason: str = Field(min_length=1, max_length=1000)


class EditAssetRequest(_StrictRequest):
    """Request body for editing a localized metadata or caption asset."""

    translated_title: str | None = None
    translated_description: str | None = None
    caption_content: str | None = None


class AlignedCuePair(BaseModel):
    """Side-by-side source and translated subtitle cue for the review dashboard."""

    index: int
    start_ms: int
    end_ms: int
    source_text: str
    translated_text: str


class ApprovalRecordSummary(BaseModel):
    """Summary of an Approval record."""

    id: int
    approved_content_hash: str
    approved_by: str
    notes: str | None
    approved_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None
    is_valid_for_current_hash: bool


class PublishDecisionPreview(BaseModel):
    """Deterministic policy engine decision preview for an asset."""

    allowed: bool
    effective_mode: PublishMode
    reason_code: str
    message: str


class PublishAttemptRecordSummary(BaseModel):
    """Summary of a `PublishAttempt` row for an asset."""

    id: int
    asset_id: int
    idempotency_key: str
    operation: str
    request_digest: str
    outcome: PublishOutcome
    response_summary: dict[str, Any]
    error_message: str | None = None
    created_at: datetime
    verified_at: datetime | None = None
    reconciled_at: datetime | None = None


class AssetDetailResponse(BaseModel):
    """Full side-by-side asset detail for the Stage 6/7 review & publish editor."""

    id: int
    youtube_video_id: str
    language: str
    language_name: str
    native_name: str
    is_rtl: bool
    kind: AssetKind
    state: AssetState
    provider_used: str | None
    prompt_version: str | None

    # Source Turkish content
    source_title: str
    source_description: str
    source_caption_content: str | None = None

    # Translated content
    translated_title: str | None = None
    translated_description: str | None = None
    caption_content: str | None = None
    caption_format: str | None = None
    youtube_published_caption_id: str | None = None
    aligned_cues: list[AlignedCuePair] = Field(default_factory=list)

    # Hashes, validation, review, approvals, and publish attempts
    content_hash: str | None
    validation_passed: bool | None
    validation_report: dict[str, Any]
    review_passed: bool | None
    source_ambiguous: bool
    review_report: dict[str, Any]
    active_approval: ApprovalRecordSummary | None
    approval_history: list[ApprovalRecordSummary]
    publish_decision: PublishDecisionPreview
    publish_attempts: list[PublishAttemptRecordSummary] = Field(default_factory=list)
    last_error: str | None = None
    updated_at: datetime


class LanguagePolicyItem(BaseModel):
    """Effective per-language configuration and publishing policy."""

    code: str
    name: str
    native_name: str
    caption_tlang: str
    rtl: bool
    enabled: bool
    popular: bool = False
    configured_mode: PublishMode
    effective_mode: PublishMode


class PolicySettingsResponse(BaseModel):
    """Complete dashboard policy and language configuration response."""

    source_language_code: str
    source_language_name: str
    youtube_publishing_enabled: bool
    global_auto_publish_enabled: bool
    default_mode: PublishMode
    protected_glossary: list[str]
    translation_sources: list[str]
    languages: list[LanguagePolicyItem]


class UpdateLanguagePolicyRequest(_StrictRequest):
    """Request to update a language's publishing mode or enabled flag."""

    mode: PublishMode | None = None
    enabled: bool | None = None
    name: str | None = None
    native_name: str | None = None
    caption_tlang: str | None = None
    rtl: bool | None = None


class UpdateGlobalPolicyRequest(_StrictRequest):
    """Request to toggle the global `auto_publish_enabled` switch."""

    auto_publish_enabled: bool
    reason: str = Field(min_length=3, max_length=500)


class AuditEventItem(BaseModel):
    """Immutable audit log entry."""

    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, Any]
    created_at: datetime
