"""single sign-on, SCIM provisioning and directory-managed roles

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_LIST = sa.text("'[]'::jsonb")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.add_column("users", sa.Column("groups", JSONB, nullable=False, server_default=EMPTY_LIST))
    op.add_column(
        "users", sa.Column("scim_managed", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.add_column("users", sa.Column("external_id", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("admin_source", sa.String(length=20), nullable=True))
    op.add_column(
        "memberships",
        sa.Column("source", sa.String(length=20), nullable=False, server_default=sa.text("'manual'")),
    )
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("provider", "subject"),
    )
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table(
        "oidc_login_states",
        sa.Column("state_hash", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("nonce", sa.String(length=100), nullable=False),
        sa.Column("code_verifier", sa.String(length=200), nullable=False),
        sa.Column("return_to", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oidc_login_states_expires_at", "oidc_login_states", ["expires_at"])
    op.create_table(
        "directory_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.String(length=200), nullable=False, unique=True),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("members", JSONB, nullable=False, server_default=EMPTY_LIST),
        *_timestamps(),
    )


def downgrade() -> None:
    op.drop_table("directory_groups")
    op.drop_index("ix_oidc_login_states_expires_at", table_name="oidc_login_states")
    op.drop_table("oidc_login_states")
    op.drop_index("ix_external_identities_user_id", table_name="external_identities")
    op.drop_table("external_identities")
    op.drop_column("memberships", "source")
    op.drop_column("users", "admin_source")
    op.drop_column("users", "external_id")
    op.drop_column("users", "scim_managed")
    op.drop_column("users", "groups")
