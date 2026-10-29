"""Translation provider abstraction and registry (`youtube_native`, `gemini`/`llm`, `fake`).

Encapsulates both YouTube's native `captions.download?tlang=...` endpoint and
LLM-backed structured JSON generation behind clean async protocols. Never
exposes credentials or tokens in logs or prompts.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.app_config import SourceLanguage, TargetLanguage
from app.prompts.loader import aload_prompt_template, assert_no_secrets_in_text
from app.schemas.localization import (
    LocalizedMetadataLLMOutput,
    QualityReviewLLMOutput,
    TranslatedCueBatchLLMOutput,
    TranslatedCueItem,
)
from app.services.captions.parser import (
    ParsedSubtitleDocument,
    SubtitleCue,
    SubtitleFormat,
    normalize_youtube_tlang_srt,
    parse_and_validate_subtitles,
    serialize_subtitles_to_srt,
)
from app.services.youtube.captions_client import (
    CaptionDownloadFormat,
    CaptionDownloadParams,
    YouTubeApiError,
    YouTubeCaptionClient,
)

TModel = TypeVar("TModel", bound=BaseModel)


class LLMOutputValidationError(RuntimeError):
    """Raised when an LLM provider fails to produce schema-valid output after retries."""


class CaptionTranslationError(RuntimeError):
    """Raised when a caption translation provider cannot produce a valid subtitle track."""

    def __init__(self, message: str, *, quota_units_used: int = 0) -> None:
        super().__init__(message)
        self.quota_units_used = quota_units_used


class LLMStructuredClient(Protocol):
    """Protocol for structured JSON generation used by localization and review agents."""

    provider_name: str

    async def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[TModel],
        model_name: str,
    ) -> TModel:
        """Execute prompt and return a validated Pydantic model instance."""


class GeminiStructuredClient:
    """Production Gemini / Vertex AI structured JSON client using ``google-genai``."""

    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str = "",
        use_vertex: bool = False,
        project: str = "",
        location: str = "europe-west1",
    ) -> None:
        self._api_key = api_key
        self._use_vertex = use_vertex
        self._project = project
        self._location = location

    @staticmethod
    def _strip_additional_properties(obj: Any) -> Any:
        """Remove ``additionalProperties`` keys that Gemini's response_schema rejects."""
        if isinstance(obj, dict):
            return {
                k: GeminiStructuredClient._strip_additional_properties(v)
                for k, v in obj.items()
                if k != "additionalProperties"
            }
        if isinstance(obj, list):
            return [GeminiStructuredClient._strip_additional_properties(item) for item in obj]
        return obj

    def _call_sync(self, prompt: str, schema: type[TModel], model_name: str) -> TModel:
        from google import genai
        from google.genai import types

        assert_no_secrets_in_text(prompt)
        if self._use_vertex:
            client = genai.Client(
                vertexai=True,
                project=self._project or None,
                location=self._location or None,
            )
        else:
            if not self._api_key:
                raise LLMOutputValidationError(
                    "GEMINI_API_KEY is not configured and GEMINI_USE_VERTEX is false."
                )
            client = genai.Client(api_key=self._api_key)

        clean_json_schema = self._strip_additional_properties(schema.model_json_schema())
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=clean_json_schema,
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            raise LLMOutputValidationError(f"Gemini API call failed: {exc}") from exc

        raw_text = (response.text or "").strip()
        if not raw_text:
            raise LLMOutputValidationError("Gemini returned an empty response body.")
        try:
            return schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise LLMOutputValidationError(
                f"Gemini output failed {schema.__name__} validation: {exc}"
            ) from exc

    async def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[TModel],
        model_name: str,
    ) -> TModel:
        return await asyncio.to_thread(self._call_sync, prompt, schema, model_name)


_LANGUAGE_GREETING_PREFIX: dict[str, str] = {
    "en": "[EN]",
    "ro": "[RO]",
    "es": "[ES]",
    "fr": "[FR]",
    "de": "[DE]",
    "ar": "🌿 [AR]",
    "it": "[IT]",
    "ru": "[RU]",
}


