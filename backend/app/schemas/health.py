"""Pydantic response schemas for liveness and readiness endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    service: str
    version: str
    environment: str
    youtube_publishing_enabled: bool


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: bool
    database_detail: str
    config: bool
    config_detail: str


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    environment: str
    youtube_publishing_enabled: bool
    auto_publish_enabled: bool
    source_language: str | None
    enabled_target_languages: list[str]
    checks: ReadinessChecks
