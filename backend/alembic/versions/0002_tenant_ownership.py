"""Add organization identity and backfill existing records to a legacy workspace.

Revision ID: 0002_tenant_ownership
Revises: 0001_initial_schema
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0002_tenant_ownership"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_USER_ID = "00000000-0000-4000-8000-000000000002"
OWNED_TABLES = ("channel_connections", "videos", "jobs", "quota_ledger", "audit_events")


def upgrade() -> None:
    now = datetime.now(UTC)
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("is_legacy", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("oidc_issuer", sa.String(255), nullable=True),
        sa.Column("oidc_subject", sa.String(255), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_table(
        "organization_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_members")),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_members_org_user"),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'viewer')",
            name="ck_organization_members_role_valid",
        ),
    )
    op.create_index(
        op.f("ix_organization_members_organization_id"),
        "organization_members",
        ["organization_id"],
    )
    op.create_index(op.f("ix_organization_members_user_id"), "organization_members", ["user_id"])

    op.bulk_insert(
        sa.table(
            "organizations",
            sa.column("id", sa.String()),
            sa.column("name", sa.String()),
            sa.column("slug", sa.String()),
            sa.column("is_legacy", sa.Boolean()),
            sa.column("is_active", sa.Boolean()),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "id": LEGACY_ORGANIZATION_ID,
                "name": "Legacy Workspace",
                "slug": "legacy-workspace",
                "is_legacy": True,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "users",
            sa.column("id", sa.String()),
            sa.column("oidc_issuer", sa.String()),
            sa.column("oidc_subject", sa.String()),
            sa.column("email", sa.String()),
            sa.column("email_verified", sa.Boolean()),
            sa.column("is_active", sa.Boolean()),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "id": LEGACY_USER_ID,
                "oidc_issuer": None,
                "oidc_subject": None,
                "email": None,
                "email_verified": False,
                "is_active": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "organization_members",
            sa.column("organization_id", sa.String()),
            sa.column("user_id", sa.String()),
            sa.column("role", sa.String()),
            sa.column("is_active", sa.Boolean()),
            sa.column("created_at", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {
                "organization_id": LEGACY_ORGANIZATION_ID,
                "user_id": LEGACY_USER_ID,
                "role": "owner",
                "is_active": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    for table in OWNED_TABLES:
        op.add_column(table, sa.Column("organization_id", sa.String(36), nullable=True))
        legacy_table = sa.table(
            table,
            sa.column("organization_id", sa.String(36)),
        )
        op.execute(legacy_table.update().values(organization_id=LEGACY_ORGANIZATION_ID))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("organization_id", existing_type=sa.String(36), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_organization_id_organizations",
                "organizations",
                ["organization_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            if table == "jobs":
                batch.drop_constraint("uq_jobs_dedupe_key", type_="unique")
                batch.create_unique_constraint(
                    "uq_jobs_org_dedupe_key", ["organization_id", "dedupe_key"]
                )
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    org_count = bind.execute(sa.text("SELECT COUNT(*) FROM organizations")).scalar_one()
    if org_count > 1:
        raise RuntimeError(
            "Cannot downgrade tenant ownership while non-legacy organizations exist."
        )

    for table in reversed(OWNED_TABLES):
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            if table == "jobs":
                batch.drop_constraint("uq_jobs_org_dedupe_key", type_="unique")
                batch.create_unique_constraint("uq_jobs_dedupe_key", ["dedupe_key"])
            batch.drop_constraint(f"fk_{table}_organization_id_organizations", type_="foreignkey")
            batch.drop_column("organization_id")
    op.drop_index(op.f("ix_organization_members_user_id"), table_name="organization_members")
    op.drop_index(
        op.f("ix_organization_members_organization_id"), table_name="organization_members"
    )
    op.drop_table("organization_members")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
