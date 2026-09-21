"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

user_role = postgresql.ENUM(
    "admin", "analyst", "manager", "employee", name="user_role", create_type=False
)
event_type = postgresql.ENUM(
    "file_access", "file_download", "usb_connect", "login", "website_visit",
    name="event_type", create_type=False,
)
alert_rule = postgresql.ENUM(
    "off_hours_access", "download_burst", "usb_first", "file_access_baseline",
    name="alert_rule", create_type=False,
)
alert_status = postgresql.ENUM(
    "open", "confirmed", "false_positive", "investigated",
    name="alert_status", create_type=False,
)
baseline_status = postgresql.ENUM(
    "ok", "insufficient_history", "zero_variance",
    name="baseline_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    event_type.create(bind, checkfirst=True)
    alert_rule.create(bind, checkfirst=True)
    alert_status.create(bind, checkfirst=True)
    baseline_status.create(bind, checkfirst=True)

    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("path", sa.String(2000), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), sa.ForeignKey("organizations.id", ondelete="SET NULL")),
    )
    op.create_index("ix_organizations_path", "organizations", ["path"])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("org_id", sa.BigInteger(), sa.ForeignKey("organizations.id", ondelete="SET NULL")),
        sa.Column("manager_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "department_memberships",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("department_id", sa.BigInteger(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("user_id", "department_id", name="uq_depmem_user_department"),
    )
    op.create_index("ix_department_memberships_user_id", "department_memberships", ["user_id"])

    op.create_table(
        "devices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("device_key", sa.String(200), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
    )
    op.create_index("ix_devices_device_key", "devices", ["device_key"], unique=True)
    op.create_index("ix_devices_user_id", "devices", ["user_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("device_id", sa.BigInteger(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("type", event_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("device_id", "event_id", name="uq_event_device_eventid"),
    )
    op.create_index("ix_events_user_type_time", "events", ["user_id", "type", "occurred_at"])

    op.create_table(
        "baselines",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False, server_default="file_access_daily"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.DateTime(), nullable=False),
        sa.Column("status", baseline_status, nullable=False),
        sa.Column("days_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mean", sa.Float(), nullable=True),
        sa.Column("stddev", sa.Float(), nullable=True),
        sa.Column("daily_counts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("observed_count", sa.Integer(), nullable=True),
        sa.Column("zscore", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "metric", "version", name="uq_baseline_user_metric_version"),
    )
    op.create_index("ix_baseline_user_metric", "baselines", ["user_id", "metric"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("rule", alert_rule, nullable=False),
        sa.Column("status", alert_status, nullable=False, server_default="open"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.BigInteger(), sa.ForeignKey("devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_key", sa.String(300), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("baseline_id", sa.BigInteger(), sa.ForeignKey("baselines.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_alert_dedup_key", "alerts", ["dedup_key"], unique=True)    op.create_index("ix_alert_user_status", "alerts", ["user_id", "status"])

    op.create_table(
        "alert_investigations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("alert_id", sa.BigInteger(), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", alert_status, nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("evidence_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_alert_investigations_alert_id", "alert_investigations", ["alert_id"])


def downgrade() -> None:
    op.drop_table("alert_investigations")
    op.drop_table("alerts")
    op.drop_table("baselines")
    op.drop_table("events")
    op.drop_table("devices")
    op.drop_table("department_memberships")
    op.drop_table("users")
    op.drop_table("organizations")
    bind = op.get_bind()
    for enum_type in (alert_status, alert_rule, event_type, baseline_status, user_role):
        enum_type.drop(bind, checkfirst=True)
