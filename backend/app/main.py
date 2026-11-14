"""FastAPI application factory and lifespan hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.approvals_and_settings import router as approvals_and_settings_router
from app.api.health import router as health_router
from app.api.identity import router as identity_router
from app.api.localization import router as localization_router
from app.api.oauth_and_discovery import router as oauth_and_discovery_router
from app.api.publishing import router as publishing_router
from app.core.app_config import aload_app_config
from app.core.settings import AppEnv, Settings, get_settings
from app.db.session import (
    create_engine_with_local_fallback,
    create_session_factory,
    ensure_sqlite_schema_initialized,
)
from app.services.youtube.oauth import GoogleHttpOAuthTokenClient, OAuthStateStore


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        allow_fallback = resolved_settings.app_env in (
            AppEnv.DEVELOPMENT,
            AppEnv.TEST,
        )
        engine, used_sqlite_fallback = await create_engine_with_local_fallback(
            resolved_settings.database_url,
            allow_sqlite_fallback=allow_fallback,
        )
        if used_sqlite_fallback or engine.dialect.name == "sqlite":
            await ensure_sqlite_schema_initialized(engine)

        session_factory = create_session_factory(engine)
        app.state.settings = resolved_settings
        app.state.engine = engine
        app.state.used_sqlite_fallback = used_sqlite_fallback
        app.state.session_factory = session_factory
        app.state.app_config = await aload_app_config(resolved_settings.config_dir)
        if not hasattr(app.state, "oauth_state_store"):
            app.state.oauth_state_store = OAuthStateStore()
        if not hasattr(app.state, "oauth_token_client"):
            app.state.oauth_token_client = GoogleHttpOAuthTokenClient()
        try:
            yield
        finally:
            await engine.dispose()

    application = FastAPI(
        title="YouTube Multilingual Publishing Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.oauth_state_store = OAuthStateStore()
    application.state.oauth_token_client = GoogleHttpOAuthTokenClient()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(identity_router)
    application.include_router(oauth_and_discovery_router)
    application.include_router(localization_router)
    application.include_router(approvals_and_settings_router)
    application.include_router(publishing_router)
    return application


app = create_app()
