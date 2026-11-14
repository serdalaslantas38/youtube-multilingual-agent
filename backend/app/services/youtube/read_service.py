"""Typed read-only YouTube Data API v3 service for channel identity and video discovery.

Handles:
- `channels.list(part="snippet,contentDetails", mine="true")` (1 quota unit)
- `playlistItems.list(part="snippet,contentDetails", playlistId=...)` (1 quota unit)
- `videos.list(part="snippet,status,localizations", id=...)` (1 quota unit)
- Live HTTP implementation of `YouTubeCaptionClient` (`HttpYouTubeCaptionClient`)
  using OAuth bearer tokens (`captions.list` and `captions.download`).

Safety invariant:
- Never modifies YouTube resources (`videos.update` and `captions.insert` are
  blocked whenever `publishing_enabled=False`).
- Never logs or exposes OAuth access or refresh tokens.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import current_organization_id
from app.models.entities import QuotaLedger
from app.models.enums import VideoPrivacyStatus
from app.services.youtube.captions_client import (
    QUOTA_CAPTIONS_DOWNLOAD,
    QUOTA_CAPTIONS_INSERT,
    QUOTA_CAPTIONS_LIST,
    CaptionDownloadParams,
    CaptionInsertRequest,
    CaptionListParams,
    YouTubeCaptionClient,
    YouTubeCaptionTrackResource,
    YouTubeMalformedResponseError,
    build_caption_insert_request,
    parse_caption_list_response,
    parse_youtube_api_error,
)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"

QUOTA_CHANNELS_LIST = 1
QUOTA_PLAYLIST_ITEMS_LIST = 1
QUOTA_VIDEOS_LIST = 1
QUOTA_VIDEOS_UPDATE = 50

_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class YouTubeChannelIdentity:
    """Authenticated YouTube channel identity returned by `channels.list(mine=true)`."""

    channel_id: str
    title: str
    custom_url: str | None
    uploads_playlist_id: str | None


@dataclass(frozen=True, slots=True)
class YouTubeVideoResource:
    """Normalized snapshot of a YouTube video (`snippet,status,localizations`)."""

    video_id: str
    channel_id: str
    title: str
    description: str
    category_id: str | None
    default_language: str | None
    default_audio_language: str | None
    privacy_status: VideoPrivacyStatus
    scheduled_publish_at: datetime | None
    published_at: datetime | None
    localizations: dict[str, dict[str, str]]
    raw_snippet: dict[str, Any] = field(default_factory=dict)

    def compute_source_content_hash(self) -> str:
        raw = f"{self.title.strip()}\n---\n{self.description.strip()}".encode()
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"


class VideoLocalizationUpdateRequest(BaseModel):
    """Validated representation of a `PUT /youtube/v3/videos` localization merge-write.

    Safety invariants enforced by schema:
    - `part` may ONLY be `"localizations"` or `"snippet,localizations"` — NEVER `"status"`.
    - `snippet` is forbidden when `part == "localizations"` and required when
      `part == "snippet,localizations"` (used only when `defaultLanguage` is unset
      and explicitly authorized).
    - `status` (`privacyStatus`, `publishAt`) can never be serialized in the request body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: str = Field(min_length=1)
    target_language: str = Field(min_length=2, max_length=16)
    part: Literal["localizations", "snippet,localizations"] = "localizations"
    localizations: dict[str, dict[str, str]]
    snippet: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_part_and_snippet_safety(self) -> VideoLocalizationUpdateRequest:
        if "status" in self.part.lower():
            raise ValueError("Never include 'status' in videos.update part.")
        if self.part == "localizations" and self.snippet is not None:
            raise ValueError("snippet must be None when part is 'localizations'.")
        if self.part == "snippet,localizations":
            if not isinstance(self.snippet, dict) or not self.snippet:
                raise ValueError("snippet dict is required when part is 'snippet,localizations'.")
            for required_key in ("title", "description", "categoryId", "defaultLanguage"):
                if required_key not in self.snippet or not str(self.snippet[required_key]).strip():
                    raise ValueError(
                        f"snippet.{required_key} must be preserved when updating snippet."
                    )
        if self.target_language not in self.localizations:
            raise ValueError(
                f"target_language {self.target_language!r} must be present in localizations."
            )
        for lang, loc in self.localizations.items():
            if not isinstance(loc, dict) or "title" not in loc or "description" not in loc:
                raise ValueError(
                    f"Localization entry for {lang!r} must contain 'title' and 'description'."
                )
        return self

    def to_api_payload(self) -> dict[str, Any]:
        """Return the exact HTTP query and body payload for `videos.update`."""
        body: dict[str, Any] = {
            "id": self.video_id,
            "localizations": {
                lang: {"title": loc["title"], "description": loc["description"]}
                for lang, loc in self.localizations.items()
            },
        }
        if self.part == "snippet,localizations" and self.snippet is not None:
            body["snippet"] = dict(self.snippet)
        return {
            "endpoint": f"{YOUTUBE_API_BASE}/videos",
            "query": {"part": self.part},
            "body": body,
        }


