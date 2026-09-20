"""initial schema and immutability trigger

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19

"""
from alembic import op

from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

IMMUTABILITY_SQL = """
CREATE OR REPLACE FUNCTION consentvault_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'consent_events is an append-only, immutable history; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_consent_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON consent_events
    FOR EACH STATEMENT EXECUTE FUNCTION consentvault_events_immutable();

CREATE OR REPLACE FUNCTION consentvault_policy_versions_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'policy_versions are immutable once published; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_policy_versions_immutable
    BEFORE UPDATE OR DELETE ON policy_versions
    FOR EACH STATEMENT EXECUTE FUNCTION consentvault_policy_versions_immutable();
"""

DROP_SQL = """
DROP TRIGGER IF EXISTS trg_consent_events_immutable ON consent_events;
DROP FUNCTION IF EXISTS consentvault_events_immutable();
DROP TRIGGER IF EXISTS trg_policy_versions_immutable ON policy_versions;
DROP FUNCTION IF EXISTS consentvault_policy_versions_immutable();
"""


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    op.execute(IMMUTABILITY_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(DROP_SQL)
    Base.metadata.drop_all(bind=bind)
