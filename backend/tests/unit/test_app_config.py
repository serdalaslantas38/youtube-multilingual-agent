from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.app_config import (
    AppConfig,
    LanguagesConfig,
    PublishingPolicyConfig,
    PublishMode,
    aload_app_config,
    load_app_config,
)

INITIAL_TARGETS = {"en", "ro", "es", "fr", "de", "ar", "it", "ru"}
ADDITIONAL_TARGETS = {
    "sq",
    "be",
    "bs",
    "bg",
    "da",
    "id",
    "hy",
    "et",
    "nl",
    "fi",
    "cy",
    "ka",
    "hi",
    "hr",
    "ja",
    "ko",
    "pl",
    "lv",
    "lt",
    "lb",
    "hu",
    "mk",
    "ms",
    "no",
    "pt-PT",
    "sk",
    "sl",
    "sr",
    "uk",
    "ur",
    "vi",
    "el",
    "cs",
    "zh",
    "uz",
    "he",
    "ga",
    "sv",
    "is",
    "az",
}
ALL_CONFIGURED_TARGETS = INITIAL_TARGETS | ADDITIONAL_TARGETS
APPROVAL_LANGS = {"es", "fr", "de", "ar", "it", "ru"}
POPULAR_ORDERED_TARGETS = [
    "ko",
    "pl",
    "it",
    "de",
    "ro",
    "sr",
    "bg",
    "hr",
    "en",
    "cs",
    "el",
    "hu",
    "bs",
    "fr",
    "es",
    "pt-PT",
    "sk",
    "uk",
    "az",
    "ja",
    "ru",
    "nl",
    "mk",
]


def test_repository_config_is_valid(config_dir: Path) -> None:
    cfg = load_app_config(config_dir)
    assert cfg.languages.source_language.code == "tr"
    assert set(cfg.languages.enabled_codes) == ALL_CONFIGURED_TARGETS
    assert len(cfg.languages.enabled_codes) == 48
    assert cfg.languages.popular_codes == POPULAR_ORDERED_TARGETS
    assert cfg.languages.enabled_codes[:23] == POPULAR_ORDERED_TARGETS


async def test_async_loader_matches_sync(config_dir: Path) -> None:
    assert await aload_app_config(config_dir) == load_app_config(config_dir)


def test_auto_publish_is_globally_disabled_in_repository_config(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    assert policy.global_.auto_publish_enabled is False


def test_initial_configured_modes(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    assert policy.configured_mode("en") is PublishMode.AUTO_AFTER_VALIDATION
    assert policy.configured_mode("ro") is PublishMode.AUTO_AFTER_VALIDATION
    for code in APPROVAL_LANGS:
        assert policy.configured_mode(code) is PublishMode.APPROVAL_REQUIRED
    # Languages added later default to approval.
    assert policy.configured_mode("ja") is PublishMode.APPROVAL_REQUIRED


def test_global_switch_off_downgrades_auto_languages_to_approval(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    for code in INITIAL_TARGETS | {"ja"}:
        assert policy.effective_mode(code) is PublishMode.APPROVAL_REQUIRED


def test_global_switch_on_enables_only_auto_languages() -> None:
    policy = PublishingPolicyConfig.model_validate(
        {
            "version": 1,
            "global": {"auto_publish_enabled": True},
            "languages": {"en": "auto_after_validation", "es": "approval_required"},
        }
    )
    assert policy.effective_mode("en") is PublishMode.AUTO_AFTER_VALIDATION
    assert policy.effective_mode("es") is PublishMode.APPROVAL_REQUIRED
    assert policy.effective_mode("xx") is PublishMode.APPROVAL_REQUIRED


def test_existing_captions_are_never_overwritten_by_config(config_dir: Path) -> None:
    policy = load_app_config(config_dir).policy
    assert policy.captions.existing_track_strategy == "skip_and_report"
    with pytest.raises(ValidationError):
        PublishingPolicyConfig.model_validate(
            {"version": 1, "global": {}, "captions": {"existing_track_strategy": "overwrite"}}
        )


def test_unknown_policy_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PublishingPolicyConfig.model_validate(
            {"version": 1, "global": {"auto_publish_enabled": False, "typo": True}}
        )


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PublishingPolicyConfig.model_validate(
            {"version": 1, "global": {}, "languages": {"en": "always"}}
        )


def test_policy_cannot_reference_unknown_language(config_dir: Path) -> None:
    cfg = load_app_config(config_dir)
    bad_policy = cfg.policy.model_copy(update={"languages": {"zz": PublishMode.DISABLED}})
    with pytest.raises(ValidationError, match="unknown languages"):
        AppConfig(languages=cfg.languages, policy=bad_policy)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["target_languages"].append(dict(d["target_languages"][0])), "duplicate"),
        (lambda d: d["target_languages"][0].update(code="tr"), "source language"),
        (lambda d: d["target_languages"][0].update(caption_tlang="eng"), "ISO 639-1"),
        (lambda d: d["target_languages"][0].update(code="EN!"), "invalid language code"),
    ],
)
def test_languages_config_validation(config_dir: Path, mutation, message: str) -> None:  # type: ignore[no-untyped-def]
    data = yaml.safe_load((config_dir / "languages.yaml").read_text(encoding="utf-8"))
    mutation(data)
    with pytest.raises(ValidationError, match=message):
        LanguagesConfig.model_validate(data)


def test_rtl_languages_and_regional_codes_are_configured(config_dir: Path) -> None:
    languages = load_app_config(config_dir).languages
    for rtl_code in ("ar", "ur", "he"):
        lang = languages.get(rtl_code)
        assert lang is not None and lang.rtl is True

    pt_pt = languages.get("pt-PT")
    assert pt_pt is not None
    assert pt_pt.caption_tlang == "pt"
    assert pt_pt.rtl is False
