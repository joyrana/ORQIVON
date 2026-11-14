"""remote repositories and pull requests

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("project_repositories", sa.Column("connection", sa.String(length=63), nullable=True))
    op.add_column(
        "project_repositories",
        sa.Column("sync_status", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "code_changes",
        sa.Column("pull_request", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("code_changes", "pull_request")
    op.drop_column("project_repositories", "sync_status")
    op.drop_column("project_repositories", "connection")
