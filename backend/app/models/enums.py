"""Domain enums for videos, caption diagnostics, language assets, jobs, and publishing."""

from __future__ import annotations

from enum import StrEnum


class ChannelConnectionStatus(StrEnum):
    CONNECTED = "connected"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105
    REVOKED = "revoked"
    ERROR = "error"


class VideoPrivacyStatus(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNLISTED = "unlisted"


class VideoWorkflowState(StrEnum):
    DISCOVERED = "discovered"
    AWAITING_SOURCE = "awaiting_source"
    SOURCE_READY = "source_ready"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


class SourceCaptionTrackKind(StrEnum):
    STANDARD = "standard"
    ASR = "asr"
    FORCED = "forced"
    UNKNOWN = "unknown"


class SourceCaptionStatus(StrEnum):
    SERVING = "serving"
    SYNCING = "syncing"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AssetKind(StrEnum):
    METADATA = "metadata"
    CAPTION = "caption"


class AssetState(StrEnum):
    PENDING = "pending"
    AWAITING_SOURCE = "awaiting_source"
    TRANSLATING = "translating"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    NEEDS_ATTENTION = "needs_attention"
    AWAITING_APPROVAL = "awaiting_approval"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    UNCERTAIN = "uncertain"
    PUBLISHED = "published"
    SKIPPED = "skipped"
    FAILED = "failed"


# States from which a language asset can be safely retried without risking duplicate writes.
# Note: UNCERTAIN requires reconciliation before retry!
RETRYABLE_ASSET_STATES: frozenset[AssetState] = frozenset(
    {
        AssetState.PENDING,
        AssetState.AWAITING_SOURCE,
        AssetState.NEEDS_ATTENTION,
        AssetState.FAILED,
    }
)


class PublishOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class JobType(StrEnum):
    DISCOVER_VIDEO = "discover_video"
    INSPECT_SOURCE_CAPTIONS = "inspect_source_captions"
    PROCESS_VIDEO = "process_video"
    TRANSLATE_ASSET = "translate_asset"
    PUBLISH_ASSET = "publish_asset"
    RECONCILE_UPLOADS = "reconcile_uploads"
    RENEW_WEBSUB = "renew_websub"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


RETRYABLE_JOB_STATES: frozenset[JobState] = frozenset(
    {
        JobState.QUEUED,
        JobState.RETRY_SCHEDULED,
        JobState.DEAD_LETTER,
    }
)
