"""Pydantic schemas for LLM agent structured outputs and localization API responses."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import AssetKind, AssetState, VideoWorkflowState


class _StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalizedMetadataLLMOutput(_StrictSchema):
    """Schema-validated output from the metadata localization LLM agent."""

    title: str = Field(min_length=1)
    description: str = Field(default="")
    source_ambiguous: bool = False
    ambiguity_notes: str | None = None


class TranslatedCueItem(_StrictSchema):
    """Single translated subtitle cue returned by the caption translation LLM agent."""

    index: int = Field(ge=1)
    text: str = Field(min_length=1)


class TranslatedCueBatchLLMOutput(_StrictSchema):
    """Schema-validated batch output from the caption translation LLM agent."""

    cues: list[TranslatedCueItem] = Field(min_length=1)
    source_ambiguous: bool = False
    ambiguity_notes: str | None = None


class ReviewIssueCategory(StrEnum):
    COMPLETENESS = "completeness"
    CONSISTENCY = "consistency"
    ALTERED_MEANING = "altered_meaning"
    PLACEHOLDER_MISMATCH = "placeholder_mismatch"
    TIMING_OR_FORMATTING = "timing_or_formatting"
    SOURCE_AMBIGUOUS = "source_ambiguous"


class ReviewIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class QualityReviewIssue(_StrictSchema):
    """Structured issue emitted by the quality-review agent or deterministic cross-checks."""

    category: ReviewIssueCategory
    severity: ReviewIssueSeverity
    message: str = Field(min_length=1)
    location: str = Field(default="general")


class QualityReviewLLMOutput(_StrictSchema):
    """Schema-validated output from the quality-review agent."""

    passed: bool
    source_ambiguous: bool = False
    summary: str = Field(min_length=1)
    issues: list[QualityReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_pass_consistency(self) -> QualityReviewLLMOutput:
        has_blocking = any(i.severity is ReviewIssueSeverity.BLOCKING for i in self.issues)
        if (has_blocking or self.source_ambiguous) and self.passed:
            # Force `passed=False` whenever a blocking issue or source ambiguity is present
            object.__setattr__(self, "passed", False)
        return self


class LocalizeVideoRequest(BaseModel):
    """Request body for triggering localization on a discovered video."""

    model_config = ConfigDict(extra="forbid")

    languages: list[str] | None = Field(
        default=None,
        description="Optional subset of enabled target language codes (defaults to all enabled).",
    )
    include_metadata: bool = True
    include_captions: bool = True
    force_regenerate: bool = False


class LanguageAssetSummary(BaseModel):
    """API representation of a localized metadata or caption asset."""

    id: int
    youtube_video_id: str
    language: str
    kind: AssetKind
    state: AssetState
    provider_used: str | None
    prompt_version: str | None
    translated_title: str | None = None
    translated_description: str | None = None
    caption_format: str | None = None
    caption_cue_count: int | None = None
    content_hash: str | None
    validation_passed: bool | None
    validation_report: dict[str, Any]
    review_passed: bool | None
    source_ambiguous: bool
    review_report: dict[str, Any]
    attempt_count: int
    last_error: str | None
    updated_at: datetime


class VideoLocalizationResponse(BaseModel):
    """Summary returned after running or querying localization for a video."""

    youtube_video_id: str
    source_title: str
    workflow_state: VideoWorkflowState
    source_caption_ready: bool
    selected_source_caption_id: str | None = None
    selected_source_track_kind: str | None = None
    assets: list[LanguageAssetSummary]
    quota_units_used_this_run: int = 0
