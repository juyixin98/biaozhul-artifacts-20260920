"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "care_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit_id", sa.String(64), nullable=False),
        sa.Column("patient_name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("byweekday", sa.JSON(), nullable=True),
        sa.Column("window_start", sa.Time(), nullable=False),
        sa.Column("window_end", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("required_qualification", sa.String(64), nullable=False),
        sa.Column("prerequisite_plan_id", sa.Integer(),
                  sa.ForeignKey("care_plans.id"), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_care_plans_unit_id", "care_plans", ["unit_id"])

    op.create_table(
        "care_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("care_plans.id"),
                  nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("required_qualification", sa.String(64), nullable=False),
        sa.Column("unit_id", sa.String(64), nullable=False),
        sa.Column("depends_on_task_id", sa.Integer(),
                  sa.ForeignKey("care_tasks.id"), nullable=True),
        sa.UniqueConstraint("plan_id", "plan_version", "scheduled_start",
                            name="uq_task_occurrence"),
    )
    op.create_index("ix_care_tasks_plan_id", "care_tasks", ["plan_id"])
    op.create_index("ix_care_tasks_unit_id", "care_tasks", ["unit_id"])

    op.create_table(
        "caregivers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("unit_id", sa.String(64), nullable=False),
    )
    op.create_index("ix_caregivers_unit_id", "caregivers", ["unit_id"])

    op.create_table(
        "caregiver_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("caregiver_id", sa.Integer(), sa.ForeignKey("caregivers.id"),
                  nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_caregiver_qualifications_caregiver_id",
                    "caregiver_qualifications", ["caregiver_id"])

    op.create_table(
        "offers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("care_tasks.id"),
                  nullable=False),
        sa.Column("caregiver_id", sa.Integer(), sa.ForeignKey("caregivers.id"),
                  nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_offers_task_id", "offers", ["task_id"])

    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("care_tasks.id"),
                  nullable=False, unique=True),
        sa.Column("caregiver_id", sa.Integer(), sa.ForeignKey("caregivers.id"),
                  nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "coordinators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
    )

    op.create_table(
        "coordinator_units",
        sa.Column("coordinator_id", sa.Integer(), sa.ForeignKey("coordinators.id"),
                  primary_key=True),
        sa.Column("unit_id", sa.String(64), primary_key=True),
    )

    op.create_table(
        "adjustment_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("care_tasks.id"),
                  nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_adjustment_logs_task_id", "adjustment_logs", ["task_id"])


def downgrade() -> None:
    op.drop_table("adjustment_logs")
    op.drop_table("coordinator_units")
    op.drop_table("coordinators")
    op.drop_table("assignments")
    op.drop_table("offers")
    op.drop_table("caregiver_qualifications")
    op.drop_table("caregivers")
    op.drop_table("care_tasks")
    op.drop_table("care_plans")
