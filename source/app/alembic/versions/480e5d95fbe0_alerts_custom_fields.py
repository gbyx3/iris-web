"""Alerts custom fields

Revision ID: 480e5d95fbe0
Revises: 3715d4fac4de
Create Date: 2024-12-10 17:29:39.103485

"""
from alembic import op
import sqlalchemy as sa

from app.alembic.alembic_utils import _table_has_column


# revision identifiers, used by Alembic.
revision = '480e5d95fbe0'
down_revision = '3715d4fac4de'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('COMMIT')

    if not _table_has_column('alerts', 'custom_attributes'):
        op.add_column('alerts', sa.Column('custom_attributes', sa.JSON, nullable=True))
    return


def downgrade():
    pass
