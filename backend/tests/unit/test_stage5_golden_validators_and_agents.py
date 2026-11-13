"""Stage 5 golden unit tests for deterministic validators, prompt security, and LLM agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.metadata_localizer import MetadataLocalizerAgent
from app.agents.quality_reviewer import QualityReviewerAgent
from app.core.app_config import load_app_config
from app.prompts.loader import (
    PromptSecurityError,
    assert_no_secrets_in_text,
    load_prompt_template,
)
from app.schemas.localization import (
    QualityReviewIssue,
    QualityReviewLLMOutput,
    ReviewIssueCategory,
    ReviewIssueSeverity,
)
from app.services.captions.parser import (
    SubtitleFormat,
    normalize_youtube_tlang_srt,
    parse_and_validate_subtitles,
    serialize_subtitles_to_srt,
    serialize_subtitles_to_vtt,
)
from app.services.captions.translator import SubtitleTranslationService
from app.services.translation.providers import (
    FakeStructuredLLMClient,
    GoogleCaptionTranslator,
    GoogleMetadataTranslator,
    LLMOutputValidationError,
    MetadataTranslationError,
)
from app.services.validation.validators import (
    extract_protected_placeholders,
    restore_placeholders,
    validate_existing_metadata_localization,
    validate_existing_target_track,
    validate_language_code,
    validate_localized_metadata,
)
from app.services.youtube.captions_client import MockYouTubeCaptionClient, parse_youtube_api_error
from tests.fixtures.turkish_golden import (
    GOLDEN_TURKISH_DESCRIPTION,
    GOLDEN_TURKISH_SRT,
    GOLDEN_TURKISH_TITLE,
    GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT,
)


def test_golden_placeholder_extraction_and_restoration() -> None:
    glossary = ["HayataDairPüfNoktalar", "@orchidhacks"]
    bundle = extract_protected_placeholders(
        GOLDEN_TURKISH_TITLE,
        GOLDEN_TURKISH_DESCRIPTION,
        glossary=glossary,
    )
    # All URLs, handles, hashtags, and glossary terms are replaced by tokens
    assert "https://www.youtube.com/@orchidhacks" not in bundle.masked_description
    assert "https://www.instagram.com/orchidhacks" not in bundle.masked_description
    assert "#orkidebakımı" not in bundle.masked_description
    assert "1" in bundle.title_numbers

    # Restoring brings back the exact original strings
    assert restore_placeholders(bundle.masked_title, bundle) == GOLDEN_TURKISH_TITLE
    assert restore_placeholders(bundle.masked_description, bundle) == GOLDEN_TURKISH_DESCRIPTION


@pytest.mark.parametrize(
    ("mutated_title", "mutated_desc", "expected_issue_code"),
    [
        # Empty title
        ("   ", "Valid description https://www.youtube.com/@orchidhacks", "empty_title"),
        # Title > 100 chars
        ("A" * 101 + " 1", GOLDEN_TURKISH_DESCRIPTION, "title_too_long"),
        # Description > 5000 UTF-8 bytes
        (
            "Just 1 Spoon for Orchids | HayataDairPüfNoktalar",
            "Çok güzel " * 600,
            "description_too_long_bytes",
        ),
        # Forbidden angle brackets
        (
            "Just 1 Spoon <Orchid> | HayataDairPüfNoktalar",
            GOLDEN_TURKISH_DESCRIPTION,
            "forbidden_angle_brackets",
        ),
        # Dropped URL
        (
            "Just 1 Spoon for Orchids | HayataDairPüfNoktalar",
            (
                "Welcome to HayataDairPüfNoktalar (@orchidhacks) "
                "#orkide #orkidebakımı #çiçekcoşturan #HayataDairPüfNoktalar"
            ),
            "missing_protected_url",
        ),
        # Dropped @handle
        (
            "Just 1 Spoon for Orchids | HayataDairPüfNoktalar",
            GOLDEN_TURKISH_DESCRIPTION.replace("(@orchidhacks)", ""),
            "missing_protected_handle",
        ),
        # Dropped #hashtag
        (
            "Just 1 Spoon for Orchids | HayataDairPüfNoktalar",
            GOLDEN_TURKISH_DESCRIPTION.replace("#orkidebakımı", ""),
            "missing_protected_hashtag",
        ),
        # Dropped glossary term
        (
            "Just 1 Spoon for Orchids",
            GOLDEN_TURKISH_DESCRIPTION.replace("HayataDairPüfNoktalar", "OrchidChannel"),
            "missing_glossary_term",
        ),
        # Dropped numeric quantity from title ('1' missing)
        (
            "Just a Spoon for Orchids | HayataDairPüfNoktalar",
            GOLDEN_TURKISH_DESCRIPTION,
            "missing_source_number",
        ),
        # Unresolved placeholder token
        (
            "Just 1 Spoon __GLOSSARY_0__ | HayataDairPüfNoktalar",
            GOLDEN_TURKISH_DESCRIPTION,
            "unresolved_placeholder_token",
        ),
    ],
)
def test_golden_metadata_validator_rules(
    mutated_title: str,
    mutated_desc: str,
    expected_issue_code: str,
) -> None:
    report = validate_localized_metadata(
        mutated_title,
        mutated_desc,
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        protected_glossary=["HayataDairPüfNoktalar", "@orchidhacks"],
    )
    assert report.passed is False
    assert any(i.code == expected_issue_code for i in report.issues)


def test_arabic_indic_numerals_satisfy_numeric_preservation() -> None:
    arabic_title = "فقط ١ ملعقة! تجعل الأوركيد تزهر طوال العام | HayataDairPüfNoktalar"
    report = validate_localized_metadata(
        arabic_title,
        GOLDEN_TURKISH_DESCRIPTION,
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        protected_glossary=["HayataDairPüfNoktalar", "@orchidhacks"],
    )
    assert report.passed is True


def test_existing_track_and_localization_detection() -> None:
    cap_rep = validate_existing_target_track("en", ["tr", "en", "ro"])
    assert cap_rep.passed is False
    assert cap_rep.issues[0].code == "existing_caption_track_detected"

    cap_allowed = validate_existing_target_track(
        "en", ["tr", "en"], explicit_overwrite_authorized=True
    )
    assert cap_allowed.passed is True

    meta_rep = validate_existing_metadata_localization("es", ["tr", "es"])
    assert meta_rep.passed is False
    assert meta_rep.issues[0].code == "existing_metadata_localization_detected"


def test_subtitle_max_line_length_and_serialization_roundtrip() -> None:
    src_doc = parse_and_validate_subtitles(
        GOLDEN_TURKISH_SRT, SubtitleFormat.SRT, max_line_length=84
    )
    assert src_doc.is_valid is True
    assert len(src_doc.cues) == 3

    # Tight max_line_length triggers `cue_line_too_long`
    strict_doc = parse_and_validate_subtitles(
        GOLDEN_TURKISH_SRT, SubtitleFormat.SRT, max_line_length=30
    )
    assert strict_doc.is_valid is False
    assert any(i.code == "cue_line_too_long" for i in strict_doc.report.issues)

    # Round-trip SRT and VTT serialization
    srt_out = serialize_subtitles_to_srt(src_doc.cues)
    vtt_out = serialize_subtitles_to_vtt(src_doc.cues)
    assert parse_and_validate_subtitles(
        srt_out, SubtitleFormat.SRT, reference_cues=src_doc.cues
    ).is_valid
    assert parse_and_validate_subtitles(
        vtt_out, SubtitleFormat.VTT, reference_cues=src_doc.cues
    ).is_valid


def test_youtube_tlang_quirk_normalization_and_llm_repair(config_dir: Path) -> None:
    # Raw tlang output with internal blank line in cue 2 and empty cue 3 fails raw parser
    raw_parsed = parse_and_validate_subtitles(GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT, SubtitleFormat.SRT)
    assert raw_parsed.is_valid is False

    normalized_srt, empty_indices = normalize_youtube_tlang_srt(GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT)
    # Cue 2's internal blank line was repaired; only cue 3 is genuinely empty
    assert empty_indices == [3]
    assert "Today we will strengthen orchid roots" in normalized_srt


async def test_subtitle_service_repairs_tlang_empty_cues_and_falls_back_on_error(
    config_dir: Path,
) -> None:
    cfg = load_app_config(config_dir)
    src_doc = parse_and_validate_subtitles(GOLDEN_TURKISH_SRT, SubtitleFormat.SRT)
    en_target = cfg.languages.get("en")
    ro_target = cfg.languages.get("ro")
    assert en_target is not None and ro_target is not None

    mock_yt = MockYouTubeCaptionClient()
    # For 'en', YouTube returns the quirky SRT with internal blank line + empty cue 3
    mock_yt.register_download(
        "cap-tr-std",
        GOLDEN_YOUTUBE_TLANG_EN_QUIRK_SRT.encode("utf-8"),
        tfmt="srt",
        tlang="en",
    )
    # For 'ro', YouTube returns 400 couldNotConvert so service must fall back to LLM
    mock_yt.register_download(
        "cap-tr-std",
        parse_youtube_api_error(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "Could not convert",
                    "errors": [{"reason": "couldNotConvert"}],
                }
            },
        ),
        tfmt="srt",
        tlang="ro",
    )

    fake_llm = FakeStructuredLLMClient()

    # Default policy is Google Translate first, then 'youtube_native', then 'llm'
    assert cfg.policy.captions.translation_sources == [
        "google_translate",
        "youtube_native",
        "llm",
    ]
    service = SubtitleTranslationService(
        translation_sources=cfg.policy.captions.translation_sources,
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
        caption_client=mock_yt,
        max_line_length=84,
    )

    # 1. 'en' uses youtube_native + targeted LLM repair for cue 3
    en_res = await service.translate(
        source_caption_id="cap-tr-std",
        source_cues=src_doc.cues,
        target_language=en_target,
        source_language=cfg.languages.source_language,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert en_res.parsed_doc.is_valid is True
    assert len(en_res.parsed_doc.cues) == 3
    assert en_res.provider_used == "youtube_native+fake_repair"
    assert en_res.prompt_version == "caption_translator.translate.v1"
    assert en_res.quota_units_used == 200

    # 2. 'ro' fails youtube_native with 400 couldNotConvert -> falls back to full LLM translation
    ro_res = await service.translate(
        source_caption_id="cap-tr-std",
        source_cues=src_doc.cues,
        target_language=ro_target,
        source_language=cfg.languages.source_language,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert ro_res.parsed_doc.is_valid is True
    assert len(ro_res.parsed_doc.cues) == 3
    assert ro_res.provider_used == "fake"
    assert ro_res.prompt_version == "caption_translator.translate.v1"
    # 200 units were spent on the failed youtube_native attempt before LLM fallback
    assert ro_res.quota_units_used == 200

    # 3. When YouTube quota is reached (HTTP 403 quotaExceeded), switch to LLM and
    # skip further youtube_native attempts in the same batch (0 extra YouTube quota wasted)
    es_target = cfg.languages.get("es")
    fr_target = cfg.languages.get("fr")
    assert es_target is not None and fr_target is not None
    mock_yt.register_download(
        "cap-tr-std",
        parse_youtube_api_error(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "The request cannot be completed: quotaExceeded.",
                    "errors": [{"reason": "quotaExceeded"}],
                }
            },
        ),
        tfmt="srt",
        tlang="es",
    )
    es_res = await service.translate(
        source_caption_id="cap-tr-std",
        source_cues=src_doc.cues,
        target_language=es_target,
        source_language=cfg.languages.source_language,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert es_res.provider_used == "fake"
    assert es_res.quota_units_used == 200

    # Subsequent language ('fr') immediately uses LLM without hitting YouTube API again
    fr_res = await service.translate(
        source_caption_id="cap-tr-std",
        source_cues=src_doc.cues,
        target_language=fr_target,
        source_language=cfg.languages.source_language,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert fr_res.provider_used == "fake"
    assert fr_res.quota_units_used == 0


async def test_metadata_agent_retries_on_invalid_llm_output_then_succeeds(
    config_dir: Path,
) -> None:
    cfg = load_app_config(config_dir)
    es_target = cfg.languages.get("es")
    assert es_target is not None

    fake_llm = FakeStructuredLLMClient(
        scripted_responses=[
            # Attempt 1: malformed JSON
            "{not valid json",
            # Attempt 2: valid JSON but contains forbidden '<' character
            {
                "title": "Solo 1 Cucharada <Orquídeas> | __GLOSSARY_0__",
                "description": GOLDEN_TURKISH_DESCRIPTION,
                "source_ambiguous": False,
                "ambiguity_notes": None,
            },
            # Attempt 3: falls through to default valid deterministic output
        ]
    )
    agent = MetadataLocalizerAgent(
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
        max_retries=2,
    )
    res = await agent.localize(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        source_language=cfg.languages.source_language,
        target_language=es_target,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert res.validation_report.passed is True
    assert res.attempt_count == 3
    assert res.prompt_version == "metadata_localizer.translate.v1"
    assert "HayataDairPüfNoktalar" in res.title
    assert "https://www.youtube.com/@orchidhacks" in res.description


async def test_metadata_agent_exhausts_retries_and_reports_failure(config_dir: Path) -> None:
    cfg = load_app_config(config_dir)
    de_target = cfg.languages.get("de")
    assert de_target is not None

    fake_llm = FakeStructuredLLMClient(
        scripted_responses=[
            LLMOutputValidationError("Bad JSON 1"),
            LLMOutputValidationError("Bad JSON 2"),
            LLMOutputValidationError("Bad JSON 3"),
        ]
    )
    agent = MetadataLocalizerAgent(
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
        max_retries=2,
    )
    res = await agent.localize(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        source_language=cfg.languages.source_language,
        target_language=de_target,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert res.validation_report.passed is False
    assert res.attempt_count == 3
    assert any(i.code == "llm_output_schema_invalid" for i in res.validation_report.issues)


async def test_quality_reviewer_blocks_untranslated_or_flagged_content(config_dir: Path) -> None:
    cfg = load_app_config(config_dir)
    fr_target = cfg.languages.get("fr")
    assert fr_target is not None

    fake_llm = FakeStructuredLLMClient()
    reviewer = QualityReviewerAgent(
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
    )

    # 1. Untranslated copy-paste of Turkish source title is caught deterministically
    untranslated_res = await reviewer.review_metadata(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        translated_title=GOLDEN_TURKISH_TITLE,
        translated_description="Description traduite...",
        source_language=cfg.languages.source_language,
        target_language=fr_target,
    )
    assert untranslated_res.passed is False
    assert any(i.severity is ReviewIssueSeverity.BLOCKING for i in untranslated_res.issues)
    assert untranslated_res.prompt_version == "quality_reviewer.review.v1"

    # 2. LLM flagging altered_meaning blocks review even if LLM mistakenly sets passed=True
    fake_llm.queue_response(
        QualityReviewLLMOutput(
            passed=True,
            source_ambiguous=False,
            summary="Changed 1 spoon to 5 spoons.",
            issues=[
                QualityReviewIssue(
                    category=ReviewIssueCategory.ALTERED_MEANING,
                    severity=ReviewIssueSeverity.BLOCKING,
                    message="Source says 1 spoon, translation says 5 spoons.",
                    location="title",
                )
            ],
        )
    )
    altered_res = await reviewer.review_metadata(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        translated_title="Seulement 1 cuillère pour les orchidées | HayataDairPüfNoktalar",
        translated_description="Description traduite...",
        source_language=cfg.languages.source_language,
        target_language=fr_target,
    )
    assert altered_res.passed is False


def test_prompt_loader_rejects_secrets_and_invalid_names() -> None:
    prompt = load_prompt_template("metadata_localizer.translate.v1")
    assert prompt.version == "v1"

    with pytest.raises(ValueError, match="Invalid prompt name"):
        load_prompt_template("unversioned_prompt")

    with pytest.raises(PromptSecurityError):
        assert_no_secrets_in_text("Here is a secret GOCSPX-abcdef1234567890xyz in prompt")

    with pytest.raises(PromptSecurityError):
        assert_no_secrets_in_text("Bearer ya29.a0AfH6SMBx1234567890abcdef")

    with pytest.raises(
        ValueError, match="Language code 'bad code!' is not a valid BCP-47 identifier"
    ):
        rep = validate_language_code("bad code!")
        if not rep.passed:
            raise ValueError(rep.issues[0].message)


async def test_google_translate_first_for_metadata_and_100_char_title_limit(
    config_dir: Path,
) -> None:
    cfg = load_app_config(config_dir)
    assert cfg.policy.metadata.translation_sources == [
        "google_translate",
        "youtube_native",
        "llm",
    ]
    assert cfg.policy.metadata.max_title_length == 100

    de_target = cfg.languages.get("de")
    assert de_target is not None

    # Simulate Google Translate expanding the title well beyond 100 characters and
    # turning the digit "1" into the word "Ein"
    async def _verbose_google_translate(text: str, sl: str, tl: str) -> str:
        assert sl == "tr"
        assert tl == "de"
        lines = text.split("\n")
        out: list[str] = []
        for idx, line in enumerate(lines):
            if idx == 0:
                out.append(
                    "Nur Ein Esslöffel sorgt dafür, dass die blütenlose Orchidee das ganze "
                    "Jahr über ununterbrochen wunderschön und reichlich blüht"
                )
            else:
                out.append(f"[DE] {line}")
        return "\n".join(out)

    google_translator = GoogleMetadataTranslator(
        max_title_length=100,
        translate_fn=_verbose_google_translate,
    )
    fake_llm = FakeStructuredLLMClient()
    agent = MetadataLocalizerAgent(
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
        translation_sources=cfg.policy.metadata.translation_sources,
        google_translator=google_translator,
        max_title_length=100,
    )
    res = await agent.localize(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        source_language=cfg.languages.source_language,
        target_language=de_target,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert res.validation_report.passed is True
    assert res.provider_used == "google_translate"
    assert res.prompt_version == "google_translate.v1"
    assert len(res.title) <= 100
    assert "1" in res.title
    assert "HayataDairPüfNoktalar" in res.title
    assert "https://www.youtube.com/@orchidhacks" in res.description
    assert len(fake_llm.calls) == 0

    # Also verify automatic fallback to LLM if google_translate fails
    async def _failing_google_translate(text: str, sl: str, tl: str) -> str:
        del text, sl, tl
        raise MetadataTranslationError("Google Translate unavailable")

    fallback_agent = MetadataLocalizerAgent(
        llm_client=fake_llm,
        model_name=cfg.policy.llm.model_name,
        translation_sources=["google_translate", "llm"],
        google_translator=GoogleMetadataTranslator(translate_fn=_failing_google_translate),
        max_title_length=100,
    )
    fallback_res = await fallback_agent.localize(
        source_title=GOLDEN_TURKISH_TITLE,
        source_description=GOLDEN_TURKISH_DESCRIPTION,
        source_language=cfg.languages.source_language,
        target_language=de_target,
        protected_glossary=cfg.languages.protected_glossary,
    )
    assert fallback_res.validation_report.passed is True
    assert fallback_res.provider_used == "fake"
    assert len(fallback_res.title) <= 100


async def test_subtitle_chain_google_then_youtube_then_llm_switches_uninterrupted(
    config_dir: Path,
) -> None:
    """Google Translate (0 YT quota) -> YouTube native -> LLM, switching uninterrupted."""
    cfg = load_app_config(config_dir)
    assert cfg.policy.captions.translation_sources == [
        "google_translate",
        "youtube_native",
        "llm",
    ]
    assert cfg.policy.metadata.translation_sources == [
        "google_translate",
        "youtube_native",
        "llm",
    ]
    src_doc = parse_and_validate_subtitles(GOLDEN_TURKISH_SRT, SubtitleFormat.SRT)
    assert src_doc.is_valid
    en_t, es_t, fr_t, de_t = (cfg.languages.get(c) for c in ("en", "es", "fr", "de"))
    assert en_t and es_t and fr_t and de_t

    mock_yt = MockYouTubeCaptionClient()
    mock_yt.register_download(
        "cap-tr-std",
        GOLDEN_TURKISH_SRT.encode("utf-8"),
        tfmt="srt",
        tlang="es",
    )
    mock_yt.register_download(
        "cap-tr-std",
        parse_youtube_api_error(
            403,
            {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}},
        ),
        tfmt="srt",
        tlang="fr",
    )

    google_calls: list[str] = []
    google_rate_limited = {"on": False}

    async def _google(text: str, sl: str, tl: str) -> str:
        google_calls.append(tl)
        if google_rate_limited["on"]:
            raise MetadataTranslationError("Google Translate request failed: HTTP 429 Too Many")
        return "\n".join(f"[{tl.upper()}] {line}" for line in text.split("\n"))

    service = SubtitleTranslationService(
        translation_sources=cfg.policy.captions.translation_sources,
        llm_client=FakeStructuredLLMClient(),
        model_name=cfg.policy.llm.model_name,
        caption_client=mock_yt,
        max_line_length=84,
        google_translator=GoogleCaptionTranslator(translate_fn=_google),
    )

    async def _run(target: object) -> object:
        return await service.translate(
            source_caption_id="cap-tr-std",
            source_cues=src_doc.cues,
            target_language=target,  # type: ignore[arg-type]
            source_language=cfg.languages.source_language,
            protected_glossary=cfg.languages.protected_glossary,
        )

    # 1. 'en': Google Translate runs first -> 0 YouTube quota units spent!
    en_res = await _run(en_t)
    assert en_res.provider_used == "google_translate"  # type: ignore[attr-defined]
    assert en_res.quota_units_used == 0  # type: ignore[attr-defined]
    assert en_res.parsed_doc.is_valid  # type: ignore[attr-defined]

    # 2. 'es': Google hits rate limit -> seamlessly falls back to YouTube native (200 units)
    google_rate_limited["on"] = True
    es_res = await _run(es_t)
    assert es_res.provider_used == "youtube_native"  # type: ignore[attr-defined]
    assert es_res.quota_units_used == 200  # type: ignore[attr-defined]
    calls_after_es = len(google_calls)

    # 3. 'fr': Google is skipped (already rate-limited); YouTube hits quota -> falls back to LLM
    fr_res = await _run(fr_t)
    assert fr_res.provider_used == "fake"  # type: ignore[attr-defined]
    assert fr_res.quota_units_used == 200  # type: ignore[attr-defined]
    assert len(google_calls) == calls_after_es

    # 4. 'de': both Google and YouTube are skipped; goes straight to LLM (0 YT quota units)
    de_res = await _run(de_t)
    assert de_res.provider_used == "fake"  # type: ignore[attr-defined]
    assert de_res.quota_units_used == 0  # type: ignore[attr-defined]
    assert len(google_calls) == calls_after_es


def test_app_main_imports_cleanly_without_circular_imports() -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "from app.main import app; assert app.title"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
