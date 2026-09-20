"""Qualification catalogue and per-worker credentials.

A worker's credential must cover the *entire* task interval, i.e. it has to be
valid before the task starts and may not expire before the task ends.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Qualification(Base):
    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class WorkerQualification(Base):
    __tablename__ = "worker_qualifications"
    __table_args__ = (
        UniqueConstraint("worker_id", "qualification_id", name="uq_worker_qual"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qualification_id: Mapped[int] = mapped_column(
        ForeignKey("qualifications.id", ondelete="RESTRICT"), nullable=False
    )
    # Timezone-aware UTC bounds. ``valid_until = NULL`` means the credential
    # never expires.
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    worker: Mapped["Worker"] = relationship(back_populates="qualifications")
    qualification: Mapped[Qualification] = relationship()
