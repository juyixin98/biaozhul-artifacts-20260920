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
        "template_versions",
        sa.Column("template_code", sa.String(length=64), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_template_version_positive"),
    )

    op.create_table(
        "instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_code", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("business_key", sa.String(length=128), nullable=False),
        sa.Column(
            "variables",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_node_id", sa.String(length=64), nullable=True),
        sa.Column("submitter", sa.String(length=64), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["template_code", "template_version"],
            ["template_versions.template_code", "template_versions.version"],
        ),
    )
    op.create_index("ix_instances_status", "instances", ["status"])
    op.create_index("ix_instances_submitter", "instances", ["submitter"])
    op.create_index(
        "uq_instance_business_key",
        "instances",
        ["template_code", "business_key"],
        unique=True,
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("assignee", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sign_strategy", sa.String(length=8), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_target", sa.String(length=64), nullable=True),
        sa.Column("escalated", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_tasks_assignee", "tasks", ["assignee"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_pending_due", "tasks", ["due_at", "status"])
    op.create_index(
        "uq_task_node_assignee",
        "tasks",
        ["instance_id", "node_id", "assignee"],
        unique=True,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Integer(), nullable=True),
        sa.Column("template_code", sa.String(length=64), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_events_instance_id", "audit_events",
                    ["instance_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])

    op.create_table(
        "idempotency_records",
        sa.Column("request_id", sa.String(length=80), primary_key=True),
        sa.Column("instance_id", sa.Integer(), nullable=True),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column(
            "response_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("audit_events")
    op.drop_table("tasks")
    op.drop_table("instances")
    op.drop_table("template_versions")
