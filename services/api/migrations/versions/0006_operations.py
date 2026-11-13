"""shared rate limiting and worker heartbeats

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_windows",
        sa.Column("key", sa.String(length=200), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=100), primary_key=True),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("jobs_done", sa.Integer(), nullable=False),
        sa.Column(
            "sandbox",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index("ix_worker_heartbeats_seen_at", "worker_heartbeats", ["seen_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_seen_at", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_table("rate_limit_windows")
