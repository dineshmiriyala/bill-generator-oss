from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from sqlalchemy import event, func, select
import json
import os

db = SQLAlchemy()


def _utcnow():
    return datetime.now(timezone.utc)


def _txn_prefix() -> str:
    """Resolve the transaction-id prefix.

    Order: BG_TXN_PREFIX env var > generic default ``TXN``.
    The application module may set ``DEFAULT_TXN_PREFIX`` at import time;
    we read from the env so this module stays decoupled from app.py.
    """
    prefix = (os.getenv("BG_TXN_PREFIX") or "TXN").strip("- ")
    return prefix or "TXN"


class customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    company = db.Column(db.String(50))
    phone = db.Column(db.String(50), nullable=False, unique=True)
    email = db.Column(db.String(50))
    gst = db.Column(db.String(30))
    address = db.Column(db.Text)
    businessType = db.Column(db.String(50))
    invoices = db.relationship("invoice", backref="customer", lazy=True)
    createdAt = db.Column(db.DateTime, default=_utcnow)
    isDeleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    deletedAt = db.Column(db.DateTime, nullable=True, index=True)

    @classmethod
    def alive(cls):
        return cls.query.filter_by(isDeleted=False)


class invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoiceId = db.Column(db.String(50), unique=True, nullable=False)
    customerId = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    createdAt = db.Column(db.DateTime, default=_utcnow, index=True)
    pdfPath = db.Column(db.String(255), nullable=False)
    totalAmount = db.Column(db.Float, nullable=False)
    items = db.relationship("invoiceItem", backref="invoice", lazy=True)
    isDeleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    deletedAt = db.Column(db.DateTime, nullable=True, index=True)
    exclude_phone = db.Column(db.Boolean, default=False)
    exclude_gst = db.Column(db.Boolean, default=False)
    exclude_addr = db.Column(db.Boolean, default=False)
    payment = db.Column(db.Boolean, nullable=False, default=False, index=True)

    @classmethod
    def alive(cls):
        return cls.query.filter_by(isDeleted=False)


class item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, index=True)
    sku = db.Column(db.Integer, unique=True, index=True, nullable=True)
    unitPrice = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Float, default=1)
    taxPercentage = db.Column(db.Float)


# Auto-assign an incrementing SKU if not provided
@event.listens_for(item, 'before_insert')
def set_incremental_sku(mapper, connection, target):
    if target.sku is None:
        max_sku = connection.execute(select(func.max(item.sku))).scalar()
        target.sku = (max_sku or 0) + 1


class invoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoiceId = db.Column(db.String, db.ForeignKey("invoice.id"), nullable=False, index=True)
    itemId = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    dcNo = db.Column(db.String(64), nullable=True, index=True)
    quantity = db.Column(db.Integer, default=1)
    rate = db.Column(db.Float, nullable=False)
    discount = db.Column(db.Float, default=0.0)
    taxPercentage = db.Column(db.Float, default=0.0)
    line_total = db.Column(db.Float, nullable=False)
    rounded = db.Column(db.Boolean, nullable=False, default=False)


class billDraft(db.Model):
    __tablename__ = "bill_draft"

    id = db.Column(db.Integer, primary_key=True)
    customerId = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    status = db.Column(db.String(16), nullable=False, default="draft", index=True)
    payloadJson = db.Column(db.Text, nullable=False, default="{}")
    totalAmount = db.Column(db.Float, nullable=False, default=0.0)
    itemCount = db.Column(db.Integer, nullable=False, default=0)
    convertedInvoiceId = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True, index=True)
    createdAt = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    updatedAt = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow, index=True)

    customer = db.relationship("customer", backref="bill_drafts", lazy=True)
    converted_invoice = db.relationship("invoice", lazy=True, foreign_keys=[convertedInvoiceId])


class role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    users = db.relationship("user", backref="role", lazy=True)


class user(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(50), unique=True)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"))
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)


class lastBackup(db.Model):
    __tablename__ = "last_backup"

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)
    note = db.Column(db.String(255))

    # audit fields
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow
    )


class layoutConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sizes_json = db.Column(db.Text, nullable=False, default=json.dumps({
        "header": 13,
        "customer": 16,
        "table": 13,
        "totals": 15,
        "payment": 13,
        "footer": 10,
        "invoice_info": 13
    }))
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False
    )

    def get_sizes(self):
        """Return sizes as Python Dictionary."""
        try:
            return json.loads(self.sizes_json)
        except (ValueError, TypeError):
            return {}

    def set_sizes(self, sizes: dict):
        """Store sizes dict as json string."""
        self.sizes_json = json.dumps(sizes)

    def reset_sizes(self):
        """Reset sizes to default values and save."""
        default_sizes = {
            "header": 13,
            "customer": 16,
            "table": 13,
            "totals": 15,
            "payment": 13,
            "footer": 10,
            "invoice_info": 13
        }
        self.set_sizes(default_sizes)
        db.session.commit()

    @classmethod
    def get_or_create(cls, sizes: dict = None):
        """Return layout config instance or create if it doesn't exist."""
        instance = cls.query.first()
        if not instance:
            instance = cls()
            if sizes:
                instance.set_sizes(sizes)
            db.session.add(instance)
            db.session.commit()
        return instance


db.Index('ix_invoiceItem_invoice_item', invoiceItem.invoiceId, invoiceItem.itemId)


class accountingTransaction(db.Model):
    __tablename__ = "accounting_transaction"

    id = db.Column(db.Integer, primary_key=True)
    txn_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    customerId = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=True, index=True)
    amount = db.Column(db.Float, nullable=False)
    txn_type = db.Column(db.String(16), nullable=False, default="income", index=True)
    mode = db.Column(db.String(32), nullable=True)
    account = db.Column(db.String(32), nullable=True)
    invoice_no = db.Column(db.String(64), nullable=True, index=True)
    remarks = db.Column(db.Text, nullable=True)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    customer = db.relationship("customer", backref="accounting_transactions", lazy=True)
    expense_items = db.relationship(
        "expenseItem",
        backref="transaction",
        lazy=True,
        cascade="all, delete-orphan"
    )


class expenseItem(db.Model):
    __tablename__ = "expense_item"

    id = db.Column(db.Integer, primary_key=True)
    transactionId = db.Column(db.Integer, db.ForeignKey("accounting_transaction.id"), nullable=False, index=True)
    description = db.Column(db.String(255), nullable=True)
    amount = db.Column(db.Float, nullable=True)


@event.listens_for(accountingTransaction, 'before_insert')
def set_accounting_txn_id(mapper, connection, target):
    if not target.created_at:
        target.created_at = _utcnow()
    if target.txn_id:
        return
    date_code = target.created_at.strftime("%d%m%y")
    prefix = f"{_txn_prefix()}-{date_code}-"
    stmt = (
        select(accountingTransaction.txn_id)
        .where(accountingTransaction.txn_id.like(f"{prefix}%"))
        .order_by(accountingTransaction.txn_id.desc())
        .limit(1)
    )
    last_txn = connection.execute(stmt).scalar()
    if last_txn:
        try:
            seq = int(last_txn.split("-")[-1])
        except (ValueError, AttributeError):
            seq = 0
    else:
        seq = 0
    seq += 1
    target.txn_id = f"{prefix}{seq:06d}"
