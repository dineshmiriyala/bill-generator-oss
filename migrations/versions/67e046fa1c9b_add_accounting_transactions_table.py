"""add accounting transactions table

Revision ID: 67e046fa1c9b
Revises: 3f6e6b9f8b4b
Create Date: 2025-11-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '67e046fa1c9b'
down_revision = '3f6e6b9f8b4b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'accounting_transaction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('txn_id', sa.String(length=32), nullable=False),
        sa.Column('customerId', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('txn_type', sa.String(length=16), server_default='income', nullable=False),
        sa.Column('mode', sa.String(length=32), nullable=True),
        sa.Column('account', sa.String(length=32), nullable=True),
        sa.Column('invoice_no', sa.String(length=64), nullable=True),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['customerId'], ['customer.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('txn_id')
    )
    op.create_index(op.f('ix_accounting_transaction_created_at'), 'accounting_transaction', ['created_at'], unique=False)
    op.create_index(op.f('ix_accounting_transaction_customerId'), 'accounting_transaction', ['customerId'], unique=False)
    op.create_index(op.f('ix_accounting_transaction_invoice_no'), 'accounting_transaction', ['invoice_no'], unique=False)
    op.create_index(op.f('ix_accounting_transaction_is_deleted'), 'accounting_transaction', ['is_deleted'], unique=False)
    op.create_index(op.f('ix_accounting_transaction_txn_type'), 'accounting_transaction', ['txn_type'], unique=False)

    op.create_table(
        'expense_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('transactionId', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('amount', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['transactionId'], ['accounting_transaction.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_expense_item_transactionId'), 'expense_item', ['transactionId'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_expense_item_transactionId'), table_name='expense_item')
    op.drop_table('expense_item')
    op.drop_index(op.f('ix_accounting_transaction_txn_type'), table_name='accounting_transaction')
    op.drop_index(op.f('ix_accounting_transaction_is_deleted'), table_name='accounting_transaction')
    op.drop_index(op.f('ix_accounting_transaction_invoice_no'), table_name='accounting_transaction')
    op.drop_index(op.f('ix_accounting_transaction_customerId'), table_name='accounting_transaction')
    op.drop_index(op.f('ix_accounting_transaction_created_at'), table_name='accounting_transaction')
    op.drop_table('accounting_transaction')
