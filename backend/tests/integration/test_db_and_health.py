"""Integration tests for FastAPI, SQLite/PostgreSQL persistence, migrations, and jobs."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command
from app.core.settings import AppEnv, Settings
from app.db.session import Base, create_engine, create_session_factory
from app.main import create_app
from app.models import (
    AssetKind,
    AssetState,
    Job,
    JobState,
    LanguageAsset,
    SourceCaptionStatus,
    SourceCaptionTrack,
    SourceCaptionTrackKind,
    Video,
    VideoPrivacyStatus,
    VideoWorkflowState,
)
from app.tasks import (
    enqueue_video_upload_notification,
    prepare_asset_for_retry,
    record_job_failure_and_schedule_retry,
    reset_job_for_manual_retry,
)

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "channel_connections",
    "videos",
    "source_caption_tracks",
    "language_assets",
    "approvals",
    "publish_attempts",
    "jobs",
    "quota_ledger",
    "audit_events",
}


def _candidate_db_urls(tmp_path: Path) -> list[str]:
    urls = [f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"]
    pg_url = os.environ.get("TEST_POSTGRES_URL")
    if pg_url:
        urls.append(pg_url)
    return urls


@pytest.fixture(params=["sqlite", "postgres"])
async def db_session(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AsyncSession]:
    kind: str = request.param
    if kind == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'integration.db'}"
    else:
        pg_url = os.environ.get("TEST_POSTGRES_URL")
        if not pg_url:
            pytest.skip(
                "PostgreSQL integration test skipped (set TEST_POSTGRES_URL or run `make db-up`)."
            )
        url = pg_url

    engine: AsyncEngine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = create_session_factory(engine)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_health_and_readiness_endpoints(tmp_path: Path, config_dir: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=AppEnv.TEST,
        database_url=db_url,
        config_dir=config_dir,
        youtube_publishing_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            h_body = health.json()
            assert h_body["status"] == "ok"
            assert h_body["youtube_publishing_enabled"] is False

            ready = await client.get("/ready")
            assert ready.status_code == 200
            r_body = ready.json()
            assert r_body["status"] == "ready"
            assert r_body["youtube_publishing_enabled"] is False
            assert r_body["auto_publish_enabled"] is False
            assert r_body["source_language"] == "tr"
            assert {
                "en",
                "ro",
                "es",
                "fr",
                "de",
                "ar",
                "it",
                "ru",
                "sq",
                "pt-PT",
                "fa",
                "ur",
                "he",
                "ja",
                "ko",
                "zh",
            } <= set(r_body["enabled_target_languages"])
            assert len(r_body["enabled_target_languages"]) == 50
            assert r_body["checks"]["database"] is True
            assert r_body["checks"]["config"] is True


def test_alembic_upgrade_and_downgrade_sqlite(tmp_path: Path, repo_root: Path) -> None:
    db_file = tmp_path / "alembic_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    alembic_cfg = Config(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(repo_root / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(alembic_cfg, "head")

    import sqlalchemy as sa

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        tables = set(inspect(sync_engine).get_table_names())
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        sync_engine.dispose()

    command.downgrade(alembic_cfg, "base")
    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        remaining = set(inspect(sync_engine).get_table_names()) - {"alembic_version"}
        assert remaining == set()
    finally:
        sync_engine.dispose()


async def test_duplicate_video_notifications_are_deduplicated(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    first = await enqueue_video_upload_notification(
        db_session,
        youtube_video_id="vid_tr_001",
        youtube_channel_id="UC_TURKISH_CHANNEL",
        authorized_channel_id="UC_TURKISH_CHANNEL",
        now=now,
    )
    await db_session.commit()
    assert first.created is True
    assert first.job.state is JobState.QUEUED

    # Second identical notification must not create a second job
    second = await enqueue_video_upload_notification(
        db_session,
        youtube_video_id="vid_tr_001",
        youtube_channel_id="UC_TURKISH_CHANNEL",
        authorized_channel_id="UC_TURKISH_CHANNEL",
        now=now,
    )
    await db_session.commit()
    assert second.created is False
    assert second.job.id == first.job.id

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert len(jobs) == 1


async def test_notification_for_unauthorized_channel_is_rejected(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(PermissionError, match="does not match authorized channel"):
        await enqueue_video_upload_notification(
            db_session,
            youtube_video_id="vid_other",
            youtube_channel_id="UC_STRANGER",
            authorized_channel_id="UC_TURKISH_CHANNEL",
        )


async def test_private_and_scheduled_videos_and_caption_diagnostics_persistence(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    scheduled_time = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)

    video = Video(
        youtube_video_id="vid_sched_01",
        youtube_channel_id="UC_TURKISH_CHANNEL",
        source_language="tr",
        source_title="Türkçe Tarif Videosu",
        source_description="Açıklama metni",
        source_content_hash="sha256:source1",
        privacy_status=VideoPrivacyStatus.PRIVATE,
        scheduled_publish_at=scheduled_time,
        workflow_state=VideoWorkflowState.AWAITING_SOURCE,
        discovered_at=now,
        updated_at=now,
    )
    db_session.add(video)
    await db_session.flush()

    track = SourceCaptionTrack(
        video_id=video.id,
        youtube_caption_id="caption_asr_tr",
        language="tr",
        name="Turkish (auto-generated)",
        track_kind=SourceCaptionTrackKind.ASR,
        processing_status=SourceCaptionStatus.SERVING,
        is_draft=False,
        is_auto_synced=True,
        is_downloadable=True,
        tlang_diagnostics={"en": "ok", "ro": "ok"},
        inspected_at=now,
    )
    db_session.add(track)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(Video).where(Video.youtube_video_id == "vid_sched_01"))
    ).scalar_one()
    assert fetched.privacy_status is VideoPrivacyStatus.PRIVATE
    assert fetched.scheduled_publish_at is not None

    fetched_track = (
        await db_session.execute(
            select(SourceCaptionTrack).where(SourceCaptionTrack.video_id == fetched.id)
        )
    ).scalar_one()
    assert fetched_track.track_kind is SourceCaptionTrackKind.ASR
    assert fetched_track.processing_status is SourceCaptionStatus.SERVING
    assert fetched_track.tlang_diagnostics["en"] == "ok"


async def test_job_and_language_asset_explicit_states_and_safe_retries(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    res = await enqueue_video_upload_notification(
        db_session,
        youtube_video_id="vid_retry_01",
        youtube_channel_id="UC_TURKISH_CHANNEL",
        authorized_channel_id="UC_TURKISH_CHANNEL",
        now=now,
    )
    job = res.job
    job.max_attempts = 2

    # First failure schedules retry with backoff
    state1 = record_job_failure_and_schedule_retry(job, "Caption track still syncing", now=now)
    assert state1 is JobState.RETRY_SCHEDULED
    assert job.attempt_count == 1
    assert job.next_run_at > now

    # Second failure reaches max_attempts and moves to DEAD_LETTER
    state2 = record_job_failure_and_schedule_retry(job, "Still missing", now=now)
    assert state2 is JobState.DEAD_LETTER

    # Manual retry resets DEAD_LETTER job back to QUEUED safely
    reset_job_for_manual_retry(job, now=now)
    assert job.state is JobState.QUEUED
    assert job.attempt_count == 0

    # Create a video and language asset to verify asset retry safety
    video = Video(
        youtube_video_id="vid_retry_01",
        youtube_channel_id="UC_TURKISH_CHANNEL",
        source_language="tr",
        source_title="Başlık",
        source_description="Açıklama",
        source_content_hash="sha256:1",
        privacy_status=VideoPrivacyStatus.PUBLIC,
        workflow_state=VideoWorkflowState.PROCESSING,
        discovered_at=now,
        updated_at=now,
    )
    db_session.add(video)
    await db_session.flush()

    asset = LanguageAsset(
        video_id=video.id,
        language="en",
        kind=AssetKind.METADATA,
        state=AssetState.FAILED,
        attempt_count=1,
        last_error="Temporary timeout",
        created_at=now,
        updated_at=now,
    )
    db_session.add(asset)
    await db_session.flush()

    prepare_asset_for_retry(asset, now=now)
    assert asset.state is AssetState.PENDING

    # Asset in UNCERTAIN state must NOT be directly retried without reconciliation
    asset.state = AssetState.UNCERTAIN
    with pytest.raises(ValueError, match="cannot be directly retried"):
        prepare_asset_for_retry(asset, now=now)

    # Unique constraint prevents duplicate (video_id, language, kind)
    duplicate_asset = LanguageAsset(
        video_id=video.id,
        language="en",
        kind=AssetKind.METADATA,
        state=AssetState.PENDING,
        created_at=now,
        updated_at=now,
    )
    db_session.add(duplicate_asset)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