_LANGUAGE_ALIAS_GROUPS: dict[str, frozenset[str]] = {
    "he": frozenset({"he", "iw"}),
    "iw": frozenset({"he", "iw"}),
    "id": frozenset({"id", "in"}),
    "in": frozenset({"id", "in"}),
    "yi": frozenset({"yi", "ji"}),
    "ji": frozenset({"yi", "ji"}),
}


def language_code_aliases(language: str) -> frozenset[str]:
    """Return lowercase equivalent BCP-47/legacy language codes (e.g. ``he`` <-> ``iw``)."""
    lowered = language.strip().lower()
    return _LANGUAGE_ALIAS_GROUPS.get(lowered, frozenset({lowered}))


def build_video_localization_update_request(
    *,
    current_video: YouTubeVideoResource,
    target_language: str,
    translated_title: str,
    translated_description: str,
    source_language: str = "tr",
    allow_set_default_language: bool = False,
    allow_overwrite_existing_localization: bool = False,
) -> VideoLocalizationUpdateRequest:
    """Build a safe read-merge-write `videos.update` request that preserves existing metadata."""
    lang = target_language.strip()
    aliases = language_code_aliases(lang)
    existing_lower_map = {k.lower(): k for k in current_video.localizations}
    if any(a in existing_lower_map for a in aliases) and not allow_overwrite_existing_localization:
        raise ValueError(
            f"Target localization {lang!r} already exists on YouTube video "
            f"{current_video.video_id!r}; refusing to overwrite without explicit authorization."
        )

    merged_localizations: dict[str, dict[str, str]] = {
        k: {"title": v.get("title", ""), "description": v.get("description", "")}
        for k, v in current_video.localizations.items()
        if k.lower() not in aliases
    }
    merged_localizations[lang] = {
        "title": translated_title,
        "description": translated_description,
    }

    if not current_video.default_language and allow_set_default_language:
        preserved_snippet = dict(current_video.raw_snippet)
        preserved_snippet["title"] = current_video.title
        preserved_snippet["description"] = current_video.description
        preserved_snippet["categoryId"] = current_video.category_id or "22"
        preserved_snippet["defaultLanguage"] = source_language
        if current_video.default_audio_language and "defaultAudioLanguage" not in preserved_snippet:
            preserved_snippet["defaultAudioLanguage"] = current_video.default_audio_language
        return VideoLocalizationUpdateRequest(
            video_id=current_video.video_id,
            target_language=lang,
            part="snippet,localizations",
            localizations=merged_localizations,
            snippet=preserved_snippet,
        )

    return VideoLocalizationUpdateRequest(
        video_id=current_video.video_id,
        target_language=lang,
        part="localizations",
        localizations=merged_localizations,
        snippet=None,
    )


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_privacy_status(raw: Any) -> VideoPrivacyStatus:
    if isinstance(raw, str):
        lowered = raw.lower()
        if lowered == "public":
            return VideoPrivacyStatus.PUBLIC
        if lowered == "unlisted":
            return VideoPrivacyStatus.UNLISTED
    return VideoPrivacyStatus.PRIVATE


