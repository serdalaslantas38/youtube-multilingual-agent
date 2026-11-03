"""Unit tests for deterministic validation, caption diagnostics, and publishing policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.app_config import PublishingPolicyConfig, PublishMode, load_app_config
from app.models.enums import SourceCaptionStatus, SourceCaptionTrackKind
from app.services.policy import PublishEvaluationInput, evaluate_publish_decision
from app.services.validation import (
    SourceCaptionCandidate,
    evaluate_source_caption_tracks,
    validate_language_code,
    validate_localized_metadata,
)


def test_validate_language_code_accepts_enabled_targets(config_dir: Path) -> None:
    languages = load_app_config(config_dir).languages
    for code in ("en", "ro", "es", "fr", "de", "ar", "it", "ru", "sq", "pt-PT", "sl", "ur", "he"):
        report = validate_language_code(code, languages)
        assert report.passed is True
        assert report.issues == ()


@pytest.mark.parametrize(
    ("bad_code", "expected_issue_code"),
    [
        ("", "invalid_language_code"),
        ("EN_US", "invalid_language_code"),
        ("english!", "invalid_language_code"),
        ("123", "invalid_language_code"),
        ("tr", "target_equals_source_language"),
        ("sw", "unsupported_target_language"),
    ],
)
def test_validate_language_code_rejects_invalid_or_unsupported(
    config_dir: Path, bad_code: str, expected_issue_code: str
) -> None:
    languages = load_app_config(config_dir).languages
    report = validate_language_code(bad_code, languages)
    assert report.passed is False
    assert any(issue.code == expected_issue_code for issue in report.issues)


def test_validate_localized_metadata_enforces_limits_and_characters() -> None:
    ok_report = validate_localized_metadata("Valid Title", "Clean description with https://x.com")
    assert ok_report.passed is True

    empty_title = validate_localized_metadata("   ", "desc")
    assert not empty_title.passed
    assert empty_title.issues[0].code == "empty_title"

    long_title = validate_localized_metadata("A" * 101, "desc")
    assert not long_title.passed
    assert long_title.issues[0].code == "title_too_long"

    # Multi-byte UTF-8 description exceeding 5000 bytes
    multibyte_desc = "ğ" * 2501  # 5002 UTF-8 bytes
    long_desc = validate_localized_metadata("Valid", multibyte_desc)
    assert not long_desc.passed
    assert long_desc.issues[0].code == "description_too_long_bytes"

    angle_brackets = validate_localized_metadata("Title", "Contains <script> tag")
    assert not angle_brackets.passed
    assert angle_brackets.issues[0].code == "forbidden_angle_brackets"


def test_source_caption_diagnostics_missing_tracks() -> None:
    diagnosis = evaluate_source_caption_tracks([], source_language="tr")
    assert diagnosis.ready is False
    assert diagnosis.retry_later is True
    assert diagnosis.selected_track is None
    assert diagnosis.reason_code == "missing_source_caption"


def test_source_caption_diagnostics_syncing_track_waits_and_retries() -> None:
    tracks = [
        SourceCaptionCandidate(
            youtube_caption_id="cap-tr-1",
            language="tr",
            track_kind=SourceCaptionTrackKind.STANDARD,
            processing_status=SourceCaptionStatus.SYNCING,
        )
    ]
    diagnosis = evaluate_source_caption_tracks(tracks, source_language="tr")
    assert diagnosis.ready is False
    assert diagnosis.retry_later is True
    assert diagnosis.reason_code == "source_caption_syncing"


def test_source_caption_diagnostics_supports_both_standard_and_asr() -> None:
    # When only ASR exists, it is selected without assuming uploaded files
    asr_only = [
        SourceCaptionCandidate(
            youtube_caption_id="cap-tr-asr",
            language="tr",
            track_kind=SourceCaptionTrackKind.ASR,
            processing_status=SourceCaptionStatus.SERVING,
        )
    ]
    asr_diag = evaluate_source_caption_tracks(asr_only, source_language="tr")
    assert asr_diag.ready is True
    assert asr_diag.selected_track is not None
    assert asr_diag.selected_track.track_kind is SourceCaptionTrackKind.ASR

    # When both ASR and standard exist, standard is preferred
    both = [
        *asr_only,
        SourceCaptionCandidate(
            youtube_caption_id="cap-tr-std",
            language="tr",
            track_kind=SourceCaptionTrackKind.STANDARD,
            processing_status=SourceCaptionStatus.SERVING,
        ),
    ]
    std_diag = evaluate_source_caption_tracks(both, source_language="tr")
    assert std_diag.ready is True
    assert std_diag.selected_track is not None
    assert std_diag.selected_track.youtube_caption_id == "cap-tr-std"


def _base_eval_input(**overrides: object) -> PublishEvaluationInput:
    defaults: dict[str, object] = {
        "language": "en",
        "youtube_publishing_enabled": True,
        "validation_passed": True,
        "review_passed": True,
        "source_ambiguous": False,
        "has_unreconciled_uncertain_attempt": False,
        "target_already_exists_on_youtube": False,
        "content_hash": "sha256:abc123",
        "approved_content_hash": None,
        "explicit_overwrite_authorized": False,
    }
    defaults.update(overrides)
    return PublishEvaluationInput(**defaults)  # type: ignore[arg-type]


def test_policy_blocks_when_kill_switch_is_off(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    decision = evaluate_publish_decision(
        policy,
        _base_eval_input(
            youtube_publishing_enabled=False,
            approved_content_hash="sha256:abc123",
        ),
    )
    assert decision.allowed is False
    assert decision.reason_code == "publishing_disabled_kill_switch"


def test_policy_requires_approval_while_global_auto_publish_is_disabled(
    config_dir: Path,
) -> None:
    policy = load_app_config(config_dir).policy  # global.auto_publish_enabled is False
    # English is configured as auto_after_validation, but global switch is off
    without_approval = evaluate_publish_decision(policy, _base_eval_input(language="en"))
    assert without_approval.allowed is False
    assert without_approval.effective_mode is PublishMode.APPROVAL_REQUIRED
    assert without_approval.reason_code == "approval_missing"

    with_approval = evaluate_publish_decision(
        policy,
        _base_eval_input(language="en", approved_content_hash="sha256:abc123"),
    )
    assert with_approval.allowed is True


def test_policy_allows_auto_languages_when_global_switch_enabled() -> None:
    policy = PublishingPolicyConfig.model_validate(
        {
            "version": 1,
            "global": {"auto_publish_enabled": True},
            "languages": {
                "en": "auto_after_validation",
                "ro": "auto_after_validation",
                "es": "approval_required",
            },
        }
    )
    # English and Romanian pass without human approval when validation + review pass
    for lang in ("en", "ro"):
        d = evaluate_publish_decision(policy, _base_eval_input(language=lang))
        assert d.allowed is True
        assert d.effective_mode is PublishMode.AUTO_AFTER_VALIDATION

    # Spanish still requires approval
    es_decision = evaluate_publish_decision(policy, _base_eval_input(language="es"))
    assert es_decision.allowed is False
    assert es_decision.reason_code == "approval_missing"


def test_policy_invalidates_approval_when_content_is_edited(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    decision = evaluate_publish_decision(
        policy,
        _base_eval_input(
            language="es",
            content_hash="sha256:new-edited-hash",
            approved_content_hash="sha256:old-approved-hash",
        ),
    )
    assert decision.allowed is False
    assert decision.reason_code == "approval_invalidated_by_edit"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"validation_passed": False}, "validation_failed"),
        ({"review_passed": False}, "quality_review_blocked"),
        ({"source_ambiguous": True}, "quality_review_blocked"),
        ({"has_unreconciled_uncertain_attempt": True}, "uncertain_previous_attempt"),
        ({"target_already_exists_on_youtube": True}, "target_already_exists"),
    ],
)
def test_policy_hard_gates_block_even_with_approval(
    config_dir: Path, overrides: dict[str, object], expected_reason: str
) -> None:
    policy = load_app_config(config_dir).policy
    decision = evaluate_publish_decision(
        policy,
        _base_eval_input(approved_content_hash="sha256:abc123", **overrides),
    )
    assert decision.allowed is False
    assert decision.reason_code == expected_reason
