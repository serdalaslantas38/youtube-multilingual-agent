"""Deterministic validation of language codes, metadata limits, placeholders, and captions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.app_config import LanguagesConfig
from app.models.enums import SourceCaptionStatus, SourceCaptionTrackKind

_BCP47_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"')]+")
_HANDLE_PATTERN = re.compile(r"(?<![A-Za-z0-9._+-])@[A-Za-z0-9_.-]{2,}")
_HASHTAG_PATTERN = re.compile(r"(?<!\w)#[\w\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff]+")
_NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")

# Map Eastern Arabic, Persian/Urdu, and Devanagari digits to ASCII digits for numeric comparison
_ARABIC_INDIC_DIGIT_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९",
    "012345678901234567890123456789",
)

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class ValidationReport:
    passed: bool
    issues: tuple[ValidationIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [
                {"code": i.code, "message": i.message, "blocking": i.blocking} for i in self.issues
            ],
        }


@dataclass(frozen=True, slots=True)
class PlaceholderBundle:
    """Deterministic placeholder mapping for protecting tokens during LLM localization."""

    masked_title: str
    masked_description: str
    token_to_literal: dict[str, str]
    title_tokens: tuple[str, ...]
    description_tokens: tuple[str, ...]
    title_numbers: tuple[str, ...]


def normalize_digits(text: str) -> str:
    """Normalize Eastern Arabic / Persian numerals to ASCII digits."""
    return text.translate(_ARABIC_INDIC_DIGIT_MAP)


def extract_numeric_tokens(text: str) -> tuple[str, ...]:
    """Extract normalized numeric literals (e.g. '1', '2.5', '500') from text."""
    normalized = normalize_digits(text)
    tokens: list[str] = []
    for match in _NUMBER_PATTERN.finditer(normalized):
        val = match.group(0).replace(",", ".")
        if val not in tokens:
            tokens.append(val)
    return tuple(tokens)


def extract_protected_placeholders(
    title: str,
    description: str,
    glossary: Sequence[str] = (),
) -> PlaceholderBundle:
    """Replace URLs, @handles, #hashtags, and glossary terms with deterministic placeholders."""
    token_to_literal: dict[str, str] = {}
    literal_to_token: dict[str, str] = {}
    counters = {"URL": 0, "HANDLE": 0, "HASHTAG": 0, "GLOSSARY": 0}

    def _register(kind: str, literal: str) -> str:
        if literal in literal_to_token:
            return literal_to_token[literal]
        idx = counters[kind]
        counters[kind] += 1
        token = f"__{kind}_{idx}__"
        token_to_literal[token] = literal
        literal_to_token[literal] = token
        return token

    def _mask_text(raw: str) -> tuple[str, tuple[str, ...]]:
        used: list[str] = []
        masked = raw

        # 1. URLs first so @ or # inside URLs are never split
        for m in list(_URL_PATTERN.finditer(masked)):
            lit = m.group(0)
            tok = _register("URL", lit)
            if tok not in used:
                used.append(tok)
        for tok, lit in list(token_to_literal.items()):
            if tok.startswith("__URL_") and lit in masked:
                masked = masked.replace(lit, tok)

        # 2. @handles (before glossary so @orchidhacks is masked as a handle)
        for m in list(_HANDLE_PATTERN.finditer(masked)):
            lit = m.group(0)
            tok = _register("HANDLE", lit)
            if tok not in used:
                used.append(tok)
        for tok, lit in list(token_to_literal.items()):
            if tok.startswith("__HANDLE_") and lit in masked:
                masked = masked.replace(lit, tok)

        # 3. #hashtags (before glossary so #HayataDairPüfNoktalar stays one hashtag token)
        for m in list(_HASHTAG_PATTERN.finditer(masked)):
            lit = m.group(0)
            tok = _register("HASHTAG", lit)
            if tok not in used:
                used.append(tok)
        for tok, lit in list(token_to_literal.items()):
            if tok.startswith("__HASHTAG_") and lit in masked:
                masked = masked.replace(lit, tok)

        # 4. Glossary terms (longest first)
        for term in sorted({g.strip() for g in glossary if g.strip()}, key=len, reverse=True):
            if term in masked:
                tok = _register("GLOSSARY", term)
                masked = masked.replace(term, tok)
                if tok not in used:
                    used.append(tok)

        return masked, tuple(used)

    # Extract numbers before masking so placeholder indices (__URL_0__) are not mistaken for numbers
    title_numbers = extract_numeric_tokens(title)
    masked_title, title_tokens = _mask_text(title)
    masked_description, description_tokens = _mask_text(description)

    return PlaceholderBundle(
        masked_title=masked_title,
        masked_description=masked_description,
        token_to_literal=token_to_literal,
        title_tokens=title_tokens,
        description_tokens=description_tokens,
        title_numbers=title_numbers,
    )


