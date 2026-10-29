"""Read-only diagnostic command for YouTube caption discovery and `tlang` translation.

Usage (Stage 3a mocked mode — default, zero network calls, zero credentials):
    .venv/bin/python -m backend.scripts.poc.caption_diagnostics --video-id dev_tr_video_01

Usage (Stage 3b live read-only mode — requires explicit owner authorization):
    .venv/bin/python -m backend.scripts.poc.caption_diagnostics \\
        --mode live-readonly \\
        --video-id <YOUR_DEV_VIDEO_ID> \\
        --i-own-this-dev-video <YOUR_DEV_VIDEO_ID>

Safety guarantees:
- Read-only: calls only `captions.list` and `captions.download` (plus local
  `prepare_caption_insert` validation). Never executes `captions.insert` or
  `videos.update`.
- Sanitized output: reports only track metadata (`language`, `track_kind`,
  `status`, `is_draft`), cue counts, validation status, and error codes.
  Never prints OAuth credentials, tokens, or raw subtitle text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.app_config import load_app_config
from app.core.settings import AppEnv, get_settings
from app.db.session import (
    create_engine_with_local_fallback,
    create_session_factory,
    ensure_sqlite_schema_initialized,
)
from app.services.captions.diagnostics import (
    CaptionTrackDiagnosticReport,
    run_caption_diagnostics_probe,
)
from app.services.youtube.captions_client import (
    CaptionDownloadFormat,
    MockYouTubeCaptionClient,
    parse_youtube_api_error,
)
from app.services.youtube.discovery import (
    ChannelNotConnectedError,
    mint_access_token_for_channel,
    run_and_persist_caption_probe,
)
from app.services.youtube.oauth import GoogleHttpOAuthTokenClient
from app.services.youtube.read_service import HttpYouTubeDataClient

_SAMPLE_TR_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Merhaba arkadaşlar, mutfağıma hoş geldiniz!\n\n"
    "2\n"
    "00:00:04,500 --> 00:00:08,000\n"
    "Bugün harika bir mercimek çorbası yapıyoruz.\n"
).encode()

_SAMPLE_TRANSLATIONS: dict[str, bytes] = {
    "en": (
        b"1\n"
        b"00:00:01,000 --> 00:00:04,000\n"
        b"Hello friends, welcome to my kitchen!\n\n"
        b"2\n"
        b"00:00:04,500 --> 00:00:08,000\n"
        b"Today we are making a wonderful lentil soup.\n"
    ),
    "ro": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Salut prieteni, bine ați venit în bucătăria mea!\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "Astăzi facem o supă minunată de linte.\n"
    ).encode(),
    "es": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "¡Hola amigos, bienvenidos a mi cocina!\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "Hoy preparamos una maravillosa sopa de lentejas.\n"
    ).encode(),
    "fr": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Bonjour les amis, bienvenue dans ma cuisine !\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "Aujourd'hui, nous préparons une merveilleuse soupe aux lentilles.\n"
    ).encode(),
    "de": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Hallo Freunde, willkommen in meiner Küche!\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "Heute machen wir eine wunderbare Linsensuppe.\n"
    ).encode(),
    "ar": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "مرحباً أصدقائي، أهلاً بكم في مطبخي!\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "اليوم نحضر شوربة عدس رائعة.\n"
    ).encode(),
    "it": (
        b"1\n"
        b"00:00:01,000 --> 00:00:04,000\n"
        b"Ciao amici, benvenuti nella mia cucina!\n\n"
        b"2\n"
        b"00:00:04,500 --> 00:00:08,000\n"
        b"Oggi prepariamo una meravigliosa zuppa di lenticchie.\n"
    ),
    "ru": (
        "1\n"
        "00:00:01,000 --> 00:00:04,000\n"
        "Привет, друзья, добро пожаловать на мою кухню!\n\n"
        "2\n"
        "00:00:04,500 --> 00:00:08,000\n"
        "Сегодня мы готовим чудесный чечевичный суп.\n"
    ).encode(),
}


def build_demo_mock_client(
    video_id: str,
    *,
    track_kind: str = "standard",
    simulate_asr_tlang_403: bool = False,
) -> MockYouTubeCaptionClient:
    """Create a pre-populated `MockYouTubeCaptionClient` for Stage 3a diagnostics."""
    caption_id = f"cap_{video_id}_tr"
    list_payload = {
        "kind": "youtube#captionListResponse",
        "items": [
            {
                "kind": "youtube#caption",
                "id": caption_id,
                "snippet": {
                    "videoId": video_id,
                    "lastUpdated": "2026-10-27T12:00:00.000Z",
                    "trackKind": track_kind,
                    "language": "tr",
                    "name": "Türkçe",
                    "audioTrackType": "primary",
                    "isCC": False,
                    "isDraft": False,
                    "isAutoSynced": track_kind == "asr",
                    "status": "serving",
                },
            }
        ],
    }

    downloads: dict[tuple[str, str, str | None], bytes | Exception] = {
        (caption_id, "srt", None): _SAMPLE_TR_SRT,
    }
    for lang_code, srt_bytes in _SAMPLE_TRANSLATIONS.items():
        if simulate_asr_tlang_403:
            downloads[(caption_id, "srt", lang_code)] = parse_youtube_api_error(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": (
                            "The caller does not have permission to download the "
                            "specified caption track."
                        ),
                        "errors": [
                            {
                                "domain": "youtube.caption",
                                "reason": "forbidden",
                                "message": "Forbidden",
                            }
                        ],
                    }
                },
            )
        else:
            downloads[(caption_id, "srt", lang_code)] = srt_bytes

    return MockYouTubeCaptionClient(
        list_responses={video_id: list_payload},
        download_responses=downloads,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only diagnostic probe for YouTube caption track selection and "
            "tlang machine translation."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "live-readonly"),
        default="mock",
        help="Execution mode: 'mock' (Stage 3a default) or 'live-readonly' (Stage 3b).",
    )
    parser.add_argument(
        "--video-id",
        default="dev_tr_video_01",
        help="YouTube video ID to inspect.",
    )
    parser.add_argument(
        "--i-own-this-dev-video",
        default="",
        help="Must match --video-id when running in --mode live-readonly.",
    )
    parser.add_argument(
        "--mock-track-kind",
        choices=("standard", "asr"),
        default="standard",
        help="In mock mode, simulate either a standard uploaded track or an ASR track.",
    )
    parser.add_argument(
        "--languages",
        default="",
        help=(
            "Optional comma-separated target language codes (defaults to enabled config languages)."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Path to directory containing languages.yaml and publishing-policy.yaml.",
    )
    return parser


async def run_cli(argv: list[str] | None = None) -> tuple[int, dict[str, object]]:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.mode == "live-readonly":
        if args.i_own_this_dev_video != args.video_id:
            return (
                2,
                {
                    "error": "missing_dev_video_confirmation",
                    "message": (
                        "Refusing to run live-readonly mode without matching "
                        "--i-own-this-dev-video <VIDEO_ID>."
                    ),
                },
            )
        settings = get_settings()
        app_cfg = load_app_config(args.config_dir)
        target_codes = (
            [c.strip() for c in args.languages.split(",") if c.strip()]
            if args.languages.strip()
            else None
        )
        engine, used_fallback = await create_engine_with_local_fallback(
            settings.database_url,
            allow_sqlite_fallback=settings.app_env in (AppEnv.DEVELOPMENT, AppEnv.TEST),
        )
        if used_fallback or engine.dialect.name == "sqlite":
            await ensure_sqlite_schema_initialized(engine)

        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                try:
                    _conn, access_token = await mint_access_token_for_channel(
                        session,
                        settings=settings,
                        token_client=GoogleHttpOAuthTokenClient(),
                    )
                except ChannelNotConnectedError as exc:
                    return (
                        2,
                        {
                            "error": "channel_not_connected",
                            "message": str(exc),
                        },
                    )
                yt_client = HttpYouTubeDataClient(access_token)
                live_report = await run_and_persist_caption_probe(
                    session,
                    yt_client=yt_client,
                    youtube_video_id=args.video_id,
                    languages_cfg=app_cfg.languages,
                    target_language_codes=target_codes,
                )
                await session.commit()
                return 0, live_report.to_safe_dict()
        finally:
            await engine.dispose()

    app_cfg = load_app_config(args.config_dir)
    target_codes = (
        [c.strip() for c in args.languages.split(",") if c.strip()]
        if args.languages.strip()
        else None
    )
    client = build_demo_mock_client(
        args.video_id,
        track_kind=args.mock_track_kind,
    )
    report: CaptionTrackDiagnosticReport = await run_caption_diagnostics_probe(
        client,
        video_id=args.video_id,
        languages_cfg=app_cfg.languages,
        target_language_codes=target_codes,
        download_format=CaptionDownloadFormat.SRT,
    )
    return 0, report.to_safe_dict()


def main(argv: list[str] | None = None) -> int:
    exit_code, output = asyncio.run(run_cli(argv))
    sys.stdout.write(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
