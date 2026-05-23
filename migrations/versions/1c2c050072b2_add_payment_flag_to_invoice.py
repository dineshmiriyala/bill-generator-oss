"""add payment flag to invoice

Revision ID: 1c2c050072b2
Revises: 67e046fa1c9b
Create Date: 2025-01-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1c2c050072b2'
down_revision = '67e046fa1c9b'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'invoice',
        sa.Column('payment', sa.Boolean(), nullable=False, server_default=sa.text('0'))
    )
    op.create_index(op.f('ix_invoice_payment'), 'invoice', ['payment'], unique=False)
    op.alter_column('invoice', 'payment', server_default=None)


def downgrade():
    op.drop_index(op.f('ix_invoice_payment'), table_name='invoice')
    op.drop_column('invoice', 'payment')
