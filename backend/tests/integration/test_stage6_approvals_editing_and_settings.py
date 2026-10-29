"""Stage 6 integration tests for hash-bound approvals, edit invalidation, and policy settings."""

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
from app.services.translation.providers import FakeStructuredLLMClient
from app.services.youtube.captions_client import MockYouTubeCaptionClient
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
    allowed_emails: list[str] | None = None,
) -> Settings:
    db_file = tmp_path / "stage6_test.db"
    return Settings(
        app_env=AppEnv.TEST,
        database_url=f"sqlite+aiosqlite:///{db_file}",
        config_dir=config_dir,
        youtube_channel_id="UCkL0_jtFQqNCGG2gTTXcZ7w",
        youtube_publishing_enabled=False,
        dashboard_allowed_emails=allowed_emails or [],
    )


async def test_approval_hash_binding_and_edit_invalidation_workflow(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(tmp_path, config_dir)
    app = create_app(settings)

    fake_llm = FakeStructuredLLMClient()
    mock_captions = MockYouTubeCaptionClient()
    mock_captions.register_download(
        "cap-tr-std",
        GOLDEN_TURKISH_SRT.encode("utf-8"),
        tfmt="srt",
    )
    ar_srt = (
        "1\n00:00:01,000 --> 00:00:04,500\nمرحباً أصدقائي HayataDairPüfNoktalar\n\n"
        "2\n00:00:04,800 --> 00:00:09,200\nاليوم ملعقة 1 قرفة للأوركيد\n\n"
        "3\n00:00:09,500 --> 00:00:14,000\n500 مل ماء دافئ مرة في الشهر\n"
    )
    mock_captions.register_download(
        "cap-tr-std",
        ar_srt.encode("utf-8"),
        tfmt="srt",
        tlang="ar",
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
                source_content_hash="sha256:src",
                privacy_status=VideoPrivacyStatus.PRIVATE,
                workflow_state=VideoWorkflowState.SOURCE_READY,
                existing_localizations={},
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
                    name="Turkish",
                    track_kind=SourceCaptionTrackKind.STANDARD,
                    processing_status=SourceCaptionStatus.SERVING,
                    is_draft=False,
                    is_auto_synced=False,
                    inspected_at=now,
                )
            )
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Localize Arabic ('ar') metadata and captions
            loc_resp = await client.post(
                "/api/videos/LYLLaIfYjWs/localize",
                json={"languages": ["ar"], "include_metadata": True, "include_captions": True},
            )
            assert loc_resp.status_code == 200
            assets = loc_resp.json()["assets"]
            meta_summary = next(a for a in assets if a["kind"] == "metadata")
            cap_summary = next(a for a in assets if a["kind"] == "caption")
            meta_id = meta_summary["id"]
            cap_id = cap_summary["id"]

            # 2. Fetch full side-by-side detail for Arabic caption (verifies RTL and aligned_cues)
            cap_detail_resp = await client.get(f"/api/assets/{cap_id}")
            assert cap_detail_resp.status_code == 200
            cap_detail = cap_detail_resp.json()
            assert cap_detail["is_rtl"] is True
            assert len(cap_detail["aligned_cues"]) == 3
            assert cap_detail["active_approval"] is None
            original_cap_hash = cap_detail["content_hash"]

            # 3. Attempting to approve with a wrong/stale content hash fails with 409 Conflict
            bad_approve = await client.post(
                f"/api/assets/{cap_id}/approve",
                json={"expected_content_hash": "sha256:wronghash"},
            )
            assert bad_approve.status_code == 409
            assert "Content hash mismatch" in bad_approve.json()["detail"]

            # 4. Approve Arabic caption with its exact content hash -> READY_TO_PUBLISH
            good_approve = await client.post(
                f"/api/assets/{cap_id}/approve",
                json={
                    "expected_content_hash": original_cap_hash,
                    "notes": "Verified Arabic RTL cues",
                },
            )
            assert good_approve.status_code == 200
            approved_data = good_approve.json()
            assert approved_data["state"] == AssetState.READY_TO_PUBLISH.value
            assert approved_data["active_approval"] is not None
            assert approved_data["active_approval"]["approved_content_hash"] == original_cap_hash
            assert approved_data["active_approval"]["is_valid_for_current_hash"] is True

            # 5. Editing the approved caption changes its hash and IMMEDIATELY invalidates approval!
            edited_srt = ar_srt.replace("مرة في الشهر", "مرتين في الشهر")
            edit_resp = await client.put(
                f"/api/assets/{cap_id}",
                json={"caption_content": edited_srt},
            )
            assert edit_resp.status_code == 200
            edited_data = edit_resp.json()
            assert edited_data["content_hash"] != original_cap_hash
            assert edited_data["state"] == AssetState.AWAITING_APPROVAL.value
            assert edited_data["active_approval"] is None
            assert len(edited_data["approval_history"]) == 1
            assert edited_data["approval_history"][0]["invalidated_at"] is not None
            assert (
                edited_data["approval_history"][0]["invalidation_reason"]
                == "content_edited_after_approval"
            )
            assert edited_data["approval_history"][0]["is_valid_for_current_hash"] is False

            # 6. Editing metadata with forbidden '<' fails validation and blocks approval
            bad_edit = await client.put(
                f"/api/assets/{meta_id}",
                json={
                    "translated_title": "فقط 1 ملعقة <أوركيد> | HayataDairPüfNoktalar",
                    "translated_description": GOLDEN_TURKISH_DESCRIPTION,
                },
            )
            assert bad_edit.status_code == 200
            bad_meta = bad_edit.json()
            assert bad_meta["validation_passed"] is False
            assert bad_meta["state"] == AssetState.NEEDS_ATTENTION.value

            blocked_approve = await client.post(
                f"/api/assets/{meta_id}/approve",
                json={"expected_content_hash": bad_meta["content_hash"]},
            )
            assert blocked_approve.status_code == 409
            assert "failed deterministic validation" in blocked_approve.json()["detail"]

            # 7. Rejecting an asset records audit log and keeps state in needs_attention
            reject_resp = await client.post(
                f"/api/assets/{cap_id}/reject",
                json={"reason": "Please re-check cue 3 phrasing"},
            )
            assert reject_resp.status_code == 200
            assert reject_resp.json()["state"] == AssetState.NEEDS_ATTENTION.value

            # 8. Audit trail contains approved, edited, and rejected events
            audit_resp = await client.get("/api/audit-events")
            assert audit_resp.status_code == 200
            actions = [ev["action"] for ev in audit_resp.json()]
            assert "asset.approved" in actions
            assert "asset.edited" in actions
            assert "asset.rejected" in actions


