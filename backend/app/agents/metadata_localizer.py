"""Metadata localization agent with deterministic placeholder protection and schema validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.core.app_config import SourceLanguage, TargetLanguage
from app.prompts.loader import aload_prompt_template
from app.schemas.localization import LocalizedMetadataLLMOutput
from app.services.translation.providers import LLMOutputValidationError, LLMStructuredClient
from app.services.validation.validators import (
    ValidationIssue,
    ValidationReport,
    extract_protected_placeholders,
    restore_placeholders,
    validate_localized_metadata,
)


def compute_metadata_content_hash(title: str, description: str) -> str:
    """Compute canonical SHA-256 content hash for localized title and description."""
    normalized_title = title.strip()
    normalized_desc = description.replace("\r\n", "\n").replace("\r", "\n").strip()
    payload = f"{normalized_title}\n---\n{normalized_desc}".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MetadataLocalizationResult:
    """Result of localizing a video's title and description for one target language."""

    title: str
    description: str
    content_hash: str
    provider_used: str
    prompt_version: str
    source_ambiguous: bool
    ambiguity_notes: str | None
    validation_report: ValidationReport
    attempt_count: int
    last_error: str | None = None


class MetadataLocalizerAgent:
    """Translates video title & description using versioned prompt and placeholder protection."""

    def __init__(
        self,
        *,
        llm_client: LLMStructuredClient,
        model_name: str,
        prompt_name: str = "metadata_localizer.translate.v1",
        prompts_dir: Path | None = None,
        max_retries: int = 2,
    ) -> None:
        self._llm = llm_client
        self._model_name = model_name
        self._prompt_name = prompt_name
        self._prompts_dir = prompts_dir
        self._max_retries = max(0, max_retries)

    async def localize(
        self,
        *,
        source_title: str,
        source_description: str,
        source_language: SourceLanguage,
        target_language: TargetLanguage,
        protected_glossary: Sequence[str] = (),
    ) -> MetadataLocalizationResult:
        """Localize Turkish metadata into ``target_language`` with retries on invalid output."""
        loaded_prompt = await aload_prompt_template(self._prompt_name, self._prompts_dir)
        bundle = extract_protected_placeholders(
            source_title,
            source_description,
            glossary=protected_glossary,
        )

        base_prompt = loaded_prompt.render(
            {
                "source_language_name": source_language.name,
                "source_language_code": source_language.code,
                "target_language_name": target_language.name,
                "target_language_code": target_language.code,
                "target_native_name": target_language.native_name,
                "protected_glossary_json": json.dumps(list(protected_glossary), ensure_ascii=False),
                "masked_title": bundle.masked_title,
                "masked_description": bundle.masked_description,
                "placeholders_json": json.dumps(bundle.token_to_literal, ensure_ascii=False),
            }
        )

        attempts = 0
        last_error: str | None = None
        last_title = ""
        last_desc = ""
        last_ambiguous = False
        last_notes: str | None = None
        last_report: ValidationReport | None = None
        retry_feedback = ""

        for _ in range(self._max_retries + 1):
            attempts += 1
            prompt_to_send = (
                base_prompt
                if not retry_feedback
                else f"{base_prompt}\n\n## Previous Attempt Validation Feedback\n{retry_feedback}"
            )
            try:
                llm_out: LocalizedMetadataLLMOutput = await self._llm.generate_structured(
                    prompt=prompt_to_send,
                    schema=LocalizedMetadataLLMOutput,
                    model_name=self._model_name,
                )
            except (LLMOutputValidationError, ValidationError) as exc:
                last_error = str(exc)
                retry_feedback = (
                    f"Your previous output failed JSON schema validation: {last_error}. "
                    "Return valid JSON matching the required schema."
                )
                continue

            restored_title = restore_placeholders(llm_out.title, bundle).strip()
            restored_desc = restore_placeholders(llm_out.description, bundle).strip()
            report = validate_localized_metadata(
                restored_title,
                restored_desc,
                source_title=source_title,
                source_description=source_description,
                protected_glossary=protected_glossary,
            )

            last_title = restored_title
            last_desc = restored_desc
            last_ambiguous = llm_out.source_ambiguous
            last_notes = llm_out.ambiguity_notes
            last_report = report

            if report.passed:
                return MetadataLocalizationResult(
                    title=restored_title,
                    description=restored_desc,
                    content_hash=compute_metadata_content_hash(restored_title, restored_desc),
                    provider_used=self._llm.provider_name,
                    prompt_version=loaded_prompt.name,
                    source_ambiguous=llm_out.source_ambiguous,
                    ambiguity_notes=llm_out.ambiguity_notes,
                    validation_report=report,
                    attempt_count=attempts,
                    last_error=None,
                )

            issue_descriptions = "; ".join(f"{i.code}: {i.message}" for i in report.issues)
            last_error = f"Metadata validation failed: {issue_descriptions}"
            retry_feedback = (
                "Your previous translation failed deterministic validation "
                f"({issue_descriptions}). Please fix every issue while preserving all "
                "placeholders and numeric quantities."
            )

        if last_report is None:
            last_report = ValidationReport(
                passed=False,
                issues=(
                    ValidationIssue(
                        code="llm_output_schema_invalid",
                        message=last_error or "LLM failed to return schema-valid JSON.",
                    ),
                ),
            )

        return MetadataLocalizationResult(
            title=last_title,
            description=last_desc,
            content_hash=compute_metadata_content_hash(last_title, last_desc),
            provider_used=self._llm.provider_name,
            prompt_version=loaded_prompt.name,
            source_ambiguous=last_ambiguous,
            ambiguity_notes=last_notes,
            validation_report=last_report,
            attempt_count=attempts,
            last_error=last_error,
        )
