"""add created_at and updated_at timestamps for sync

Revision ID: 7f3a1b2c4d5e
Revises: f2752132036f
Create Date: 2025-11-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7f3a1b2c4d5e'
down_revision = 'f2752132036f'
branch_labels = None
depends_on = None


def upgrade():
    # Use batch_alter_table for SQLite safety
    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('invoice', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('invoiceItem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('invoiceItem', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')

    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('created_at')

    with op.batch_alter_table('invoice', schema=None) as batch_op:
        batch_op.drop_column('updated_at')

    with op.batch_alter_table('customer', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
