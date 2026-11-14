"""external deployments

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("external", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.alter_column("deployments", "external", server_default=None)


def downgrade() -> None:
    op.drop_column("deployments", "external")
