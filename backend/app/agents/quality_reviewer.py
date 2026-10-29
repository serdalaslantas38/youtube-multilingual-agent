"""Quality-review agent combining deterministic cross-checks with structured LLM review."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.app_config import SourceLanguage, TargetLanguage
from app.models.enums import AssetKind
from app.prompts.loader import aload_prompt_template
from app.schemas.localization import (
    QualityReviewIssue,
    QualityReviewLLMOutput,
    ReviewIssueCategory,
    ReviewIssueSeverity,
)
from app.services.captions.parser import SubtitleCue
from app.services.translation.providers import LLMOutputValidationError, LLMStructuredClient
from app.services.validation.validators import extract_numeric_tokens


@dataclass(frozen=True, slots=True)
class QualityReviewResult:
    """Combined deterministic + LLM quality review report for a localized asset."""

    passed: bool
    source_ambiguous: bool
    summary: str
    issues: tuple[QualityReviewIssue, ...]
    prompt_version: str
    attempt_count: int
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "source_ambiguous": self.source_ambiguous,
            "summary": self.summary,
            "prompt_version": self.prompt_version,
            "issues": [
                {
                    "category": i.category.value,
                    "severity": i.severity.value,
                    "message": i.message,
                    "location": i.location,
                }
                for i in self.issues
            ],
        }


class QualityReviewerAgent:
    """Evaluates localized metadata and caption tracks against the Turkish source."""

    def __init__(
        self,
        *,
        llm_client: LLMStructuredClient,
        model_name: str,
        prompt_name: str = "quality_reviewer.review.v1",
        prompts_dir: Path | None = None,
        max_retries: int = 2,
    ) -> None:
        self._llm = llm_client
        self._model_name = model_name
        self._prompt_name = prompt_name
        self._prompts_dir = prompts_dir
        self._max_retries = max(0, max_retries)

    @staticmethod
    def _deterministic_metadata_checks(
        *,
        source_title: str,
        source_description: str,
        translated_title: str,
        translated_description: str,
    ) -> list[QualityReviewIssue]:
        issues: list[QualityReviewIssue] = []
        if (
            translated_title.strip().lower() == source_title.strip().lower()
            and len(source_title.strip()) > 5
        ):
            issues.append(
                QualityReviewIssue(
                    category=ReviewIssueCategory.COMPLETENESS,
                    severity=ReviewIssueSeverity.BLOCKING,
                    message=(
                        "Localized title is identical to the Turkish source title (untranslated)."
                    ),
                    location="title",
                )
            )

        if source_description.strip() and not translated_description.strip():
            issues.append(
                QualityReviewIssue(
                    category=ReviewIssueCategory.COMPLETENESS,
                    severity=ReviewIssueSeverity.BLOCKING,
                    message="Source description is non-empty, but localized description is empty.",
                    location="description",
                )
            )

        src_nums = extract_numeric_tokens(source_title)
        tgt_nums = set(extract_numeric_tokens(translated_title))
        for num in src_nums:
            if num not in tgt_nums:
                issues.append(
                    QualityReviewIssue(
                        category=ReviewIssueCategory.ALTERED_MEANING,
                        severity=ReviewIssueSeverity.BLOCKING,
                        message=(
                            f"Numeric quantity {num!r} in source title is missing "
                            "in localized title."
                        ),
                        location="title",
                    )
                )

        return issues

    @staticmethod
    def _deterministic_caption_checks(
        *,
        source_cues: Sequence[SubtitleCue],
        translated_cues: Sequence[SubtitleCue],
    ) -> list[QualityReviewIssue]:
        issues: list[QualityReviewIssue] = []
        if len(source_cues) != len(translated_cues):
            issues.append(
                QualityReviewIssue(
                    category=ReviewIssueCategory.COMPLETENESS,
                    severity=ReviewIssueSeverity.BLOCKING,
                    message=(
                        f"Cue count mismatch: source has {len(source_cues)} cues, "
                        f"translation has {len(translated_cues)} cues."
                    ),
                    location="captions",
                )
            )
            return issues

        identical_count = sum(
            1
            for s, t in zip(source_cues, translated_cues, strict=True)
            if len(s.text.strip()) > 8 and s.text.strip().lower() == t.text.strip().lower()
        )
        if source_cues and identical_count > max(2, len(source_cues) // 2):
            issues.append(
                QualityReviewIssue(
                    category=ReviewIssueCategory.COMPLETENESS,
                    severity=ReviewIssueSeverity.BLOCKING,
                    message=(
                        f"{identical_count} of {len(source_cues)} subtitle cues are identical "
                        "to the Turkish source (untranslated)."
                    ),
                    location="captions",
                )
            )
        return issues

    async def _run_llm_review(
        self,
        *,
        asset_kind: AssetKind,
        source_payload: dict[str, Any],
        translated_payload: dict[str, Any],
        source_language: SourceLanguage,
        target_language: TargetLanguage,
        protected_glossary: Sequence[str],
        pre_issues: list[QualityReviewIssue],
    ) -> QualityReviewResult:
        loaded_prompt = await aload_prompt_template(self._prompt_name, self._prompts_dir)
        rendered_prompt = loaded_prompt.render(
            {
                "asset_kind": asset_kind.value,
                "source_language_name": source_language.name,
                "source_language_code": source_language.code,
                "target_language_name": target_language.name,
                "target_language_code": target_language.code,
                "target_native_name": target_language.native_name,
                "protected_glossary_json": json.dumps(list(protected_glossary), ensure_ascii=False),
                "source_payload_json": json.dumps(source_payload, ensure_ascii=False, indent=2),
                "translated_payload_json": json.dumps(
                    translated_payload, ensure_ascii=False, indent=2
                ),
            }
        )

        attempts = 0
        last_error: str | None = None
        for _ in range(self._max_retries + 1):
            attempts += 1
            try:
                llm_out = await self._llm.generate_structured(
                    prompt=rendered_prompt,
                    schema=QualityReviewLLMOutput,
                    model_name=self._model_name,
                )
                combined_issues = tuple([*pre_issues, *llm_out.issues])
                has_blocking = any(
                    i.severity is ReviewIssueSeverity.BLOCKING for i in combined_issues
                )
                passed = llm_out.passed and not has_blocking and not llm_out.source_ambiguous
                return QualityReviewResult(
                    passed=passed,
                    source_ambiguous=llm_out.source_ambiguous,
                    summary=llm_out.summary,
                    issues=combined_issues,
                    prompt_version=loaded_prompt.name,
                    attempt_count=attempts,
                    last_error=None,
                )
            except (LLMOutputValidationError, ValidationError) as exc:
                last_error = str(exc)

        fallback_issue = QualityReviewIssue(
            category=ReviewIssueCategory.CONSISTENCY,
            severity=ReviewIssueSeverity.BLOCKING,
            message=(
                f"Quality review LLM failed schema validation after {attempts} attempts: "
                f"{last_error}"
            ),
            location="reviewer",
        )
        return QualityReviewResult(
            passed=False,
            source_ambiguous=False,
            summary="Quality review failed to produce a valid structured report.",
            issues=tuple([*pre_issues, fallback_issue]),
            prompt_version=loaded_prompt.name,
            attempt_count=attempts,
            last_error=last_error,
        )

    async def review_metadata(
        self,
        *,
        source_title: str,
        source_description: str,
        translated_title: str,
        translated_description: str,
        source_language: SourceLanguage,
        target_language: TargetLanguage,
        protected_glossary: Sequence[str] = (),
    ) -> QualityReviewResult:
        """Review localized title and description against the Turkish source."""
        pre_issues = self._deterministic_metadata_checks(
            source_title=source_title,
            source_description=source_description,
            translated_title=translated_title,
            translated_description=translated_description,
        )
        return await self._run_llm_review(
            asset_kind=AssetKind.METADATA,
            source_payload={"title": source_title, "description": source_description},
            translated_payload={
                "title": translated_title,
                "description": translated_description,
            },
            source_language=source_language,
            target_language=target_language,
            protected_glossary=protected_glossary,
            pre_issues=pre_issues,
        )

    async def review_captions(
        self,
        *,
        source_cues: Sequence[SubtitleCue],
        translated_cues: Sequence[SubtitleCue],
        source_language: SourceLanguage,
        target_language: TargetLanguage,
        protected_glossary: Sequence[str] = (),
        sample_size: int = 24,
    ) -> QualityReviewResult:
        """Review translated subtitle cues against the Turkish source cues."""
        pre_issues = self._deterministic_caption_checks(
            source_cues=source_cues,
            translated_cues=translated_cues,
        )
        # Sample 3 contiguous windows (beginning, middle, end) so multi-cue spoken
        # sentences are evaluated in context rather than severed mid-sentence.
        total = min(len(source_cues), len(translated_cues))
        if total <= sample_size:
            indices = list(range(total))
        else:
            win = max(4, sample_size // 3)
            mid_start = max(win, (total // 2) - (win // 2))
            end_start = max(mid_start + win, total - win)
            indices = sorted(
                {
                    *range(0, min(win, total)),
                    *range(mid_start, min(mid_start + win, total)),
                    *range(end_start, total),
                }
            )

        src_sample = [{"index": source_cues[i].index, "text": source_cues[i].text} for i in indices]
        tgt_sample = [
            {"index": translated_cues[i].index, "text": translated_cues[i].text} for i in indices
        ]
        return await self._run_llm_review(
            asset_kind=AssetKind.CAPTION,
            source_payload={"total_cues": len(source_cues), "sampled_cues": src_sample},
            translated_payload={
                "total_cues": len(translated_cues),
                "sampled_cues": tgt_sample,
            },
            source_language=source_language,
            target_language=target_language,
            protected_glossary=protected_glossary,
            pre_issues=pre_issues,
        )
