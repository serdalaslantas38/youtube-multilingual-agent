"""FastAPI routers."""

from app.api.health import router as health_router
from app.api.oauth_and_discovery import router as oauth_and_discovery_router

__all__ = ["health_router", "oauth_and_discovery_router"]
