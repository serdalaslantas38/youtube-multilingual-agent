"""SQLAlchemy 2.0 ORM models for the YouTube Multilingual Publishing Agent.

Design safeguards encoded in schema:
- Both immediately public and private/scheduled videos are supported (`privacy_status`,
  `scheduled_publish_at`), with a strict rule that the application never modifies
  a video's privacy or scheduling settings.
- Source caption diagnostics record `track_kind` (standard vs. ASR), language,
  `processing_status` (serving/syncing/failed), `is_draft`, and `tlang` capabilities
  without assuming how Turkish subtitles were created.
- Channel connection stores the authenticated channel identity (`youtube_channel_id`,
  `channel_title`, `custom_url`, `uploads_playlist_id`, `is_brand_account_suspected`)
  with encrypted refresh token storage.
- Every `LanguageAsset` and `Job` has an explicit status enum, retry counters,
  and deterministic idempotency/dedupe keys.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import (
    AssetKind,
    AssetState,
    ChannelConnectionStatus,
    JobState,
    JobType,
    PublishOutcome,
    SourceCaptionStatus,
    SourceCaptionTrackKind,
    VideoPrivacyStatus,
    VideoWorkflowState,
)


def _enum_col(enum_cls: type[Any]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
        length=64,
    )


class ChannelConnection(Base):
    """Authorized YouTube channel identity and encrypted OAuth credentials."""

    __tablename__ = "channel_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    youtube_channel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_title: Mapped[str] = mapped_column(String(255), nullable=False)
    custom_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorized_google_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[ChannelConnectionStatus] = mapped_column(
        _enum_col(ChannelConnectionStatus),
        nullable=False,
        default=ChannelConnectionStatus.CONNECTED,
    )
    token_WARNING: Mapped[str | None] = mapped_column("token_warning", Text, nullable=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="channel_connections")


class Video(Base):
    """Tracked YouTube video (public, unlisted, private, or scheduled).

    NOTE: `privacy_status` and `scheduled_publish_at` are read-only snapshots from
    YouTube so the orchestrator can prioritize localizing private/scheduled videos
    before their release time. The application NEVER mutates these fields on YouTube.
    """

    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    youtube_video_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    source_language: Mapped[str] = mapped_column(String(16), nullable=False, default="tr")
    default_audio_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_category_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    privacy_status: Mapped[VideoPrivacyStatus] = mapped_column(
        _enum_col(VideoPrivacyStatus),
        nullable=False,
        default=VideoPrivacyStatus.PRIVATE,
    )
    scheduled_publish_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow_state: Mapped[VideoWorkflowState] = mapped_column(
        _enum_col(VideoWorkflowState),
        nullable=False,
        default=VideoWorkflowState.DISCOVERED,
        index=True,
    )
    existing_localizations: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source_captions: Mapped[list[SourceCaptionTrack]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    language_assets: Mapped[list[LanguageAsset]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    organization: Mapped[Organization] = relationship(back_populates="videos")


class SourceCaptionTrack(Base):
    """Discovered caption track on YouTube with full diagnostics.

    Does not assume whether Turkish subtitles are uploaded files (`standard`)
    or auto-generated (`asr`). Records processing status and download/tlang
    diagnostics before any translation strategy is chosen.
    """

    __tablename__ = "source_caption_tracks"
    __table_args__ = (
        UniqueConstraint("video_id", "youtube_caption_id", name="uq_source_caption_video_track"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    youtube_caption_id: Mapped[str] = mapped_column(String(128), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    track_kind: Mapped[SourceCaptionTrackKind] = mapped_column(
        _enum_col(SourceCaptionTrackKind),
        nullable=False,
        default=SourceCaptionTrackKind.UNKNOWN,
    )
    processing_status: Mapped[SourceCaptionStatus] = mapped_column(
        _enum_col(SourceCaptionStatus),
        nullable=False,
        default=SourceCaptionStatus.UNKNOWN,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_auto_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Diagnostics populated by caption inspection
    is_downloadable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tlang_diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cue_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_updated_on_youtube: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    video: Mapped[Video] = relationship(back_populates="source_captions")


class LanguageAsset(Base):
    """Per-video, per-language localized asset (either metadata or caption track)."""

    __tablename__ = "language_assets"
    __table_args__ = (
        UniqueConstraint("video_id", "language", "kind", name="uq_language_assets_video_lang_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[AssetKind] = mapped_column(_enum_col(AssetKind), nullable=False)
    state: Mapped[AssetState] = mapped_column(
        _enum_col(AssetState), nullable=False, default=AssetState.PENDING, index=True
    )

    # Provider / provenance
    provider_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_caption_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_caption_tracks.id", ondelete="SET NULL"), nullable=True
    )
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Content payload (metadata fields OR subtitle text)
    translated_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    translated_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    youtube_published_caption_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Hash of current content — bound to Approval so any edit invalidates approval
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Deterministic validation & LLM quality review reports
    validation_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Retry tracking
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    video: Mapped[Video] = relationship(back_populates="language_assets")
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    publish_attempts: Mapped[list[PublishAttempt]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class Approval(Base):
    """Explicit human approval bound to a specific `content_hash`."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("language_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approved_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(320), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    asset: Mapped[LanguageAsset] = relationship(back_populates="approvals")


class PublishAttempt(Base):
    """Idempotent record of a YouTube write operation and its post-write verification."""

    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("language_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[PublishOutcome] = mapped_column(
        _enum_col(PublishOutcome), nullable=False, index=True
    )
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    asset: Mapped[LanguageAsset] = relationship(back_populates="publish_attempts")


class Job(Base):
    """Durable, resumable, deduplicated background job record."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_state_next_run_at", "state", "next_run_at"),
        UniqueConstraint("organization_id", "dedupe_key", name="uq_jobs_org_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    job_type: Mapped[JobType] = mapped_column(_enum_col(JobType), nullable=False, index=True)
    state: Mapped[JobState] = mapped_column(
        _enum_col(JobState), nullable=False, default=JobState.QUEUED
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="jobs")


class QuotaLedger(Base):
    """Tracks daily YouTube Data API v3 quota usage (Pacific Time reset)."""

    __tablename__ = "quota_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quota_date_pt: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    api_method: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="quota_entries")


class AuditEvent(Base):
    """Immutable audit log for approvals, policy updates, and publishing actions."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="audit_events")


class User(Base):
    """Application identity keyed by OIDC issuer and subject, never by email alone."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    oidc_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memberships: Mapped[list[OrganizationMember]] = relationship(back_populates="user")


class Organization(Base):
    """Customer workspace that owns channels and workflow data."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memberships: Mapped[list[OrganizationMember]] = relationship(back_populates="organization")
    channel_connections: Mapped[list[ChannelConnection]] = relationship(
        back_populates="organization"
    )
    videos: Mapped[list[Video]] = relationship(back_populates="organization")
    jobs: Mapped[list[Job]] = relationship(back_populates="organization")
    quota_entries: Mapped[list[QuotaLedger]] = relationship(back_populates="organization")
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="organization")


class OrganizationMember(Base):
    """Role assignment joining an application user to an organization."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_members_org_user"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'viewer')",
            name="ck_organization_members_role_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")