class FakeStructuredLLMClient:
    """Deterministic, offline structured LLM client for automated tests and local PoC."""

    provider_name = "fake"

    def __init__(
        self,
        *,
        scripted_responses: list[Any] | None = None,
    ) -> None:
        self._scripted_responses: list[Any] = list(scripted_responses or [])
        self.calls: list[dict[str, Any]] = []

    def queue_response(self, response: Any) -> None:
        self._scripted_responses.append(response)

    async def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[TModel],
        model_name: str,
    ) -> TModel:
        assert_no_secrets_in_text(prompt)
        self.calls.append(
            {
                "schema": schema.__name__,
                "model_name": model_name,
                "prompt_length": len(prompt),
            }
        )

        if self._scripted_responses:
            nxt = self._scripted_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            if isinstance(nxt, schema):
                return nxt
            if isinstance(nxt, str):
                try:
                    return schema.model_validate_json(nxt)
                except ValidationError as exc:
                    raise LLMOutputValidationError(
                        f"Scripted JSON failed {schema.__name__} validation: {exc}"
                    ) from exc
            if isinstance(nxt, dict):
                try:
                    return schema.model_validate(nxt)
                except ValidationError as exc:
                    raise LLMOutputValidationError(
                        f"Scripted dict failed {schema.__name__} validation: {exc}"
                    ) from exc

        # Default deterministic behavior by schema type
        lang_match = re.search(r"into [^(\n]+\(`(?P<code>[a-z]{2,3}(?:-[A-Za-z0-9]+)?)`", prompt)
        lang_code = lang_match.group("code") if lang_match else "en"
        prefix = _LANGUAGE_GREETING_PREFIX.get(lang_code, f"[{lang_code.upper()}]")

        if schema is LocalizedMetadataLLMOutput:
            title_match = re.search(
                r"\n<SOURCE_TITLE>\n(?P<title>.*?)\n</SOURCE_TITLE>", prompt, re.DOTALL
            )
            desc_match = re.search(
                r"\n<SOURCE_DESCRIPTION>\n(?P<desc>.*?)\n</SOURCE_DESCRIPTION>",
                prompt,
                re.DOTALL,
            )
            raw_title = (title_match.group("title") if title_match else "Orchid Care").strip()
            raw_desc = (desc_match.group("desc") if desc_match else "").strip()
            clean_title = raw_title.replace("<", "").replace(">", "")
            clean_desc = raw_desc.replace("<", "").replace(">", "")
            candidate_title = f"{prefix} {clean_title}".strip()
            if len(candidate_title) > 95:
                candidate_title = candidate_title[:95].rstrip()
            candidate_desc = f"{prefix} {clean_desc}".strip() if clean_desc else f"{prefix} Info"
            return schema.model_validate(
                {
                    "title": candidate_title,
                    "description": candidate_desc,
                    "source_ambiguous": False,
                    "ambiguity_notes": None,
                }
            )

        if schema is TranslatedCueBatchLLMOutput:
            cues_match = re.search(
                r"\n<SOURCE_CUES_JSON>\n(?P<cues>.*?)\n</SOURCE_CUES_JSON>",
                prompt,
                re.DOTALL,
            )
            cues_raw = json.loads(cues_match.group("cues")) if cues_match else []
            translated_items = [
                {
                    "index": int(item["index"]),
                    "text": f"{prefix} {str(item['text']).strip()}",
                }
                for item in cues_raw
            ]
            return schema.model_validate(
                {
                    "cues": translated_items,
                    "source_ambiguous": False,
                    "ambiguity_notes": None,
                }
            )

        if schema is QualityReviewLLMOutput:
            return schema.model_validate(
                {
                    "passed": True,
                    "source_ambiguous": False,
                    "summary": f"Deterministic fake review passed for {lang_code}.",
                    "issues": [],
                }
            )

        raise LLMOutputValidationError(f"Unsupported schema in FakeStructuredLLMClient: {schema}")


