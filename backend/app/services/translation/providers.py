"""Translation provider abstraction and registry (`youtube_native`, `gemini`/`llm`, `fake`).

Encapsulates both YouTube's native `captions.download?tlang=...` endpoint and
LLM-backed structured JSON generation behind clean async protocols. Never
exposes credentials or tokens in logs or prompts.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
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
from app.services.validation.validators import (
    MAX_DESCRIPTION_BYTES,
    MAX_TITLE_CHARS,
    PlaceholderBundle,
    enforce_title_max_length,
    extract_protected_placeholders,
    restore_placeholders,
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
        google_repair_translator: GoogleCaptionTranslator | None = None,
    ) -> None:
        self._caption_client = caption_client
        self._llm_repair = llm_repair_translator
        self._google_repair = google_repair_translator

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
        # just those empty cues (Google Translate first, then LLM) keeping all other native cues.
        if (
            empty_indices
            and (self._google_repair is not None or self._llm_repair is not None)
            and len(empty_indices) <= max(3, len(source_cues) // 4)
        ):
            empty_set = set(empty_indices)
            missing_source_cues = [c for c in source_cues if c.index in empty_set]
            if missing_source_cues:
                repaired_map: dict[int, str] | None = None
                repair_provider_name = ""
                if self._google_repair is not None:
                    try:
                        (
                            repaired_map,
                            _,
                            _,
                            _,
                            prompt_ver,
                        ) = await self._google_repair.translate_cue_subset(
                            cues_to_translate=missing_source_cues,
                            target_language=target_language,
                            source_language=source_language,
                            protected_glossary=protected_glossary,
                            max_line_length=max_line_length,
                        )
                        repair_provider_name = self._google_repair.provider_name
                    except CaptionTranslationError:
                        repaired_map = None
                if repaired_map is None and self._llm_repair is not None:
                    try:
                        (
                            repaired_map,
                            _,
                            _,
                            _,
                            prompt_ver,
                        ) = await self._llm_repair.translate_cue_subset(
                            cues_to_translate=missing_source_cues,
                            target_language=target_language,
                            source_language=source_language,
                            protected_glossary=protected_glossary,
                            max_line_length=max_line_length,
                        )
                        repair_provider_name = self._llm_repair.provider_name
                    except CaptionTranslationError:
                        # LLM credits/quota unavailable: leave cues empty so validation fails
                        # below (reporting the 200 units spent) and the service falls back.
                        repaired_map = None
                if repaired_map is not None:
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
                    provider_label = f"youtube_native+{repair_provider_name}_repair"

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


class MetadataTranslationError(RuntimeError):
    """Raised when a metadata translation provider fails to translate title/description."""


_PLACEHOLDER_TOKEN_RE = re.compile(r"(__[A-Z]+_\d+__)")


class GoogleMetadataTranslator:
    """Translates video title & description via Google Translate while preserving placeholders.

    Never sends URLs, @handles, #hashtags, or protected glossary literals over the wire:
    only plain-text segments between placeholders are translated, and the localized title
    is deterministically constrained to ``<= max_title_length`` (100 characters).
    """

    provider_name = "google_translate"
    prompt_version = "google_translate.v1"

    _BATCHEXECUTE_URLS: tuple[str, ...] = (
        "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute",
        "https://translate.google.co.uk/_/TranslateWebserverUi/data/batchexecute",
        "https://translate.google.de/_/TranslateWebserverUi/data/batchexecute",
        "https://translate.google.fr/_/TranslateWebserverUi/data/batchexecute",
        "https://translate.google.ca/_/TranslateWebserverUi/data/batchexecute",
        "https://translate.google.com.tr/_/TranslateWebserverUi/data/batchexecute",
    )
    _SINGLE_URLS: tuple[str, ...] = (
        "https://translate.googleapis.com/translate_a/single",
        "https://translate.google.com/translate_a/single",
        "https://clients5.google.com/translate_a/single",
    )
    _CLIENT_PROFILES: tuple[str, ...] = ("at", "it", "gtx")
    _USER_AGENT: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        max_title_length: int = MAX_TITLE_CHARS,
        translate_fn: Callable[[str, str, str], Awaitable[str]] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._max_title_length = min(MAX_TITLE_CHARS, max(20, max_title_length))
        self._translate_fn = translate_fn
        self._timeout = timeout_seconds
        self._preferred_client_idx = 0
        self._rpc_host_cursor = 0

    @staticmethod
    def resolve_google_lang_code(target_language: TargetLanguage) -> str:
        code = target_language.code.strip()
        if code == "zh":
            return "zh-CN"
        if code == "pt-PT":
            return "pt-PT"
        return target_language.caption_tlang or code

    @staticmethod
    def _parse_batchexecute_response(raw_text: str) -> dict[int, str]:
        """Parse Google Translate ``batchexecute`` (``MkEWBc``) multi-RPC response."""
        results: dict[int, str] = {}
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("[["):
                continue
            try:
                outer = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(outer, list):
                continue
            for item in outer:
                if (
                    not isinstance(item, list)
                    or len(item) < 7
                    or item[0] != "wrb.fr"
                    or item[1] != "MkEWBc"
                    or not isinstance(item[2], str)
                ):
                    continue
                call_id_str = str(item[6])
                if not call_id_str.isdigit():
                    continue
                try:
                    inner = json.loads(item[2])
                    sents = inner[1][0][0][5]
                except (ValueError, IndexError, TypeError):
                    continue
                if not isinstance(sents, list):
                    continue
                parts: list[str] = []
                for s in sents:
                    if not isinstance(s, list) or not s or s[0] is None:
                        continue
                    if len(s) > 2 and s[2] is True and parts and not parts[-1].endswith("\n"):
                        parts.append(" ")
                    parts.append(str(s[0]))
                results[int(call_id_str)] = "".join(parts).strip()
        return results

    async def _translate_batch_rpc(
        self,
        segments: list[str],
        *,
        source_code: str,
        target_code: str,
    ) -> list[str] | None:
        """Translate multiple text segments in a single ``batchexecute`` (``MkEWBc``) POST.

        Rotates across 6 regional Google Translate domains so high-volume runs (47 metadata
        languages + 47 subtitle languages) distribute load across independent pools.
        """
        if not segments:
            return []

        calls = [
            [
                "MkEWBc",
                json.dumps([[seg, source_code, target_code, True], [None]], ensure_ascii=False),
                None,
                str(i),
            ]
            for i, seg in enumerate(segments)
        ]
        f_req = json.dumps([calls], ensure_ascii=False)
        num_hosts = len(self._BATCHEXECUTE_URLS)

        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "User-Agent": self._USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
        ) as client:
            for offset in range(num_hosts):
                host_idx = (self._rpc_host_cursor + offset) % num_hosts
                url = self._BATCHEXECUTE_URLS[host_idx]
                try:
                    resp = await client.post(
                        url,
                        params={"rpcids": "MkEWBc"},
                        data={"f.req": f_req},
                    )
                except httpx.HTTPError:
                    continue
                if resp.status_code != 200:
                    continue
                parsed = self._parse_batchexecute_response(resp.text)
                if len(parsed) == len(segments) and all(
                    i in parsed and parsed[i] for i in range(len(segments))
                ):
                    self._rpc_host_cursor = (host_idx + 1) % num_hosts
                    return [parsed[i] for i in range(len(segments))]
        return None

    async def _translate_raw(self, text: str, source_code: str, target_code: str) -> str:
        if not text.strip():
            return text
        if self._translate_fn is not None:
            return await self._translate_fn(text, source_code, target_code)

        # 1. Try multi-domain batchexecute RPC first (highest quota pool)
        rpc_res = await self._translate_batch_rpc(
            [text],
            source_code=source_code,
            target_code=target_code,
        )
        if rpc_res and rpc_res[0]:
            return rpc_res[0]

        # 2. Fallback to multi-domain /translate_a/single across client profiles
        backoffs = (1.5, 4.0)
        payload: Any = None
        last_exc: Exception | None = None
        endpoints = [(url, cid) for cid in self._CLIENT_PROFILES for url in self._SINGLE_URLS]
        num_endpoints = len(endpoints)

        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": self._USER_AGENT},
        ) as client:
            for attempt in range(len(backoffs) + 1):
                for offset in range(num_endpoints):
                    idx = (self._preferred_client_idx + offset) % num_endpoints
                    url, client_id = endpoints[idx]
                    params = {
                        "client": client_id,
                        "sl": source_code,
                        "tl": target_code,
                        "dt": "t",
                    }
                    try:
                        resp = await client.post(url, params=params, data={"q": text})
                    except httpx.HTTPError as exc:
                        last_exc = exc
                        continue

                    if resp.status_code in (302, 403, 429, 503):
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        continue

                    try:
                        resp.raise_for_status()
                        payload = resp.json()
                        self._preferred_client_idx = idx
                        last_exc = None
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        last_exc = exc

                if last_exc is None:
                    break
                if attempt < len(backoffs):
                    await asyncio.sleep(backoffs[attempt])

        if last_exc is not None:
            status = (
                last_exc.response.status_code
                if isinstance(last_exc, httpx.HTTPStatusError)
                else "network error"
            )
            raise MetadataTranslationError(
                f"Google Translate request failed for tl={target_code!r} (HTTP {status})."
            ) from last_exc

        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            raise MetadataTranslationError(
                f"Google Translate returned unexpected payload for tl={target_code!r}."
            )
        parts: list[str] = []
        for item in payload[0]:
            if isinstance(item, list) and item and item[0] is not None:
                parts.append(str(item[0]))
        return "".join(parts)

    async def _translate_core_batch(
        self,
        cores: list[str],
        *,
        source_code: str,
        target_code: str,
    ) -> list[str]:
        """Translate a list of text segments with minimal HTTP calls."""
        if not cores:
            return []
        if len(cores) == 1:
            single = (await self._translate_raw(cores[0], source_code, target_code)).strip()
            return [single or cores[0]]

        # When running against live Google Translate (no mock translate_fn), pack all segments
        # into a single multi-RPC batchexecute POST (chunks of 50) so each segment has its own
        # indexed call_id and 1 language = 1 HTTP request with zero newline-merging issues.
        if self._translate_fn is None:
            rpc_results: list[str] = []
            rpc_ok = True
            for start in range(0, len(cores), 50):
                chunk = cores[start : start + 50]
                chunk_out = await self._translate_batch_rpc(
                    chunk,
                    source_code=source_code,
                    target_code=target_code,
                )
                if chunk_out is None or len(chunk_out) != len(chunk):
                    rpc_ok = False
                    break
                rpc_results.extend(chunk_out)
            if rpc_ok and len(rpc_results) == len(cores):
                return rpc_results

        # 1. Try single-newline batching first
        joined_nl = "\n".join(cores)
        out_nl = await self._translate_raw(joined_nl, source_code, target_code)
        split_nl = [seg.strip() for seg in out_nl.split("\n")]
        if len(split_nl) == len(cores) and all(split_nl):
            return split_nl

        # 2. Try double-newline paragraph batching (prevents Google from merging sentences)
        joined_para = "\n\n".join(cores)
        out_para = await self._translate_raw(joined_para, source_code, target_code)
        split_para = [seg.strip() for seg in out_para.split("\n\n") if seg.strip()]
        if len(split_para) == len(cores):
            return split_para

        # 3. Fall back to small chunks of 8 before ever doing single-line requests
        if len(cores) > 8:
            results: list[str] = []
            for start in range(0, len(cores), 8):
                chunk = cores[start : start + 8]
                results.extend(
                    await self._translate_core_batch(
                        chunk,
                        source_code=source_code,
                        target_code=target_code,
                    )
                )
            return results

        singles: list[str] = []
        for core in cores:
            s = (await self._translate_raw(core, source_code, target_code)).strip()
            singles.append(s or core)
        return singles

    async def _translate_masked_lines(
        self,
        masked_lines: list[str],
        bundle: PlaceholderBundle,
        *,
        source_code: str,
        target_code: str,
    ) -> list[str]:
        """Translate plain-text segments across lines while restoring placeholders in Python."""
        # Each line is decomposed into a list of parts:
        # either ("literal", str) or ("translatable", lead_ws, core_Index, trail_ws)
        line_structures: list[list[tuple[str, ...]]] = []
        cores_to_translate: list[str] = []

        for line in masked_lines:
            pieces = _PLACEHOLDER_TOKEN_RE.split(line)
            struct: list[tuple[str, ...]] = []
            for piece in pieces:
                if not piece:
                    continue
                if _PLACEHOLDER_TOKEN_RE.fullmatch(piece):
                    literal = bundle.token_to_literal.get(piece, piece)
                    struct.append(("literal", literal))
                elif any(ch.isalpha() for ch in piece):
                    stripped = piece.strip()
                    lead_len = len(piece) - len(piece.lstrip())
                    trail_len = len(piece) - len(piece.rstrip())
                    lead_ws = piece[:lead_len] if lead_len > 0 else ""
                    trail_ws = piece[len(piece) - trail_len :] if trail_len > 0 else ""
                    idx = len(cores_to_translate)
                    cores_to_translate.append(stripped)
                    struct.append(("translatable", lead_ws, str(idx), trail_ws))
                else:
                    struct.append(("literal", piece))
            line_structures.append(struct)

        translated_cores = await self._translate_core_batch(
            cores_to_translate,
            source_code=source_code,
            target_code=target_code,
        )

        rebuilt_lines: list[str] = []
        for struct in line_structures:
            line_parts: list[str] = []
            for item in struct:
                if item[0] == "literal":
                    line_parts.append(item[1])
                else:
                    _, lead_ws, idx_str, trail_ws = item
                    core_trans = translated_cores[int(idx_str)]
                    line_parts.append(f"{lead_ws}{core_trans}{trail_ws}")
            rebuilt_lines.append("".join(line_parts))
        return rebuilt_lines

    @staticmethod
    def _enforce_description_byte_limit(
        description: str,
        *,
        bundle: PlaceholderBundle,
        max_bytes: int = MAX_DESCRIPTION_BYTES,
    ) -> str:
        cleaned = description.replace("<", "").replace(">", "").strip()
        if len(cleaned.encode("utf-8")) <= max_bytes:
            return cleaned

        # Preserve lines containing protected tokens (URLs, handles, hashtags, glossary)
        required_literals = [bundle.token_to_literal[t] for t in bundle.description_tokens]
        lines = cleaned.split("\n")
        protected_lines: list[str] = []
        narrative_lines: list[str] = []
        for line in lines:
            if any(lit in line for lit in required_literals):
                protected_lines.append(line)
            else:
                narrative_lines.append(line)

        protected_block = "\n".join(protected_lines)
        protected_bytes = len(protected_block.encode("utf-8"))
        budget = max(0, max_bytes - protected_bytes - 2)
        kept_narrative: list[str] = []
        used_bytes = 0
        for line in narrative_lines:
            line_bytes = len((line + "\n").encode("utf-8"))
            if used_bytes + line_bytes <= budget:
                kept_narrative.append(line)
                used_bytes += line_bytes
            else:
                break
        combined = "\n".join([*kept_narrative, *protected_lines]).strip()
        while len(combined.encode("utf-8")) > max_bytes and len(combined) > 1:
            combined = combined[:-1]
        return combined.strip()

    async def translate_metadata(
        self,
        *,
        source_title: str,
        source_description: str,
        source_language: SourceLanguage,
        target_language: TargetLanguage,
        protected_glossary: Sequence[str] = (),
    ) -> tuple[str, str]:
        """Return ``(localized_title, localized_description)`` with <=100-char title."""
        bundle = extract_protected_placeholders(
            source_title,
            source_description,
            glossary=protected_glossary,
        )
        source_code = source_language.code.strip()
        target_code = self.resolve_google_lang_code(target_language)

        title_lines = [" ".join(bundle.masked_title.splitlines())]
        desc_lines = bundle.masked_description.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        all_lines = [*title_lines, *desc_lines]

        translated_all = await self._translate_masked_lines(
            all_lines,
            bundle,
            source_code=source_code,
            target_code=target_code,
        )
        raw_title = restore_placeholders(translated_all[0], bundle)
        raw_desc = restore_placeholders("\n".join(translated_all[1:]), bundle)

        final_title = enforce_title_max_length(
            raw_title,
            max_chars=self._max_title_length,
            source_title=source_title,
            protected_glossary=protected_glossary,
        )
        final_desc = self._enforce_description_byte_limit(raw_desc, bundle=bundle)
        return final_title, final_desc


class GoogleCaptionTranslator:
    """Translates subtitle cues via Google Translate while preserving cue indices and timings."""

    provider_name = "google_translate"
    prompt_version = "google_translate.captions.v1"

    def __init__(
        self,
        *,
        batch_size: int = 40,
        translate_fn: Callable[[str, str, str], Awaitable[str]] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._batch_size = max(1, batch_size)
        self._meta_helper = GoogleMetadataTranslator(
            translate_fn=translate_fn,
            timeout_seconds=timeout_seconds,
        )

    async def translate_cue_subset(
        self,
        *,
        cues_to_translate: Sequence[SubtitleCue],
        target_language: TargetLanguage,
        source_language: SourceLanguage,
        protected_glossary: Sequence[str] = (),
        max_line_length: int = 84,
    ) -> tuple[dict[int, str], bool, str | None, int, str]:
        """Translate a subset of subtitle cues while preserving cue indices and glossary terms."""
        if not cues_to_translate:
            return {}, False, None, 0, self.prompt_version

        source_code = source_language.code.strip()
        target_code = GoogleMetadataTranslator.resolve_google_lang_code(target_language)
        translated_by_index: dict[int, str] = {}
        attempts = 0

        for start in range(0, len(cues_to_translate), self._batch_size):
            batch = cues_to_translate[start : start + self._batch_size]
            attempts += 1
            single_line_cues = [" ".join(c.text.splitlines()).strip() for c in batch]
            joined_cues = "\n".join(single_line_cues)
            bundle = extract_protected_placeholders(
                "",
                joined_cues,
                glossary=protected_glossary,
            )
            masked_lines = bundle.masked_description.split("\n")
            try:
                translated_lines = await self._meta_helper._translate_masked_lines(
                    masked_lines,
                    bundle,
                    source_code=source_code,
                    target_code=target_code,
                )
            except MetadataTranslationError as exc:
                raise CaptionTranslationError(str(exc), quota_units_used=0) from exc

            for cue, raw_line in zip(batch, translated_lines, strict=True):
                restored = restore_placeholders(raw_line, bundle).strip()
                cleaned = restored.replace("<", "").replace(">", "").strip()
                if not cleaned:
                    cleaned = " ".join(cue.text.splitlines()).strip()
                wrapped = LLMCaptionTranslator._wrap_cue_lines(cleaned, max_line_length)
                translated_by_index[cue.index] = wrapped

        return translated_by_index, False, None, max(1, attempts), self.prompt_version

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
        if not parsed.is_valid:
            issue_codes = [i.code for i in parsed.report.issues]
            raise CaptionTranslationError(
                f"Google Translate captions for {target_language.code!r} failed validation: "
                f"{issue_codes}",
                quota_units_used=0,
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


__all__ = [
    "CaptionTranslationError",
    "CaptionTranslationProvider",
    "CaptionTranslationResult",
    "FakeStructuredLLMClient",
    "GeminiStructuredClient",
    "GoogleCaptionTranslator",
    "GoogleMetadataTranslator",
    "LLMCaptionTranslator",
    "LLMOutputValidationError",
    "LLMStructuredClient",
    "MetadataTranslationError",
    "TranslatedCueItem",
    "YouTubeNativeCaptionTranslator",
    "create_llm_structured_client",
]
