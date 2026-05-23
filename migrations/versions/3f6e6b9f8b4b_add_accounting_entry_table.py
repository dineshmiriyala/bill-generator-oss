"""add accounting entry table

Revision ID: 3f6e6b9f8b4b
Revises: b1aa5b3f6449
Create Date: 2025-11-05 09:38:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f6e6b9f8b4b'
down_revision = 'b1aa5b3f6449'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'accounting_entry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('invoice_id', sa.Integer(), nullable=True),
        sa.Column('entry_type', sa.String(length=32), nullable=False, server_default='payment'),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('payment_method', sa.String(length=64), nullable=True),
        sa.Column('reference', sa.String(length=128), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('account', sa.String(length=16), nullable=False, server_default='cash'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('payment_reference', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id'], ),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoice.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_accounting_entry_customer_id', 'accounting_entry', ['customer_id'], unique=False)
    op.create_index('ix_accounting_entry_invoice_id', 'accounting_entry', ['invoice_id'], unique=False)
    op.create_index('ix_accounting_entry_entry_type', 'accounting_entry', ['entry_type'], unique=False)
    op.create_index('ix_accounting_entry_occurred_at', 'accounting_entry', ['occurred_at'], unique=False)
    op.create_index('ix_accounting_entry_customer_type', 'accounting_entry', ['customer_id', 'entry_type'], unique=False)
    op.create_index(op.f('ix_accounting_entry_payment_reference'), 'accounting_entry', ['payment_reference'], unique=True)


def downgrade():
    op.drop_index('ix_accounting_entry_payment_reference', table_name='accounting_entry')
    op.drop_index('ix_accounting_entry_customer_type', table_name='accounting_entry')
    op.drop_index('ix_accounting_entry_occurred_at', table_name='accounting_entry')
    op.drop_index('ix_accounting_entry_entry_type', table_name='accounting_entry')
    op.drop_index('ix_accounting_entry_invoice_id', table_name='accounting_entry')
    op.drop_index('ix_accounting_entry_customer_id', table_name='accounting_entry')
    op.drop_table('accounting_entry')
