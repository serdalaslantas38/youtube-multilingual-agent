"""Integration tests for Stage 4: OAuth flow, channel binding, discovery, and captions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.settings import AppEnv, Settings
from app.main import create_app
from app.models import ChannelConnection, VideoPrivacyStatus
from app.services.youtube import (
    DEFAULT_YOUTUBE_SCOPES,
    MockYouTubeReadOnlyDataClient,
    OAuthClientConfig,
    OAuthTokenBundle,
    YouTubeChannelIdentity,
    YouTubeVideoResource,
)
from scripts.poc.caption_diagnostics import build_demo_mock_client

pytestmark = pytest.mark.integration

FAKE_ACCESS_TOKEN = "ya29.fake_access_token_never_leak"  # noqa: S105
FAKE_REFRESH_TOKEN = "1//0g_fake_refresh_token_never_leak"  # noqa: S105
FAKE_CLIENT_SECRET = "GOCSPX-fake_client_secret_never_leak"  # noqa: S105


@dataclass
class FakeOAuthTokenClient:
    """In-memory fake OAuth token exchanger for tests."""

    access_token: str = FAKE_ACCESS_TOKEN
    refresh_token: str = FAKE_REFRESH_TOKEN

    async def exchange_code(
        self,
        client_cfg: OAuthClientConfig,
        *,
        code: str,
        code_verifier: str,
    ) -> OAuthTokenBundle:
        assert code == "valid_auth_code"
        assert len(code_verifier) >= 43
        return OAuthTokenBundle(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=3600,
            scopes=DEFAULT_YOUTUBE_SCOPES,
        )

    async def refresh_access_token(
        self,
        client_cfg: OAuthClientConfig,
        *,
        refresh_token: str,
    ) -> OAuthTokenBundle:
        assert refresh_token == self.refresh_token
        return OAuthTokenBundle(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=3600,
            scopes=DEFAULT_YOUTUBE_SCOPES,
        )


def _build_test_settings(
    tmp_path: Path, config_dir: Path, *, channel_id: str = "UC_WIFE_TR"
) -> Settings:
    fernet_key = Fernet.generate_key().decode("ascii")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'stage4.db'}",
        config_dir=config_dir,
        google_client_id="150413206432-fake.apps.googleusercontent.com",
        google_client_secret=SecretStr(FAKE_CLIENT_SECRET),
        google_redirect_uri="http://localhost:8000/auth/callback",
        youtube_channel_id=channel_id,
        youtube_publishing_enabled=False,
        encryption_key=SecretStr(fernet_key),
    )


def _build_mock_yt_client(channel_id: str = "UC_WIFE_TR") -> MockYouTubeReadOnlyDataClient:
    caption_mock = build_demo_mock_client("vid_tr_public_01", track_kind="standard")
    return MockYouTubeReadOnlyDataClient(
        channel=YouTubeChannelIdentity(
            channel_id=channel_id,
            title="Ayşe'nin Mutfağı",
            custom_url="@ayseninmutfagi",
            uploads_playlist_id="UU_WIFE_TR",
        ),
        playlist_video_ids={"UU_WIFE_TR": ["vid_tr_public_01", "vid_tr_scheduled_02"]},
        videos_by_id={
            "vid_tr_public_01": YouTubeVideoResource(
                video_id="vid_tr_public_01",
                channel_id=channel_id,
                title="Mercimek Çorbası Tarifi",
                description="Harika bir mercimek çorbası: https://example.com #çorba",
                category_id="26",
                default_language="tr",
                default_audio_language="tr",
                privacy_status=VideoPrivacyStatus.PUBLIC,
                scheduled_publish_at=None,
                published_at=datetime(2026, 10, 27, 10, 0, tzinfo=UTC),
                localizations={},
            ),
            "vid_tr_scheduled_02": YouTubeVideoResource(
                video_id="vid_tr_scheduled_02",
                channel_id=channel_id,
                title="Baklava Tarifi (Özel)",
                description="Bayramlık baklava tarifi.",
                category_id="26",
                default_language="tr",
                default_audio_language="tr",
                privacy_status=VideoPrivacyStatus.PRIVATE,
                scheduled_publish_at=datetime(2026, 11, 5, 16, 0, tzinfo=UTC),
                published_at=None,
                localizations={
                    "en": {"title": "Baklava Recipe", "description": "Festive baklava."}
                },
            ),
        },
        caption_client=caption_mock,
    )


async def test_full_oauth_video_discovery_and_caption_inspection_flow(
    tmp_path: Path, config_dir: Path
) -> None:
    settings = _build_test_settings(tmp_path, config_dir, channel_id="UC_WIFE_TR")
    app = create_app(settings)
    mock_yt = _build_mock_yt_client("UC_WIFE_TR")

    app.state.oauth_token_client = FakeOAuthTokenClient()
    app.state.youtube_client_factory = lambda _token: mock_yt

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Before connecting, channel status is connected=False
            status_before = await client.get("/api/channel/status")
            assert status_before.status_code == 200
            assert status_before.json()["connected"] is False

            # 2. Start OAuth flow
            start_resp = await client.get("/auth/google/start")
            assert start_resp.status_code == 200
            start_data = start_resp.json()
            state = start_data["state"]
            assert "accounts.google.com" in start_data["authorization_url"]
            assert FAKE_CLIENT_SECRET not in json.dumps(start_data)

            # 3. Complete OAuth callback
            cb_resp = await client.get(
                "/auth/callback",
                params={"code": "valid_auth_code", "state": state},
            )
            assert cb_resp.status_code == 200
            cb_data = cb_resp.json()
            assert cb_data["connected"] is True
            assert cb_data["youtube_channel_id"] == "UC_WIFE_TR"
            assert cb_data["channel_title"] == "Ayşe'nin Mutfağı"
            assert cb_data["uploads_playlist_id"] == "UU_WIFE_TR"
            assert cb_data["today_quota_units_used"] == 1

            # Verify refresh token is encrypted in DB and never leaked in API response
            serialized_cb = json.dumps(cb_data)
            assert FAKE_REFRESH_TOKEN not in serialized_cb
            assert FAKE_ACCESS_TOKEN not in serialized_cb
            assert FAKE_CLIENT_SECRET not in serialized_cb

            async with app.state.session_factory() as db_sess:
                conn_row = (await db_sess.execute(select(ChannelConnection))).scalar_one()
                assert conn_row.encrypted_refresh_token != FAKE_REFRESH_TOKEN
                assert FAKE_REFRESH_TOKEN not in conn_row.encrypted_refresh_token

            # 4. Discover videos from uploads playlist (includes public + private/scheduled)
            disc_resp = await client.post("/api/videos/discover?max_results=10")
            assert disc_resp.status_code == 200
            disc_data = disc_resp.json()
            assert disc_data["discovered_count"] == 2
            by_id = {v["youtube_video_id"]: v for v in disc_data["videos"]}
            assert by_id["vid_tr_public_01"]["privacy_status"] == "public"
            assert by_id["vid_tr_scheduled_02"]["privacy_status"] == "private"
            assert by_id["vid_tr_scheduled_02"]["scheduled_publish_at"] is not None
            assert by_id["vid_tr_scheduled_02"]["existing_localization_languages"] == ["en"]

            # 5. Inspect captions and run Stage 3b read-only tlang probe on vid_tr_public_01
            insp_resp = await client.post(
                "/api/videos/vid_tr_public_01/captions/inspect",
                params={"probe_translations": "true", "languages": "en,ro,ar"},
            )
            assert insp_resp.status_code == 200
            insp_data = insp_resp.json()
            assert insp_data["source_ready"] is True
            assert insp_data["workflow_state"] == "source_ready"
            assert len(insp_data["tracks"]) == 1
            track = insp_data["tracks"][0]
            assert track["language"] == "tr"
            assert track["track_kind"] == "standard"
            assert track["processing_status"] == "serving"
            assert track["is_downloadable"] is True
            assert track["cue_count"] == 2
            assert track["tlang_diagnostics"]["en"]["download_succeeded"] is True
            assert track["tlang_diagnostics"]["ar"]["validation_passed"] is True


async def test_oauth_callback_rejects_mismatched_channel_id(
    tmp_path: Path, config_dir: Path
) -> None:
    # Configured for UC_WIFE_TR, but user accidentally selects personal channel UC_WRONG_PERSONAL
    settings = _build_test_settings(tmp_path, config_dir, channel_id="UC_WIFE_TR")
    app = create_app(settings)
    wrong_yt = _build_mock_yt_client("UC_WRONG_PERSONAL")

    app.state.oauth_token_client = FakeOAuthTokenClient()
    app.state.youtube_client_factory = lambda _token: wrong_yt

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            start_resp = await client.get("/auth/google/start")
            state = start_resp.json()["state"]

            cb_resp = await client.get(
                "/auth/callback",
                params={"code": "valid_auth_code", "state": state},
            )
            assert cb_resp.status_code == 403
            assert "does not match configured YOUTUBE_CHANNEL_ID" in cb_resp.json()["detail"]
            assert "Brand Account" in cb_resp.json()["detail"]