def parse_channels_list_response(payload: Any) -> list[YouTubeChannelIdentity]:
    """Parse a `channels.list` JSON response into `YouTubeChannelIdentity` objects."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise YouTubeMalformedResponseError("channels.list response is missing an 'items' list.")
    identities: list[YouTubeChannelIdentity] = []
    for idx, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            raise YouTubeMalformedResponseError(f"channels.list item[{idx}] is not a JSON object.")
        channel_id = item.get("id")
        snippet = item.get("snippet")
        content_details = item.get("contentDetails") or {}
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise YouTubeMalformedResponseError(
                f"channels.list item[{idx}] is missing a valid 'id'."
            )
        if not isinstance(snippet, dict):
            raise YouTubeMalformedResponseError(
                f"channels.list item[{idx}] is missing a 'snippet' object."
            )
        related = (
            content_details.get("relatedPlaylists") if isinstance(content_details, dict) else None
        )
        uploads_id = (
            str(related["uploads"])
            if isinstance(related, dict) and isinstance(related.get("uploads"), str)
            else None
        )
        custom_url = (
            str(snippet["customUrl"]) if isinstance(snippet.get("customUrl"), str) else None
        )
        identities.append(
            YouTubeChannelIdentity(
                channel_id=channel_id.strip(),
                title=str(snippet.get("title") or "").strip(),
                custom_url=custom_url,
                uploads_playlist_id=uploads_id,
            )
        )
    return identities


def parse_playlist_items_video_ids(payload: Any) -> list[str]:
    """Extract ordered video IDs from a `playlistItems.list` response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise YouTubeMalformedResponseError(
            "playlistItems.list response is missing an 'items' list."
        )
    video_ids: list[str] = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        content_details = item.get("contentDetails")
        snippet = item.get("snippet")
        vid: str | None = None
        if isinstance(content_details, dict) and isinstance(content_details.get("videoId"), str):
            vid = content_details["videoId"].strip()
        elif isinstance(snippet, dict):
            res_id = snippet.get("resourceId")
            if isinstance(res_id, dict) and isinstance(res_id.get("videoId"), str):
                vid = res_id["videoId"].strip()
        if vid and vid not in video_ids:
            video_ids.append(vid)
    return video_ids


