"""work tool integrations

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_links",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("connection", sa.String(length=40), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("external_key", sa.String(length=200), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_external_links_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_links")),
        sa.UniqueConstraint(
            "connection",
            "entity_type",
            "entity_id",
            name=op.f("uq_external_links_connection_entity_type_entity_id"),
        ),
    )
    op.create_index(op.f("ix_external_links_project_id"), "external_links", ["project_id"], unique=False)
    op.add_column(
        "projects",
        sa.Column(
            "integrations", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
    )
    op.alter_column("projects", "integrations", server_default=None)


def downgrade() -> None:
    op.drop_column("projects", "integrations")
    op.drop_index(op.f("ix_external_links_project_id"), table_name="external_links")
    op.drop_table("external_links")
