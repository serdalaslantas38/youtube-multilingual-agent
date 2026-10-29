"""Typed loading and validation of the YAML configuration files.

``config/languages.yaml`` and ``config/publishing-policy.yaml`` are validated at
startup so a typo can never silently change publishing behavior.
"""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LANGUAGES_FILE = "languages.yaml"
POLICY_FILE = "publishing-policy.yaml"

# BCP-47 subset sufficient for YouTube localizations (e.g. "en", "pt-BR", "zh-Hant").
_BCP47 = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_ISO639_1 = re.compile(r"^[a-z]{2}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceLanguage(_Strict):
    code: str
    name: str
    native_name: str

    @field_validator("code")
    @classmethod
    def _bcp47(cls, v: str) -> str:
        if not _BCP47.match(v):
            raise ValueError(f"invalid language code: {v!r}")
        return v


class TargetLanguage(SourceLanguage):
    caption_tlang: str
    enabled: bool = True
    rtl: bool = False

    @field_validator("caption_tlang")
    @classmethod
    def _iso639_1(cls, v: str) -> str:
        if not _ISO639_1.match(v):
            raise ValueError(f"caption_tlang must be ISO 639-1 (two letters): {v!r}")
        return v


class LanguagesConfig(_Strict):
    version: Literal[1]
    source_language: SourceLanguage
    target_languages: list[TargetLanguage]
    protected_glossary: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> LanguagesConfig:
        codes = [t.code for t in self.target_languages]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate target language codes")
        if self.source_language.code in codes:
            raise ValueError("source language must not be a target language")
        return self

    def get(self, code: str) -> TargetLanguage | None:
        return next((t for t in self.target_languages if t.code == code), None)

    @property
    def enabled_codes(self) -> list[str]:
        return [t.code for t in self.target_languages if t.enabled]


class PublishMode(StrEnum):
    AUTO_AFTER_VALIDATION = "auto_after_validation"
    APPROVAL_REQUIRED = "approval_required"
    DISABLED = "disabled"


class GlobalPolicy(_Strict):
    auto_publish_enabled: bool = False


TranslationSource = Literal["youtube_native", "llm"]


def _default_translation_sources() -> list[TranslationSource]:
    return ["youtube_native", "llm"]


class CaptionPolicy(_Strict):
    existing_track_strategy: Literal["skip_and_report"] = "skip_and_report"
    translation_sources: list[TranslationSource] = Field(
        default_factory=_default_translation_sources, min_length=1
    )
    max_line_length: int = Field(default=84, ge=20, le=200)
    llm_batch_size: int = Field(default=40, ge=1, le=200)


class MetadataPolicy(_Strict):
    existing_localization_strategy: Literal["skip_and_report"] = "skip_and_report"


class LLMPolicy(_Strict):
    provider: Literal["gemini", "fake"] = "gemini"
    model_name: str = "gemini-2.5-flash"
    max_output_retries: int = Field(default=2, ge=0, le=5)
    metadata_prompt_name: str = "metadata_localizer.translate.v1"
    caption_prompt_name: str = "caption_translator.translate.v1"
    review_prompt_name: str = "quality_reviewer.review.v1"


class PublishingPolicyConfig(_Strict):
    version: Literal[1]
    global_: GlobalPolicy = Field(alias="global")
    default_mode: PublishMode = PublishMode.APPROVAL_REQUIRED
    languages: dict[str, PublishMode] = Field(default_factory=dict)
    captions: CaptionPolicy = Field(default_factory=CaptionPolicy)
    metadata: MetadataPolicy = Field(default_factory=MetadataPolicy)
    llm: LLMPolicy = Field(default_factory=LLMPolicy)

    def configured_mode(self, code: str) -> PublishMode:
        """Mode as written in configuration (ignores the global switch)."""
        return self.languages.get(code, self.default_mode)

    def effective_mode(self, code: str) -> PublishMode:
        """Mode after applying the global auto-publish switch.

        While ``global.auto_publish_enabled`` is false, automatic languages are
        downgraded to ``approval_required``. This is only the *mode*; the policy
        engine still applies the hard gates (validation, ambiguity, uncertain
        API outcome, kill switch) before anything is published.
        """
        mode = self.configured_mode(code)
        if mode is PublishMode.AUTO_AFTER_VALIDATION and not self.global_.auto_publish_enabled:
            return PublishMode.APPROVAL_REQUIRED
        return mode


class AppConfig(_Strict):
    languages: LanguagesConfig
    policy: PublishingPolicyConfig

    @model_validator(mode="after")
    def _cross_validate(self) -> AppConfig:
        known = {t.code for t in self.languages.target_languages}
        unknown = set(self.policy.languages) - known
        if unknown:
            raise ValueError(f"policy references unknown languages: {sorted(unknown)}")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_app_config(config_dir: Path) -> AppConfig:
    return AppConfig(
        languages=LanguagesConfig.model_validate(_read_yaml(config_dir / LANGUAGES_FILE)),
        policy=PublishingPolicyConfig.model_validate(_read_yaml(config_dir / POLICY_FILE)),
    )


async def aload_app_config(config_dir: Path) -> AppConfig:
    """Non-blocking variant for use inside the event loop."""
    return await asyncio.to_thread(load_app_config, config_dir)
