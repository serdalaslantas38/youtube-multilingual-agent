"""Subtitle translation service orchestrating configured providers (`youtube_native` -> `llm`)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from app.core.app_config import SourceLanguage, TargetLanguage, TranslationSource
from app.services.captions.parser import SubtitleCue
from app.services.translation.providers import (
    CaptionTranslationError,
    CaptionTranslationProvider,
    CaptionTranslationResult,
    LLMCaptionTranslator,
    LLMStructuredClient,
    YouTubeNativeCaptionTranslator,
)
from app.services.youtube.captions_client import YouTubeCaptionClient


class SubtitleTranslationService:
    """Translates subtitle tracks using the ordered provider chain in `publishing-policy.yaml`."""

    def __init__(
        self,
        *,
        translation_sources: Sequence[TranslationSource],
        llm_client: LLMStructuredClient,
        model_name: str,
        caption_client: YouTubeCaptionClient | None = None,
        caption_prompt_name: str = "caption_translator.translate.v1",
        prompts_dir: Path | None = None,
        max_line_length: int = 84,
        llm_batch_size: int = 40,
        max_output_retries: int = 2,
    ) -> None:
        self._sources = list(translation_sources)
        self._max_line_length = max_line_length
        self._llm_translator = LLMCaptionTranslator(
            llm_client=llm_client,
            model_name=model_name,
            prompt_name=caption_prompt_name,
            prompts_dir=prompts_dir,
            batch_size=llm_batch_size,
            max_retries=max_output_retries,
        )
        self._native_translator = (
            YouTubeNativeCaptionTranslator(
                caption_client=caption_client,
                llm_repair_translator=self._llm_translator,
            )
            if caption_client is not None
            else None
        )

    def _resolve_provider(
        self, source_name: TranslationSource
    ) -> CaptionTranslationProvider | None:
        if source_name == "youtube_native":
            return self._native_translator
        if source_name == "llm":
            return self._llm_translator
        return None

    async def translate(
        self,
        *,
        source_caption_id: str,
        source_cues: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
    ) -> CaptionTranslationResult:
        """Try configured translation sources in order; fall back automatically on failure."""
        errors: list[str] = []
        total_quota_units = 0
        total_attempts = 0

        for source_name in self._sources:
            provider = self._resolve_provider(source_name)
            if provider is None:
                errors.append(f"{source_name}: provider not available")
                continue

            try:
                result = await provider.translate_captions(
                    source_caption_id=source_caption_id,
                    source_cues=source_cues,
                    target_language=target_language,
                    source_language=source_language,
                    protected_glossary=protected_glossary,
                    max_line_length=self._max_line_length,
                )
                total_quota_units += result.quota_units_used
                total_attempts += result.attempt_count
                return CaptionTranslationResult(
                    provider_used=result.provider_used,
                    prompt_version=result.prompt_version,
                    srt_content=result.srt_content,
                    parsed_doc=result.parsed_doc,
                    source_ambiguous=result.source_ambiguous,
                    ambiguity_notes=result.ambiguity_notes,
                    quota_units_used=total_quota_units,
                    attempt_count=max(1, total_attempts),
                )
            except CaptionTranslationError as exc:
                total_quota_units += exc.quota_units_used
                total_attempts += 1
                errors.append(f"{source_name}: {exc}")

        raise CaptionTranslationError(
            f"All configured caption translation sources failed for {target_language.code!r}: "
            + " | ".join(errors),
            quota_units_used=total_quota_units,
        )
