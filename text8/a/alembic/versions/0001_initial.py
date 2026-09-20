"""${message}

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "current_version_id", sa.BigInteger(), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "template_version",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "template_id", sa.BigInteger, sa.ForeignKey("template.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("definition", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("template_id", "version", name="uq_template_version"),
    )
    op.create_foreign_key(
        "fk_template_current_version",
        "template",
        "template_version",
        ["current_version_id"],
        ["id"],
    )

    op.create_table(
        "instance",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_id", sa.BigInteger, sa.ForeignKey("template.id"), nullable=False
        ),
        sa.Column(
            "template_version_id",
            sa.BigInteger,
            sa.ForeignKey("template_version.id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("submitter", sa.String(64), nullable=False),
        sa.Column("context", postgresql.JSONB, nullable=False),
        sa.Column("current_node_id", sa.String(64), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("start_request_id", sa.String(64), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "instance_id", sa.String(36), sa.ForeignKey("instance.id"), nullable=False
        ),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("assignee", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("handled_by", sa.String(64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_instance_node", "task", ["instance_id", "node_id"])

    op.create_table(
        "history_event",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "instance_id", sa.String(36), sa.ForeignKey("instance.id"), nullable=False
        ),
        sa.Column("node_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_history_instance", "history_event", ["instance_id", "id"])

    op.create_table(
        "escalation",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "instance_id", sa.String(36), sa.ForeignKey("instance.id"), nullable=False
        ),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("targets", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("instance_id", "node_id", name="uq_escalation_instance_node"),
    )
    op.create_index("ix_escalation_due", "escalation", ["status", "due_at"])

    op.create_table(
        "idempotency_record",
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("idempotency_record")
    op.drop_index("ix_escalation_due", table_name="escalation")
    op.drop_table("escalation")
    op.drop_index("ix_history_instance", table_name="history_event")
    op.drop_table("history_event")
    op.drop_index("ix_task_instance_node", table_name="task")
    op.drop_table("task")
    op.drop_table("instance")
    op.drop_constraint("fk_template_current_version", "template", type_="foreignkey")
    op.drop_table("template_version")
    op.drop_table("template")