@dataclass(frozen=True, slots=True)
class CaptionTranslationResult:
    """Output of a caption translation provider after parsing and validation."""

    provider_used: str
    prompt_version: str | None
    srt_content: str
    parsed_doc: ParsedSubtitleDocument
    source_ambiguous: bool = False
    ambiguity_notes: str | None = None
    quota_units_used: int = 0
    attempt_count: int = 1


class CaptionTranslationProvider(Protocol):
    """Protocol implemented by all subtitle translation providers."""

    provider_name: str

    async def translate_captions(
        self,
        *,
        source_caption_id: str,
        source_cues: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
        max_line_length: int = 84,
    ) -> CaptionTranslationResult:
        """Translate ``source_cues`` into ``target_language`` and return validated SRT."""


class LLMCaptionTranslator:
    """Translates subtitle cues via LLM while strictly preserving source cue indices & timings."""

    def __init__(
        self,
        *,
        llm_client: LLMStructuredClient,
        model_name: str,
        prompt_name: str = "caption_translator.translate.v1",
        prompts_dir: Path | None = None,
        batch_size: int = 40,
        max_retries: int = 2,
    ) -> None:
        self._llm = llm_client
        self._model_name = model_name
        self._prompt_name = prompt_name
        self._prompts_dir = prompts_dir
        self._batch_size = max(1, batch_size)
        self._max_retries = max(0, max_retries)
        self.provider_name = llm_client.provider_name

    async def translate_cue_subset(
        self,
        *,
        cues_to_translate: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
        max_line_length: int = 84,
    ) -> tuple[dict[int, str], bool, str | None, int, str]:
        """Translate a specific subset of cues and return indexed text plus metadata."""
        loaded_prompt = await aload_prompt_template(self._prompt_name, self._prompts_dir)
        translated_by_index: dict[int, str] = {}
        any_ambiguous = False
        ambiguity_notes_list: list[str] = []
        total_attempts = 0

        for start in range(0, len(cues_to_translate), self._batch_size):
            batch = cues_to_translate[start : start + self._batch_size]
            expected_indices = [c.index for c in batch]
            cues_payload = [{"index": c.index, "text": c.text} for c in batch]
            rendered_prompt = loaded_prompt.render(
                {
                    "source_language_name": source_language.name,
                    "source_language_code": source_language.code,
                    "target_language_name": target_language.name,
                    "target_language_code": target_language.code,
                    "target_native_name": target_language.native_name,
                    "target_is_rtl": str(target_language.rtl).lower(),
                    "max_line_length": str(max_line_length),
                    "protected_glossary_json": json.dumps(
                        list(protected_glossary), ensure_ascii=False
                    ),
                    "source_cues_json": json.dumps(cues_payload, ensure_ascii=False, indent=2),
                }
            )

            last_err: Exception | None = None
            batch_output: TranslatedCueBatchLLMOutput | None = None
            for _attempt in range(1, self._max_retries + 2):
                total_attempts += 1
                try:
                    candidate = await self._llm.generate_structured(
                        prompt=rendered_prompt,
                        schema=TranslatedCueBatchLLMOutput,
                        model_name=self._model_name,
                    )
                    returned_indices = [item.index for item in candidate.cues]
                    if returned_indices != expected_indices:
                        raise LLMOutputValidationError(
                            f"LLM cue indices {returned_indices} do not match source indices "
                            f"{expected_indices}."
                        )
                    if any(not item.text.strip() for item in candidate.cues):
                        raise LLMOutputValidationError("LLM returned an empty cue text.")
                    batch_output = candidate
                    break
                except (LLMOutputValidationError, ValidationError) as exc:
                    last_err = exc

            if batch_output is None:
                raise CaptionTranslationError(
                    f"LLM subtitle translation failed after {self._max_retries + 1} attempts: "
                    f"{last_err}"
                )

            for item in batch_output.cues:
                translated_by_index[item.index] = self._wrap_cue_lines(
                    item.text.strip(), max_line_length
                )
            if batch_output.source_ambiguous:
                any_ambiguous = True
                if batch_output.ambiguity_notes:
                    ambiguity_notes_list.append(batch_output.ambiguity_notes)

        return (
            translated_by_index,
            any_ambiguous,
            "; ".join(ambiguity_notes_list) if ambiguity_notes_list else None,
            max(1, total_attempts),
            loaded_prompt.name,
        )

    @staticmethod
    def _wrap_cue_lines(text: str, max_line_length: int) -> str:
        """Soft-wrap long single subtitle lines at word boundaries so lines stay readable."""
        wrapped_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            while len(line) > max_line_length and " " in line[:max_line_length]:
                split_at = line.rfind(" ", 0, max_line_length + 1)
                if split_at <= 0:
                    break
                wrapped_lines.append(line[:split_at].strip())
                line = line[split_at + 1 :].strip()
            if line:
                wrapped_lines.append(line)
        return "\n".join(wrapped_lines) if wrapped_lines else text.strip()

    async def translate_captions(
        self,
        *,
        source_caption_id: str,
        source_cues: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
        max_line_length: int = 84,
    ) -> CaptionTranslationResult:
        del source_caption_id
        if not source_cues:
            raise CaptionTranslationError("Source caption track has no cues to translate.")

        (
            translated_map,
            any_ambiguous,
            notes,
            attempts,
            prompt_ver,
        ) = await self.translate_cue_subset(
            cues_to_translate=source_cues,
            target_language=target_language,
            source_language=source_language,
            protected_glossary=protected_glossary,
            max_line_length=max_line_length,
        )

        rebuilt_cues = tuple(
            SubtitleCue(
                index=src.index,
                start_ms=src.start_ms,
                end_ms=src.end_ms,
                text=translated_map[src.index],
            )
            for src in source_cues
        )
        srt_text = serialize_subtitles_to_srt(rebuilt_cues)
        parsed = parse_and_validate_subtitles(
            srt_text,
            SubtitleFormat.SRT,
            reference_cues=tuple(source_cues),
            max_line_length=max_line_length,
        )
        return CaptionTranslationResult(
            provider_used=self.provider_name,
            prompt_version=prompt_ver,
            srt_content=srt_text,
            parsed_doc=parsed,
            source_ambiguous=any_ambiguous,
            ambiguity_notes=notes,
            quota_units_used=0,
            attempt_count=attempts,
        )


