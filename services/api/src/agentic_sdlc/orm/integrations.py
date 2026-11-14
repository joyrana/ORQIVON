"""Links between platform records and their copies in the customer's work tools."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentic_sdlc.core.db import Base, IdMixin, TimestampMixin


class ExternalLink(IdMixin, TimestampMixin, Base):
    """A backlog item exported to Jira/Azure Boards, or a document imported from Confluence."""

    __tablename__ = "external_links"
    __table_args__ = (UniqueConstraint("connection", "entity_type", "entity_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    connection: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str] = mapped_column(String(40))  # backlog_item | source_document
    entity_id: Mapped[uuid.UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(200))
    external_key: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)  # last error, external version
