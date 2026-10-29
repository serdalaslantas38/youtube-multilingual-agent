"""Unit tests for OAuth PKCE/state generation, Fernet token encryption, and YouTube parsers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.crypto import TokenEncryptionError, decrypt_secret, encrypt_secret
from app.models.enums import VideoPrivacyStatus
from app.services.youtube import (
    DEFAULT_YOUTUBE_SCOPES,
    OAuthClientConfig,
    OAuthStateError,
    OAuthStateStore,
    YouTubeMalformedResponseError,
    build_authorization_url,
    parse_channels_list_response,
    parse_playlist_items_video_ids,
    parse_videos_list_response,
)


def test_fernet_token_encryption_and_decryption_roundtrip() -> None:
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    plaintext_refresh = "1//0g_secret_refresh_token_value_12345"

    ciphertext = encrypt_secret(plaintext_refresh, key)
    assert plaintext_refresh not in ciphertext
    assert decrypt_secret(ciphertext, key) == plaintext_refresh


def test_passphrase_derived_fernet_key_roundtrip() -> None:
    passphrase = SecretStr("my-local-dev-passphrase-with-at-least-16-chars")
    ciphertext = encrypt_secret("refresh-xyz", passphrase)
    assert decrypt_secret(ciphertext, passphrase) == "refresh-xyz"


def test_encryption_rejects_empty_or_wrong_key() -> None:
    key1 = SecretStr(Fernet.generate_key().decode("ascii"))
    key2 = SecretStr(Fernet.generate_key().decode("ascii"))

    with pytest.raises(TokenEncryptionError):
        encrypt_secret("secret", SecretStr(""))

    with pytest.raises(TokenEncryptionError):
        encrypt_secret("   ", key1)

    ciphertext = encrypt_secret("valid-token", key1)
    with pytest.raises(TokenEncryptionError, match="Failed to decrypt"):
        decrypt_secret(ciphertext, key2)


def test_oauth_state_store_pkce_and_single_use_consumption() -> None:
    store = OAuthStateStore(ttl_seconds=300)
    now = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)

    session, challenge = store.create_session(
        redirect_uri="http://localhost:8000/auth/callback",
        now=now,
    )
    assert len(session.state) >= 32
    assert len(session.code_verifier) >= 43
    assert challenge != session.code_verifier

    cfg = OAuthClientConfig(
        client_id="client-123.apps.googleusercontent.com",
        client_secret="secret-xyz",  # noqa: S106
        redirect_uri="http://localhost:8000/auth/callback",
    )
    url = build_authorization_url(cfg, state=session.state, code_challenge=challenge)
    assert "access_type=offline" in url
    assert "code_challenge_method=S256" in url
    assert "youtube.force-ssl" in url
    assert "secret-xyz" not in url

    # Single-use consumption succeeds once and rejects replay
    consumed = store.consume_session(session.state, now=now + timedelta(seconds=10))
    assert consumed.code_verifier == session.code_verifier
    assert consumed.scopes == DEFAULT_YOUTUBE_SCOPES

    with pytest.raises(OAuthStateError, match="Invalid or expired"):
        store.consume_session(session.state, now=now + timedelta(seconds=11))


def test_oauth_state_store_rejects_expired_state() -> None:
    store = OAuthStateStore(ttl_seconds=60)
    now = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)
    session, _ = store.create_session(redirect_uri="http://localhost:8000/auth/callback", now=now)
    with pytest.raises(OAuthStateError, match="Invalid or expired"):
        store.consume_session(session.state, now=now + timedelta(seconds=61))


def test_parse_channels_playlist_and_videos_responses() -> None:
    channels = parse_channels_list_response(
        {
            "kind": "youtube#channelListResponse",
            "items": [
                {
                    "id": "UC_TURKISH_CH",
                    "snippet": {
                        "title": "Nefis Ev Yemekleri",
                        "customUrl": "@nefisevyemekleri",
                    },
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU_TURKISH_CH"}},
                }
            ],
        }
    )
    assert len(channels) == 1
    assert channels[0].channel_id == "UC_TURKISH_CH"
    assert channels[0].title == "Nefis Ev Yemekleri"
    assert channels[0].custom_url == "@nefisevyemekleri"
    assert channels[0].uploads_playlist_id == "UU_TURKISH_CH"

    video_ids = parse_playlist_items_video_ids(
        {
            "items": [
                {"contentDetails": {"videoId": "vid_001"}},
                {"snippet": {"resourceId": {"videoId": "vid_002"}}},
                {"contentDetails": {"videoId": "vid_001"}},  # duplicate ignored
            ]
        }
    )
    assert video_ids == ["vid_001", "vid_002"]

    videos = parse_videos_list_response(
        {
            "items": [
                {
                    "id": "vid_001",
                    "snippet": {
                        "channelId": "UC_TURKISH_CH",
                        "title": "Mercimek Çorbası Tarifi",
                        "description": "Detaylı tarif burada.",
                        "categoryId": "26",
                        "defaultLanguage": "tr",
                        "defaultAudioLanguage": "tr",
                        "publishedAt": "2026-10-27T18:00:00Z",
                    },
                    "status": {
                        "privacyStatus": "private",
                        "publishAt": "2026-10-28T16:00:00Z",
                    },
                    "localizations": {
                        "en": {
                            "title": "Lentil Soup Recipe",
                            "description": "Detailed recipe here.",
                        }
                    },
                }
            ]
        }
    )
    assert len(videos) == 1
    v = videos[0]
    assert v.video_id == "vid_001"
    assert v.privacy_status is VideoPrivacyStatus.PRIVATE
    assert v.scheduled_publish_at is not None
    assert "en" in v.localizations
    assert v.compute_source_content_hash().startswith("sha256:")


@pytest.mark.parametrize(
    ("parser_fn", "bad_input"),
    [
        (parse_channels_list_response, {}),
        (parse_channels_list_response, {"items": [{"id": ""}]}),
        (parse_playlist_items_video_ids, "not-a-dict"),
        (parse_videos_list_response, {"items": [{"id": "v1", "snippet": {}}]}),
    ],
)
def test_parsers_reject_malformed_payloads(parser_fn, bad_input: object) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(YouTubeMalformedResponseError):
        parser_fn(bad_input)
