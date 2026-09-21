"""initial schema

Revision ID: 0001_initial
Creates every table defined in app.models from the shared metadata, so the
migration can never drift out of sync with the ORM definitions.
"""
from alembic import op

from app import models  # noqa: F401  (register all tables)
from app.database import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
