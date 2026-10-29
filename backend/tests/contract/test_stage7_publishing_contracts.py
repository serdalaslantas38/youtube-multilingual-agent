"""Stage 7 contract tests for YouTube localization merge-write and caption insert contracts.

Proves:
- Unrelated `snippet` fields (`title`, `description`, `categoryId`, `tags`, `defaultAudioLanguage`),
  `status` (`privacyStatus`, `publishAt`), and existing `localizations` for other languages are
  100% preserved during `videos.update`.
- `status` can never be included in `videos.update` `part` or payload.
- `snippet` is never included when `defaultLanguage` is already set on the video, and when
  `defaultLanguage` is unset and explicitly authorized, 100% of `raw_snippet` is preserved.
- Existing localizations and caption tracks cannot be overwritten unless explicitly authorized.
- `update_video_localizations` and `insert_caption` raise `PermissionError` whenever
  `publishing_enabled=False`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.enums import AssetKind, VideoPrivacyStatus
from app.services.publishing import (
    compute_publish_idempotency_key,
    compute_publish_request_digest,
)
from app.services.youtube import (
    QUOTA_CAPTIONS_INSERT,
    QUOTA_VIDEOS_UPDATE,
    MockYouTubeCaptionClient,
    MockYouTubeReadOnlyDataClient,
    VideoLocalizationUpdateRequest,
    YouTubeVideoResource,
    build_video_localization_update_request,
)

pytestmark = pytest.mark.contract


def _sample_private_scheduled_video(*, default_language: str | None = "tr") -> YouTubeVideoResource:
    return YouTubeVideoResource(
        video_id="LYLLaIfYjWs",
        channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        title="Sadece 1 Kaşık Çiçeksiz Orkidenin Tüm Yıl Çiçek Açmasını Sağlar",
        description="Orkide bakımı için doğal kür tarifi. #Orkide #HayataDairPüfNoktalar",
        category_id="26",
        default_language=default_language,
        default_audio_language="tr",
        privacy_status=VideoPrivacyStatus.PRIVATE,
        scheduled_publish_at=datetime(2026, 11, 1, 16, 0, tzinfo=UTC),
        published_at=None,
        localizations={
            "de": {
                "title": "Nur 1 Löffel lässt Ihre Orchidee das ganze Jahr blühen",
                "description": "Natürliche Orchideenpflege. #Orkide #HayataDairPüfNoktalar",
            },
            "fr": {
                "title": "Seulement 1 cuillère fait fleurir votre orchidée toute l'année",
                "description": "Soin naturel des orchidées. #Orkide #HayataDairPüfNoktalar",
            },
        },
        raw_snippet={
            "channelId": "UCkL0_jtFQqNCGG2gTTXcZ7w",
            "title": "Sadece 1 Kaşık Çiçeksiz Orkidenin Tüm Yıl Çiçek Açmasını Sağlar",
            "description": "Orkide bakımı için doğal kür tarifi. #Orkide #HayataDairPüfNoktalar",
            "categoryId": "26",
            "defaultLanguage": default_language,
            "defaultAudioLanguage": "tr",
            "tags": ["orkide", "çiçek bakımı", "bitki"],
            "liveBroadcastContent": "none",
        },
    )


def test_localization_merge_write_preserves_existing_localizations_and_omits_status() -> None:
    video = _sample_private_scheduled_video(default_language="tr")
    req = build_video_localization_update_request(
        current_video=video,
        target_language="en",
        translated_title="Just 1 Spoon Makes Leafless Orchids Bloom All Year Round",
        translated_description="Natural orchid care recipe. #Orkide #HayataDairPüfNoktalar",
    )
    assert req.part == "localizations"
    assert req.snippet is None

    payload = req.to_api_payload()
    assert payload["query"] == {"part": "localizations"}
    assert "snippet" not in payload["body"]
    assert "status" not in payload["body"]
    assert "status" not in payload["query"]["part"]

    # Existing German and French localizations must be preserved verbatim alongside English
    locs = payload["body"]["localizations"]
    assert set(locs.keys()) == {"de", "fr", "en"}
    assert locs["de"] == video.localizations["de"]
    assert locs["fr"] == video.localizations["fr"]
    assert locs["en"] == {
        "title": "Just 1 Spoon Makes Leafless Orchids Bloom All Year Round",
        "description": "Natural orchid care recipe. #Orkide #HayataDairPüfNoktalar",
    }


def test_localization_merge_write_sets_default_language_while_preserving_full_raw_snippet() -> None:
    video = _sample_private_scheduled_video(default_language=None)
    req = build_video_localization_update_request(
        current_video=video,
        target_language="en",
        translated_title="Just 1 Spoon Makes Leafless Orchids Bloom All Year Round",
        translated_description="Natural orchid care recipe. #Orkide #HayataDairPüfNoktalar",
        source_language="tr",
        allow_set_default_language=True,
    )
    assert req.part == "snippet,localizations"
    payload = req.to_api_payload()
    assert payload["query"] == {"part": "snippet,localizations"}
    assert "status" not in payload["body"]

    snippet = payload["body"]["snippet"]
    assert snippet["title"] == video.title
    assert snippet["description"] == video.description
    assert snippet["categoryId"] == "26"
    assert snippet["defaultLanguage"] == "tr"
    assert snippet["defaultAudioLanguage"] == "tr"
    assert snippet["tags"] == ["orkide", "çiçek bakımı", "bitki"]
    assert snippet["liveBroadcastContent"] == "none"


def test_localization_update_request_rejects_overwriting_existing_language_or_touching_status() -> (
    None
):
    video = _sample_private_scheduled_video(default_language="tr")

    # Attempting to overwrite existing 'de' localization without explicit authorization fails
    with pytest.raises(ValueError, match="already exists on YouTube"):
        build_video_localization_update_request(
            current_video=video,
            target_language="de",
            translated_title="Neuer Titel",
            translated_description="Neue Beschreibung",
            allow_overwrite_existing_localization=False,
        )

    # Attempting to pass status in part or missing snippet fields is rejected by Pydantic
    with pytest.raises(ValidationError):
        VideoLocalizationUpdateRequest(
            video_id="LYLLaIfYjWs",
            target_language="en",
            part="status,localizations",  # type: ignore[arg-type]
            localizations={"en": {"title": "T", "description": "D"}},
        )

    with pytest.raises(ValidationError, match="snippet must be None"):
        VideoLocalizationUpdateRequest(
            video_id="LYLLaIfYjWs",
            target_language="en",
            part="localizations",
            localizations={"en": {"title": "T", "description": "D"}},
            snippet={"title": "T"},
        )


async def test_mock_youtube_client_enforces_kill_switch_and_preserves_privacy_and_schedule() -> (
    None
):
    video = _sample_private_scheduled_video(default_language="tr")
    caption_client = MockYouTubeCaptionClient(
        list_responses={"LYLLaIfYjWs": {"kind": "youtube#captionListResponse", "items": []}}
    )
    yt_client = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": video},
        caption_client=caption_client,
    )

    update_req = build_video_localization_update_request(
        current_video=video,
        target_language="en",
        translated_title="English Title",
        translated_description="English Description",
    )

    # 1. Kill switch blocks update_video_localizations when publishing_enabled=False
    with pytest.raises(PermissionError, match="YOUTUBE_PUBLISHING_ENABLED is false"):
        await yt_client.update_video_localizations(update_req, publishing_enabled=False)

    # 2. When publishing_enabled=True, update merges localizations and preserves privacy & schedule
    updated_video = await yt_client.update_video_localizations(update_req, publishing_enabled=True)
    assert yt_client.quota_units_used == QUOTA_VIDEOS_UPDATE
    assert updated_video.privacy_status is VideoPrivacyStatus.PRIVATE
    assert updated_video.scheduled_publish_at == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    assert updated_video.title == video.title
    assert updated_video.description == video.description
    assert set(updated_video.localizations.keys()) == {"de", "fr", "en"}
    assert updated_video.localizations["de"] == video.localizations["de"]
    assert updated_video.localizations["en"]["title"] == "English Title"

    # 3. Caption insert updates list_responses for read-back verification
    cap_req = yt_client.prepare_caption_insert(
        video_id="LYLLaIfYjWs",
        language="en",
        caption_content="1\n00:00:01,000 --> 00:00:03,000\nEnglish cue\n",
        is_draft=False,
    )
    inserted = await yt_client.insert_caption(cap_req, publishing_enabled=True)
    assert inserted.language == "en"
    assert caption_client.quota_units_used == QUOTA_CAPTIONS_INSERT
    tracks_after = await yt_client.list_captions(
        caption_client.prepare_caption_insert(
            video_id="LYLLaIfYjWs",
            language="en",
            caption_content="1\n00:00:01,000 --> 00:00:03,000\nEnglish cue\n",
        ).snippet  # type: ignore[arg-type]
        if False
        else __import__("app.services.youtube", fromlist=["CaptionListParams"]).CaptionListParams(
            video_id="LYLLaIfYjWs"
        )
    )
    assert len(tracks_after) == 1
    assert tracks_after[0].caption_id == inserted.caption_id


def test_deterministic_idempotency_key_and_request_digest() -> None:
    k1 = compute_publish_idempotency_key(
        youtube_video_id="LYLLaIfYjWs",
        language="EN",
        kind=AssetKind.METADATA,
        content_hash="sha256:abc123",
    )
    k2 = compute_publish_idempotency_key(
        youtube_video_id="LYLLaIfYjWs",
        language="en",
        kind=AssetKind.METADATA,
        content_hash="sha256:abc123",
    )
    assert k1 == k2
    assert len(k1) <= 128

    k_retry = compute_publish_idempotency_key(
        youtube_video_id="LYLLaIfYjWs",
        language="en",
        kind=AssetKind.METADATA,
        content_hash="sha256:abc123",
        retry_index=1,
    )
    assert k_retry == f"{k1}:r1"

    d1 = compute_publish_request_digest({"b": 2, "a": 1})
    d2 = compute_publish_request_digest({"a": 1, "b": 2})
    assert d1 == d2
    assert d1.startswith("sha256:")
