"""Phase 2 application identity, role, CSRF, and tenant boundary tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.api import identity as identity_api
from app.api.dependencies import SESSION_COOKIE
from app.core.settings import AppEnv, Settings
from app.core.tenancy import (
    LEGACY_ORGANIZATION_ID,
    bind_organization_id,
    reset_organization_id,
)
from app.main import create_app
from app.models.entities import (
    ChannelConnection,
    LanguageAsset,
    Organization,
    OrganizationMember,
    User,
    UserSession,
    Video,
)
from app.models.enums import (
    AssetKind,
    AssetState,
    ChannelConnectionStatus,
    VideoPrivacyStatus,
    VideoWorkflowState,
)
from app.services.youtube.discovery import (
    ChannelOwnershipConflictError,
    upsert_authorized_channel_connection,
)
from app.services.youtube.read_service import YouTubeChannelIdentity

pytestmark = pytest.mark.integration


async def _create_session(app, *, role: str = "owner") -> tuple[str, str]:
    now = datetime.now(UTC)
    raw_token = f"test-session-token-{role}"
    csrf_token = "test-csrf-token"
    async with app.state.session_factory() as db:
        user = User(
            id=f"phase2-test-user-{role}",
            oidc_issuer="https://accounts.google.com",
            oidc_subject=f"subject-{role}",
            email=f"{role}@example.test",
            email_verified=True,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.add(
            OrganizationMember(
                organization_id=LEGACY_ORGANIZATION_ID,
                user_id=user.id,
                role=role,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            UserSession(
                user_id=user.id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                csrf_token=csrf_token,
                active_organization_id=LEGACY_ORGANIZATION_ID,
                expires_at=now + timedelta(hours=1),
                created_at=now,
                last_seen_at=now,
            )
        )
        await db.commit()
    return raw_token, csrf_token


def _settings(tmp_path: Path, config_dir: Path) -> Settings:
    return Settings(
        app_env=AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase2_identity.db'}",
        config_dir=config_dir,
        youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        youtube_publishing_enabled=False,
    )


async def test_forged_identity_headers_do_not_authenticate(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _settings(tmp_path, config_dir)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {
                "X-Dashboard-Operator-Email": "owner@example.test",
                "X-Goog-Authenticated-User-Email": "owner@example.test",
            }
            assert (await client.get("/api/settings/policy", headers=headers)).status_code == 401
            assert (
                await client.post("/api/videos/discover", headers=headers)
            ).status_code == 401


async def test_oidc_login_uses_state_nonce_pkce_and_hashed_server_session(
    tmp_path: Path,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, config_dir).model_copy(
        update={
            "google_client_id": "test-client-id",
            "google_client_secret": SecretStr("test-client-secret"),
            "dashboard_allowed_emails": ["login@example.test"],
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            start = await client.get("/auth/oidc/start", follow_redirects=False)
            assert start.status_code == 302
            authorization = urlparse(start.headers["location"])
            params = parse_qs(authorization.query)
            assert params["response_type"] == ["code"]
            assert params["code_challenge_method"] == ["S256"]
            assert params["nonce"] and params["state"]
            state = params["state"][0]
            nonce = params["nonce"][0]
            assert client.cookies.get("yma_oidc_state") == state

            async def fake_exchange(_config, *, code: str, code_verifier: str) -> str:
                assert code == "authorization-code"
                assert code_verifier
                return "signed-id-token"

            class FakeVerifier:
                async def verify(self, token: str, *, audience: str):
                    assert token == "signed-id-token"
                    assert audience == "test-client-id"
                    return {
                        "iss": "https://accounts.google.com",
                        "sub": "google-subject-1",
                        "email": "login@example.test",
                        "email_verified": True,
                        "nonce": nonce,
                    }

            monkeypatch.setattr(identity_api, "exchange_oidc_code", fake_exchange)
            app.state.oidc_verifier = FakeVerifier()
            callback = await client.get(
                "/auth/oidc/callback",
                params={"code": "authorization-code", "state": state},
                follow_redirects=False,
            )
            assert callback.status_code == 303
            raw_session = client.cookies.get(SESSION_COOKIE)
            assert raw_session
            assert "signed-id-token" not in callback.text

            async with app.state.session_factory() as db:
                saved = (await db.execute(select(UserSession))).scalar_one()
                assert saved.token_hash == hashlib.sha256(raw_session.encode()).hexdigest()
                assert saved.token_hash != raw_session

            me = await client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json()["authenticated"] is True
            assert me.json()["email"] == "login@example.test"
            assert me.json()["role"] == "owner"
            logout = await client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": me.json()["csrf_token"]},
            )
            assert logout.status_code == 204
            assert (await client.get("/api/auth/me")).json()["authenticated"] is False


async def test_oidc_rejects_unverified_email_and_replayed_state(
    tmp_path: Path,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, config_dir).model_copy(
        update={
            "google_client_id": "test-client-id",
            "google_client_secret": SecretStr("test-client-secret"),
            "dashboard_allowed_emails": ["login@example.test"],
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            start = await client.get("/auth/oidc/start", follow_redirects=False)
            params = parse_qs(urlparse(start.headers["location"]).query)
            state = params["state"][0]

            async def fake_exchange(_config, *, code: str, code_verifier: str) -> str:
                assert code_verifier
                return "signed-id-token"

            class UnverifiedEmailVerifier:
                async def verify(self, _token: str, *, audience: str):
                    return {
                        "iss": "https://accounts.google.com",
                        "sub": "google-subject-unverified",
                        "email": "login@example.test",
                        "email_verified": False,
                        "nonce": params["nonce"][0],
                    }

            monkeypatch.setattr(identity_api, "exchange_oidc_code", fake_exchange)
            app.state.oidc_verifier = UnverifiedEmailVerifier()
            callback = await client.get(
                "/auth/oidc/callback",
                params={"code": "authorization-code", "state": state},
                follow_redirects=False,
            )
            assert callback.status_code == 403
            replay = await client.get(
                "/auth/oidc/callback",
                params={"code": "authorization-code", "state": state},
                follow_redirects=False,
            )
            assert replay.status_code == 400
            async with app.state.session_factory() as db:
                assert (await db.execute(select(UserSession))).scalars().all() == []


async def test_workspace_activation_requires_membership(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    app = create_app(_settings(tmp_path, config_dir))
    async with app.router.lifespan_context(app):
        raw_token, csrf_token = await _create_session(app)
        async with app.state.session_factory() as db:
            db.add(
                Organization(
                    id="00000000-0000-4000-8000-000000000099",
                    name="Unrelated Workspace",
                    slug="unrelated-workspace",
                    is_legacy=False,
                    is_active=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.cookies.set(SESSION_COOKIE, raw_token)
            response = await client.post(
                "/api/organizations/00000000-0000-4000-8000-000000000099/activate",
                headers={"X-CSRF-Token": csrf_token},
            )
            assert response.status_code == 404


async def test_session_csrf_role_and_cross_organization_isolation(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _settings(tmp_path, config_dir)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        viewer_token, viewer_csrf = await _create_session(app, role="viewer")
        now = datetime.now(UTC)
        foreign_org_id = "00000000-0000-4000-8000-000000000088"
        foreign_video_id = "ForeignVideo1"
        foreign_asset_id: int
        async with app.state.session_factory() as db:
            db.add(
                Organization(
                    id=foreign_org_id,
                    name="Foreign Workspace",
                    slug="foreign-workspace",
                    is_legacy=False,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            video = Video(
                organization_id=foreign_org_id,
                youtube_video_id=foreign_video_id,
                youtube_channel_id="UCforeignchannel",
                source_language="tr",
                source_title="Foreign video",
                source_description="",
                source_content_hash="sha256:foreign",
                privacy_status=VideoPrivacyStatus.PRIVATE,
                workflow_state=VideoWorkflowState.DISCOVERED,
                existing_localizations={},
                discovered_at=now,
                updated_at=now,
            )
            db.add(video)
            await db.flush()
            asset = LanguageAsset(
                video_id=video.id,
                language="en",
                kind=AssetKind.METADATA,
                state=AssetState.PENDING,
                validation_report={},
                review_report={},
                created_at=now,
                updated_at=now,
            )
            db.add(asset)
            db.add(
                ChannelConnection(
                    organization_id=foreign_org_id,
                    youtube_channel_id="UCforeignchannel",
                    channel_title="Foreign channel",
                    encrypted_refresh_token="ciphertext-never-returned",
                    scopes=[],
                    status=ChannelConnectionStatus.CONNECTED,
                    connected_at=now,
                    updated_at=now,
                )
            )
            await db.flush()
            foreign_asset_id = asset.id
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.cookies.set(SESSION_COOKIE, viewer_token)
            assert (await client.get("/api/auth/me")).json()["role"] == "viewer"

            denied_without_csrf = await client.post("/api/auth/logout")
            assert denied_without_csrf.status_code == 403

            denied_by_role = await client.post(
                "/api/assets/999/publish",
                headers={"X-CSRF-Token": viewer_csrf},
            )
            assert denied_by_role.status_code == 403

            listed = await client.get("/api/videos")
            assert listed.status_code == 200
            assert foreign_video_id not in {video["youtube_video_id"] for video in listed.json()}

            foreign_assets = await client.get(f"/api/videos/{foreign_video_id}/assets")
            assert foreign_assets.status_code == 404

            # An administrator in this organization cannot retrieve, edit, or publish
            # an asset whose parent video belongs to another organization.
            owner_token, owner_csrf = await _create_session(app, role="owner")
            client.cookies.set(SESSION_COOKIE, owner_token)
            foreign_asset = await client.get(f"/api/assets/{foreign_asset_id}")
            assert foreign_asset.status_code == 404
            assert (
                await client.put(
                    f"/api/assets/{foreign_asset_id}",
                    json={"translated_title": "tampered"},
                    headers={"X-CSRF-Token": owner_csrf},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/assets/{foreign_asset_id}/approve",
                    json={"expected_content_hash": "sha256:foreign"},
                    headers={"X-CSRF-Token": owner_csrf},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/api/videos/{foreign_video_id}/localize",
                    json={"languages": ["en"]},
                    headers={"X-CSRF-Token": owner_csrf},
                )
            ).status_code == 404
            publish_foreign = await client.post(
                f"/api/assets/{foreign_asset_id}/publish",
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert publish_foreign.status_code == 409
            assert foreign_video_id not in publish_foreign.text

            # Channel status is selected within the active organization; another
            # organization's stored OAuth connection and credentials stay hidden.
            channel_status = await client.get("/api/channel/status")
            assert channel_status.status_code == 200
            assert channel_status.json()["connected"] is False
            assert "ciphertext-never-returned" not in channel_status.text

            # The same globally unique YouTube channel cannot be reassigned by
            # authorizing it from a different workspace.
            tenant_token = bind_organization_id("00000000-0000-4000-8000-000000000077")
            try:
                async with app.state.session_factory() as db:
                    with pytest.raises(ChannelOwnershipConflictError):
                        await upsert_authorized_channel_connection(
                            db,
                            settings=settings,
                            identity=YouTubeChannelIdentity(
                                channel_id="UCforeignchannel",
                                title="Foreign channel",
                                custom_url=None,
                                uploads_playlist_id=None,
                            ),
                            refresh_token="unused",
                            scopes=(),
                        )
            finally:
                reset_organization_id(tenant_token)
