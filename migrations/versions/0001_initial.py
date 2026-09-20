"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("current_version", sa.Integer, nullable=True),
    )

    op.create_table(
        "template_versions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("template_id", UUID, sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("definition", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("template_id", "version", name="uq_template_version"),
    )

    op.create_table(
        "instances",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("template_id", UUID, sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("template_version_id", UUID, sa.ForeignKey("template_versions.id"),
                  nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("definition_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("business_key", sa.String(255), nullable=True),
        sa.Column("submitter", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("current_node_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text, nullable=True),
    )
    op.create_index("ix_instances_status", "instances", ["status"])

    op.create_table(
        "tasks",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", UUID, sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("assignee", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index(
        "ix_tasks_open",
        "tasks",
        ["instance_id", "node_id", "generation", "assignee"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "approval_requests",
        sa.Column("request_id", sa.String(128), primary_key=True),
        sa.Column("instance_id", UUID, sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("expected_version", sa.Integer, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("response_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "history_records",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", UUID, sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("node_id", sa.String(128), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_history_instance_seq", "history_records", ["instance_id", "seq"])

    op.create_table(
        "scheduled_escalations",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("instance_id", UUID, sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_scheduled_escalations_due_at", "scheduled_escalations", ["due_at"])
    op.create_index("ix_scheduled_escalations_done", "scheduled_escalations", ["done"])


def downgrade() -> None:
    op.drop_table("scheduled_escalations")
    op.drop_table("history_records")
    op.drop_table("approval_requests")
    op.drop_index("ix_tasks_open", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_instances_status", table_name="instances")
    op.drop_table("instances")
    op.drop_table("template_versions")
    op.drop_table("templates")
