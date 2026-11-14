"""repository security scanning

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_acceptances",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.String(length=200), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("accepted_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["accepted_by"],
            ["users.id"],
            name=op.f("fk_security_acceptances_accepted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_security_acceptances_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_acceptances")),
        sa.UniqueConstraint(
            "project_id", "fingerprint", name=op.f("uq_security_acceptances_project_id_fingerprint")
        ),
    )
    op.create_index(
        op.f("ix_security_acceptances_project_id"), "security_acceptances", ["project_id"], unique=False
    )
    op.create_table(
        "security_scans",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("base_commit", sa.String(length=64), nullable=False),
        sa.Column("head_commit", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocking", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scanners", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_security_scans_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_security_scans_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_scans")),
        sa.UniqueConstraint("run_id", "attempt", name=op.f("uq_security_scans_run_id_attempt")),
    )
    op.create_index(op.f("ix_security_scans_project_id"), "security_scans", ["project_id"], unique=False)
    op.create_index(op.f("ix_security_scans_run_id"), "security_scans", ["run_id"], unique=False)
    op.add_column(
        "projects",
        sa.Column(
            "security_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
    )
    op.alter_column("projects", "security_config", server_default=None)


def downgrade() -> None:
    op.drop_column("projects", "security_config")
    op.drop_index(op.f("ix_security_scans_run_id"), table_name="security_scans")
    op.drop_index(op.f("ix_security_scans_project_id"), table_name="security_scans")
    op.drop_table("security_scans")
    op.drop_index(op.f("ix_security_acceptances_project_id"), table_name="security_acceptances")
    op.drop_table("security_acceptances")
