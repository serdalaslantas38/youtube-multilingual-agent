"""Async SQLAlchemy engine, session factory, and declarative base.

Supports SQLite (default for local development and tests) and PostgreSQL
(production and optional local Docker integration tests).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DEFAULT_LOCAL_SQLITE_URL = "sqlite+aiosqlite:///./data/app.db"

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint naming for Alembic."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return
    _, _, raw_path = database_url.partition(":///")
    if not raw_path or raw_path == ":memory:" or raw_path.startswith("file:"):
        return
    db_file = Path(raw_path.split("?", 1)[0])
    db_file.parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an AsyncEngine configured for either SQLite or PostgreSQL."""
    _ensure_sqlite_parent_dir(database_url)
    engine_kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
    engine = create_async_engine(database_url, **engine_kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def check_database_health(engine: AsyncEngine) -> tuple[bool, str]:
    """Execute a lightweight connectivity probe against the database."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        dialect = engine.dialect.name
        return True, f"ok ({dialect})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def create_engine_with_local_fallback(
    database_url: str,
    *,
    allow_sqlite_fallback: bool = True,
    fallback_sqlite_url: str = DEFAULT_LOCAL_SQLITE_URL,
    echo: bool = False,
) -> tuple[AsyncEngine, bool]:
    """Create an AsyncEngine and optionally fall back to local SQLite in development.

    Returns `(engine, used_fallback)`. In production (`allow_sqlite_fallback=False`),
    PostgreSQL connectivity failures are never masked.
    """
    primary_engine = create_engine(database_url, echo=echo)
    if database_url.startswith("sqlite") or not allow_sqlite_fallback:
        return primary_engine, False

    ok, _ = await check_database_health(primary_engine)
    if ok:
        return primary_engine, False

    await primary_engine.dispose()
    fallback_engine = create_engine(fallback_sqlite_url, echo=echo)
    return fallback_engine, True


async def ensure_sqlite_schema_initialized(engine: AsyncEngine) -> None:
    """Ensure tables exist when running against a fresh local SQLite database."""
    if engine.dialect.name != "sqlite":
        return
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