async def test_per_language_policy_settings_and_allowlist_auth(
    tmp_path: Path,
    config_dir: Path,
) -> None:
    settings = _make_settings(
        tmp_path,
        config_dir,
        allowed_emails=["belkiz@example.com", "serdal@example.com"],
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Unauthenticated request is rejected when DASHBOARD_ALLOWED_EMAILS is set
            unauth = await client.get("/api/settings/policy")
            assert unauth.status_code == 401

            # 2. Non-allowlisted email is rejected with 403 Forbidden
            bad_login = await client.post(
                "/api/dashboard/auth/login",
                json={"email": "intruder@example.com"},
            )
            assert bad_login.status_code == 403

            # 3. Allowlisted email signs in and receives session cookie
            ok_login = await client.post(
                "/api/dashboard/auth/login",
                json={"email": "belkiz@example.com"},
            )
            assert ok_login.status_code == 200
            assert ok_login.json()["authenticated"] is True

            # 4. Update Spanish ('es') mode to auto_after_validation and add Portuguese ('pt')
            upd_es = await client.put(
                "/api/settings/policy/languages/es",
                json={"mode": "auto_after_validation", "enabled": True},
            )
            assert upd_es.status_code == 200
            es_item = next(item for item in upd_es.json()["languages"] if item["code"] == "es")
            assert es_item["configured_mode"] == "auto_after_validation"
            # Effective mode is still approval_required while global_auto_publish_enabled is False
            assert es_item["effective_mode"] == "approval_required"

            add_pt = await client.put(
                "/api/settings/policy/languages/pt",
                json={
                    "name": "Portuguese",
                    "native_name": "Português",
                    "caption_tlang": "pt",
                    "enabled": True,
                    "mode": "approval_required",
                },
            )
            assert add_pt.status_code == 200
            codes = [item["code"] for item in add_pt.json()["languages"]]
            assert "pt" in codes

            # 5. Toggle global auto_publish_enabled (audited)
            upd_global = await client.put(
                "/api/settings/policy/global",
                json={
                    "auto_publish_enabled": True,
                    "reason": "Testing effective mode promotion in Stage 6 test",
                },
            )
            assert upd_global.status_code == 200
            assert upd_global.json()["global_auto_publish_enabled"] is True
            es_after = next(item for item in upd_global.json()["languages"] if item["code"] == "es")
            assert es_after["effective_mode"] == "auto_after_validation"