class YouTubeNativeCaptionTranslator:
    """Translates subtitles via ``captions.download?tfmt=srt&tlang=...``.

    Includes automatic normalization of YouTube's internal blank-line quirk and
    targeted LLM backfill if YouTube's machine translator leaves individual cues
    empty (as observed live on ``tlang=en`` during Stage 3b).
    """

    provider_name = "youtube_native"

    def __init__(
        self,
        *,
        caption_client: YouTubeCaptionClient,
        llm_repair_translator: LLMCaptionTranslator | None = None,
    ) -> None:
        self._caption_client = caption_client
        self._llm_repair = llm_repair_translator

    async def translate_captions(
        self,
        *,
        source_caption_id: str,
        source_cues: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
        max_line_length: int = 84,
    ) -> CaptionTranslationResult:
        if not source_caption_id:
            raise CaptionTranslationError("source_caption_id is required for youtube_native.")

        try:
            raw_bytes = await self._caption_client.download_caption(
                CaptionDownloadParams(
                    caption_id=source_caption_id,
                    tfmt=CaptionDownloadFormat.SRT,
                    tlang=target_language.caption_tlang,
                )
            )
        except YouTubeApiError as exc:
            raise CaptionTranslationError(
                f"YouTube native tlang={target_language.caption_tlang!r} failed: "
                f"{exc.reason} (HTTP {exc.http_status})",
                quota_units_used=200,
            ) from exc

        normalized_srt, empty_indices = normalize_youtube_tlang_srt(raw_bytes)
        prompt_ver: str | None = None
        provider_label = "youtube_native"

        # If YouTube's tlang merged adjacent cues and left a few cues empty, backfill
        # just those empty cues using the LLM translator while preserving all other native cues.
        if (
            empty_indices
            and self._llm_repair is not None
            and len(empty_indices) <= max(3, len(source_cues) // 4)
        ):
            empty_set = set(empty_indices)
            missing_source_cues = [c for c in source_cues if c.index in empty_set]
            if missing_source_cues:
                repaired_map, _, _, _, prompt_ver = await self._llm_repair.translate_cue_subset(
                    cues_to_translate=missing_source_cues,
                    target_language=target_language,
                    source_language=source_language,
                    protected_glossary=protected_glossary,
                    max_line_length=max_line_length,
                )
                # Inject repaired cue texts into the normalized SRT blocks
                blocks = normalized_srt.strip().split("\n\n")
                patched_blocks: list[str] = []
                for block in blocks:
                    lines = block.split("\n")
                    if len(lines) >= 2 and lines[0].strip().isdigit():
                        cue_idx = int(lines[0].strip())
                        if cue_idx in repaired_map:
                            patched_blocks.append(
                                f"{lines[0].strip()}\n{lines[1].strip()}\n{repaired_map[cue_idx]}"
                            )
                            continue
                    patched_blocks.append(block)
                normalized_srt = "\n\n".join(patched_blocks) + "\n"
                provider_label = f"youtube_native+{self._llm_repair.provider_name}_repair"

        # Soft-wrap any lines in YouTube's native output that exceed max_line_length
        prelim_doc = parse_and_validate_subtitles(
            normalized_srt,
            SubtitleFormat.SRT,
            reference_cues=tuple(source_cues),
        )
        if prelim_doc.is_valid and prelim_doc.cues:
            wrapped_cues = tuple(
                SubtitleCue(
                    index=c.index,
                    start_ms=c.start_ms,
                    end_ms=c.end_ms,
                    text=LLMCaptionTranslator._wrap_cue_lines(c.text, max_line_length),
                )
                for c in prelim_doc.cues
            )
            normalized_srt = serialize_subtitles_to_srt(wrapped_cues)

        final_doc = parse_and_validate_subtitles(
            normalized_srt,
            SubtitleFormat.SRT,
            reference_cues=tuple(source_cues),
            max_line_length=max_line_length,
        )
        if not final_doc.is_valid:
            issue_codes = [i.code for i in final_doc.report.issues]
            raise CaptionTranslationError(
                f"YouTube native tlang={target_language.caption_tlang!r} failed validation: "
                f"{issue_codes}",
                quota_units_used=200,
            )

        return CaptionTranslationResult(
            provider_used=provider_label,
            prompt_version=prompt_ver,
            srt_content=normalized_srt,
            parsed_doc=final_doc,
            source_ambiguous=False,
            ambiguity_notes=None,
            quota_units_used=200,
            attempt_count=1,
        )


def create_llm_structured_client(
    provider_name: str,
    *,
    api_key: str = "",
    use_vertex: bool = False,
    project: str = "",
    location: str = "europe-west1",
) -> LLMStructuredClient:
    """Resolve an ``LLMStructuredClient`` from configuration name (`gemini` or `fake`)."""
    normalized = provider_name.strip().lower()
    if normalized == "fake":
        return FakeStructuredLLMClient()
    if normalized in ("gemini", "llm"):
        return GeminiStructuredClient(
            api_key=api_key,
            use_vertex=use_vertex,
            project=project,
            location=location,
        )
    raise ValueError(f"Unknown LLM provider {provider_name!r}; expected 'gemini' or 'fake'.")


__all__ = [
    "CaptionTranslationError",
    "CaptionTranslationProvider",
    "CaptionTranslationResult",
    "FakeStructuredLLMClient",
    "GeminiStructuredClient",
    "LLMCaptionTranslator",
    "LLMOutputValidationError",
    "LLMStructuredClient",
    "TranslatedCueItem",
    "YouTubeNativeCaptionTranslator",
    "create_llm_structured_client",
]