def parse_videos_list_response(payload: Any) -> list[YouTubeVideoResource]:
    """Parse a `videos.list(part='snippet,status,localizations')` response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise YouTubeMalformedResponseError("videos.list response is missing an 'items' list.")
    videos: list[YouTubeVideoResource] = []
    for idx, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            raise YouTubeMalformedResponseError(f"videos.list item[{idx}] is not a JSON object.")
        video_id = item.get("id")
        snippet = item.get("snippet")
        status_obj = item.get("status") or {}
        raw_locs = item.get("localizations") or {}
        if not isinstance(video_id, str) or not video_id.strip():
            raise YouTubeMalformedResponseError(f"videos.list item[{idx}] is missing a valid 'id'.")
        if not isinstance(snippet, dict):
            raise YouTubeMalformedResponseError(
                f"videos.list item[{idx}] is missing a 'snippet' object."
            )
        channel_id = snippet.get("channelId")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise YouTubeMalformedResponseError(
                f"videos.list item[{idx}].snippet is missing 'channelId'."
            )

        localizations: dict[str, dict[str, str]] = {}
        if isinstance(raw_locs, dict):
            for lang, loc_val in raw_locs.items():
                if isinstance(lang, str) and isinstance(loc_val, dict):
                    localizations[lang] = {
                        "title": str(loc_val.get("title") or ""),
                        "description": str(loc_val.get("description") or ""),
                    }

        videos.append(
            YouTubeVideoResource(
                video_id=video_id.strip(),
                channel_id=channel_id.strip(),
                title=str(snippet.get("title") or ""),
                description=str(snippet.get("description") or ""),
                category_id=(
                    str(snippet["categoryId"])
                    if isinstance(snippet.get("categoryId"), str)
                    else None
                ),
                default_language=(
                    str(snippet["defaultLanguage"])
                    if isinstance(snippet.get("defaultLanguage"), str)
                    else None
                ),
                default_audio_language=(
                    str(snippet["defaultAudioLanguage"])
                    if isinstance(snippet.get("defaultAudioLanguage"), str)
                    else None
                ),
                privacy_status=_parse_privacy_status(
                    status_obj.get("privacyStatus") if isinstance(status_obj, dict) else None
                ),
                scheduled_publish_at=_parse_iso_datetime(
                    status_obj.get("publishAt") if isinstance(status_obj, dict) else None
                ),
                published_at=_parse_iso_datetime(snippet.get("publishedAt")),
                localizations=localizations,
                raw_snippet=dict(snippet),
            )
        )
    return videos


class YouTubeReadOnlyDataClient(YouTubeCaptionClient, Protocol):
    """Typed protocol combining channel/video discovery, caption ops, and guarded writes."""

    async def get_authenticated_channel(self) -> YouTubeChannelIdentity:
        """Return the authenticated channel's identity (`channels.list(mine=true)`)."""

    async def list_upload_video_ids(
        self, uploads_playlist_id: str, *, max_results: int = 20
    ) -> list[str]:
        """List recent video IDs from the channel's uploads playlist."""

    async def get_videos(self, video_ids: list[str]) -> list[YouTubeVideoResource]:
        """Fetch metadata, status, and localizations for one or more video IDs."""

    async def update_video_localizations(
        self,
        request: VideoLocalizationUpdateRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeVideoResource:
        """Update video localizations (`videos.update`, 50 quota units).

        Must raise `PermissionError` whenever `publishing_enabled` is False.
        """


class HttpYouTubeDataClient:
    """Async httpx client for authenticated YouTube Data API v3 calls."""

    def __init__(self, access_token: str, *, timeout: float = 25.0) -> None:
        if not access_token or not access_token.strip():
            raise ValueError("An OAuth access_token is required for YouTube API calls.")
        self._access_token = access_token.strip()
        self._timeout = timeout
        self.quota_units_used = 0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{YOUTUBE_API_BASE}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=self._headers())
        if resp.status_code != 200:
            raise parse_youtube_api_error(resp.status_code, resp.content)
        try:
            data = resp.json()
        except ValueError as exc:
            raise YouTubeMalformedResponseError(
                f"Invalid JSON returned from {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise YouTubeMalformedResponseError(f"Expected JSON object from {path}.")
        return data

    async def get_authenticated_channel(self) -> YouTubeChannelIdentity:
        self.quota_units_used += QUOTA_CHANNELS_LIST
        payload = await self._get_json(
            "channels",
            {"part": "snippet,contentDetails", "mine": "true"},
        )
        channels = parse_channels_list_response(payload)
        if not channels:
            raise parse_youtube_api_error(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": "No YouTube channel found for the authenticated account.",
                        "errors": [{"domain": "youtube.channel", "reason": "channelNotFound"}],
                    }
                },
            )
        return channels[0]

    async def list_upload_video_ids(
        self, uploads_playlist_id: str, *, max_results: int = 20
    ) -> list[str]:
        target_total = max(1, min(max_results, 500))
        collected: list[str] = []
        page_token: str | None = None

        while len(collected) < target_total:
            page_size = min(50, target_total - len(collected))
            params: dict[str, str] = {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": str(page_size),
            }
            if page_token:
                params["pageToken"] = page_token
            self.quota_units_used += QUOTA_PLAYLIST_ITEMS_LIST
            payload = await self._get_json("playlistItems", params)
            page_ids = parse_playlist_items_video_ids(payload)
            for vid in page_ids:
                if vid not in collected:
                    collected.append(vid)
                    if len(collected) >= target_total:
                        break
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token.strip() or not page_ids:
                break
            page_token = next_token.strip()

        return collected

    async def get_videos(self, video_ids: list[str]) -> list[YouTubeVideoResource]:
        cleaned = [v.strip() for v in video_ids if v.strip()]
        if not cleaned:
            return []
        results: list[YouTubeVideoResource] = []
        for start in range(0, len(cleaned), 50):
            chunk = cleaned[start : start + 50]
            self.quota_units_used += QUOTA_VIDEOS_LIST
            payload = await self._get_json(
                "videos",
                {
                    "part": "snippet,status,localizations",
                    "id": ",".join(chunk),
                },
            )
            results.extend(parse_videos_list_response(payload))
        return results

    async def update_video_localizations(
        self,
        request: VideoLocalizationUpdateRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeVideoResource:
        if not publishing_enabled:
            raise PermissionError(
                "Video localization update is blocked because YOUTUBE_PUBLISHING_ENABLED is false."
            )
        self.quota_units_used += QUOTA_VIDEOS_UPDATE
        api_payload = request.to_api_payload()
        url = f"{YOUTUBE_API_BASE}/videos"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(
                url,
                params=api_payload["query"],
                json=api_payload["body"],
                headers=self._headers(),
            )
        if resp.status_code != 200:
            raise parse_youtube_api_error(resp.status_code, resp.content)
        try:
            data = resp.json()
        except ValueError as exc:
            raise YouTubeMalformedResponseError(
                f"Invalid JSON returned from videos.update: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise YouTubeMalformedResponseError("Expected JSON object from videos.update.")
        # Normalize into YouTubeVideoResource (read-back verification still runs videos.list)
        if "snippet" not in data:
            data = {
                **data,
                "snippet": request.snippet or {"channelId": "unknown", "title": ""},
            }
        parsed = parse_videos_list_response({"items": [data]})
        return parsed[0]

    async def list_captions(self, params: CaptionListParams) -> list[YouTubeCaptionTrackResource]:
        self.quota_units_used += QUOTA_CAPTIONS_LIST
        payload = await self._get_json("captions", params.to_query_params())
        return parse_caption_list_response(payload)

    async def download_caption(self, params: CaptionDownloadParams) -> bytes:
        self.quota_units_used += QUOTA_CAPTIONS_DOWNLOAD
        url = f"{YOUTUBE_API_BASE}/captions/{params.caption_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params.to_query_params(), headers=self._headers())
        if resp.status_code != 200:
            raise parse_youtube_api_error(resp.status_code, resp.content)
        if not resp.content:
            raise YouTubeMalformedResponseError("captions.download returned an empty byte stream.")
        return resp.content

    def prepare_caption_insert(
        self,
        *,
        video_id: str,
        language: str,
        caption_content: str | bytes,
        name: str = "",
        is_draft: bool = True,
    ) -> CaptionInsertRequest:
        return build_caption_insert_request(
            video_id=video_id,
            language=language,
            caption_content=caption_content,
            name=name,
            is_draft=is_draft,
        )

    async def insert_caption(
        self,
        request: CaptionInsertRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeCaptionTrackResource:
        if not publishing_enabled:
            raise PermissionError(
                "Caption insertion is blocked because YOUTUBE_PUBLISHING_ENABLED is false."
            )
        self.quota_units_used += QUOTA_CAPTIONS_INSERT
        api_payload = request.to_api_payload()
        boundary = f"yma_caption_{uuid.uuid4().hex}"
        metadata_json = json.dumps(api_payload["body"], ensure_ascii=False).encode("utf-8")
        multipart_body = (
            (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n").encode(
                "ascii"
            )
            + metadata_json
            + (f"\r\n--{boundary}\r\nContent-Type: {request.media_mime_type}\r\n\r\n").encode(
                "ascii"
            )
            + request.media_body
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )

        headers = {
            **self._headers(),
            "Content-Type": f"multipart/related; boundary={boundary}",
        }
        query = {**api_payload["query"], "uploadType": "multipart"}
        url = f"{YOUTUBE_UPLOAD_BASE}/captions"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, params=query, content=multipart_body, headers=headers)
        if resp.status_code != 200:
            raise parse_youtube_api_error(resp.status_code, resp.content)
        try:
            data = resp.json()
        except ValueError as exc:
            raise YouTubeMalformedResponseError(
                f"Invalid JSON returned from captions.insert: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise YouTubeMalformedResponseError("Expected JSON object from captions.insert.")
        parsed = parse_caption_list_response({"items": [data]})
        return parsed[0]


@dataclass
class MockYouTubeReadOnlyDataClient:
    """Deterministic in-memory mock of `YouTubeReadOnlyDataClient` for tests."""

    channel: YouTubeChannelIdentity | None = None
    playlist_video_ids: dict[str, list[str]] = field(default_factory=dict)
    videos_by_id: dict[str, YouTubeVideoResource] = field(default_factory=dict)
    caption_client: YouTubeCaptionClient | None = None
    update_video_outcomes: dict[tuple[str, str], YouTubeVideoResource | Exception] = field(
        default_factory=dict
    )
    persist_on_update_video: bool = True
    simulate_update_landed_before_error: bool = False
    recorded_calls: list[dict[str, Any]] = field(default_factory=list)
    quota_units_used: int = 0

    async def get_authenticated_channel(self) -> YouTubeChannelIdentity:
        self.quota_units_used += QUOTA_CHANNELS_LIST
        self.recorded_calls.append({"method": "channels.list"})
        if self.channel is None:
            raise parse_youtube_api_error(
                404,
                {
                    "error": {
                        "code": 404,
                        "errors": [{"domain": "youtube.channel", "reason": "channelNotFound"}],
                    }
                },
            )
        return self.channel

    async def list_upload_video_ids(
        self, uploads_playlist_id: str, *, max_results: int = 20
    ) -> list[str]:
        self.quota_units_used += QUOTA_PLAYLIST_ITEMS_LIST
        self.recorded_calls.append(
            {"method": "playlistItems.list", "playlist_id": uploads_playlist_id}
        )
        ids = self.playlist_video_ids.get(uploads_playlist_id, [])
        return ids[:max_results]

    async def get_videos(self, video_ids: list[str]) -> list[YouTubeVideoResource]:
        self.quota_units_used += QUOTA_VIDEOS_LIST
        self.recorded_calls.append({"method": "videos.list", "video_ids": list(video_ids)})
        return [self.videos_by_id[vid] for vid in video_ids if vid in self.videos_by_id]

    async def update_video_localizations(
        self,
        request: VideoLocalizationUpdateRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeVideoResource:
        if not publishing_enabled:
            raise PermissionError(
                "Video localization update is blocked because YOUTUBE_PUBLISHING_ENABLED is false."
            )
        self.quota_units_used += QUOTA_VIDEOS_UPDATE
        self.recorded_calls.append({"method": "videos.update", "payload": request.to_api_payload()})
        current = self.videos_by_id.get(request.video_id)
        if current is None:
            raise parse_youtube_api_error(
                404,
                {
                    "error": {
                        "code": 404,
                        "errors": [{"domain": "youtube.video", "reason": "videoNotFound"}],
                    }
                },
            )
        updated_default_lang = current.default_language
        updated_raw_snippet = dict(current.raw_snippet)
        if request.part == "snippet,localizations" and request.snippet is not None:
            updated_default_lang = str(request.snippet.get("defaultLanguage") or "")
            updated_raw_snippet = dict(request.snippet)

        merged_video = replace(
            current,
            default_language=updated_default_lang,
            localizations={
                k: {"title": v["title"], "description": v["description"]}
                for k, v in request.localizations.items()
            },
            raw_snippet=updated_raw_snippet,
        )

        outcome = self.update_video_outcomes.get((request.video_id, request.target_language))
        if isinstance(outcome, Exception):
            if self.simulate_update_landed_before_error:
                self.videos_by_id[request.video_id] = merged_video
            raise outcome
        if isinstance(outcome, YouTubeVideoResource):
            if self.persist_on_update_video:
                self.videos_by_id[request.video_id] = outcome
            return outcome

        if self.persist_on_update_video:
            self.videos_by_id[request.video_id] = merged_video
        return merged_video

    async def list_captions(self, params: CaptionListParams) -> list[YouTubeCaptionTrackResource]:
        if self.caption_client is None:
            return []
        self.quota_units_used += QUOTA_CAPTIONS_LIST
        self.recorded_calls.append({"method": "captions.list", "query": params.to_query_params()})
        return await self.caption_client.list_captions(params)

    async def download_caption(self, params: CaptionDownloadParams) -> bytes:
        if self.caption_client is None:
            raise YouTubeMalformedResponseError("No caption client configured.")
        self.quota_units_used += QUOTA_CAPTIONS_DOWNLOAD
        self.recorded_calls.append(
            {
                "method": "captions.download",
                "caption_id": params.caption_id,
                "query": params.to_query_params(),
            }
        )
        return await self.caption_client.download_caption(params)

    def prepare_caption_insert(
        self,
        *,
        video_id: str,
        language: str,
        caption_content: str | bytes,
        name: str = "",
        is_draft: bool = True,
    ) -> CaptionInsertRequest:
        if self.caption_client is not None:
            return self.caption_client.prepare_caption_insert(
                video_id=video_id,
                language=language,
                caption_content=caption_content,
                name=name,
                is_draft=is_draft,
            )
        return build_caption_insert_request(
            video_id=video_id,
            language=language,
            caption_content=caption_content,
            name=name,
            is_draft=is_draft,
        )

    async def insert_caption(
        self,
        request: CaptionInsertRequest,
        *,
        publishing_enabled: bool = False,
    ) -> YouTubeCaptionTrackResource:
        if self.caption_client is None:
            raise PermissionError("Publishing is disabled.")
        self.quota_units_used += QUOTA_CAPTIONS_INSERT
        self.recorded_calls.append(
            {"method": "captions.insert", "payload": request.to_api_payload()}
        )
        return await self.caption_client.insert_caption(
            request, publishing_enabled=publishing_enabled
        )


async def record_quota_usage(
    session: AsyncSession,
    *,
    api_method: str,
    units: int,
    video_id: str | None = None,
    now: datetime | None = None,
) -> QuotaLedger:
    """Record YouTube Data API v3 quota units against the Pacific Time daily ledger."""
    current = now or datetime.now(_PACIFIC_TZ)
    pt_date = current.astimezone(_PACIFIC_TZ).date()
    entry = QuotaLedger(
        organization_id=current_organization_id(),
        quota_date_pt=pt_date,
        api_method=api_method,
        units=units,
        video_id=video_id,
        recorded_at=current,
    )
    session.add(entry)
    await session.flush()
    return entry


async def get_today_quota_used(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Return total quota units recorded for today (Pacific Time)."""
    current = now or datetime.now(_PACIFIC_TZ)
    pt_date = current.astimezone(_PACIFIC_TZ).date()
    stmt = select(func.coalesce(func.sum(QuotaLedger.units), 0)).where(
        QuotaLedger.organization_id == current_organization_id(),
        QuotaLedger.quota_date_pt == pt_date
    )
    total = (await session.execute(stmt)).scalar_one()
    return int(total)
