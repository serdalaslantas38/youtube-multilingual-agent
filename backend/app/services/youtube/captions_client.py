"""Typed models, request builders, and error taxonomy for YouTube Data API v3 Captions.

This module sits inside `app/services/youtube/`, the ONLY layer allowed to
interact with the YouTube Data API. It separates:
- request construction & validation (`CaptionListParams`, `CaptionDownloadParams`,
  `CaptionInsertRequest`)
- response parsing (`YouTubeCaptionTrackResource`, `parse_caption_list_response`)
- structured error classification (`YouTubeApiError`, `parse_youtube_api_error`)
- transport execution (`YouTubeCaptionClient` protocol and `MockYouTubeCaptionClient`)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SourceCaptionStatus, SourceCaptionTrackKind

_ISO639_1_RE = re.compile(r"^[a-z]{2}$")
_BCP47_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

# Official YouTube Data API v3 quota costs
QUOTA_CAPTIONS_LIST = 50
QUOTA_CAPTIONS_DOWNLOAD = 200
QUOTA_CAPTIONS_INSERT = 400


class CaptionDownloadFormat(StrEnum):
    """Supported `tfmt` values documented by `captions.download`."""

    SBV = "sbv"
    SCC = "scc"
    SRT = "srt"
    TTML = "ttml"
    VTT = "vtt"


class CaptionListParams(BaseModel):
    """Parameters for `GET https://www.googleapis.com/youtube/v3/captions`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: str = Field(min_length=1)
    part: Literal["snippet", "id", "id,snippet", "snippet,id"] = "snippet"
    on_behalf_of_content_owner: str | None = None

    def to_query_params(self) -> dict[str, str]:
        params: dict[str, str] = {
            "part": self.part,
            "videoId": self.video_id,
        }
        if self.on_behalf_of_content_owner:
            params["onBehalfOfContentOwner"] = self.on_behalf_of_content_owner
        return params


class CaptionDownloadParams(BaseModel):
    """Parameters for `GET https://www.googleapis.com/youtube/v3/captions/{id}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    caption_id: str = Field(min_length=1)
    tfmt: CaptionDownloadFormat = CaptionDownloadFormat.SRT
    tlang: str | None = None
    on_behalf_of_content_owner: str | None = None

    @field_validator("tlang")
    @classmethod
    def _validate_tlang_iso639_1(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _ISO639_1_RE.match(value):
            raise ValueError(f"tlang must be a 2-letter ISO 639-1 language code (got {value!r}).")
        return value

    def to_query_params(self) -> dict[str, str]:
        params: dict[str, str] = {"tfmt": self.tfmt.value}
        if self.tlang is not None:
            params["tlang"] = self.tlang
        if self.on_behalf_of_content_owner:
            params["onBehalfOfContentOwner"] = self.on_behalf_of_content_owner
        return params


class CaptionInsertSnippet(BaseModel):
    """`snippet` object inside `captions.insert` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: str = Field(alias="videoId", min_length=1)
    language: str
    name: str = Field(default="", max_length=255)
    is_draft: bool = Field(default=True, alias="isDraft")

    @field_validator("language")
    @classmethod
    def _validate_bcp47(cls, value: str) -> str:
        if not _BCP47_RE.match(value):
            raise ValueError(f"Invalid caption language code {value!r}.")
        return value


