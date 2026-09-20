"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("login", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
    )
    op.create_index("ix_users_login", "users", ["login"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "supervisor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("enrollment_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("capacity >= 0", name="ck_program_capacity_nonneg"),
    )

    op.create_table(
        "program_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "program_id", sa.Integer(), sa.ForeignKey("programs.id"), nullable=False
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("program_id", "version_number", name="uq_version_number"),
    )
    op.create_foreign_key(
        "fk_program_current_version",
        "programs",
        "program_versions",
        ["current_version_id"],
        ["id"],
    )

    op.create_table(
        "steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("pass_condition", sa.Text(), nullable=False),
        sa.UniqueConstraint("version_id", "position", name="uq_step_position"),
    )

    op.create_table(
        "step_prerequisites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "step_id",
            sa.Integer(),
            sa.ForeignKey("steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prerequisite_id",
            sa.Integer(),
            sa.ForeignKey("steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("step_id", "prerequisite_id", name="uq_step_prereq"),
        sa.CheckConstraint("step_id <> prerequisite_id", name="ck_prereq_not_self"),
    )

    op.create_table(
        "enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "program_id", sa.Integer(), sa.ForeignKey("programs.id"), nullable=False
        ),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "learner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("waitlist_position", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("seat_granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_enrolment_active_learner",
        "enrollments",
        ["program_id", "learner_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'confirmed', 'waitlisted')"),
    )

    op.create_table(
        "step_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enrollment_id",
            sa.Integer(),
            sa.ForeignKey("enrollments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.Integer(),
            sa.ForeignKey("steps.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_correction_reason", sa.Text(), nullable=True),
        sa.Column("last_corrected_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("enrollment_id", "step_id", name="uq_result_enrolment_step"),
    )

    op.create_table(
        "result_corrections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "result_id",
            sa.Integer(),
            sa.ForeignKey("step_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supervisor_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "certificates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enrollment_id",
            sa.Integer(),
            sa.ForeignKey("enrollments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id"),
            nullable=False,
        ),
        sa.Column("serial_number", sa.String(40), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column(
            "revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("enrollment_id", name="uq_certificate_enrolment"),
        sa.UniqueConstraint("serial_number", name="uq_certificate_serial"),
    )


def downgrade() -> None:
    op.drop_table("certificates")
    op.drop_table("result_corrections")
    op.drop_table("step_results")
    op.drop_index("uq_enrolment_active_learner", table_name="enrollments")
    op.drop_table("enrollments")
    op.drop_table("step_prerequisites")
    op.drop_table("steps")
    op.drop_constraint("fk_program_current_version", "programs", type_="foreignkey")
    op.drop_table("program_versions")
    op.drop_table("programs")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_login", table_name="users")
    op.drop_table("users")
