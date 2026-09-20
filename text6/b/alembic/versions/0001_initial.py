"""initial schema with append-only triggers

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column(
            "role", sa.Enum("admin", "auditor", name="api_key_role"), nullable=False
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    op.create_table(
        "subjects",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_ref", sa.String(300), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(300), nullable=True),
        sa.Column("erased", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "external_ref", name="uq_subjects_org_external_ref"
        ),
    )
    op.create_index("ix_subjects_org", "subjects", ["organization_id"])

    op.create_table(
        "export_copies",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("destination", sa.String(300), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "policy_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id", "version", name="uq_policy_org_version"
        ),
    )

    op.create_table(
        "consent_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum("grant", "withdraw", name="consent_event_type"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "policy_version_id",
            sa.BigInteger(),
            sa.ForeignKey("policy_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id", "event_id", name="uq_consent_event_org_event_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "subject_id",
            "purpose",
            "version",
            name="uq_consent_stream_version",
        ),
    )
    op.create_index(
        "ix_consent_events_stream",
        "consent_events",
        ["organization_id", "subject_id", "purpose"],
    )

    op.create_table(
        "consent_states",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column(
            "last_event_id",
            sa.BigInteger(),
            sa.ForeignKey("consent_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "subject_id",
            "purpose",
            name="uq_consent_state_stream",
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.BigInteger(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "api_key_id",
            sa.BigInteger(),
            sa.ForeignKey("api_keys.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "subject_created",
                "subject_erased",
                "policy_published",
                "consent_granted",
                "consent_withdrawn",
                "consent_batch_imported",
                "export_registered",
                "states_rebuilt",
                name="audit_action",
            ),
            nullable=False,
        ),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_audit_logs_org_created", "audit_logs", ["organization_id", "created_at"]
    )

    # --------------------------------------------------------------------- #
    # Database-enforced immutability.
    #
    # The consent ledger and published policies are append-only: no UPDATE or
    # DELETE (application bugs, manual SQL) may rewrite history. INSERTs work
    # normally; the rebuilder only mutates consent_states, which is not locked.
    # --------------------------------------------------------------------- #
    op.execute(
        """
        CREATE OR REPLACE FUNCTION consentvault_block_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only; % is not permitted',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_consent_events_no_update
        BEFORE UPDATE OR DELETE ON consent_events
        FOR EACH ROW EXECUTE FUNCTION consentvault_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_versions_no_update
        BEFORE UPDATE OR DELETE ON policy_versions
        FOR EACH ROW EXECUTE FUNCTION consentvault_block_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_consent_events_no_update ON consent_events")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_versions_no_update ON policy_versions")
    op.execute("DROP FUNCTION IF EXISTS consentvault_block_mutation()")

    op.drop_index("ix_audit_logs_org_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("consent_states")
    op.drop_index("ix_consent_events_stream", table_name="consent_events")
    op.drop_table("consent_events")
    op.drop_table("policy_versions")
    op.drop_table("export_copies")
    op.drop_index("ix_subjects_org", table_name="subjects")
    op.drop_table("subjects")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("organizations")
    sa.Enum(name="api_key_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="consent_event_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="audit_action").drop(op.get_bind(), checkfirst=True)
