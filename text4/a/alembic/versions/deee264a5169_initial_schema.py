"""initial schema

Revision ID: deee264a5169
Revises:
Create Date: 2026-09-20 00:06:44.193254
"""
import sqlalchemy as sa
from alembic import op

revision = "deee264a5169"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clock_overrides",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("virtual_now", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("MANAGER", "LEARNER", name="user_role"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "programs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        # Added later via ALTER to break the programs <-> versions FK cycle.
        sa.Column("current_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "program_versions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("program_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.Enum("DRAFT", "PUBLISHED", name="version_status"), nullable=False
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["program_id"], ["programs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("program_id", "version", name="ux_version_program_version"),
    )
    op.create_foreign_key(
        "fk_program_current_version",
        "programs",
        "program_versions",
        ["current_version_id"],
        ["id"],
    )
    op.create_table(
        "courses",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("enrollment_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capacity > 0", name="ck_course_capacity_positive"),
        sa.ForeignKeyConstraint(["version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id"),
    )
    op.create_table(
        "steps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("pass_criteria", sa.Text(), nullable=False),
        sa.CheckConstraint("order_index BETWEEN 1 AND 50", name="ck_step_order_range"),
        sa.ForeignKeyConstraint(["version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "order_index", name="ux_step_version_order"),
    )
    op.create_table(
        "step_dependencies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("step_id", sa.BigInteger(), nullable=False),
        sa.Column("prerequisite_step_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("step_id <> prerequisite_step_id", name="ck_dep_no_self"),
        sa.ForeignKeyConstraint(["prerequisite_step_id"], ["steps.id"]),
        sa.ForeignKeyConstraint(["step_id"], ["steps.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", "prerequisite_step_id", name="ux_dep_pair"),
    )
    op.create_table(
        "enrollments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("learner_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("course_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ENROLLED",
                "CONFIRMED",
                "WAITLISTED",
                "CANCELLED",
                "EXPIRED",
                name="enrollment_status",
            ),
            nullable=False,
        ),
        sa.Column("seat_number", sa.Integer(), nullable=True),
        sa.Column("waitlist_position", sa.Integer(), nullable=True),
        sa.Column("seat_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.ForeignKeyConstraint(["learner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learner_id", "version_id", name="ux_enrollment_learner_version"),
        sa.UniqueConstraint("version_id", "seat_number", name="ux_enrollment_seat_number"),
    )
    op.create_index(
        "ix_enrollment_version_status", "enrollments", ["version_id", "status"]
    )
    op.create_index(
        "ix_enrollment_waitlist", "enrollments", ["version_id", "waitlist_position"]
    )
    op.create_table(
        "step_results",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(), nullable=False),
        sa.Column("step_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Enum("PASSED", "FAILED", name="result_status"), nullable=False),
        sa.Column("latest_submission", sa.Text(), nullable=False),
        sa.Column("submission_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("passed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corrected_by", sa.BigInteger(), nullable=True),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["corrected_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["enrollment_id"], ["enrollments.id"]),
        sa.ForeignKeyConstraint(["step_id"], ["steps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("enrollment_id", "step_id", name="ux_result_enrollment_step"),
    )
    op.create_table(
        "certificates",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("enrollment_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("serial", sa.String(length=64), nullable=False),
        sa.Column("learner_name", sa.String(length=255), nullable=False),
        sa.Column("program_title", sa.String(length=255), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("VALID", "REVOKED", name="certificate_status"),
            nullable=False,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["enrollment_id"], ["enrollments.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["program_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("enrollment_id", name="ux_certificate_enrollment"),
        sa.UniqueConstraint("serial", name="ux_certificate_serial"),
    )


def downgrade() -> None:
    op.drop_table("certificates")
    op.drop_table("step_results")
    op.drop_index("ix_enrollment_waitlist", table_name="enrollments")
    op.drop_index("ix_enrollment_version_status", table_name="enrollments")
    op.drop_table("enrollments")
    op.drop_table("step_dependencies")
    op.drop_table("steps")
    op.drop_table("courses")
    op.drop_constraint("fk_program_current_version", "programs", type_="foreignkey")
    op.drop_table("program_versions")
    op.drop_table("programs")
    op.drop_table("users")
    op.drop_table("clock_overrides")
    for enum_name in (
        "certificate_status",
        "enrollment_status",
        "result_status",
        "version_status",
        "user_role",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
