"""Stage 5 integration tests for per-video localization orchestration and API endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.settings import AppEnv, Settings
from app.main import create_app
from app.models.entities import SourceCaptionTrack, Video
from app.models.enums import (
    AssetState,
    SourceCaptionStatus,
    SourceCaptionTrackKind,
    VideoPrivacyStatus,
    VideoWorkflowState,
)
from app.schemas.localization import LocalizedMetadataLLMOutput
from app.services.translation.providers import FakeStructuredLLMClient
from app.services.youtube.captions_client import MockYouTubeCaptionClient
from tests.fixtures.turkish_golden import (
    GOLDEN_TURKISH_DESCRIPTION,
    GOLDEN_TURKISH_SRT,
    GOLDEN_TURKISH_TITLE,
    GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT,
)

pytestmark = pytest.mark.integration


def _make_test_settings(tmp_path: Path, config_dir: Path) -> Settings:
    db_file = tmp_path / "stage5_test.db"
    return Settings(
        app_env=AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{db_file}",
        config_dir=config_dir,
        youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        youtube_publishing_enabled=False,
    )


async def test_localize_video_endpoint_end_to_end_with_skip_and_fallback(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_test_settings(tmp_path, config_dir)
    app = create_app(settings)

    fake_llm = FakeStructuredLLMClient()
    mock_captions = MockYouTubeCaptionClient()

    # Register native tlang downloads for all languages except 'ro' (which will test LLM fallback)
    # and use the quirky SRT for 'en' (which will test youtube_native + LLM cue repair).
    mock_captions.register_download(
        "cap-tr-std",
        GOLDEN_TURKISH_SRT.encode("utf-8"),
        tfmt="srt",
    )
    mock_captions.register_download(
        "cap-tr-std",
        GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT.encode("utf-8"),
        tfmt="srt",
        tlang="en",
    )
    for tlang in ("es", "fr", "de", "ar", "it", "ru"):
        translated_srt = (
            f"1\n00:00:01,000 --> 00:00:04,500\n[{tlang.upper()}] HayataDairPüfNoktalar 1\n\n"
            f"2\n00:00:04,800 --> 00:00:09,200\n[{tlang.upper()}] 1 spoon cinnamon\n\n"
            f"3\n00:00:09,500 --> 00:00:14,000\n[{tlang.upper()}] 500 ml warm water\n"
        )
        mock_captions.register_download(
            "cap-tr-std",
            translated_srt.encode("utf-8"),
            tfmt="srt",
            tlang=tlang,
        )

    async with app.router.lifespan_context(app):
        app.state.llm_client = fake_llm
        app.state.caption_client = mock_captions

        now = datetime.now(UTC)
        async with app.state.session_factory() as session:
            video = Video(
                youtube_video_id="LYLLaIfYjWs",
                youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
                source_language="tr",
                source_title=GOLDEN_TURKISH_TITLE,
                source_description=GOLDEN_TURKISH_DESCRIPTION,
                source_content_hash="sha256:test",
                privacy_status=VideoPrivacyStatus.PRIVATE,
                workflow_state=VideoWorkflowState.SOURCE_READY,
                # Suppose 'it' already has metadata localization on YouTube
                existing_localizations={
                    "tr": {"title": GOLDEN_TURKISH_TITLE},
                    "it": {"title": "IT"},
                },
                discovered_at=now,
                updated_at=now,
            )
            session.add(video)
            await session.flush()

            session.add_all(
                [
                    SourceCaptionTrack(
                        video_id=video.id,
                        youtube_caption_id="cap-tr-asr",
                        language="tr",
                        name="",
                        track_kind=SourceCaptionTrackKind.ASR,
                        processing_status=SourceCaptionStatus.SERVING,
                        is_draft=False,
                        is_auto_synced=True,
                        inspected_at=now,
                    ),
                    SourceCaptionTrack(
                        video_id=video.id,
                        youtube_caption_id="cap-tr-std",
                        language="tr",
                        name="Turkish",
                        track_kind=SourceCaptionTrackKind.STANDARD,
                        processing_status=SourceCaptionStatus.SERVING,
                        is_draft=False,
                        is_auto_synced=False,
                        inspected_at=now,
                    ),
                    # Suppose 'ru' already has a serving caption track on YouTube
                    SourceCaptionTrack(
                        video_id=video.id,
                        youtube_caption_id="cap-ru-existing",
                        language="ru",
                        name="Russian",
                        track_kind=SourceCaptionTrackKind.STANDARD,
                        processing_status=SourceCaptionStatus.SERVING,
                        is_draft=False,
                        is_auto_synced=False,
                        inspected_at=now,
                    ),
                ]
            )
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"include_metadata": True, "include_captions": True},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()

            assert data["youtube_video_id"] == "LYLLaIfYjWs"
            assert data["selected_source_caption_id"] == "cap-tr-std"
            assert data["selected_source_track_kind"] == "standard"
            # 47 target languages * 2 asset kinds (metadata + caption) = 94 assets
            assert len(data["assets"]) == 94

            by_key = {(a["language"], a["kind"]): a for a in data["assets"]}

            # 'it' metadata was skipped because it already existed on YouTube
            assert by_key[("it", "metadata")]["state"] == AssetState.SKIPPED.value
            # 'ru' caption was skipped because it already existed on YouTube
            assert by_key[("ru", "caption")]["state"] == AssetState.SKIPPED.value

            # 'en' caption used youtube_native first with targeted LLM repair for the empty cue
            en_cap = by_key[("en", "caption")]
            assert en_cap["state"] == AssetState.AWAITING_APPROVAL.value
            assert en_cap["provider_used"] == "youtube_native+fake_repair"
            assert en_cap["validation_passed"] is True
            assert en_cap["review_passed"] is True
            assert en_cap["caption_cue_count"] == 3
            assert en_cap["prompt_version"] == "caption_translator.translate.v1"

            # 'ro' caption fell back to LLM ('fake') when youtube_native failed
            ro_cap = by_key[("ro", "caption")]
            assert ro_cap["state"] == AssetState.AWAITING_APPROVAL.value
            assert ro_cap["provider_used"] == "fake"
            assert ro_cap["validation_passed"] is True
            assert ro_cap["caption_cue_count"] == 3

            # 'ar' metadata & caption both passed validation and review, awaiting approval
            ar_meta = by_key[("ar", "metadata")]
            assert ar_meta["state"] == AssetState.AWAITING_APPROVAL.value
            assert ar_meta["validation_passed"] is True
            assert ar_meta["review_passed"] is True
            assert ar_meta["prompt_version"] == "metadata_localizer.translate.v1"
            assert "https://www.youtube.com/@orchidhacks" in ar_meta["translated_description"]

            # GET /api/videos/LYLLaIfYjWs/assets returns the persisted assets
            get_resp = await client.get("/api/videos/LYLLaIfYjWs/assets")
            assert get_resp.status_code == 200
            assert len(get_resp.json()["assets"]) == 94


async def test_localize_video_transitions_to_needs_attention_on_source_ambiguity(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_test_settings(tmp_path, config_dir)
    app = create_app(settings)

    fake_llm = FakeStructuredLLMClient(
        scripted_responses=[
            LocalizedMetadataLLMOutput(
                title="[EN] 1 Spoon Orchid Care | __GLOSSARY_0__",
                description=GOLDEN_TURKISH_DESCRIPTION,
                source_ambiguous=True,
                ambiguity_notes="Source audio measurement is contradictory.",
            )
        ]
    )

    async with app.router.lifespan_context(app):
        app.state.llm_client = fake_llm
        now = datetime.now(UTC)
        async with app.state.session_factory() as session:
            video = Video(
                youtube_video_id="NdOxYWETSdA",
                youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
                source_language="tr",
                source_title=GOLDEN_TURKISH_TITLE,
                source_description=GOLDEN_TURKISH_DESCRIPTION,
                source_content_hash="sha256:test2",
                privacy_status=VideoPrivacyStatus.PRIVATE,
                workflow_state=VideoWorkflowState.DISCOVERED,
                existing_localizations={},
                discovered_at=now,
                updated_at=now,
            )
            session.add(video)
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/videos/NdOxYWETSdA/localize",
                json={"languages": ["en"], "include_metadata": True, "include_captions": True},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["workflow_state"] == VideoWorkflowState.NEEDS_ATTENTION.value
            by_kind = {a["kind"]: a for a in body["assets"]}
            # Metadata asset is marked needs_attention because source_ambiguous=True
            assert by_kind["metadata"]["state"] == AssetState.NEEDS_ATTENTION.value
            assert by_kind["metadata"]["source_ambiguous"] is True
            # Caption asset is marked awaiting_source because no Turkish caption track exists yet
            assert by_kind["caption"]["state"] == AssetState.AWAITING_SOURCE.value
