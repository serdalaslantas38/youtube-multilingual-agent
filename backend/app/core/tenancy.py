"""Shared tenant identifiers and transitional legacy workspace behavior."""

from contextvars import ContextVar, Token

LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_ORGANIZATION_SLUG = "legacy-workspace"

_organization_id: ContextVar[str | None] = ContextVar("organization_id", default=None)


def current_organization_id() -> str:
    """Return the explicitly bound organization, failing closed without a tenant."""
    organization_id = _organization_id.get()
    if organization_id is None:
        raise RuntimeError("An organization context is required for customer data access.")
    return organization_id


def bind_organization_id(organization_id: str) -> Token[str | None]:
    """Bind an organization for a request or worker operation and return its reset token."""
    return _organization_id.set(organization_id)


def reset_organization_id(token: Token[str | None]) -> None:
    """Restore the organization context after a request or worker operation."""
    _organization_id.reset(token)