class CaptionInsertRequest(BaseModel):
    """Validated representation of a `POST /upload/youtube/v3/captions` call.

    NOTE: `sync` is deprecated by YouTube and intentionally omitted so our
    validated timestamps are never stripped or re-timed blindly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    part: Literal["snippet"] = "snippet"
    snippet: CaptionInsertSnippet
    media_body: bytes
    media_mime_type: Literal["application/octet-stream", "text/plain", "text/vtt"] = (
        "application/octet-stream"
    )
    on_behalf_of_content_owner: str | None = None

    @field_validator("media_body")
    @classmethod
    def _validate_non_empty_media(cls, value: bytes) -> bytes:
        if not value or not value.strip():
            raise ValueError("contentRequired: caption media_body must not be empty.")
        return value

    def to_api_payload(self) -> dict[str, Any]:
        """Return the exact metadata body and query parameters for `captions.insert`."""
        query: dict[str, str] = {"part": self.part}
        if self.on_behalf_of_content_owner:
            query["onBehalfOfContentOwner"] = self.on_behalf_of_content_owner
        body = {
            "snippet": {
                "videoId": self.snippet.video_id,
                "language": self.snippet.language,
                "name": self.snippet.name,
                "isDraft": self.snippet.is_draft,
            }
        }
        return {
            "endpoint": "https://www.googleapis.com/upload/youtube/v3/captions",
            "query": query,
            "body": body,
            "media_mime_type": self.media_mime_type,
            "media_size_bytes": len(self.media_body),
        }


def build_caption_insert_request(
    *,
    video_id: str,
    language: str,
    caption_content: str | bytes,
    name: str = "",
    is_draft: bool = True,
    media_mime_type: Literal["application/octet-stream", "text/plain", "text/vtt"] = (
        "application/octet-stream"
    ),
) -> CaptionInsertRequest:
    """Construct and validate a `captions.insert` payload without executing a network write."""
    raw_bytes = (
        caption_content.encode("utf-8") if isinstance(caption_content, str) else caption_content
    )
    snippet = CaptionInsertSnippet(
        videoId=video_id,
        language=language,
        name=name,
        isDraft=is_draft,
    )
    return CaptionInsertRequest(
        snippet=snippet,
        media_body=raw_bytes,
        media_mime_type=media_mime_type,
    )


@dataclass(frozen=True, slots=True)
class YouTubeCaptionTrackResource:
    """Normalized representation of a YouTube `caption` resource item."""

    caption_id: str
    video_id: str
    language: str
    name: str
    track_kind: SourceCaptionTrackKind
    status: SourceCaptionStatus
    failure_reason: str | None = None
    is_draft: bool = False
    is_auto_synced: bool = False
    is_cc: bool = False
    last_updated: datetime | None = None


# ---------------------------------------------------------------------------
# Error taxonomy & parser (matches Google JSON error envelope + non-JSON media errors)
# ---------------------------------------------------------------------------

_NON_RETRYABLE_REASONS: frozenset[str] = frozenset(
    {
        "forbidden",
        "insufficientPermissions",
        "captionNotFound",
        "videoNotFound",
        "captionExists",
        "couldNotConvert",
        "invalidValue",
        "invalidMetadata",
        "invalidParameter",
        "required",
        "contentRequired",
        "incompatibleParameters",
        "quotaExceeded",
        "dailyLimitExceeded",
        "uploadLimitExceeded",
    }
)

_RETRYABLE_REASONS: frozenset[str] = frozenset(
    {
        "backendError",
        "internalError",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)


class YouTubeApiError(Exception):
    """Structured exception for YouTube Data API v3 failures."""

    def __init__(
        self,
        *,
        http_status: int,
        reason: str,
        message: str,
        domain: str | None = None,
        location: str | None = None,
        location_type: str | None = None,
        retryable: bool = False,
        conflict: bool = False,
    ) -> None:
        super().__init__(f"YouTube API HTTP {http_status} ({reason}): {message}")
        self.http_status = http_status
        self.reason = reason
        self.message = message
        self.domain = domain
        self.location = location
        self.location_type = location_type
        self.retryable = retryable
        self.conflict = conflict


class YouTubeMalformedResponseError(Exception):
    """Raised when the YouTube API returns an unexpected or malformed payload."""


def _classify_retryable(http_status: int, reason: str) -> bool:
    if reason in _NON_RETRYABLE_REASONS:
        return False
    if reason in _RETRYABLE_REASONS:
        return True
    if http_status in (408, 500, 502, 503, 504):
        return True
    return http_status == 429 and reason not in ("quotaExceeded", "dailyLimitExceeded")


def parse_youtube_api_error(
    http_status: int,
    raw_body: bytes | str | dict[str, Any] | None,
) -> YouTubeApiError:
    """Parse a YouTube Data API error response without assuming a single structure.

    Handles:
    1. Standard Google JSON error envelope (`{"error": {"code": ..., "errors": [...]}}`)
    2. RFC 7807 / Google v2 status details (`{"error": {"status": "...", "details": [...]}}`)
    3. Plain-text or empty error bodies returned by the `/download` media endpoint
    """
    parsed_json: dict[str, Any] | None = None
    text_fallback = ""

    if isinstance(raw_body, dict):
        parsed_json = raw_body
    elif isinstance(raw_body, (bytes, str)):
        text_fallback = (
            raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else raw_body
        ).strip()
        if text_fallback.startswith("{"):
            try:
                loaded = json.loads(text_fallback)
                if isinstance(loaded, dict):
                    parsed_json = loaded
            except json.JSONDecodeError:
                parsed_json = None

    reason = "unknownError"
    message = text_fallback or f"HTTP {http_status}"
    domain: str | None = None
    location: str | None = None
    location_type: str | None = None

    if parsed_json is not None:
        err_obj = parsed_json.get("error")
        if isinstance(err_obj, dict):
            if isinstance(err_obj.get("message"), str) and err_obj["message"].strip():
                message = err_obj["message"].strip()
            errors_list = err_obj.get("errors")
            if isinstance(errors_list, list) and errors_list:
                first = errors_list[0]
                if isinstance(first, dict):
                    reason = str(first.get("reason") or reason)
                    domain = str(first["domain"]) if "domain" in first else None
                    location = str(first["location"]) if "location" in first else None
                    location_type = str(first["locationType"]) if "locationType" in first else None
                    if not err_obj.get("message") and isinstance(first.get("message"), str):
                        message = first["message"].strip()
            elif isinstance(err_obj.get("status"), str):
                reason = err_obj["status"]
        elif isinstance(err_obj, str) and err_obj.strip():
            reason = err_obj.strip()

    if reason == "unknownError":
        if http_status == 403:
            reason = "forbidden"
        elif http_status == 404:
            reason = "notFound"
        elif http_status == 409:
            reason = "conflict"
        elif http_status >= 500:
            reason = "backendError"

    is_conflict = http_status == 409 or reason == "captionExists"
    is_retryable = _classify_retryable(http_status, reason)

    return YouTubeApiError(
        http_status=http_status,
        reason=reason,
        message=message,
        domain=domain,
        location=location,
        location_type=location_type,
        retryable=is_retryable,
        conflict=is_conflict,
    )


def _parse_track_kind(raw: str | None) -> SourceCaptionTrackKind:
    if not raw:
        return SourceCaptionTrackKind.UNKNOWN
    lowered = raw.lower()
    if lowered == "standard":
        return SourceCaptionTrackKind.STANDARD
    if lowered == "asr":
        return SourceCaptionTrackKind.ASR
    if lowered == "forced":
        return SourceCaptionTrackKind.FORCED
    return SourceCaptionTrackKind.UNKNOWN


def _parse_track_status(raw: str | None) -> SourceCaptionStatus:
    if not raw:
        return SourceCaptionStatus.UNKNOWN
    lowered = raw.lower()
    if lowered == "serving":
        return SourceCaptionStatus.SERVING
    if lowered == "syncing":
        return SourceCaptionStatus.SYNCING
    if lowered == "failed":
        return SourceCaptionStatus.FAILED
    return SourceCaptionStatus.UNKNOWN


def parse_caption_list_response(payload: Any) -> list[YouTubeCaptionTrackResource]:
    """Validate and parse a `captions.list` JSON payload into typed resources."""
    if not isinstance(payload, dict):
        raise YouTubeMalformedResponseError("Expected JSON object from captions.list response.")
    items = payload.get("items")
    if items is None or not isinstance(items, list):
        raise YouTubeMalformedResponseError("captions.list response is missing an 'items' list.")

    results: list[YouTubeCaptionTrackResource] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise YouTubeMalformedResponseError(f"captions.list item[{idx}] is not a JSON object.")
        caption_id = item.get("id")
        snippet = item.get("snippet")
        if not isinstance(caption_id, str) or not caption_id.strip():
            raise YouTubeMalformedResponseError(
                f"captions.list item[{idx}] is missing a valid string 'id'."
            )
        if not isinstance(snippet, dict):
            raise YouTubeMalformedResponseError(
                f"captions.list item[{idx}] is missing a 'snippet' object."
            )
        video_id = snippet.get("videoId")
        language = snippet.get("language")
        if not isinstance(video_id, str) or not video_id.strip():
            raise YouTubeMalformedResponseError(
                f"captions.list item[{idx}].snippet is missing 'videoId'."
            )
        if not isinstance(language, str) or not language.strip():
            raise YouTubeMalformedResponseError(
                f"captions.list item[{idx}].snippet is missing 'language'."
            )

        last_updated: datetime | None = None
        raw_updated = snippet.get("lastUpdated")
        if isinstance(raw_updated, str) and raw_updated:
            try:
                last_updated = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
            except ValueError:
                last_updated = None

        results.append(
            YouTubeCaptionTrackResource(
                caption_id=caption_id,
                video_id=video_id,
                language=language,
                name=str(snippet.get("name") or ""),
                track_kind=_parse_track_kind(snippet.get("trackKind")),
                status=_parse_track_status(snippet.get("status")),
                failure_reason=(
                    str(snippet["failureReason"])
                    if snippet.get("failureReason") is not None
                    else None
                ),
                is_draft=bool(snippet.get("isDraft", False)),
                is_auto_synced=bool(snippet.get("isAutoSynced", False)),
                is_cc=bool(snippet.get("isCC", False)),
                last_updated=last_updated,
            )
        )
    return results


class YouTubeCaptionClient(Protocol):
    """Typed protocol for YouTube caption operations.

    Orchestration and caption services depend ONLY on this protocol, never on
    `googleapiclient` or raw HTTP clients directly.
    """

    async def list_captions(self, params: CaptionListParams) -> list[YouTubeCaptionTrackResource]:
        """List caption tracks for a video (`captions.list`, 50 quota units)."""

    async def download_caption(self, params: CaptionDownloadParams) -> bytes:
        """Download a caption track (`captions.download`, 200 quota units)."""

    def prepare_caption_insert(
        self,
        *,
        video_id: str,
        language: str,
        caption_content: str | bytes,
        name: str = "",
        is_draft: bool = True,
    ) -> CaptionInsertRequest:
        """Construct and validate a `captions.insert` request without executing it."""

    async def insert_caption(
        self,
        request: CaptionInsertRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeCaptionTrackResource:
        """Insert a caption track (`captions.insert`, 400 quota units).

        Must raise `PermissionError` whenever `publishing_enabled` is False.
        """


@dataclass
class MockYouTubeCaptionClient:
    """Deterministic in-memory mock implementation of `YouTubeCaptionClient`.

    Performs zero network I/O and enforces the same request validation, error
    formats, and publishing kill-switch rules as the real API layer.
    """

    list_responses: dict[str, Any] = field(default_factory=dict)
    download_responses: dict[tuple[str, str, str | None], bytes | Exception] = field(
        default_factory=dict
    )
    insert_responses: dict[tuple[str, str, str], dict[str, Any] | Exception] = field(
        default_factory=dict
    )
    persist_on_insert: bool = True
    simulate_insert_landed_before_error: bool = False
    recorded_calls: list[dict[str, Any]] = field(default_factory=list)
    quota_units_used: int = 0

    async def list_captions(self, params: CaptionListParams) -> list[YouTubeCaptionTrackResource]:
        self.recorded_calls.append({"method": "captions.list", "query": params.to_query_params()})
        self.quota_units_used += QUOTA_CAPTIONS_LIST

        if params.video_id not in self.list_responses:
            raise parse_youtube_api_error(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": (
                            f"The video identified by the 'videoId' parameter "
                            f"({params.video_id}) could not be found."
                        ),
                        "errors": [
                            {
                                "domain": "youtube.caption",
                                "reason": "videoNotFound",
                                "message": "Video not found.",
                                "locationType": "parameter",
                                "location": "videoId",
                            }
                        ],
                    }
                },
            )
        outcome = self.list_responses[params.video_id]
        if isinstance(outcome, Exception):
            raise outcome
        return parse_caption_list_response(outcome)

    def register_download(
        self,
        caption_id: str,
        outcome: bytes | Exception,
        *,
        tfmt: str = "srt",
        tlang: str | None = None,
    ) -> None:
        """Register a mocked response or exception for `captions.download`."""
        self.download_responses[(caption_id, tfmt, tlang)] = outcome

    async def download_caption(self, params: CaptionDownloadParams) -> bytes:
        self.recorded_calls.append(
            {
                "method": "captions.download",
                "caption_id": params.caption_id,
                "query": params.to_query_params(),
            }
        )
        self.quota_units_used += QUOTA_CAPTIONS_DOWNLOAD

        key = (params.caption_id, params.tfmt.value, params.tlang)
        if key not in self.download_responses:
            raise parse_youtube_api_error(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": (
                            f"The caption track identified by the 'id' parameter "
                            f"({params.caption_id}) could not be found."
                        ),
                        "errors": [
                            {
                                "domain": "youtube.caption",
                                "reason": "captionNotFound",
                                "message": "Caption track not found.",
                                "locationType": "parameter",
                                "location": "id",
                            }
                        ],
                    }
                },
            )
        outcome = self.download_responses[key]
        if isinstance(outcome, Exception):
            raise outcome
        if not outcome:
            raise YouTubeMalformedResponseError("captions.download returned an empty byte stream.")
        return outcome

    def prepare_caption_insert(
        self,
        *,
        video_id: str,
        language: str,
        caption_content: str | bytes,
        name: str = "",
        is_draft: bool = True,
    ) -> CaptionInsertRequest:
        req = build_caption_insert_request(
            video_id=video_id,
            language=language,
            caption_content=caption_content,
            name=name,
            is_draft=is_draft,
        )
        self.recorded_calls.append(
            {"method": "captions.insert.prepare", "payload": req.to_api_payload()}
        )
        return req

    async def insert_caption(
        self,
        request: CaptionInsertRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeCaptionTrackResource:
        if not publishing_enabled:
            raise PermissionError(
                "Caption insertion is blocked because YOUTUBE_PUBLISHING_ENABLED is false "
                "(Stage 3a does not execute caption insertions)."
            )
        self.recorded_calls.append(
            {"method": "captions.insert", "payload": request.to_api_payload()}
        )
        self.quota_units_used += QUOTA_CAPTIONS_INSERT

        key = (
            request.snippet.video_id,
            request.snippet.language,
            request.snippet.name,
        )
        default_item = {
            "kind": "youtube#caption",
            "id": f"cap_{request.snippet.video_id}_{request.snippet.language}",
            "snippet": {
                "videoId": request.snippet.video_id,
                "language": request.snippet.language,
                "name": request.snippet.name,
                "trackKind": "standard",
                "status": "serving",
                "isDraft": request.snippet.is_draft,
                "isAutoSynced": False,
                "isCC": False,
            },
        }
        outcome = self.insert_responses.get(key)
        if isinstance(outcome, Exception):
            if self.simulate_insert_landed_before_error:
                self._append_to_list_response(request.snippet.video_id, default_item)
            raise outcome
        if isinstance(outcome, dict):
            parsed = parse_caption_list_response({"items": [outcome]})
            if self.persist_on_insert:
                self._append_to_list_response(request.snippet.video_id, outcome)
            return parsed[0]

        if self.persist_on_insert:
            self._append_to_list_response(request.snippet.video_id, default_item)
        return YouTubeCaptionTrackResource(
            caption_id=f"cap_{request.snippet.video_id}_{request.snippet.language}",
            video_id=request.snippet.video_id,
            language=request.snippet.language,
            name=request.snippet.name,
            track_kind=SourceCaptionTrackKind.STANDARD,
            status=SourceCaptionStatus.SERVING,
            is_draft=request.snippet.is_draft,
        )

    def _append_to_list_response(self, video_id: str, item: dict[str, Any]) -> None:
        existing = self.list_responses.get(video_id)
        if isinstance(existing, dict) and isinstance(existing.get("items"), list):
            items_list: list[dict[str, Any]] = existing["items"]
            if not any(i.get("id") == item.get("id") for i in items_list if isinstance(i, dict)):
                items_list.append(dict(item))
        else:
            self.list_responses[video_id] = {
                "kind": "youtube#captionListResponse",
                "items": [dict(item)],
            }
