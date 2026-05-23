"""Add isDeleted and deletedAt to invoice(soft delete)

Revision ID: f2752132036f
Revises: 4e06ec8305d7
Create Date: 2025-08-30 15:52:59.301591

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2752132036f'
down_revision = '4e06ec8305d7'
branch_labels = None
depends_on = None


def upgrade():
    # 1) Add columns with a server_default so SQLite can backfill existing rows
    with op.batch_alter_table('invoice', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('isDeleted', sa.Boolean(), nullable=True, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('deletedAt', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_invoice_deletedAt'), ['deletedAt'], unique=False)
        batch_op.create_index(batch_op.f('ix_invoice_isDeleted'), ['isDeleted'], unique=False)

    # 2) Backfill any NULLs just in case
    op.execute("UPDATE invoice SET isDeleted = 0 WHERE isDeleted IS NULL")

    # 3) Tighten constraint: NOT NULL and (optionally) drop server_default
    with op.batch_alter_table('invoice', recreate='always') as batch_op:
        batch_op.alter_column('isDeleted', nullable=False)
        batch_op.alter_column('isDeleted', server_default=None)


def downgrade():
    with op.batch_alter_table('invoice', recreate='always') as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoice_isDeleted'))
        batch_op.drop_index(batch_op.f('ix_invoice_deletedAt'))
        batch_op.drop_column('deletedAt')
        batch_op.drop_column('isDeleted')
