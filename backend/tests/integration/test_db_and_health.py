"""Integration tests for FastAPI, SQLite/PostgreSQL persistence, migrations, and jobs."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command
from app.core.settings import AppEnv, Settings
from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.db.session import Base, create_engine, create_session_factory
from app.main import create_app
from app.models import (
    AssetKind,
    AssetState,
    Job,
    JobState,
    LanguageAsset,
    Organization,
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
    "users",
    "organizations",
    "organization_members",
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

    from app.core.tenancy import LEGACY_ORGANIZATION_ID, LEGACY_ORGANIZATION_SLUG

    now = datetime.now(UTC)
    async with AsyncSession(engine) as seed_session:
        seed_session.add(
            Organization(
                id=LEGACY_ORGANIZATION_ID,
                name="Legacy Workspace",
                slug=LEGACY_ORGANIZATION_SLUG,
                is_legacy=True,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        await seed_session.commit()

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
                "sl",
                "ur",
                "he",
                "ja",
                "ko",
                "zh",
            } <= set(r_body["enabled_target_languages"])
            assert len(r_body["enabled_target_languages"]) == 47
            assert r_body["checks"]["database"] is True
            assert r_body["checks"]["config"] is True


async def test_legacy_video_endpoints_do_not_expose_another_organization(
    tmp_path: Path, config_dir: Path
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'tenant-api.db'}",
        config_dir=config_dir,
        youtube_publishing_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        now = datetime.now(UTC)
        foreign_org_id = str(uuid4())
        async with app.state.session_factory() as session:
            session.add(
                Organization(
                    id=foreign_org_id,
                    name="Other Workspace",
                    slug=f"other-{foreign_org_id[:8]}",
                    is_legacy=False,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Video(
                    organization_id=foreign_org_id,
                    youtube_video_id="foreignvid01",
                    youtube_channel_id="foreign-channel",
                    source_language="tr",
                    source_title="Private workspace video",
                    source_description="",
                    source_content_hash="sha256:foreign",
                    privacy_status=VideoPrivacyStatus.PRIVATE,
                    workflow_state=VideoWorkflowState.DISCOVERED,
                    existing_localizations={},
                    discovered_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/videos")
            assert listed.status_code == 200
            assert "foreignvid01" not in {row["youtube_video_id"] for row in listed.json()}
            detail = await client.get("/api/videos/foreignvid01/assets")
            assert detail.status_code == 404


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


def test_tenant_migration_backfills_existing_jobs(tmp_path: Path, repo_root: Path) -> None:
    db_file = tmp_path / "tenant_backfill.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    alembic_cfg = Config(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(repo_root / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "0001_initial_schema")

    import sqlalchemy as sa

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO jobs (dedupe_key, job_type, state, payload, attempt_count, "
                    "max_attempts, next_run_at, created_at, updated_at) VALUES "
                    "('process_video:legacy', 'process_video', 'queued', '{}', 0, 5, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
    finally:
        sync_engine.dispose()

    command.upgrade(alembic_cfg, "head")
    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with sync_engine.connect() as connection:
            org_id = connection.execute(
                sa.text(
                    "SELECT organization_id FROM jobs "
                    "WHERE dedupe_key = 'process_video:legacy'"
                )
            ).scalar_one()
            assert org_id == "00000000-0000-4000-8000-000000000001"
            constraints = inspect(sync_engine).get_unique_constraints("jobs")
            assert any(
                item["column_names"] == ["organization_id", "dedupe_key"]
                for item in constraints
            )
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
        organization_id=LEGACY_ORGANIZATION_ID,
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
        organization_id=LEGACY_ORGANIZATION_ID,
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