def restore_placeholders(text: str, bundle: PlaceholderBundle) -> str:
    """Restore ``__KIND_N__`` placeholders back to their original literal strings."""
    restored = text
    for token, literal in reversed(list(bundle.token_to_literal.items())):
        restored = restored.replace(token, literal)

    # Also repair near-miss placeholder typos (e.g. __HASHTIG_18__ -> __HASHTAG_18__)
    def _fuzzy_restore(match: re.Match[str]) -> str:
        raw_kind = match.group("kind").upper()
        idx = match.group("idx")
        for canonical_kind in ("URL", "HANDLE", "HASHTAG", "GLOSSARY"):
            if raw_kind[:3] == canonical_kind[:3]:
                candidate = f"__{canonical_kind}_{idx}__"
                if candidate in bundle.token_to_literal:
                    return bundle.token_to_literal[candidate]
        return match.group(0)

    restored = re.sub(r"__(?P<kind>[A-Za-z]+)_(?P<idx>\d+)__", _fuzzy_restore, restored)
    return restored


def validate_language_code(
    code: str,
    languages_cfg: LanguagesConfig | None = None,
    *,
    require_enabled: bool = True,
) -> ValidationReport:
    """Validate that a language code is well-formed BCP-47 and supported by configuration."""
    issues: list[ValidationIssue] = []
    normalized = code.strip()
    if not normalized or not _BCP47_PATTERN.match(normalized):
        issues.append(
            ValidationIssue(
                code="invalid_language_code",
                message=f"Language code {code!r} is not a valid BCP-47 identifier.",
            )
        )
        return ValidationReport(passed=False, issues=tuple(issues))

    if languages_cfg is not None:
        if normalized == languages_cfg.source_language.code:
            issues.append(
                ValidationIssue(
                    code="target_equals_source_language",
                    message=(
                        f"Language {normalized!r} is the source language and cannot be a "
                        "translation target."
                    ),
                )
            )
        else:
            target = languages_cfg.get(normalized)
            if target is None:
                issues.append(
                    ValidationIssue(
                        code="unsupported_target_language",
                        message=f"Language {normalized!r} is not configured in languages.yaml.",
                    )
                )
            elif require_enabled and not target.enabled:
                issues.append(
                    ValidationIssue(
                        code="disabled_target_language",
                        message=f"Language {normalized!r} is currently disabled.",
                    )
                )

    return ValidationReport(passed=not any(i.blocking for i in issues), issues=tuple(issues))


