"""Durable, idempotent, resumable background job and asset state helpers.

Backed by the relational database (`jobs` and `language_assets` tables) — never
in-memory queues.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.models.entities import Job, LanguageAsset
from app.models.enums import (
    RETRYABLE_ASSET_STATES,
    RETRYABLE_JOB_STATES,
    AssetState,
    JobState,
    JobType,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job: Job
    created: bool


async def enqueue_job_idempotent(
    session: AsyncSession,
    *,
    organization_id: str = LEGACY_ORGANIZATION_ID,
    dedupe_key: str,
    job_type: JobType,
    payload: dict[str, Any],
    max_attempts: int = 5,
    run_after: timedelta | None = None,
    now: datetime | None = None,
) -> EnqueueResult:
    """Idempotently enqueue a durable job by `dedupe_key`.

    If an active job with the same `dedupe_key` already exists, returns it with
    `created=False` instead of creating a duplicate.
    """
    current = now or _utcnow()
    next_run = current + run_after if run_after else current

    stmt = select(Job).where(
        Job.organization_id == organization_id,
        Job.dedupe_key == dedupe_key,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return EnqueueResult(job=existing, created=False)

    job = Job(
        organization_id=organization_id,
        dedupe_key=dedupe_key,
        job_type=job_type,
        state=JobState.QUEUED,
        payload=payload,
        attempt_count=0,
        max_attempts=max_attempts,
        next_run_at=next_run,
        created_at=current,
        updated_at=current,
    )
    session.add(job)
    await session.flush()
    return EnqueueResult(job=job, created=True)


async def enqueue_video_upload_notification(
    session: AsyncSession,
    *,
    youtube_video_id: str,
    youtube_channel_id: str,
    authorized_channel_id: str,
    organization_id: str = LEGACY_ORGANIZATION_ID,
    now: datetime | None = None,
) -> EnqueueResult:
    """Validate that an upload notification belongs to the authorized channel and enqueue.

    Duplicate notifications for the same `youtube_video_id` return the existing job
    with `created=False`.
    """
    if not authorized_channel_id or youtube_channel_id != authorized_channel_id:
        raise PermissionError(
            f"Upload notification channel {youtube_channel_id!r} does not match "
            f"authorized channel {authorized_channel_id!r}."
        )
    dedupe_key = f"process_video:{youtube_video_id}"
    return await enqueue_job_idempotent(
        session,
        organization_id=organization_id,
        dedupe_key=dedupe_key,
        job_type=JobType.PROCESS_VIDEO,
        payload={
            "youtube_video_id": youtube_video_id,
            "youtube_channel_id": youtube_channel_id,
            "organization_id": organization_id,
        },
        now=now,
    )


def record_job_failure_and_schedule_retry(
    job: Job,
    error_message: str,
    *,
    base_delay_seconds: int = 30,
    now: datetime | None = None,
) -> JobState:
    """Record a failure on `job` and either schedule a backoff retry or move to DEAD_LETTER."""
    current = now or _utcnow()
    job.attempt_count += 1
    job.last_error = error_message
    job.locked_by = None
    job.locked_until = None
    job.updated_at = current

    if job.attempt_count >= job.max_attempts:
        job.state = JobState.DEAD_LETTER
        return job.state

    delay = base_delay_seconds * (2 ** (job.attempt_count - 1))
    job.next_run_at = current + timedelta(seconds=delay)
    job.state = JobState.RETRY_SCHEDULED
    return job.state


def reset_job_for_manual_retry(job: Job, *, now: datetime | None = None) -> None:
    """Safely re-queue a job from a retryable state."""
    if job.state not in RETRYABLE_JOB_STATES:
        raise ValueError(f"Job in state {job.state.value!r} cannot be retried.")
    current = now or _utcnow()
    job.state = JobState.QUEUED
    job.attempt_count = 0
    job.next_run_at = current
    job.locked_by = None
    job.locked_until = None
    job.updated_at = current


def prepare_asset_for_retry(asset: LanguageAsset, *, now: datetime | None = None) -> None:
    """Safely reset a failed or attention-needed language asset to PENDING for retry.

    Refuses to reset an asset in `UNCERTAIN`, `PUBLISHING`, `VERIFYING`, or `PUBLISHED`
    states to prevent duplicate writes before reconciliation.
    """
    if asset.state not in RETRYABLE_ASSET_STATES:
        raise ValueError(
            f"LanguageAsset in state {asset.state.value!r} cannot be directly retried "
            "without reconciliation."
        )
    current = now or _utcnow()
    asset.state = AssetState.PENDING
    asset.updated_at = current
