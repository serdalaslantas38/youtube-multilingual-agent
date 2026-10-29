"""Initial schema for YouTube Multilingual Publishing Agent.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-10-27 13:55:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("youtube_channel_id", sa.String(length=64), nullable=False),
        sa.Column("channel_title", sa.String(length=255), nullable=False),
        sa.Column("custom_url", sa.String(length=255), nullable=True),
        sa.Column("uploads_playlist_id", sa.String(length=64), nullable=True),
        sa.Column("authorized_google_email", sa.String(length=320), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("token_warning", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_connections")),
        sa.UniqueConstraint(
            "youtube_channel_id", name=op.f("uq_channel_connections_youtube_channel_id")
        ),
    )

    op.create_table(
        "videos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("youtube_video_id", sa.String(length=32), nullable=False),
        sa.Column("youtube_channel_id", sa.String(length=64), nullable=False),
        sa.Column("source_language", sa.String(length=16), nullable=False),
        sa.Column("default_audio_language", sa.String(length=16), nullable=True),
        sa.Column("source_title", sa.String(length=200), nullable=False),
        sa.Column("source_description", sa.Text(), nullable=False),
        sa.Column("source_category_id", sa.String(length=16), nullable=True),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("privacy_status", sa.String(length=64), nullable=False),
        sa.Column("scheduled_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workflow_state", sa.String(length=64), nullable=False),
        sa.Column("existing_localizations", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_videos")),
        sa.UniqueConstraint("youtube_video_id", name=op.f("uq_videos_youtube_video_id")),
    )
    op.create_index(
        op.f("ix_videos_youtube_channel_id"), "videos", ["youtube_channel_id"], unique=False
    )
    op.create_index(op.f("ix_videos_workflow_state"), "videos", ["workflow_state"], unique=False)

    op.create_table(
        "source_caption_tracks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("youtube_caption_id", sa.String(length=128), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("track_kind", sa.String(length=64), nullable=False),
        sa.Column("processing_status", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.String(length=128), nullable=True),
        sa.Column("is_draft", sa.Boolean(), nullable=False),
        sa.Column("is_auto_synced", sa.Boolean(), nullable=False),
        sa.Column("is_downloadable", sa.Boolean(), nullable=True),
        sa.Column("download_error", sa.Text(), nullable=True),
        sa.Column("tlang_diagnostics", sa.JSON(), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=True),
        sa.Column("content_format", sa.String(length=16), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("cue_count", sa.Integer(), nullable=True),
        sa.Column("last_updated_on_youtube", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_source_caption_tracks_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_caption_tracks")),
        sa.UniqueConstraint("video_id", "youtube_caption_id", name="uq_source_caption_video_track"),
    )
    op.create_index(
        op.f("ix_source_caption_tracks_video_id"),
        "source_caption_tracks",
        ["video_id"],
        unique=False,
    )

    op.create_table(
        "language_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider_used", sa.String(length=64), nullable=True),
        sa.Column("source_caption_track_id", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("translated_title", sa.String(length=200), nullable=True),
        sa.Column("translated_description", sa.Text(), nullable=True),
        sa.Column("caption_content", sa.Text(), nullable=True),
        sa.Column("caption_format", sa.String(length=16), nullable=True),
        sa.Column("youtube_published_caption_id", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("validation_passed", sa.Boolean(), nullable=True),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("review_passed", sa.Boolean(), nullable=True),
        sa.Column("source_ambiguous", sa.Boolean(), nullable=False),
        sa.Column("review_report", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_caption_track_id"],
            ["source_caption_tracks.id"],
            name=op.f("fk_language_assets_source_caption_track_id_source_caption_tracks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_language_assets_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_language_assets")),
        sa.UniqueConstraint(
            "video_id", "language", "kind", name="uq_language_assets_video_lang_kind"
        ),
    )
    op.create_index(
        op.f("ix_language_assets_video_id"), "language_assets", ["video_id"], unique=False
    )
    op.create_index(
        op.f("ix_language_assets_language"), "language_assets", ["language"], unique=False
    )
    op.create_index(op.f("ix_language_assets_state"), "language_assets", ["state"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("approved_content_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=320), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["language_assets.id"],
            name=op.f("fk_approvals_asset_id_language_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(op.f("ix_approvals_asset_id"), "approvals", ["asset_id"], unique=False)

    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("response_summary", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["language_assets.id"],
            name=op.f("fk_publish_attempts_asset_id_language_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_attempts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_publish_attempts_idempotency_key")),
    )
    op.create_index(
        op.f("ix_publish_attempts_asset_id"), "publish_attempts", ["asset_id"], unique=False
    )
    op.create_index(
        op.f("ix_publish_attempts_outcome"), "publish_attempts", ["outcome"], unique=False
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_jobs_dedupe_key")),
    )
    op.create_index(op.f("ix_jobs_job_type"), "jobs", ["job_type"], unique=False)
    op.create_index("ix_jobs_state_next_run_at", "jobs", ["state", "next_run_at"], unique=False)

    op.create_table(
        "quota_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quota_date_pt", sa.Date(), nullable=False),
        sa.Column("api_method", sa.String(length=64), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.String(length=32), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_ledger")),
    )
    op.create_index(
        op.f("ix_quota_ledger_quota_date_pt"),
        "quota_ledger",
        ["quota_date_pt"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(length=320), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_quota_ledger_quota_date_pt"), table_name="quota_ledger")
    op.drop_table("quota_ledger")
    op.drop_index("ix_jobs_state_next_run_at", table_name="jobs")
    op.drop_index(op.f("ix_jobs_job_type"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_publish_attempts_outcome"), table_name="publish_attempts")
    op.drop_index(op.f("ix_publish_attempts_asset_id"), table_name="publish_attempts")
    op.drop_table("publish_attempts")
    op.drop_index(op.f("ix_approvals_asset_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_index(op.f("ix_language_assets_state"), table_name="language_assets")
    op.drop_index(op.f("ix_language_assets_language"), table_name="language_assets")
    op.drop_index(op.f("ix_language_assets_video_id"), table_name="language_assets")
    op.drop_table("language_assets")
    op.drop_index(op.f("ix_source_caption_tracks_video_id"), table_name="source_caption_tracks")
    op.drop_table("source_caption_tracks")
    op.drop_index(op.f("ix_videos_workflow_state"), table_name="videos")
    op.drop_index(op.f("ix_videos_youtube_channel_id"), table_name="videos")
    op.drop_table("videos")
    op.drop_table("channel_connections")
