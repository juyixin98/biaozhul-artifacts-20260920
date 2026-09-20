"""Organisational structure: care units, workers, coordinators.

Coordinators may only schedule the units explicitly granted to them through
``coordinator_units``. Worker membership of a unit works the same way.
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

coordinator_units = Table(
    "coordinator_units",
    Base.metadata,
    Column("coordinator_id", ForeignKey("coordinators.id", ondelete="CASCADE"), primary_key=True),
    Column("unit_id", ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
)

worker_units = Table(
    "worker_units",
    Base.metadata,
    Column("worker_id", ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True),
    Column("unit_id", ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
)


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    workers: Mapped[list["Worker"]] = relationship(
        secondary=worker_units, back_populates="units"
    )
    coordinators: Mapped[list["Coordinator"]] = relationship(
        secondary=coordinator_units, back_populates="units"
    )


class Coordinator(Base):
    __tablename__ = "coordinators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # In a real system this would reference an identity provider; here the
    # header ``X-Coordinator-Id`` selects the acting coordinator.
    external_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    units: Mapped[list[Unit]] = relationship(
        secondary=coordinator_units, back_populates="coordinators"
    )


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    # Week boundaries (Mon 00:00) and rest rules are evaluated in the worker's
    # home timezone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    units: Mapped[list[Unit]] = relationship(
        secondary=worker_units, back_populates="workers"
    )
    qualifications: Mapped[list["WorkerQualification"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )
