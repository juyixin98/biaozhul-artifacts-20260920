"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20 00:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_clock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offset_seconds", sa.Float(), nullable=False, server_default="0"),
    )
    op.execute("INSERT INTO app_clock (id, offset_seconds) VALUES (1, 0)")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("role IN ('supervisor', 'learner')", name="ck_users_role"),
    )

    op.create_table(
        "programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
    )

    op.create_table(
        "program_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "program_id",
            sa.Integer(),
            sa.ForeignKey("programs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "program_id", "version_number", name="uq_version_program_number"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')", name="ck_version_status"
        ),
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
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("pass_condition", sa.Text(), nullable=False),
        sa.UniqueConstraint("version_id", "key", name="uq_step_version_key"),
        sa.UniqueConstraint(
            "version_id", "position", name="uq_step_version_position"
        ),
        sa.CheckConstraint("position >= 1", name="ck_step_position"),
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
        sa.Column("prerequisite_key", sa.String(length=60), nullable=False),
        sa.UniqueConstraint(
            "step_id", "prerequisite_key", name="uq_prereq_step_key"
        ),
    )

    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("enroll_deadline", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("capacity >= 0", name="ck_course_capacity"),
    )

    op.create_table(
        "enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "learner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("waitlist_position", sa.Integer(), nullable=True),
        sa.Column("offered_at", sa.DateTime(), nullable=True),
        sa.Column("seat_expires_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint(
            "course_id", "learner_id", name="uq_enrollment_course_learner"
        ),
        sa.CheckConstraint(
            "status IN ('waitlisted', 'pending_confirmation', 'confirmed', "
            "'cancelled', 'expired')",
            name="ck_enrollment_status",
        ),
    )
    op.create_index(
        "ix_enrollments_course_status", "enrollments", ["course_id", "status"]
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
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "submission_content", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_submitted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "reviewed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "corrected", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.UniqueConstraint(
            "enrollment_id", "step_id", name="uq_result_enrollment_step"
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed')", name="ck_result_status"
        ),
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
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("previous_status", sa.String(length=20), nullable=False),
        sa.Column("new_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
    )

    op.create_table(
        "certificates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "enrollment_id",
            sa.Integer(),
            sa.ForeignKey("enrollments.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("serial_number", sa.String(length=60), nullable=False, unique=True),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("program_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('valid', 'invalid')", name="ck_certificate_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("certificates")
    op.drop_table("result_corrections")
    op.drop_table("step_results")
    op.drop_index("ix_enrollments_course_status", table_name="enrollments")
    op.drop_table("enrollments")
    op.drop_table("courses")
    op.drop_table("step_prerequisites")
    op.drop_table("steps")
    op.drop_constraint("fk_program_current_version", "programs", type_="foreignkey")
    op.drop_table("program_versions")
    op.drop_table("programs")
    op.drop_table("users")
    op.drop_table("app_clock")
