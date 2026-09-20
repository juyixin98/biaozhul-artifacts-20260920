"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
    )
    op.create_table(
        "coordinators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "workers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "coordinator_units",
        sa.Column(
            "coordinator_id",
            sa.Integer(),
            sa.ForeignKey("coordinators.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("units.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "worker_units",
        sa.Column(
            "worker_id",
            sa.Integer(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("units.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
    )
    op.create_table(
        "worker_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.Integer(),
            sa.ForeignKey("workers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "qualification_id",
            sa.Integer(),
            sa.ForeignKey("qualifications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.UniqueConstraint("worker_id", "qualification_id", name="uq_worker_qual"),
    )
    op.create_index(
        "ix_worker_qualifications_worker_id",
        "worker_qualifications",
        ["worker_id"],
    )
    op.create_table(
        "care_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "unit_id",
            sa.Integer(),
            sa.ForeignKey("units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_care_plans_unit_id", "care_plans", ["unit_id"])
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("care_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("period_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("anchor_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_note", sa.String(500), nullable=True),
        sa.UniqueConstraint("plan_id", "version_number", name="uq_plan_version"),
    )
    op.create_index(
        "ix_plan_versions_plan_id", "plan_versions", ["plan_id"]
    )
    op.create_table(
        "weekly_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.Time(), nullable=False),
        sa.Column("latest_start_at", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "plan_version_id", "weekday", "start_at", name="uq_weekly_slot"
        ),
    )
    op.create_index(
        "ix_weekly_slots_plan_version_id", "weekly_slots", ["plan_version_id"]
    )
    op.create_table(
        "plan_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "qualification_id",
            sa.Integer(),
            sa.ForeignKey("qualifications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "plan_version_id", "qualification_id", name="uq_plan_qualification"
        ),
    )
    op.create_index(
        "ix_plan_qualifications_plan_version_id",
        "plan_qualifications",
        ["plan_version_id"],
    )
    op.create_table(
        "plan_prerequisites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "required_plan_id",
            sa.Integer(),
            sa.ForeignKey("care_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "offset_minutes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.UniqueConstraint(
            "plan_version_id", "required_plan_id", name="uq_plan_prerequisite"
        ),
    )
    op.create_index(
        "ix_plan_prerequisites_plan_version_id",
        "plan_prerequisites",
        ["plan_version_id"],
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("care_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "slot_id",
            sa.Integer(),
            sa.ForeignKey("weekly_slots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "manually_adjusted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.UniqueConstraint(
            "plan_version_id",
            "slot_id",
            "occurrence_date",
            name="uq_task_occurrence",
        ),
    )
    op.create_index("ix_tasks_plan_id", "tasks", ["plan_id"])
    op.create_index("ix_tasks_plan_version_id", "tasks", ["plan_version_id"])
    op.create_index("ix_tasks_occurrence_date", "tasks", ["occurrence_date"])
    op.create_index("ix_tasks_starts_at", "tasks", ["starts_at"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_table(
        "task_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "qualification_id",
            sa.Integer(),
            sa.ForeignKey("qualifications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint("task_id", "qualification_id", name="uq_task_qualification"),
    )
    op.create_index(
        "ix_task_qualifications_task_id", "task_qualifications", ["task_id"]
    )
    op.create_table(
        "task_prerequisites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "required_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint("task_id", "required_task_id", name="uq_task_prerequisite"),
    )
    op.create_index(
        "ix_task_prerequisites_task_id", "task_prerequisites", ["task_id"]
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "worker_id",
            sa.Integer(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "invited_by", sa.String(200), nullable=False, server_default="system"
        ),
    )
    op.create_index("ix_assignments_task_id", "assignments", ["task_id"])
    op.create_index("ix_assignments_worker_id", "assignments", ["worker_id"])
    op.create_index("ix_assignments_status", "assignments", ["status"])
    op.create_index("ix_assignments_expires_at", "assignments", ["expires_at"])
    # Partial unique indexes: at most one live invitation / acceptance/task.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "uq_one_pending_per_task",
            "assignments",
            ["task_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
        )
        op.create_index(
            "uq_one_accepted_per_task",
            "assignments",
            ["task_id"],
            unique=True,
            postgresql_where=sa.text("status = 'accepted'"),
        )
    else:
        op.create_index(
            "uq_one_pending_per_task",
            "assignments",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
        )
        op.create_index(
            "uq_one_accepted_per_task",
            "assignments",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("status = 'accepted'"),
        )

    op.create_table(
        "assignment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assignment_id",
            sa.Integer(),
            sa.ForeignKey("assignments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "worker_id",
            sa.Integer(),
            sa.ForeignKey("workers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "coordinator_id",
            sa.Integer(),
            sa.ForeignKey("coordinators.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("detail", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_assignment_events_task_id", "assignment_events", ["task_id"]
    )
    op.create_index(
        "ix_assignment_events_assignment_id",
        "assignment_events",
        ["assignment_id"],
    )
    op.create_index(
        "ix_assignment_events_created_at", "assignment_events", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("assignment_events")
    op.drop_index("uq_one_accepted_per_task", table_name="assignments")
    op.drop_index("uq_one_pending_per_task", table_name="assignments")
    op.drop_table("assignments")
    op.drop_table("task_prerequisites")
    op.drop_table("task_qualifications")
    op.drop_table("tasks")
    op.drop_table("plan_prerequisites")
    op.drop_table("plan_qualifications")
    op.drop_table("weekly_slots")
    op.drop_table("plan_versions")
    op.drop_table("care_plans")
    op.drop_table("worker_qualifications")
    op.drop_table("qualifications")
    op.drop_table("worker_units")
    op.drop_table("coordinator_units")
    op.drop_table("workers")
    op.drop_table("coordinators")
    op.drop_table("units")