def validate_localized_metadata(
    title: str,
    description: str,
    *,
    source_title: str | None = None,
    source_description: str | None = None,
    protected_glossary: Sequence[str] = (),
) -> ValidationReport:
    """Validate YouTube Data API v3 title/description limits and placeholder preservation."""
    issues: list[ValidationIssue] = []
    stripped_title = title.strip()
    if not stripped_title:
        issues.append(
            ValidationIssue(code="empty_title", message="Localized title must not be empty.")
        )
    elif len(title) > MAX_TITLE_CHARS:
        issues.append(
            ValidationIssue(
                code="title_too_long",
                message=f"Title is {len(title)} characters (maximum is {MAX_TITLE_CHARS}).",
            )
        )

    desc_bytes = len(description.encode("utf-8"))
    if desc_bytes > MAX_DESCRIPTION_BYTES:
        issues.append(
            ValidationIssue(
                code="description_too_long_bytes",
                message=(
                    f"Description is {desc_bytes} UTF-8 bytes "
                    f"(maximum is {MAX_DESCRIPTION_BYTES} bytes)."
                ),
            )
        )

    if "<" in title or ">" in title or "<" in description or ">" in description:
        issues.append(
            ValidationIssue(
                code="forbidden_angle_brackets",
                message="YouTube metadata cannot contain '<' or '>' characters.",
            )
        )

    if "\ufffd" in title or "\ufffd" in description:
        issues.append(
            ValidationIssue(
                code="unicode_replacement_character",
                message="Metadata contains Unicode replacement character (U+FFFD / mojibake).",
            )
        )

    # Check unresolved placeholder leakage (including any __TOKEN_N__ variant)
    if re.search(r"__[A-Za-z]+_\d+__", f"{title}\n{description}"):
        issues.append(
            ValidationIssue(
                code="unresolved_placeholder_token",
                message="Localized metadata contains an unresolved placeholder token.",
            )
        )

    # Source-to-target preservation checks when source metadata is provided
    if source_title is not None or source_description is not None:
        src_t = source_title or ""
        src_d = source_description or ""
        combined_src = f"{src_t}\n{src_d}"
        combined_tgt = f"{title}\n{description}"

        for url in _URL_PATTERN.findall(combined_src):
            if url not in combined_tgt:
                issues.append(
                    ValidationIssue(
                        code="missing_protected_url",
                        message=f"Source URL {url!r} was dropped or altered in localization.",
                    )
                )

        src_without_urls = _URL_PATTERN.sub(" ", combined_src)
        tgt_without_urls = _URL_PATTERN.sub(" ", combined_tgt)

        for handle in _HANDLE_PATTERN.findall(src_without_urls):
            if handle not in tgt_without_urls:
                issues.append(
                    ValidationIssue(
                        code="missing_protected_handle",
                        message=f"Source handle {handle!r} was dropped or altered in localization.",
                    )
                )

        for hashtag in _HASHTAG_PATTERN.findall(src_without_urls):
            if hashtag not in tgt_without_urls:
                issues.append(
                    ValidationIssue(
                        code="missing_protected_hashtag",
                        message=f"Source hashtag {hashtag!r} was dropped in localization.",
                    )
                )

        for term in protected_glossary:
            clean_term = term.strip()
            if clean_term and clean_term in combined_src and clean_term not in combined_tgt:
                issues.append(
                    ValidationIssue(
                        code="missing_glossary_term",
                        message=(
                            f"Protected glossary term {clean_term!r} present in source was not "
                            "preserved in localization."
                        ),
                    )
                )

        if source_title is not None:
            src_nums = extract_numeric_tokens(source_title)
            tgt_nums = set(extract_numeric_tokens(title))
            for num in src_nums:
                if num not in tgt_nums:
                    issues.append(
                        ValidationIssue(
                            code="missing_source_number",
                            message=(
                                f"Numeric quantity {num!r} from source title is missing in "
                                f"localized title {title!r}."
                            ),
                        )
                    )

    return ValidationReport(passed=not any(i.blocking for i in issues), issues=tuple(issues))


def validate_existing_target_track(
    language: str,
    existing_caption_languages: set[str] | Sequence[str],
    *,
    explicit_overwrite_authorized: bool = False,
) -> ValidationReport:
    """Verify whether a caption track already exists for the target language."""
    normalized_existing = {lang.strip().lower() for lang in existing_caption_languages}
    if language.strip().lower() in normalized_existing and not explicit_overwrite_authorized:
        return ValidationReport(
            passed=False,
            issues=(
                ValidationIssue(
                    code="existing_caption_track_detected",
                    message=(
                        f"Caption track for language {language!r} already exists on YouTube; "
                        "skip_and_report policy forbids overwriting."
                    ),
                ),
            ),
        )
    return ValidationReport(passed=True, issues=())


def validate_existing_metadata_localization(
    language: str,
    existing_localization_languages: set[str] | Sequence[str],
    *,
    explicit_overwrite_authorized: bool = False,
) -> ValidationReport:
    """Verify whether localized metadata already exists for the target language."""
    normalized_existing = {lang.strip().lower() for lang in existing_localization_languages}
    if language.strip().lower() in normalized_existing and not explicit_overwrite_authorized:
        return ValidationReport(
            passed=False,
            issues=(
                ValidationIssue(
                    code="existing_metadata_localization_detected",
                    message=(
                        f"Metadata localization for language {language!r} already exists on "
                        "YouTube; skip_and_report policy forbids overwriting."
                    ),
                ),
            ),
        )
    return ValidationReport(passed=True, issues=())


@dataclass(frozen=True, slots=True)
class SourceCaptionCandidate:
    """Lightweight representation of a discovered caption track for selection/diagnostics."""

    youtube_caption_id: str
    language: str
    track_kind: SourceCaptionTrackKind
    processing_status: SourceCaptionStatus
    is_draft: bool = False
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCaptionDiagnosis:
    """Result of inspecting available caption tracks for a video."""

    ready: bool
    retry_later: bool
    selected_track: SourceCaptionCandidate | None
    reason_code: str
    message: str


def evaluate_source_caption_tracks(
    tracks: list[SourceCaptionCandidate],
    source_language: str = "tr",
) -> SourceCaptionDiagnosis:
    """Inspect discovered tracks without assuming standard vs ASR.

    - If no track matches `source_language`: returns `ready=False, retry_later=True`
      (`missing_source_caption`) so the job waits and retries with bounded backoff.
    - If matching tracks are still `syncing`: returns `ready=False, retry_later=True`
      (`source_caption_syncing`).
    - If a `serving` non-draft `standard` track exists, prefers it over `asr`, while
      still accepting `asr` if it is the only serving track.
    """
    matching = [t for t in tracks if t.language.lower() == source_language.lower()]
    if not matching:
        return SourceCaptionDiagnosis(
            ready=False,
            retry_later=True,
            selected_track=None,
            reason_code="missing_source_caption",
            message=f"No {source_language!r} caption track found yet.",
        )

    serving = [
        t for t in matching if t.processing_status is SourceCaptionStatus.SERVING and not t.is_draft
    ]
    if serving:
        # Prefer standard (creator-uploaded) over ASR if both exist, but support either.
        serving.sort(
            key=lambda t: (
                0
                if t.track_kind is SourceCaptionTrackKind.STANDARD
                else 1
                if t.track_kind is SourceCaptionTrackKind.ASR
                else 2
            )
        )
        chosen = serving[0]
        return SourceCaptionDiagnosis(
            ready=True,
            retry_later=False,
            selected_track=chosen,
            reason_code="source_caption_ready",
            message=(
                f"Source caption track {chosen.youtube_caption_id!r} "
                f"(kind={chosen.track_kind.value}, status={chosen.processing_status.value}) "
                "is ready."
            ),
        )

    if any(t.processing_status is SourceCaptionStatus.SYNCING for t in matching):
        return SourceCaptionDiagnosis(
            ready=False,
            retry_later=True,
            selected_track=None,
            reason_code="source_caption_syncing",
            message=f"Source {source_language!r} caption track is still syncing on YouTube.",
        )

    if all(t.is_draft for t in matching):
        return SourceCaptionDiagnosis(
            ready=False,
            retry_later=True,
            selected_track=None,
            reason_code="source_caption_is_draft",
            message=f"Source {source_language!r} caption track is still marked as draft.",
        )

    return SourceCaptionDiagnosis(
        ready=False,
        retry_later=False,
        selected_track=None,
        reason_code="source_caption_failed",
        message=f"Source {source_language!r} caption track failed processing on YouTube.",
    )
