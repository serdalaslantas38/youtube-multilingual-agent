"""Health and readiness HTTP endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.core.app_config import aload_app_config
from app.core.settings import Settings
from app.db.session import check_database_health
from app.schemas.health import HealthResponse, ReadinessChecks, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Lightweight liveness check that never reveals secrets."""
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service="youtube-multilingual-agent",
        version=request.app.version,
        environment=settings.app_env.value,
        youtube_publishing_enabled=settings.youtube_publishing_enabled,
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(request: Request, response: Response) -> ReadinessResponse:
    """Readiness probe verifying database connectivity and YAML configuration."""
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine

    db_ok, db_detail = await check_database_health(engine)

    cfg_ok = False
    cfg_detail = "unloaded"
    auto_publish = False
    source_lang: str | None = None
    enabled_langs: list[str] = []

    try:
        app_cfg = await aload_app_config(settings.config_dir)
        cfg_ok = True
        cfg_detail = "ok"
        auto_publish = app_cfg.policy.global_.auto_publish_enabled
        source_lang = app_cfg.languages.source_language.code
        enabled_langs = app_cfg.languages.enabled_codes
    except Exception as exc:
        cfg_detail = f"{type(exc).__name__}: {exc}"

    all_ready = db_ok and cfg_ok
    if not all_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if all_ready else "degraded",
        environment=settings.app_env.value,
        youtube_publishing_enabled=settings.youtube_publishing_enabled,
        auto_publish_enabled=auto_publish,
        source_language=source_lang,
        enabled_target_languages=enabled_langs,
        checks=ReadinessChecks(
            database=db_ok,
            database_detail=db_detail,
            config=cfg_ok,
            config_detail=cfg_detail,
        ),
    )
