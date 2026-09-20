"""initial schema with immutability triggers

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20 00:00:00

Creates the full ConsentVault schema and installs PostgreSQL triggers that
reject UPDATE and DELETE against the immutable ledger (``consent_events``) and
published policy versions (``policy_versions``).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# Two trigger functions. Both reject DELETE outright. The ledger trigger
# additionally permits exactly one kind of UPDATE: clearing the identifying
# subject snapshot while leaving every other column byte-for-byte unchanged
# (the right-to-erasure operation). Any other UPDATE is rejected.
TRIGGER_FN = """
CREATE OR REPLACE FUNCTION consentvault_reject_row_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '%.% is immutable; % is not permitted',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION consent_events_guard()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '%.% is immutable; DELETE is not permitted',
            TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;

    -- Allow ONLY the erasure-style anonymisation of the identifying snapshot.
    IF OLD.subject_key_snapshot IS NOT NULL
       AND NEW.subject_key_snapshot IS NULL
       AND ROW(
            NEW.id, NEW.event_id, NEW.organization_id, NEW.subject_id,
            NEW.purpose, NEW.action, NEW.policy_version, NEW.expires_at,
            NEW.expected_version, NEW.state_version, NEW.request_hash,
            NEW.created_at
       ) IS NOT DISTINCT FROM ROW(
            OLD.id, OLD.event_id, OLD.organization_id, OLD.subject_id,
            OLD.purpose, OLD.action, OLD.policy_version, OLD.expires_at,
            OLD.expected_version, OLD.state_version, OLD.request_hash,
            OLD.created_at
       )
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION '%.% is immutable; UPDATE is not permitted',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("role IN ('admin','auditor')", name="api_keys_role_check"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    op.create_table(
        "subjects",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_key", sa.String(length=400), nullable=True),
        sa.Column(
            "erased", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ux_subjects_org_key_live",
        "subjects",
        ["organization_id", "subject_key"],
        unique=True,
        postgresql_where=sa.text("erased = false"),
    )

    op.create_table(
        "policy_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("organization_id", "version", name="ux_policy_org_version"),
    )

    op.create_table(
        "consent_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_key_snapshot", sa.String(length=400), nullable=True),
        sa.Column("purpose", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("state_version", sa.BigInteger(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("organization_id", "event_id", name="ux_events_org_event_id"),
        sa.CheckConstraint("action IN ('grant','withdraw')", name="consent_events_action_check"),
    )
    op.create_index(
        "ix_events_org_subject_purpose",
        "consent_events",
        ["organization_id", "subject_id", "purpose"],
    )

    op.create_table(
        "consent_states",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("granted_event_id", sa.String(length=100), nullable=True),
        sa.Column("latest_event_id", sa.String(length=100), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "organization_id", "subject_id", "purpose", name="ux_states_org_subject_purpose"
        ),
        sa.CheckConstraint(
            "status IN ('granted','withdrawn')", name="consent_states_status_check"
        ),
    )

    op.create_table(
        "subject_exports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "subject_deletions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "organization_id", "subject_id", name="ux_subject_deletions_subject"
        ),
    )

    op.create_table(
        "audit_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=True),
        sa.Column("event_id", sa.String(length=100), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("outcome IN ('success','failure')", name="audit_outcome_check"),
    )
    op.create_index(
        "ix_audit_org_created", "audit_records", ["organization_id", "created_at"]
    )

    # --- immutability enforcement -----------------------------------------
    op.execute(TRIGGER_FN)
    op.execute(
        """
        CREATE TRIGGER trg_consent_events_immutable
        BEFORE UPDATE OR DELETE ON consent_events
        FOR EACH ROW EXECUTE FUNCTION consent_events_guard();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_versions_immutable
        BEFORE UPDATE OR DELETE ON policy_versions
        FOR EACH ROW EXECUTE FUNCTION consentvault_reject_row_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_consent_events_immutable ON consent_events")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_versions_immutable ON policy_versions")
    op.execute("DROP FUNCTION IF EXISTS consent_events_guard()")
    op.execute("DROP FUNCTION IF EXISTS consentvault_reject_row_mutation()")
    op.drop_index("ix_audit_org_created", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_table("subject_deletions")
    op.drop_table("subject_exports")
    op.drop_table("consent_states")
    op.drop_index("ix_events_org_subject_purpose", table_name="consent_events")
    op.drop_table("consent_events")
    op.drop_table("policy_versions")
    op.drop_index("ux_subjects_org_key_live", table_name="subjects")
    op.drop_table("subjects")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("organizations")
