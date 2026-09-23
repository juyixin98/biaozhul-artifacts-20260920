"""SQLAlchemy 表定义（PostgreSQL）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sig: Mapped[str | None] = mapped_column(String(256), nullable=True)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replay_version_after: Mapped[int] = mapped_column(Integer, nullable=False)


class ReportRow(Base):
    __tablename__ = "reports"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    created_from_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
