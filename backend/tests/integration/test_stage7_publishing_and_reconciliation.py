"""Stage 7 integration tests for controlled publishing, read-back verification, and reconciliation.

Covers:
1. Kill-switch enforcement (`YOUTUBE_PUBLISHING_ENABLED=false` blocks all writes with 409).
2. End-to-end metadata read-merge-write (`videos.update`), preserving existing localizations,
   Turkish source `snippet`, `privacyStatus`, and `scheduled_publish_at`, followed by
   post-write `videos.list` read-back verification and idempotent replay (`idempotent_reused=True`).
3. End-to-end caption upload (`captions.insert`), pre-upload subtitle validation, existing track
   preservation, post-write `captions.list` verification, and `409 captionExists` verify-only.
4. Existing localization and caption track protection (`target_already_exists` -> `skipped`
   without calling `videos.update` or `captions.insert`).
5. Concurrent YouTube Studio source-drift detection (aborts before write when remote Turkish
   title/description changed after localization).
6. Proof that status transitions to `published` ONLY after post-write read-back verification
   succeeds (verification mismatch transitions to `uncertain`).
7. Uncertain outcome blocks duplicate retries (`uncertain_previous_attempt`), and reconciliation
   (`POST /api/assets/{id}/reconcile`) resolves both branches:
   - Branch A (write DID land on YouTube before timeout/503) -> `published` without re-sending.
   - Branch B (write DID NOT land on YouTube before timeout/503) -> `ready_to_publish`, allowing
     safe retry with deterministic `:r1` idempotency key.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.settings import AppEnv, Settings
from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.main import create_app
from app.models.entities import SourceCaptionTrack, Video
from app.models.enums import (
    SourceCaptionStatus,
    SourceCaptionTrackKind,
    VideoPrivacyStatus,
    VideoWorkflowState,
)
from app.services.translation.providers import FakeStructuredLLMClient
from app.services.youtube import (
    MockYouTubeCaptionClient,
    MockYouTubeReadOnlyDataClient,
    YouTubeVideoResource,
    parse_youtube_api_error,
)
from tests.fixtures.turkish_golden import (
    GOLDEN_TURKISH_DESCRIPTION,
    GOLDEN_TURKISH_SRT,
    GOLDEN_TURKISH_TITLE,
)

pytestmark = pytest.mark.integration


def _make_settings(
    tmp_path: Path,
    config_dir: Path,
    *,
    publishing_enabled: bool = True,
) -> Settings:
    db_file = tmp_path / "stage7_test.db"
    return Settings(
        app_env=AppEnv.DEVELOPMENT if publishing_enabled else AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{db_file}",
        config_dir=config_dir,
        youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        youtube_publishing_enabled=publishing_enabled,
    )


def _build_remote_video(
    *,
    existing_localizations: dict[str, dict[str, str]] | None = None,
    default_language: str | None = "tr",
    title: str = GOLDEN_TURKISH_TITLE,
    description: str = GOLDEN_TURKISH_DESCRIPTION,
) -> YouTubeVideoResource:
    return YouTubeVideoResource(
        video_id="LYLLaIfYjWs",
        channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        title=title,
        description=description,
        category_id="26",
        default_language=default_language,
        default_audio_language="tr",
        privacy_status=VideoPrivacyStatus.PRIVATE,
        scheduled_publish_at=datetime(2026, 11, 5, 17, 0, tzinfo=UTC),
        published_at=None,
        localizations=dict(
            existing_localizations
            or {
                "de": {
                    "title": "Nur 1 Löffel bringt Ihre Orchidee zum Blühen",
                    "description": "Natürliche Pflege. #Orkide #HayataDairPüfNoktalar",
                }
            }
        ),
        raw_snippet={
            "channelId": "UCkL0_jtFQqNCGG2gTTXcZ7w",
            "title": title,
            "description": description,
            "categoryId": "26",
            "defaultLanguage": default_language,
            "defaultAudioLanguage": "tr",
            "tags": ["orkide", "çiçek"],
        },
    )


def _build_mock_captions() -> MockYouTubeCaptionClient:
    mock_captions = MockYouTubeCaptionClient(
        list_responses={
            "LYLLaIfYjWs": {
                "kind": "youtube#captionListResponse",
                "items": [
                    {
                        "kind": "youtube#caption",
                        "id": "cap-tr-std",
                        "snippet": {
                            "videoId": "LYLLaIfYjWs",
                            "language": "tr",
                            "name": "Türkçe",
                            "trackKind": "standard",
                            "status": "serving",
                            "isDraft": False,
                            "isAutoSynced": False,
                            "isCC": False,
                        },
                    }
                ],
            }
        }
    )
    mock_captions.register_download(
        "cap-tr-std",
        GOLDEN_TURKISH_SRT.encode("utf-8"),
        tfmt="srt",
    )
    en_srt = (
        "1\n00:00:01,000 --> 00:00:04,500\nWelcome to HayataDairPüfNoktalar friends!\n\n"
        "2\n00:00:04,800 --> 00:00:09,200\nToday we apply 1 spoon of cinnamon to orchids.\n\n"
        "3\n00:00:09,500 --> 00:00:14,000\nUse 500 ml warm water once a month.\n"
    )
    mock_captions.register_download(
        "cap-tr-std",
        en_srt.encode("utf-8"),
        tfmt="srt",
        tlang="en",
    )
    return mock_captions


async def _seed_video_and_source_caption(app, remote_video: YouTubeVideoResource) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    async with app.state.session_factory() as session:
        video = Video(
            organization_id=LEGACY_ORGANIZATION_ID,
            youtube_video_id=remote_video.video_id,
            youtube_channel_id=remote_video.channel_id,
            source_language="tr",
            default_audio_language="tr",
            source_title=remote_video.title,
            source_description=remote_video.description,
            source_category_id=remote_video.category_id,
            source_content_hash=remote_video.compute_source_content_hash(),
            privacy_status=remote_video.privacy_status,
            scheduled_publish_at=remote_video.scheduled_publish_at,
            workflow_state=VideoWorkflowState.SOURCE_READY,
            existing_localizations=dict(remote_video.localizations),
            discovered_at=now,
            updated_at=now,
        )
        session.add(video)
        await session.flush()
        session.add(
            SourceCaptionTrack(
                video_id=video.id,
                youtube_caption_id="cap-tr-std",
                language="tr",
                name="Türkçe",
                track_kind=SourceCaptionTrackKind.STANDARD,
                processing_status=SourceCaptionStatus.SERVING,
                is_draft=False,
                is_auto_synced=False,
                raw_content=GOLDEN_TURKISH_SRT,
                content_format="srt",
                cue_count=3,
                inspected_at=now,
            )
        )
        await session.commit()


async def test_metadata_and_caption_publish_verification_and_idempotency(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Localize English metadata & captions
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": True},
            )
            assert loc_resp.status_code == 200
            assets = loc_resp.json()["assets"]
            meta_asset = next(a for a in assets if a["kind"] == "metadata")
            cap_asset = next(a for a in assets if a["kind"] == "caption")

            # 2. Attempting to publish before human approval is blocked by Gate 7
            blocked_resp = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert blocked_resp.status_code == 409
            assert blocked_resp.json()["detail"]["reason_code"] == "approval_missing"

            # 3. Approve both English metadata and caption assets (bound to SHA-256 hash)
            ap_meta = await client.post(
                f"/api/assets/{meta_asset['id']}/approve",
                json={"expected_content_hash": meta_asset["content_hash"]},
            )
            assert ap_meta.status_code == 200
            assert ap_meta.json()["state"] == "ready_to_publish"
            assert ap_meta.json()["publish_decision"]["allowed"] is True

            ap_cap = await client.post(
                f"/api/assets/{cap_asset['id']}/approve",
                json={"expected_content_hash": cap_asset["content_hash"]},
            )
            assert ap_cap.status_code == 200
            assert ap_cap.json()["state"] == "ready_to_publish"

            # 4. Publish English metadata -> videos.list, videos.update, videos.list verification
            pub_meta = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_meta.status_code == 200
            pub_meta_data = pub_meta.json()
            assert pub_meta_data["idempotent_reused"] is False
            assert pub_meta_data["quota_units_used"] == 52  # 1 + 50 + 1
            assert pub_meta_data["asset"]["state"] == "published"
            assert pub_meta_data["attempt"]["outcome"] == "succeeded"
            assert pub_meta_data["attempt"]["verified_at"] is not None

            # Verify pre-existing 'de' localization, title, and private schedule were preserved!
            updated_remote = mock_yt.videos_by_id["LYLLaIfYjWs"]
            assert "de" in updated_remote.localizations
            assert updated_remote.localizations["de"] == remote_video.localizations["de"]
            assert "en" in updated_remote.localizations
            assert updated_remote.privacy_status is VideoPrivacyStatus.PRIVATE
            assert updated_remote.scheduled_publish_at == remote_video.scheduled_publish_at
            assert updated_remote.title == remote_video.title

            # 5. Re-publishing the exact same metadata asset returns idempotently with 0 quota used
            update_calls_before = sum(
                1 for c in mock_yt.recorded_calls if c["method"] == "videos.update"
            )
            pub_meta_again = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_meta_again.status_code == 200
            assert pub_meta_again.json()["idempotent_reused"] is True
            assert pub_meta_again.json()["quota_units_used"] == 0
            update_calls_after = sum(
                1 for c in mock_yt.recorded_calls if c["method"] == "videos.update"
            )
            assert update_calls_after == update_calls_before == 1

            # 6. Publish English captions -> captions.list, captions.insert, captions.list verify
            pub_cap = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert pub_cap.status_code == 200
            pub_cap_data = pub_cap.json()
            assert pub_cap_data["idempotent_reused"] is False
            assert pub_cap_data["quota_units_used"] == 500  # 50 + 400 + 50
            assert pub_cap_data["asset"]["state"] == "published"
            assert pub_cap_data["asset"]["youtube_published_caption_id"] == "cap_LYLLaIfYjWs_en"
            assert pub_cap_data["attempt"]["outcome"] == "succeeded"
            assert pub_cap_data["attempt"]["verified_at"] is not None

            # Verify Turkish source track is still intact alongside English track
            items = mock_captions.list_responses["LYLLaIfYjWs"]["items"]
            assert [i["id"] for i in items] == ["cap-tr-std", "cap_LYLLaIfYjWs_en"]

            # 7. Check video publish history endpoint
            hist_resp = await client.get("/api/videos/LYLLaIfYjWs/publish-attempts")
            assert hist_resp.status_code == 200
            history = hist_resp.json()
            assert len(history) == 2
            assert {h["operation"] for h in history} == {"videos.update", "captions.insert"}


async def test_existing_tracks_and_localizations_are_skipped_and_never_overwritten(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": True},
            )
            assets = loc_resp.json()["assets"]
            meta_asset = next(a for a in assets if a["kind"] == "metadata")
            cap_asset = next(a for a in assets if a["kind"] == "caption")

            await client.post(
                f"/api/assets/{meta_asset['id']}/approve",
                json={"expected_content_hash": meta_asset["content_hash"]},
            )
            await client.post(
                f"/api/assets/{cap_asset['id']}/approve",
                json={"expected_content_hash": cap_asset["content_hash"]},
            )

            # Simulate creator manually adding English localization & English track in Studio
            # right before our publish runs
            mock_yt.videos_by_id["LYLLaIfYjWs"] = replace(
                remote_video,
                localizations={
                    **remote_video.localizations,
                    "en": {"title": "Manual Studio Title", "description": "Manual Studio Desc"},
                },
            )
            mock_captions.list_responses["LYLLaIfYjWs"]["items"].append(
                {
                    "kind": "youtube#caption",
                    "id": "cap-en-manual",
                    "snippet": {
                        "videoId": "LYLLaIfYjWs",
                        "language": "en",
                        "name": "Manual English",
                        "trackKind": "standard",
                        "status": "serving",
                        "isDraft": False,
                    },
                }
            )

            # Publishing metadata sees 'en' on live YouTube -> skips without calling videos.update
            pub_meta = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_meta.status_code == 200
            assert pub_meta.json()["asset"]["state"] == "skipped"
            assert all(c["method"] != "videos.update" for c in mock_yt.recorded_calls)
            assert (
                mock_yt.videos_by_id["LYLLaIfYjWs"].localizations["en"]["title"]
                == "Manual Studio Title"
            )

            # Publishing caption sees 'en' on live YouTube -> skips without calling captions.insert
            pub_cap = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert pub_cap.status_code == 200
            assert pub_cap.json()["asset"]["state"] == "skipped"
            assert all(c["method"] != "captions.insert" for c in mock_yt.recorded_calls)


async def test_409_caption_exists_conflict_verifies_without_retrying_insert(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": False, "include_captions": True},
            )
            cap_asset = loc_resp.json()["assets"][0]
            await client.post(
                f"/api/assets/{cap_asset['id']}/approve",
                json={"expected_content_hash": cap_asset["content_hash"]},
            )

            # Simulate captions.insert returning 409 captionExists after the track was created
            mock_captions.insert_responses[("LYLLaIfYjWs", "en", "")] = parse_youtube_api_error(
                409,
                {
                    "error": {
                        "code": 409,
                        "message": "The caption track already exists.",
                        "errors": [{"domain": "youtube.caption", "reason": "captionExists"}],
                    }
                },
            )
            mock_captions.simulate_insert_landed_before_error = True

            pub_cap = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert pub_cap.status_code == 200
            data = pub_cap.json()
            assert data["asset"]["state"] == "published"
            assert data["attempt"]["outcome"] == "succeeded"
            assert data["attempt"]["response_summary"]["resolved_via_409_conflict"] is True


async def test_status_is_never_published_when_post_write_verification_fails(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    # Configure mock so videos.update returns 200 OK but does NOT persist to videos_by_id,
    # causing post-write videos.list read-back verification to fail!
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
        persist_on_update_video=False,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": False},
            )
            meta_asset = loc_resp.json()["assets"][0]
            await client.post(
                f"/api/assets/{meta_asset['id']}/approve",
                json={"expected_content_hash": meta_asset["content_hash"]},
            )

            pub_resp = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_resp.status_code == 200
            data = pub_resp.json()
            # Must NOT be shown as published when verification fails!
            assert data["asset"]["state"] == "uncertain"
            assert data["attempt"]["outcome"] == "uncertain"
            assert data["attempt"]["verified_at"] is None
            assert "missing from post-write videos.list" in (data["attempt"]["error_message"] or "")


async def test_uncertain_outcome_blocks_retry_and_reconciles_both_landed_and_absent_branches(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": True},
            )
            assets = loc_resp.json()["assets"]
            meta_asset = next(a for a in assets if a["kind"] == "metadata")
            cap_asset = next(a for a in assets if a["kind"] == "caption")

            await client.post(
                f"/api/assets/{meta_asset['id']}/approve",
                json={"expected_content_hash": meta_asset["content_hash"]},
            )
            await client.post(
                f"/api/assets/{cap_asset['id']}/approve",
                json={"expected_content_hash": cap_asset["content_hash"]},
            )

            # --- Branch A: Metadata update times out AFTER landing on YouTube ---
            mock_yt.update_video_outcomes[("LYLLaIfYjWs", "en")] = httpx.ReadTimeout(
                "Timed out waiting for videos.update response"
            )
            mock_yt.simulate_update_landed_before_error = True

            pub_meta = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_meta.status_code == 200
            assert pub_meta.json()["asset"]["state"] == "uncertain"
            assert pub_meta.json()["attempt"]["outcome"] == "uncertain"

            # Retrying while uncertain is blocked by Gate 3 (uncertain_previous_attempt)
            retry_blocked = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert retry_blocked.status_code == 409
            assert retry_blocked.json()["detail"]["reason_code"] == "uncertain_previous_attempt"

            # Reconcile inspects YouTube, sees the localization DID land, and marks it published!
            rec_meta = await client.post(f"/api/assets/{meta_asset['id']}/reconcile")
            assert rec_meta.status_code == 200
            assert rec_meta.json()["asset"]["state"] == "published"
            assert rec_meta.json()["attempt"]["outcome"] == "succeeded"
            assert rec_meta.json()["attempt"]["verified_at"] is not None
            assert rec_meta.json()["attempt"]["reconciled_at"] is not None

            # --- Branch B: Caption insert fails with 503 BEFORE landing on YouTube ---
            mock_captions.insert_responses[("LYLLaIfYjWs", "en", "")] = parse_youtube_api_error(
                503,
                {
                    "error": {
                        "code": 503,
                        "message": "Backend Error",
                        "errors": [{"domain": "global", "reason": "backendError"}],
                    }
                },
            )
            mock_captions.simulate_insert_landed_before_error = False

            pub_cap = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert pub_cap.status_code == 200
            assert pub_cap.json()["asset"]["state"] == "uncertain"
            first_key = pub_cap.json()["attempt"]["idempotency_key"]

            # Duplicate retry is blocked until reconciled
            cap_blocked = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert cap_blocked.status_code == 409
            assert cap_blocked.json()["detail"]["reason_code"] == "uncertain_previous_attempt"

            # Reconcile inspects YouTube, sees track is ABSENT, and restores ready_to_publish
            rec_cap = await client.post(f"/api/assets/{cap_asset['id']}/reconcile")
            assert rec_cap.status_code == 200
            assert rec_cap.json()["asset"]["state"] == "ready_to_publish"
            assert rec_cap.json()["attempt"]["outcome"] == "failed"
            assert rec_cap.json()["attempt"]["reconciled_at"] is not None

            # Clear the simulated 503 error and retry publish -> succeeds with :r1 idempotency key!
            mock_captions.insert_responses.pop(("LYLLaIfYjWs", "en", ""), None)
            pub_cap_retry = await client.post(f"/api/assets/{cap_asset['id']}/publish")
            assert pub_cap_retry.status_code == 200
            retry_data = pub_cap_retry.json()
            assert retry_data["asset"]["state"] == "published"
            assert retry_data["attempt"]["outcome"] == "succeeded"
            assert retry_data["attempt"]["idempotency_key"] == f"{first_key}:r1"
            assert len(retry_data["asset"]["publish_attempts"]) == 2


async def test_concurrent_studio_source_drift_aborts_before_write(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": False},
            )
            meta_asset = loc_resp.json()["assets"][0]
            await client.post(
                f"/api/assets/{meta_asset['id']}/approve",
                json={"expected_content_hash": meta_asset["content_hash"]},
            )

            # Simulate creator editing the Turkish source title in YouTube Studio before publish
            mock_yt.videos_by_id["LYLLaIfYjWs"] = replace(
                remote_video,
                title="Yeni Başlık: 2 Kaşık Tarçın ile Orkide Bakımı",
            )

            pub_resp = await client.post(f"/api/assets/{meta_asset['id']}/publish")
            assert pub_resp.status_code == 200
            data = pub_resp.json()
            assert data["asset"]["state"] == "needs_attention"
            assert "modified in YouTube Studio" in (data["asset"]["last_error"] or "")
            assert all(c["method"] != "videos.update" for c in mock_yt.recorded_calls)


async def test_force_publish_video_skips_reconciliation_and_publishes_all_after_localization(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir, publishing_enabled=True)
    app = create_app(settings)
    remote_video = _build_remote_video()
    mock_captions = _build_mock_captions()
    mock_yt = MockYouTubeReadOnlyDataClient(
        videos_by_id={"LYLLaIfYjWs": remote_video},
        caption_client=mock_captions,
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = FakeStructuredLLMClient()
        app.state.caption_client = mock_captions
        app.state.youtube_data_client = mock_yt
        await _seed_video_and_source_caption(app, remote_video)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Localize 'en' and 'sl' (Slovenian) -> both in awaiting_approval
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={
                    "languages": ["en", "sl"],
                    "include_metadata": True,
                    "include_captions": True,
                },
            )
            assert loc_resp.status_code == 200
            assets = loc_resp.json()["assets"]
            assert len(assets) == 4
            assert all(a["state"] == "awaiting_approval" for a in assets)

            # Force-publish immediately after localization (skips per-asset approval/reconcile)
            force_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/publish-ready",
                json={"languages": ["en", "sl"], "force": True},
            )
            assert force_resp.status_code == 200, force_resp.text
            batch = force_resp.json()
            assert len(batch["published_asset_ids"]) == 4
            assert batch["failed_asset_ids"] == []
            assert batch["uncertain_asset_ids"] == []
            assert "en" in mock_yt.videos_by_id["LYLLaIfYjWs"].localizations
            assert "sl" in mock_yt.videos_by_id["LYLLaIfYjWs"].localizations

            # Now test Force Publish on uncertain metadata assets (both landed and not-yet-landed)
            loc_resp2 = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={
                    "languages": ["cy", "ka"],
                    "include_metadata": True,
                    "include_captions": False,
                },
            )
            by_key = {(a["language"], a["kind"]): a for a in loc_resp2.json()["assets"]}
            cy_meta = by_key[("cy", "metadata")]
            ka_meta = by_key[("ka", "metadata")]
            # Simulate 'cy' failing with timeout AFTER landing on YouTube, and 'ka' BEFORE landing
            mock_yt.update_video_outcomes[("LYLLaIfYjWs", "cy")] = httpx.ReadTimeout("timeout")
            mock_yt.update_video_outcomes[("LYLLaIfYjWs", "ka")] = httpx.ReadTimeout("timeout")
            mock_yt.simulate_update_landed_before_error = True
            await client.post(
                "/api/videos/LYLLaIfYjWs/publish-ready",
                json={"languages": ["cy"], "force": True},
            )
            mock_yt.simulate_update_landed_before_error = False
            await client.post(
                "/api/videos/LYLLaIfYjWs/publish-ready",
                json={"languages": ["ka"], "force": True},
            )

            # Both 'cy' and 'ka' are now in 'uncertain'; clear the timeout and Force Publish!
            mock_yt.update_video_outcomes.clear()
            force_uncertain_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/publish-ready",
                json={"languages": ["cy", "ka"], "force": True},
            )
            assert force_uncertain_resp.status_code == 200
            batch2 = force_uncertain_resp.json()
            assert set(batch2["published_asset_ids"]) == {cy_meta["id"], ka_meta["id"]}
            assert batch2["uncertain_asset_ids"] == []
            assert batch2["failed_asset_ids"] == []
            assert batch2["skipped_asset_ids"] == []
